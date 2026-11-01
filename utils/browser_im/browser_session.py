"""浏览器态 IM 会话（`AgisoIdleClient` 里 CefSharp `ChromiumWebBrowser` 的等价实现）。

为什么要这一步
--------------
逆向 `鱼店长Pro` 得到的结论：

* 它的 IM WebSocket **不是**由 C# 自己连的，而是页面 JS 自己连的
  （运行时日志：`[JsWs#1] IM的 WebSoket 打开` / `关闭`）。
* token 由 `GetTokenFromWebAsync()` 从网页里取，不是在进程内调 mtop。
* 断线由 `WsDisconnectWatcher` 计时 60 秒后 `RefreshPage` 自愈。
* 客服页每 60 分钟整页重载一次（日志：`重新加载客服聊天窗口(定时刷新)`）。

因此它从不出现滑块：所有 mtop 请求都由真实浏览器带着
`_m_h5_tk` / `_m_h5_tk_enc` / `x5sec` / `baxia` 等风控 cookie 与完整
`sec-ch-ua` / `Referer` / TLS 指纹发出，平台侧看起来就是正常网页。

本模块用 Playwright 复刻该环境：常驻持久化 Chromium 上下文 + 页面 WS 劫持桥。
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional

from loguru import logger

from .ws_hijack_js import build_init_script

try:  # Playwright 是可选重依赖，缺失时给出清晰降级路径
    from playwright.async_api import async_playwright
    _PLAYWRIGHT_IMPORT_ERROR: Optional[Exception] = None
except Exception as _exc:  # pragma: no cover
    async_playwright = None  # type: ignore[assignment]
    _PLAYWRIGHT_IMPORT_ERROR = _exc


IM_HOME_URLS = (
    "https://www.goofish.com/im",
    "https://www.goofish.com/message",
)

MTOP_APP_KEY = "444e9908a51d1cb236a27862abc769c9"
# 注意：淘宝 mtop 的外层 appKey（URL 参数 + 签名）与内层 data 里的 appKey 不同。
# 项目 utils/xianyu_utils.generate_sign 固定用 34839810 算签名，
# 而 data JSON 里内嵌 444e9908a51d1cb236a27862abc769c9。搞混会直接
# 返回 FAIL_SYS_PARAMINVALID_ERROR::请求参数非法。
MTOP_SIGN_APP_KEY = "34839810"
IM_TOKEN_API = "mtop.taobao.idlemessage.pc.login.token"

# 在页面里请求一个**已经拼好签名**的 mtop URL。
#
# 关键教训：浏览器 Web Crypto（crypto.subtle.digest）**只支持 SHA-1/256/384/512，
# 不支持 MD5**。早期版本在页面里用 crypto.subtle.digest('MD5') 算 sign，
# 直接抛 NotSupportedError，导致 IM token 永远拿不到（表现为"令牌为空"）。
# 因此 sign 一律在 Python 侧算好（token 从浏览器 cookie 读），页面只负责携带
# 浏览器自身的 cookie / 指纹把请求发出去。
TOKEN_FETCH_JS = r"""
async (params) => {
  try {
    const resp = await fetch(params.url, {
      method: 'GET',
      credentials: 'include',
      headers: { 'accept': 'application/json' }
    });
    const text = await resp.text();
    let j = null;
    try { j = JSON.parse(text); } catch (e) { j = null; }
    const data = (j && j.data) ? j.data : null;
    return {
      ok: !!(data && data.accessToken),
      httpStatus: resp.status,
      ret: (j && j.ret) ? Array.from(j.ret) : [],
      token: data ? (data.accessToken || '') : '',
      raw: text.slice(0, 2000)
    };
  } catch (e) {
    return { ok: false, reason: 'fetch_failed: ' + String(e) };
  }
}
"""

# 判断页面是否还活着 / 是否被风控挡住
PAGE_HEALTH_JS = r"""
() => {
  const st = (window.__xyWsState ? window.__xyWsState() : { exists: false, readyState: -1 });
  const body = document.body ? document.body.innerText : '';
  const risky = /滑块|验证|安全验证|操作过于频繁|账号存在风险|请完成验证/.test(body);
  return {
    ws: st,
    url: location.href,
    title: document.title,
    risky: risky,
    readyState: document.readyState
  };
}
"""


class BrowserSessionError(RuntimeError):
    pass


# 容器/精简镜像里通常没有 playwright 自带的 chromium，需要回退到系统浏览器。
# 桌面版尤其重要：Windows 自带 Edge、多数机器装了 Chrome，因此**无需随包分发
# Chromium**（PyInstaller 也打不进去），直接用系统浏览器即可。
_SYSTEM_CHROMIUM_CANDIDATES = (
    # Linux / 服务器
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/opt/google/chrome/chrome",
    # Windows（桌面版主战场：Edge 为系统自带，命中率最高）
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    # macOS
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
)


def _available_memory_mb() -> Optional[int]:
    """读 /proc/meminfo 的 MemAvailable（MB）。非 Linux 或读不到返回 None。"""
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except Exception:
        return None
    return None


def pick_browser_executable(explicit: Optional[str] = None) -> Optional[str]:
    """决定用哪个浏览器可执行文件。

    返回 None 表示让 Playwright 用它自己下载的 chromium。
    容器里常见情况：`PLAYWRIGHT_BROWSERS_PATH` 指向的目录是空的，
    此时必须回退到系统 chromium，否则启动直接报 "Executable doesn't exist"。
    """
    if explicit and os.path.exists(explicit):
        return explicit

    env_path = (os.environ.get("XY_IM_BROWSER_PATH") or "").strip()
    if env_path and os.path.exists(env_path):
        return env_path

    browsers_path = (os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or "").strip()
    if browsers_path and os.path.isdir(browsers_path):
        try:
            for name in os.listdir(browsers_path):
                if name.startswith("chromium"):
                    return None  # 自带浏览器可用
        except OSError:
            pass

    for cand in _SYSTEM_CHROMIUM_CANDIDATES:
        if os.path.exists(cand):
            return cand
    return None


class BrowserIMSession:
    """一个账号一个实例：常驻浏览器 + IM WS 事件桥。

    事件通过 ``event_queue`` 抛出，元素形如::

        {"type": "open"|"message"|"close"|"error"|"construct",
         "id": "ws_1", "url": "...", "im": true, "data": "...", "ts": 1.23}
    """

    def __init__(
        self,
        cookie_id: str,
        cookies_str: str = "",
        *,
        user_data_root: Optional[str] = None,
        headless: bool = True,
        proxy: Optional[Dict[str, Any]] = None,
        lang: str = "zh-CN",
        timezone_id: str = "Asia/Shanghai",
        viewport: Optional[Dict[str, int]] = None,
        attach_context: Optional[Any] = None,
        executable_path: Optional[str] = None,
        navigate_home: bool = True,
    ) -> None:
        self.cookie_id = cookie_id
        self.cookies_str = cookies_str or ""
        self.headless = headless
        self.proxy = proxy or {}
        self.lang = lang
        self.timezone_id = timezone_id
        self.viewport = viewport or {"width": 1366, "height": 860}

        # attach_context: 挂到已有的常驻 context 上（复用 utils.persistent_browser），
        # 这样不会为同一账号再开一个 Chromium —— 容器内存有限，开两个会 OOM。
        self._attach_context = attach_context
        self._owns_context = attach_context is None
        self.executable_path = executable_path
        # navigate_home=False：只起浏览器不导航 IM 页（扫码/登录等临时用途，
        # 避免白白加载一次 IM 页面浪费内存和时间）
        self.navigate_home = bool(navigate_home)

        if user_data_root is None:
            # 桌面版（PyInstaller）里 __file__ 指向安装目录，通常只读或会被升级覆盖，
            # 浏览器 profile 必须落到用户可写目录，否则登录态每次更新都丢。
            user_data_root = (os.environ.get("FISHCLOUD_BROWSER_DATA") or "").strip()
            if not user_data_root:
                project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
                user_data_root = os.path.join(project_root, "browser_data", "im_sessions")
        self.user_data_dir = os.path.join(user_data_root, str(cookie_id))
        os.makedirs(self.user_data_dir, exist_ok=True)

        self.event_queue: asyncio.Queue = asyncio.Queue(maxsize=4000)

        self._pw = None
        self._context = None
        self._page = None
        self._started = False
        self._lock = asyncio.Lock()

        self._last_event_ts: float = 0.0
        self._last_open_ts: float = 0.0
        self._last_close_ts: float = 0.0
        self._page_load_ts: float = 0.0
        self._reload_count: int = 0
        self._closed = False

    # ------------------------------------------------------------------ 生命周期

    async def start(self) -> None:
        """启动或挂载浏览器会话并挂上 WS 劫持。幂等。"""
        if self._started:
            return
        if async_playwright is None and self._attach_context is None:
            raise BrowserSessionError(
                f"Playwright 不可用: {_PLAYWRIGHT_IMPORT_ERROR}"
            )

        async with self._lock:
            if self._started:
                return

            if self._attach_context is not None:
                # ---- 挂载模式：复用已有常驻 context（不新开 Chromium）----
                self._pw = None
                self._context = self._attach_context
                self._owns_context = False
                logger.info(f"【{self.cookie_id}】挂载到已有常驻浏览器上下文")
            else:
                # ---- 自建模式：本地开发/未提供常驻上下文时 ----
                self._pw = await async_playwright().start()

                launch_args: Dict[str, Any] = {
                    "user_data_dir": self.user_data_dir,
                    "headless": self.headless,
                    "locale": self.lang,
                    "timezone_id": self.timezone_id,
                    "viewport": self.viewport,
                    "args": [
                        # 容器内通常以 root 运行 Chromium，必须关沙箱，否则启动即失败
                        "--no-sandbox",
                        "--disable-setuid-sandbox",
                        "--disable-blink-features=AutomationControlled",
                        "--disable-features=IsolateOrigins,site-per-process",
                        "--no-default-browser-check",
                        "--no-first-run",
                        "--disable-dev-shm-usage",
                        "--disable-gpu",
                        "--disable-accelerated-2d-canvas",
                        "--disable-software-rasterizer",
                        # 内存受限环境（如 2G 容器）下的必要收敛：
                        # 限制渲染进程数量，避免一个页面炸出多个进程把宿主撑爆
                        "--renderer-process-limit=1",
                        "--process-per-site",
                        "--disable-background-timer-throttling",
                        "--disable-renderer-backgrounding",
                        "--disable-backgrounding-occluded-windows",
                        "--js-flags=--max-old-space-size=256 --max-semi-space-size=4",
                    ],
                    "ignore_default_args": ["--enable-automation"],
                }
                exe = pick_browser_executable(self.executable_path)
                if exe:
                    launch_args["executable_path"] = exe
                    logger.info(f"【{self.cookie_id}】使用浏览器可执行文件: {exe}")

                # 有头模式下把窗口移到屏幕外：
                # 闲鱼会对无头浏览器直接返回「非法访问 为了保障您的体验，请使用正常
                # 浏览器访问闲鱼~」，所以必须用有头；但用户又不该看到一个多余的
                # 浏览器窗口，因此把窗口放到屏幕外（存在且正常渲染，只是看不见）。
                if not self.headless:
                    launch_args["args"].extend([
                        "--window-position=-32000,-32000",
                        "--window-size=1280,900",
                    ])

                proxy_cfg = self._build_proxy_cfg()
                if proxy_cfg:
                    launch_args["proxy"] = proxy_cfg

                self._context = await self._pw.chromium.launch_persistent_context(**launch_args)

            # 整个 context 生效：所有 frame 都能回调进来
            await self._context.expose_binding("__xyWsEvent", self._on_ws_event)
            await self._context.add_init_script(build_init_script())

            if self.cookies_str:
                await self._apply_cookies_str(self.cookies_str)

            self._page = await self._ensure_page()

            if self._owns_context:
                if self.navigate_home:
                    await self._navigate_home()
            else:
                # 挂载模式：页面多半已经加载过，init script 只对之后生效，
                # 必须重载一次，WS 劫持才会挂到页面的 WebSocket 构造上。
                if self.navigate_home:
                    await self._reload_for_attach()

            self._started = True
            logger.info(
                f"【{self.cookie_id}】浏览器态会话已就绪"
                f"（headless={self.headless}, 自有上下文={self._owns_context}）"
            )

    async def _reload_for_attach(self) -> None:
        """挂载模式下的首帧重载：让 init script 真正生效。

        重载是内存峰值最高的动作，容器内存吃紧时宁可先不重载——劫持脚本会在
        下一次自然导航时生效，而把宿主 OOM 掉是更糟的结果。
        """
        avail = _available_memory_mb()
        if avail is not None and avail < 220:
            logger.warning(
                f"【{self.cookie_id}】可用内存仅 {avail}MB，跳过挂载后重载（避免 OOM）"
            )
            self._page_load_ts = time.time()
            return
        try:
            page = await self._ensure_page()
            cur = page.url or ""
            if cur in ("", "about:blank"):
                await self._navigate_home()
                return
            logger.info(f"【{self.cookie_id}】重载页面以注入 WS 劫持（当前: {cur[:80]}）")
            await page.reload(wait_until="domcontentloaded", timeout=45000)
            await self._wait_im_ws(timeout=25)
            self._page_load_ts = time.time()
        except Exception as exc:
            logger.warning(f"【{self.cookie_id}】挂载后重载失败（不阻断）: {exc}")
            try:
                await self._navigate_home()
            except Exception:
                pass

    def _build_proxy_cfg(self) -> Optional[Dict[str, Any]]:
        if not self.proxy:
            return None
        host = self.proxy.get("proxy_host") or self.proxy.get("host")
        port = self.proxy.get("proxy_port") or self.proxy.get("port")
        if not host or not port:
            return None
        user = self.proxy.get("proxy_user") or self.proxy.get("username")
        pwd = self.proxy.get("proxy_pass") or self.proxy.get("password")
        server = f"http://{host}:{port}"
        cfg: Dict[str, Any] = {"server": server}
        if user:
            cfg["username"] = str(user)
            cfg["password"] = str(pwd or "")
        return cfg

    async def _ensure_page(self):
        if self._context is None:
            raise BrowserSessionError("浏览器上下文未就绪")
        pages = [p for p in self._context.pages if not p.is_closed()]
        if pages:
            return pages[0]
        return await self._context.new_page()

    async def _navigate_home(self) -> None:
        page = await self._ensure_page()
        last_err: Optional[Exception] = None
        for url in IM_HOME_URLS:
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                # 等待页面自身完成 IM 初始化并建立 WS
                await self._wait_im_ws(timeout=25)
                self._page_load_ts = time.time()
                logger.info(f"【{self.cookie_id}】IM 页面加载完成: {url}")
                return
            except Exception as exc:
                last_err = exc
                logger.warning(f"【{self.cookie_id}】IM 页面加载失败 {url}: {exc}")
        if last_err:
            raise BrowserSessionError(f"IM 页面无法加载: {last_err}")

    async def _wait_im_ws(self, timeout: float = 25.0) -> bool:
        """等待页面自己把 IM WebSocket 建起来。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                state = await self.im_ws_state()
            except Exception:
                state = None
            if state and state.get("exists"):
                if state.get("readyState") == 1:
                    return True
                if state.get("readyState") in (0, 2, 3):
                    # 已存在但未 open，继续等
                    pass
            await asyncio.sleep(0.5)
        return False

    async def reload_page(self, reason: str = "") -> bool:
        """整页重载：等价鱼店长 Pro 的 `RefreshPage(reason)`。"""
        async with self._lock:
            if self._closed:
                return False
            try:
                page = await self._ensure_page()
                self._reload_count += 1
                logger.warning(
                    f"【{self.cookie_id}】刷新 IM 页面(第{self._reload_count}次) 原因: {reason or '未指定'}"
                )
                await page.reload(wait_until="domcontentloaded", timeout=45000)
                ok = await self._wait_im_ws(timeout=25)
                self._page_load_ts = time.time()
                if ok:
                    logger.info(f"【{self.cookie_id}】刷新后 IM WebSocket 已恢复")
                else:
                    logger.warning(f"【{self.cookie_id}】刷新后 IM WebSocket 尚未建立")
                return ok
            except Exception as exc:
                logger.error(f"【{self.cookie_id}】刷新 IM 页面失败: {exc}")
                return False

    async def close(self) -> None:
        self._closed = True
        try:
            # 挂载模式下 context 属于 utils.persistent_browser，由它负责释放，
            # 这里关掉会把账号的常驻浏览器一起干掉，导致其它链路全部失效。
            if self._context is not None and self._owns_context:
                await self._context.close()
        except Exception as exc:
            logger.debug(f"【{self.cookie_id}】关闭浏览器上下文异常（忽略）: {exc}")
        finally:
            self._attach_context = None
            self._context = None
            self._page = None
            try:
                if self._pw is not None:
                    await self._pw.stop()
            except Exception:
                pass
            self._pw = None
            self._started = False
            logger.info(f"【{self.cookie_id}】浏览器态会话已关闭")

    # ------------------------------------------------------------------ 事件桥

    async def _on_ws_event(self, source, payload: str) -> None:
        """Playwright binding 回调：页面 WS 事件 -> asyncio 队列。"""
        try:
            data = json.loads(payload) if isinstance(payload, str) else dict(payload)
        except Exception:
            return

        now = time.time()
        self._last_event_ts = now
        etype = data.get("type")
        if etype == "open":
            self._last_open_ts = now
        elif etype == "close":
            self._last_close_ts = now
        data["ts"] = now

        try:
            self.event_queue.put_nowait(data)
        except asyncio.QueueFull:
            # 队列满时丢弃最旧的，保证最新事件能进
            try:
                self.event_queue.get_nowait()
                self.event_queue.put_nowait(data)
            except Exception:
                pass

    async def next_event(self, timeout: Optional[float] = None) -> Optional[Dict[str, Any]]:
        try:
            if timeout is None:
                return await self.event_queue.get()
            return await asyncio.wait_for(self.event_queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    # ------------------------------------------------------------------ IM 控制

    async def im_ws_state(self) -> Dict[str, Any]:
        page = await self._ensure_page()
        for frame in page.frames:
            try:
                st = await frame.evaluate("() => (window.__xyWsState ? window.__xyWsState() : null)")
                if st and st.get("exists"):
                    return st
            except Exception:
                continue
        try:
            return await page.evaluate("() => (window.__xyWsState ? window.__xyWsState() : {exists:false, readyState:-1})")
        except Exception:
            return {"exists": False, "readyState": -1}

    async def send_im_raw(self, text: str) -> bool:
        """把一段 LWP JSON 文本经页面 IM WebSocket 发出。"""
        page = await self._ensure_page()
        for frame in page.frames:
            try:
                ok = await frame.evaluate(
                    "(t) => (window.__xyWsSendIm ? window.__xyWsSendIm(t) : false)", text
                )
                if ok:
                    return True
            except Exception:
                continue
        return False

    async def health(self) -> Dict[str, Any]:
        page = await self._ensure_page()
        try:
            return await page.evaluate(PAGE_HEALTH_JS)
        except Exception as exc:
            return {"ws": {"exists": False, "readyState": -1}, "error": str(exc)}

    # ------------------------------------------------------------------ Cookie / Token

    async def dump_cookies(self) -> List[Dict[str, Any]]:
        if self._context is None:
            return []
        try:
            return await self._context.cookies()
        except Exception as exc:
            logger.debug(f"【{self.cookie_id}】导出 cookie 失败: {exc}")
            return []

    async def cookies_to_string(self) -> str:
        """把浏览器 cookie jar 转成项目通用的 ``k=v; k2=v2`` 串。"""
        cookies = await self.dump_cookies()
        if not cookies:
            return ""
        seen: Dict[str, str] = {}
        order: List[str] = []
        for c in cookies:
            name = c.get("name")
            if not name:
                continue
            # 同名 cookie 以最后写入者为准
            if name not in seen:
                order.append(name)
            seen[name] = c.get("value", "")
        return "; ".join(f"{n}={seen[n]}" for n in order)

    def _parse_cookies_str(self, cookies_str: str) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for part in (cookies_str or "").split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            name, _, value = part.partition("=")
            name = name.strip()
            value = value.strip()
            if not name:
                continue
            out.append(
                {
                    "name": name,
                    "value": value,
                    "domain": ".goofish.com",
                    "path": "/",
                }
            )
        return out

    async def _apply_cookies_str(self, cookies_str: str) -> None:
        if self._context is None or not cookies_str:
            return
        items = self._parse_cookies_str(cookies_str)
        if not items:
            return
        try:
            await self._context.add_cookies(items)
            logger.debug(f"【{self.cookie_id}】已注入 {len(items)} 条 cookie 到浏览器")
        except Exception as exc:
            logger.warning(f"【{self.cookie_id}】注入 cookie 失败: {exc}")

    async def apply_cookies(self, cookies_str: str, *, reload_if_changed: bool = True) -> bool:
        """把外部（扫码登录/手动导入）产生的新 cookie 灌进浏览器会话。

        场景：用户在面板扫码登录，cookie 变了，但浏览器里还是旧会话。
        这里覆盖写入并刷新页面，等价于"换号后重新打开客服页"。
        """
        if not cookies_str or self._context is None:
            return False
        items = self._parse_cookies_str(cookies_str)
        if not items:
            return False
        try:
            await self._context.add_cookies(items)
        except Exception as exc:
            logger.warning(f"【{self.cookie_id}】同步 cookie 到浏览器失败: {exc}")
            return False

        self.cookies_str = cookies_str
        logger.info(f"【{self.cookie_id}】已同步 {len(items)} 条新 cookie 到浏览器")

        if reload_if_changed:
            await self.reload_page(reason='cookie 已更新，重载会话')
        return True

    async def get_im_token(self, device_id: str = "") -> Dict[str, Any]:
        """从网页上下文换 IM accessToken（`GetTokenFromWebAsync` 等价）。

        sign 在 Python 侧算（浏览器 Web Crypto 不支持 MD5），页面只负责用自身
        cookie 与指纹把请求发出去。
        """
        # 1) 先确保 `_m_h5_tk` 已下发（扫码后常见缺失）
        cookies = await self.dump_cookies()
        tk_full = ""
        for c in cookies:
            if c.get("name") == "_m_h5_tk":
                tk_full = c.get("value") or ""
                break
        tk = tk_full.split("_")[0] if tk_full else ""
        if not tk:
            return {"ok": False, "reason": "no_m_h5_tk"}

        # 2) Python 侧计算签名（外层 appKey=34839810，内层 data 里是 444e9908...）
        import hashlib
        import json as _json
        import urllib.parse as _up

        data_str = _json.dumps(
            {"appKey": MTOP_APP_KEY, "deviceId": device_id},
            separators=(",", ":"),
        )
        ts = str(int(time.time() * 1000))
        sign = hashlib.md5(
            f"{tk}&{ts}&{MTOP_SIGN_APP_KEY}&{data_str}".encode("utf-8")
        ).hexdigest()

        query = _up.urlencode({
            "jsv": "2.7.2",
            "appKey": MTOP_SIGN_APP_KEY,
            "t": ts,
            "sign": sign,
            "v": "1.0",
            "type": "originaljson",
            "accountSite": "xianyu",
            "dataType": "json",
            "timeout": "20000",
            "api": IM_TOKEN_API,
            "sessionOption": "AutoLoginOnly",
            "smToken": "token",
            "queryToken": "sm",
            "sm": "sm",
            "spm_cnt": "a21ybx.im.0.0",
            "data": data_str,
        })
        url = f"https://h5api.m.goofish.com/h5/{IM_TOKEN_API}/1.0/?{query}"

        # 3) 交给页面发出（自带 cookie / UA / 指纹）
        params = {"url": url}
        page = await self._ensure_page()
        for frame in page.frames:
            try:
                res = await frame.evaluate(TOKEN_FETCH_JS, params)
            except Exception as exc:
                logger.debug(f"【{self.cookie_id}】token 取用异常(frame): {exc}")
                continue
            if not isinstance(res, dict):
                continue
            if res.get("ok") and res.get("token"):
                logger.info(f"【{self.cookie_id}】从浏览器取得 IM token")
                return res
            last_reason = res.get("ret") or res.get("reason")
        else:
            last_reason = "all_frames_failed"

        try:
            res = await page.evaluate(TOKEN_FETCH_JS, params)
            if isinstance(res, dict) and res.get("ok") and res.get("token"):
                logger.info(f"【{self.cookie_id}】从浏览器取得 IM token")
                return res
            if isinstance(res, dict):
                last_reason = res.get("ret") or res.get("reason") or last_reason
        except Exception as exc:
            logger.debug(f"【{self.cookie_id}】token 取用异常(page): {exc}")

        return {"ok": False, "reason": last_reason or "token_failed"}

    # ------------------------------------------------------------------ 诊断

    def stats(self) -> Dict[str, Any]:
        now = time.time()
        return {
            "cookie_id": self.cookie_id,
            "started": self._started,
            "headless": self.headless,
            "reload_count": self._reload_count,
            "page_uptime": (now - self._page_load_ts) if self._page_load_ts else 0.0,
            "last_event_age": (now - self._last_event_ts) if self._last_event_ts else None,
            "last_open_age": (now - self._last_open_ts) if self._last_open_ts else None,
            "last_close_age": (now - self._last_close_ts) if self._last_close_ts else None,
        }
