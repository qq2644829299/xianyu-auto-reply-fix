"""在常驻浏览器里完成扫码登录（对应「鱼店长Pro」的 QRCodeImage / QRCodeVisibility）。

为什么不再走纯 HTTP 扫码
========================
原实现（`utils/qr_login.py`）是用 HTTP 请求取二维码、再轮询登录状态。这条路的问题：

* 二维码与"最终登录出来的会话"不在同一个浏览器环境里，风控 cookie
  （`_m_h5_tk` / `baxia` / `x5sec`）在后续请求里对不上；
* 扫码成功后仍要把 cookie 字符串搬运到另一个浏览器，等价于"换了个环境登录"，
  平台很容易判定异常；
* 一旦要过验证，纯 HTTP 链路完全无能为力。

鱼店长的做法：**二维码就在它自己的持久化 Chromium 里显示**（`QRCodeImage` 绑定到
`ImgQRcode` / `PopQRCode`），扫完 cookie 天然落在同一个 profile，全程一个浏览器身份。

本模块按同样思路实现：
    新开标签页 -> 打开登录页 -> 截取二维码 -> 轮询 cookie 判定 -> 关页
成功后的 cookie 已经在 context 里，可直接回灌给账号 profile。
"""

from __future__ import annotations

import asyncio
import base64
import time
import uuid
from typing import Any, Dict, List, Optional

from loguru import logger

from .browser_session import BrowserIMSession

# 闲鱼/淘宝登录页里的二维码元素候选（多版本并存）
QRCODE_SELECTORS = (
    "#login-qrcode",
    "#qrcode",
    ".qrcode-img",
    ".login-qrcode",
    "#J_QRCodeImg",
    "canvas#qrcode",
    "img[src^='data:image']",
    "div[class*='qrcode'] img",
    "div[class*='qr'] canvas",
    ".qrcode canvas",
)

LOGIN_PAGE_URLS = (
    "https://passport.goofish.com/mini_login.htm?lang=zh_cn&appName=xianyu&appEntrance=web&styleType=vertical&bizParams=&notLoadSsoView=false&notKeepLogin=false&isMobile=false&ad__wfp=&rnd=0.1",
    "https://www.goofish.com/login",
)

RISK_URL_HINTS = ("punish", "_____tmd_____", "x5secdata", "sec.taobao.com")

# 触发 `_m_h5_tk` 下发。
#
# 扫码登录成功只会拿到 passport 主站的登录 cookie，**不含 `_m_h5_tk`**；
# 而 `_m_h5_tk` 是 h5api.m.goofish.com 这个域下发的，只有访问过该域才会有。
# 没有它，后续所有 mtop 调用都会报 FAIL_SYS_TOKEN_EMPTY::令牌为空，
# IM 通道也注册不上（表现为"登录成功了但跑不通"）。
#
# mtop 的标准握手就是：第一次请求不带有效 sign -> 服务端返回错误但 set-cookie
# 下发 `_m_h5_tk` -> 客户端带 token 重算 sign 再请求。这里复刻第一步。
BOOTSTRAP_TOKEN_JS = r"""
async (params) => {
  const api = params.api;
  const appKey = params.appKey;
  const t = String(Date.now());
  const qs = new URLSearchParams({
    jsv: '2.7.2',
    appKey: appKey,
    t: t,
    sign: 'bootstrap',          // 故意无效：只为触达服务端拿 set-cookie
    api: api,
    v: '1.0',
    type: 'originaljson',
    dataType: 'json',
    data: '{}'
  });
  let respText = '';
  try {
    const r = await fetch('https://h5api.m.goofish.com/h5/' + api + '/1.0/?' + qs.toString(),
                          { credentials: 'include' });
    respText = (await r.text()).slice(0, 300);
  } catch (e) {
    respText = 'fetch_error: ' + String(e);
  }
  const m = document.cookie.match(/(?:^|; )_m_h5_tk=([^;]*)/);
  return { hasTk: !!m, resp: respText };
}
"""

_TOKEN_API = "mtop.taobao.idlemessage.pc.login.token"
_DEFAULT_APP_KEY = "444e9908a51d1cb236a27862abc769c9"


class BrowserQRLogin:
    """一次扫码登录会话。"""

    def __init__(
        self,
        session: BrowserIMSession,
        *,
        timeout_seconds: float = 180.0,
        login_urls: Optional[tuple] = None,
    ) -> None:
        self.session = session
        self.cookie_id = session.cookie_id
        self.timeout_seconds = float(timeout_seconds)
        # 传空元组表示不主动导航（自检/页面已就绪时使用）
        self.login_urls = LOGIN_PAGE_URLS if login_urls is None else login_urls

        self.session_id = uuid.uuid4().hex[:16]
        self.created_at = time.time()
        self.status = "init"          # init / waiting / scanned / success / expired / error
        self.message = ""
        self.image_b64 = ""
        self._page = None
        self._owns_page = False
        self._closed = False

    # ------------------------------------------------------------------ 生命周期

    async def start(self) -> Dict[str, Any]:
        """打开登录页并截取二维码。"""
        try:
            ctx = self.session._context
            if ctx is None:
                return self._fail("浏览器上下文未就绪")

            if not self.login_urls:
                # 调用方已备好页面（自检场景）：复用主页面，不要新开也不要关闭它
                self._page = await self.session._ensure_page()
                self._owns_page = False
                opened = True
            else:
                self._page = await ctx.new_page()
                self._owns_page = True
                for url in self.login_urls:
                    try:
                        await self._page.goto(url, wait_until="domcontentloaded", timeout=45000)
                        await asyncio.sleep(0.8)
                        opened = True
                        break
                    except Exception as exc:
                        logger.debug(f"【{self.cookie_id}】扫码页加载失败 {url[:50]}: {exc}")
            if not opened:
                return self._fail("登录页加载失败")

            img_b64 = await self._capture_qrcode()
            if not img_b64:
                # 有些版本二维码在 iframe 里，等一会儿再取一次
                for _ in range(6):
                    await asyncio.sleep(0.7)
                    img_b64 = await self._capture_qrcode()
                    if img_b64:
                        break
            if not img_b64:
                return self._fail("未找到二维码元素")

            self.image_b64 = img_b64
            self.status = "waiting"
            data_url = f"data:image/png;base64,{img_b64}"
            logger.info(f"【{self.cookie_id}】[扫码] 二维码已就绪 (session={self.session_id})")
            return {
                "success": True,
                "session_id": self.session_id,
                # 前端（static/js/app.js: showQRCodeImage(data.qr_code_url)）读的是
                # qr_code_url，必须保持与原 HTTP 扫码同名，否则前端拿到 undefined。
                "qr_code_url": data_url,
                "image": data_url,
                "status": "waiting",
                "message": "请使用闲鱼 App 扫码",
                "engine": "browser",
            }
        except Exception as exc:
            logger.error(f"【{self.cookie_id}】[扫码] 启动异常: {exc}")
            return self._fail(f"启动扫码异常: {exc}")

    async def check(self) -> Dict[str, Any]:
        """轮询登录状态。"""
        if self.status in ("success", "expired", "error"):
            return self._result()

        if self._closed or self._page is None:
            return self._fail("扫码会话已关闭")

        if time.time() - self.created_at > self.timeout_seconds:
            self.status = "expired"
            self.message = "二维码已超时，请重新获取"
            await self.cancel()
            return self._result()

        try:
            # 1) cookie 直判（最可靠）
            cookies = await self.session.dump_cookies()
            names = {c.get("name") for c in cookies}
            if "unb" in names and ({"cookie2", "sgcookie", "_tb_token_"} & names):
                # 登录成功了，但还缺 `_m_h5_tk`：必须先握手拿到它，否则后续
                # mtop 全报"令牌为空"，IM 通道注册不上。
                if "_m_h5_tk" not in names:
                    await self._bootstrap_mtop_token()
                    cookies = await self.session.dump_cookies()
                    names = {c.get("name") for c in cookies}

                self.status = "success"
                self.message = "扫码登录成功"
                cookies_str = await self.session.cookies_to_string()
                logger.info(
                    f"【{self.cookie_id}】[扫码] 登录成功"
                    f"（_m_h5_tk {'已就绪' if '_m_h5_tk' in names else '仍缺失'}）"
                )
                await self.cancel()
                return {
                    "success": True,
                    "status": "success",
                    "session_id": self.session_id,
                    "message": self.message,
                    "cookies": cookies_str,
                    "has_m_h5_tk": "_m_h5_tk" in names,
                }

            # 2) 页面状态
            url = self._page.url or ""
            if any(h in url for h in RISK_URL_HINTS):
                self.status = "error"
                self.message = "扫码后需要安全验证"
                return self._result()

            # 3) 已被扫但未确认
            try:
                body = await self._page.evaluate(
                    "() => (document.body ? document.body.innerText.slice(0,600) : '')"
                ) or ""
                if "扫描成功" in body or "已扫描" in body or "请在手机上确认" in body:
                    if self.status != "scanned":
                        self.status = "scanned"
                        self.message = "已扫码，请在手机上确认"
                        logger.info(f"【{self.cookie_id}】[扫码] 已扫码，等待手机确认")
            except Exception:
                pass

            return self._result()
        except Exception as exc:
            logger.debug(f"【{self.cookie_id}】[扫码] 轮询异常: {exc}")
            return self._result()

    async def cancel(self) -> None:
        """关闭扫码标签页（cookie 已留在 context 里，不受影响）。

        只关闭**本会话新开**的页面；复用主页面时绝不能关，否则会把 IM 页干掉。
        """
        self._closed = True
        if self._page is not None and self._owns_page:
            try:
                await self._page.close()
            except Exception:
                pass
        self._page = None

    # ------------------------------------------------------------------ 内部

    async def _bootstrap_mtop_token(self, attempts: int = 3) -> bool:
        """在页面里握手一次 h5api，让服务端下发 `_m_h5_tk`。

        这是"扫码成功却跑不通"的根因修复点：没有 `_m_h5_tk` 时所有 mtop 调用
        都会 FAIL_SYS_TOKEN_EMPTY，IM 注册不上。
        """
        page = self._page
        if page is None:
            return False

        frames: List[Any] = []
        try:
            # 优先在 goofish 主域的 frame 里执行（cookie 域匹配才拿得到 set-cookie）
            frames = [f for f in page.frames if "goofish.com" in (f.url or "")]
            if not frames:
                frames = list(page.frames)
        except Exception:
            frames = [page.main_frame]

        for attempt in range(1, attempts + 1):
            for frame in frames:
                try:
                    res = await frame.evaluate(
                        BOOTSTRAP_TOKEN_JS,
                        {"api": _TOKEN_API, "appKey": _DEFAULT_APP_KEY},
                    )
                    if isinstance(res, dict) and res.get("hasTk"):
                        logger.info(
                            f"【{self.cookie_id}】[扫码] 已取得 _m_h5_tk（第 {attempt} 次握手）"
                        )
                        return True
                except Exception as exc:
                    logger.debug(f"【{self.cookie_id}】[扫码] token 握手异常: {exc}")
            await asyncio.sleep(0.6)

        logger.warning(
            f"【{self.cookie_id}】[扫码] 未能取得 _m_h5_tk，"
            f"后续 mtop 可能报「令牌为空」，需要在页面里访问一次 h5api"
        )
        return False

    async def _capture_qrcode(self) -> str:
        """定位二维码元素并截图返回 base64。"""
        page = self._page
        if page is None:
            return ""
        frames: List[Any] = []
        try:
            frames = list(page.frames)
        except Exception:
            frames = [page.main_frame]

        for frame in frames:
            for sel in QRCODE_SELECTORS:
                try:
                    el = await frame.query_selector(sel)
                    if not el:
                        continue
                    box = await el.bounding_box()
                    if not box or box["width"] < 40 or box["height"] < 40:
                        continue
                    png = await el.screenshot(type="png")
                    if png:
                        return base64.b64encode(png).decode("ascii")
                except Exception:
                    continue
        return ""

    def _fail(self, message: str) -> Dict[str, Any]:
        self.status = "error"
        self.message = message
        logger.warning(f"【{self.cookie_id}】[扫码] {message}")
        return {
            "success": False,
            "status": "error",
            "session_id": self.session_id,
            "message": message,
        }

    def _result(self) -> Dict[str, Any]:
        return {
            "success": self.status == "success",
            "status": self.status,
            "session_id": self.session_id,
            "message": self.message,
            "age_seconds": round(time.time() - self.created_at, 1),
        }


class BrowserQRLoginManager:
    """扫码会话注册表（进程内）。"""

    def __init__(self, max_sessions: int = 3) -> None:
        self._items: Dict[str, BrowserQRLogin] = {}
        self.max_sessions = max(1, int(max_sessions))

    def _reap(self) -> None:
        now = time.time()
        for sid in list(self._items.keys()):
            sess = self._items[sid]
            if sess._closed or now - sess.created_at > sess.timeout_seconds + 60:
                self._items.pop(sid, None)

    async def create(self, session: BrowserIMSession, *, timeout_seconds: float = 180.0) -> BrowserQRLogin:
        self._reap()
        if len(self._items) >= self.max_sessions:
            # 关掉最老的一个，避免堆积标签页吃内存
            oldest = min(self._items.values(), key=lambda s: s.created_at)
            await oldest.cancel()
            self._items.pop(oldest.session_id, None)
        sess = BrowserQRLogin(session, timeout_seconds=timeout_seconds)
        self._items[sess.session_id] = sess
        return sess

    def get(self, session_id: str) -> Optional[BrowserQRLogin]:
        return self._items.get(session_id)

    async def drop(self, session_id: str) -> None:
        sess = self._items.pop(session_id, None)
        if sess is not None:
            await sess.cancel()

    async def drop_all(self) -> None:
        for sid in list(self._items.keys()):
            await self.drop(sid)

    def stats(self) -> Dict[str, Any]:
        return {
            "active": len(self._items),
            "sessions": {
                sid: {"status": s.status, "age": round(time.time() - s.created_at, 1)}
                for sid, s in self._items.items()
            },
        }


qr_login_manager_browser = BrowserQRLoginManager()
