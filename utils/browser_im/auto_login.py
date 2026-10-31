"""浏览器内自动登录（对应「鱼店长Pro」的 `AutoLoginProcedure` 四步流程）。

逆向依据
========
`AgisoIdleClient.Enums.AutoLoginProcedure` 直接给出了它的登录步骤枚举：

    切换账号登录,
    勾选隐私条款,
    自动输入账号密码,
    点击授权并登录

配套的运行时结构：

| 成员 | 含义 |
|---|---|
| `AccountConfig.EnableAutoLogin` / `UserName` / `Password` | 账号密码自动登录开关与凭据 |
| `LoginInfo { LoginPhoneOrName, Password }` | 登录凭据模型 |
| `IUserInfoManager.GetLoginInfo / SaveLoginInfo` | 凭据持久化（加密） |
| `LoginDoResponse.Content.Data { Redirect, TitleMsg, LoginResult }` | 登录接口响应 |
| `OnFrameLoadEnd` / `ExecuteScriptsInOrderAsync` | 页面加载完按序执行脚本 |
| `LoginAuthFailAsync` | 登录失败分支 |
| `LoginSuccessAsync(BaseUserInfo)` | 登录成功，落账号信息 |
| `BaseUserInfo { NickName, PlatformShopId, ShopName, ReplyDeadLine }` | 账号档案 |

要点：**登录全程在同一个持久化 Chromium 里完成**，凭据由程序填入、协议由程序勾选、
提交按钮由程序点击，成功后 cookie 天然留在 profile 里——不需要把 cookie 搬来搬去。

本模块就是在 `BrowserIMSession` 的页面上复刻这四步。
"""

from __future__ import annotations

import asyncio
import random
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from .browser_session import BrowserIMSession

# ---------------------------------------------------------------------------
# 登录页地址与元素候选
# ---------------------------------------------------------------------------

LOGIN_URLS = (
    "https://passport.goofish.com/mini_login.htm?lang=zh_cn&appName=xianyu&appEntrance=web&styleType=vertical&bizParams=&notLoadSsoView=false&notKeepLogin=false&isMobile=false&ad__wfp=&rnd=0.1",
    "https://www.goofish.com/login",
    "https://www.goofish.com/",
)

# 第 1 步：切换到「账号密码登录」
SWITCH_ACCOUNT_SELECTORS = (
    "a:has-text('账号密码登录')",
    "div:has-text('账号密码登录')",
    ".login-switch",
    "#J_Quick2Static",
    "[data-spm*='password']",
    "a:has-text('密码登录')",
)

SWITCH_ACCOUNT_TEXTS = ("账号密码登录", "密码登录", "账号登录")

# 第 2 步：勾选隐私条款
AGREEMENT_SELECTORS = (
    "#J_AgreementBtn",
    ".agree-checkbox",
    "input[type='checkbox'][name*='agree']",
    "label:has-text('同意') input[type='checkbox']",
    "#fm-agreement-checkbox",
    ".login-blocks .checkbox",
    "i.icon-checkbox",
)

AGREEMENT_TEXTS = ("同意", "已阅读并同意", "隐私政策", "服务协议")

# 第 3 步：账号 / 密码输入框
USERNAME_SELECTORS = (
    "#fm-login-id",
    "input[name='loginId']",
    "input[name='account']",
    "input#fm-login-id",
    "input[placeholder*='手机号']",
    "input[placeholder*='账号']",
    "input[type='text']",
)

PASSWORD_SELECTORS = (
    "#fm-login-password",
    "input[name='password']",
    "input[type='password']",
    "input[placeholder*='密码']",
)

# 第 4 步：提交
SUBMIT_SELECTORS = (
    "button.fm-button",
    "button[type='submit']",
    ".login-submit",
    "#login-submit",
    "button:has-text('登录')",
    "div:has-text('授权并登录')",
    "button:has-text('授权并登录')",
)

# 风控/异常识别
RISK_URL_HINTS = ("punish", "_____tmd_____", "x5secdata", "sec.taobao.com", "captcha")
RISK_TEXT_HINTS = ("滑动", "拖动", "请完成验证", "安全验证", "验证码")

PASSWORD_ERROR_HINTS = ("密码错误", "账号或密码", "账户名或密码不正确", "密码不正确")
LOCKED_HINTS = ("账号被冻结", "账号存在风险", "已被限制", "暂时无法登录", "账号异常")
NOT_EXIST_HINTS = ("账号不存在", "用户不存在", "该账号未注册")


class AutoLoginResult(dict):
    """登录结果。字段：ok / reason / user_info / cookies / attempts。"""

    @property
    def ok(self) -> bool:
        return bool(self.get("ok"))


class BrowserAutoLogin:
    """在常驻浏览器里执行四步自动登录。"""

    def __init__(
        self,
        session: BrowserIMSession,
        username: str,
        password: str,
        *,
        slider: Any = None,
        login_timeout_seconds: float = 60.0,
        step_delay: Tuple[float, float] = (0.35, 0.9),
        login_urls: Optional[Tuple[str, ...]] = None,
    ) -> None:
        self.session = session
        self.cookie_id = session.cookie_id
        self.username = (username or "").strip()
        self.password = password or ""
        self.slider = slider
        self.login_timeout_seconds = float(login_timeout_seconds)
        self.step_delay = step_delay
        # 传空元组表示不主动导航（自检/已在登录页时使用）
        self.login_urls = LOGIN_URLS if login_urls is None else login_urls

        # 登录接口响应，供结果判定
        self._login_payloads: List[Dict[str, Any]] = []
        self._responses_hooked = False
        self.steps_done: List[str] = []

    # ------------------------------------------------------------------ 公共入口

    async def login(self) -> AutoLoginResult:
        if not self.username or not self.password:
            return AutoLoginResult(ok=False, reason="missing_credentials", attempts=0)

        # ---------------------------------------------------------------
        # 关键：登录用**独立标签页**，不碰常驻的 IM 页面。
        #
        # 逆向鱼店长的结构：`AgisoIdleClient.Login : Window` 是一个独立窗口，
        # 登录完成后才进入主界面（`LoginSuccessAsync` -> MainWindow）。
        # 早期实现是在 IM 页面上直接 goto 登录页，代价是：
        #   goto -> IM WebSocket 断开 -> 登录 -> 再切回 IM -> 重建 WS 与 token
        # 这一套来回跑下来既慢又容易把会话搞脏，用户看到的就是"一直转圈"。
        # ---------------------------------------------------------------
        login_page = None
        main_page = None
        try:
            main_page = await self.session._ensure_page()
            login_page = await self._open_isolated_page()
            await self._install_response_hook(login_page)

            logger.info(
                f"【{self.cookie_id}】[自动登录] 开始四步登录"
                f"（账号={self._mask(self.username)}，独立标签页）"
            )

            # 0) 打开登录页
            if not await self._open_login_page(login_page):
                return AutoLoginResult(ok=False, reason="login_page_unreachable", attempts=1)

            # 1) 切换账号登录
            await self._step_switch_to_password(login_page)

            # 2) 勾选隐私条款
            await self._step_agree_terms(login_page)

            # 3) 输入账号密码
            if not await self._step_fill_credentials(login_page):
                return AutoLoginResult(ok=False, reason="credentials_input_failed", attempts=1)

            # 4) 提交
            if not await self._step_submit(login_page):
                return AutoLoginResult(ok=False, reason="submit_failed", attempts=1)

            # 5) 等结果
            outcome = await self._await_login_outcome(login_page)

            # 6) 需要滑块 -> 同会话页内过验证后重试一次
            if outcome.get("reason") in ("verify_required", "risk_control"):
                if await self._try_slider(login_page):
                    outcome = await self._await_login_outcome(login_page)

            if outcome.get("ok"):
                cookies = await self.session.cookies_to_string()
                user_info = await self._collect_user_info(login_page)
                logger.info(
                    f"【{self.cookie_id}】[自动登录] 成功"
                    + (f"，昵称={user_info.get('nick_name')}" if user_info.get("nick_name") else "")
                )
                return AutoLoginResult(
                    ok=True,
                    reason="ok",
                    user_info=user_info,
                    cookies=cookies,
                    attempts=1,
                )

            logger.warning(
                f"【{self.cookie_id}】[自动登录] 失败: {outcome.get('reason')} "
                f"{outcome.get('detail') or ''}"
            )
            return AutoLoginResult(
                ok=False,
                reason=outcome.get("reason") or "unknown",
                user_info={},
                cookies="",
                attempts=1,
            )

        except Exception as exc:
            logger.error(f"【{self.cookie_id}】[自动登录] 异常: {exc}")
            return AutoLoginResult(ok=False, reason=f"exception: {exc}", attempts=1)
        finally:
            # 只关闭真正新开的登录标签页；主页面（自检/降级场景）绝不能关
            if login_page is not None and login_page is not main_page:
                try:
                    await login_page.close()
                    logger.debug(f"【{self.cookie_id}】[自动登录] 已关闭登录标签页")
                except Exception:
                    pass

    async def _open_isolated_page(self):
        """在同一个 context 里新开一个标签页做登录（共享 cookie，不干扰 IM 页）。

        例外：`login_urls` 为空表示"调用方已备好页面"（自检场景），此时直接用当前页。
        """
        if not self.login_urls:
            return await self.session._ensure_page()
        ctx = self.session._context
        if ctx is None:
            return await self.session._ensure_page()
        try:
            page = await ctx.new_page()
            # init script 已注册在 context 上，新页面自动继承（含 WS 劫持与 stealth）
            return page
        except Exception as exc:
            logger.warning(f"【{self.cookie_id}】[自动登录] 新建标签页失败，改用主页面: {exc}")
            return await self.session._ensure_page()

    # ------------------------------------------------------------------ 响应拦截

    async def _install_response_hook(self, page) -> None:
        if self._responses_hooked:
            return
        self._responses_hooked = True

        def _on_response(resp):
            try:
                url = (resp.url or "").lower()
                if not any(k in url for k in ("login.do", "newlogin", "haslogin", "xianyu")):
                    return
                if "json" not in (resp.headers.get("content-type") or ""):
                    return
                # 只记下异步任务，避免阻塞事件循环
                asyncio.ensure_future(self._capture_payload(resp))
            except Exception:
                pass

        try:
            page.on("response", _on_response)
            logger.debug(f"【{self.cookie_id}】[自动登录] 已挂载登录响应拦截")
        except Exception as exc:
            logger.debug(f"【{self.cookie_id}】[自动登录] 响应拦截挂载失败: {exc}")

    async def _capture_payload(self, resp) -> None:
        try:
            body = await resp.json()
            if isinstance(body, dict):
                self._login_payloads.append({"url": resp.url, "body": body})
                if len(self._login_payloads) > 20:
                    self._login_payloads.pop(0)
        except Exception:
            pass

    # ------------------------------------------------------------------ 步骤 0

    async def _open_login_page(self, page) -> bool:
        # login_urls 为空：认为调用方已经把页面准备好了（自检场景）
        if not self.login_urls:
            logger.info(f"【{self.cookie_id}】[自动登录] 使用当前页面作为登录页")
            return True
        for url in self.login_urls:
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                await asyncio.sleep(random.uniform(*self.step_delay))
                body = await page.evaluate(
                    "() => (document.body ? document.body.innerText.slice(0, 500) : '')"
                )
                if body and ("登录" in body or "扫码" in body or "账号" in body):
                    logger.info(f"【{self.cookie_id}】[自动登录] 登录页就绪: {page.url[:90]}")
                    return True
            except Exception as exc:
                logger.debug(f"【{self.cookie_id}】[自动登录] 打开 {url[:60]} 失败: {exc}")
        logger.warning(f"【{self.cookie_id}】[自动登录] 登录页加载失败")
        return False

    # ------------------------------------------------------------------ 步骤 1

    async def _credentials_visible(self, page) -> bool:
        """账号/密码输入框是否真的可见（用于判定"是否已处于密码登录态"）。"""
        for sel in (USERNAME_SELECTORS[0], PASSWORD_SELECTORS[0], "#fm-login-password"):
            for frame in await self._frames(page):
                try:
                    el = await frame.query_selector(sel)
                    if el and await el.is_visible():
                        return True
                except Exception:
                    continue
        return False

    async def _step_switch_to_password(self, page) -> bool:
        """切换账号登录。

        关键：`div:has-text()` 这类选择器会命中**外层容器**（点空白处不触发
        tab 的点击处理），所以每个候选点完都必须回验"表单是否真的出现了"，
        否则会出现"日志说切换成功、实际还在扫码页"的假成功。
        """
        # 已经是密码登录态就不用切
        if await self._credentials_visible(page):
            self.steps_done.append("账号密码登录(已是)")
            logger.info(f"【{self.cookie_id}】[自动登录] 1/4 已处于账号密码登录态")
            return True

        candidates = []
        for txt in SWITCH_ACCOUNT_TEXTS:
            # 由精确到宽泛排列：text= 会命中文本最内层元素
            candidates.extend([
                f"text={txt}",
                f"a:has-text('{txt}')",
                f"span:has-text('{txt}')",
                f"div:has-text('{txt}')",
            ])
        candidates.extend(SWITCH_ACCOUNT_SELECTORS)

        seen = set()
        for sel in candidates:
            if sel in seen:
                continue
            seen.add(sel)
            for frame in await self._frames(page):
                try:
                    el = await frame.query_selector(sel)
                    if not el or not await el.is_visible():
                        continue
                    await el.click(timeout=4000)
                    # 回验：表单真的出现了才算切换成功
                    for _ in range(6):
                        await asyncio.sleep(0.15)
                        if await self._credentials_visible(page):
                            self.steps_done.append("切换账号登录")
                            logger.info(f"【{self.cookie_id}】[自动登录] 1/4 已切换到账号密码登录")
                            return True
                except Exception:
                    continue

        logger.info(f"【{self.cookie_id}】[自动登录] 1/4 未切到密码登录（可能本就无需切换）")
        return False

    async def _step_agree_terms(self, page) -> bool:
        """勾选隐私条款（对齐 `CheckedAgreementCheckBoxWithRetryAsync`）。"""
        for attempt in range(3):
            for frame in await self._frames(page):
                for sel in AGREEMENT_SELECTORS:
                    try:
                        el = await frame.query_selector(sel)
                        if not el:
                            continue
                        # 不可见的协议控件（例如表单还没展开）先跳过，避免无谓超时
                        try:
                            if not await el.is_visible():
                                continue
                        except Exception:
                            pass
                        checked = await el.evaluate(
                            "e => (e.checked !== undefined ? !!e.checked : (e.className||'').toString().includes('checked'))"
                        )
                        if checked:
                            self.steps_done.append("勾选隐私条款(已勾选)")
                            logger.info(f"【{self.cookie_id}】[自动登录] 2/4 条款已勾选")
                            return True
                        await el.click(timeout=4000)
                        self.steps_done.append("勾选隐私条款")
                        logger.info(f"【{self.cookie_id}】[自动登录] 2/4 已勾选隐私条款")
                        await asyncio.sleep(random.uniform(0.15, 0.35))
                        return True
                    except Exception:
                        continue
            await asyncio.sleep(0.3)
        logger.info(f"【{self.cookie_id}】[自动登录] 2/4 未找到条款控件（继续尝试提交）")
        return False

    # ------------------------------------------------------------------ 步骤 3

    async def _step_fill_credentials(self, page) -> bool:
        """自动输入账号密码。"""
        user_el = await self._first_visible(page, USERNAME_SELECTORS)
        pass_el = await self._first_visible(page, PASSWORD_SELECTORS)
        if not user_el or not pass_el:
            logger.warning(f"【{self.cookie_id}】[自动登录] 3/4 找不到账号/密码输入框")
            return False

        try:
            await self._human_type(user_el, self.username)
            await asyncio.sleep(random.uniform(0.2, 0.5))
            await self._human_type(pass_el, self.password)
            self.steps_done.append("自动输入账号密码")
            logger.info(f"【{self.cookie_id}】[自动登录] 3/4 已填入账号密码")
            await asyncio.sleep(random.uniform(*self.step_delay))
            return True
        except Exception as exc:
            logger.warning(f"【{self.cookie_id}】[自动登录] 3/4 填写失败: {exc}")
            return False

    async def _human_type(self, element, text: str) -> None:
        """拟人输入：先聚焦，再逐字符键入。"""
        try:
            await element.click(timeout=5000)
        except Exception:
            pass
        try:
            await element.fill("")
        except Exception:
            pass
        # 少量字符用 fill 提速，其余逐字键入保持事件完整
        for ch in text:
            await element.type(ch, delay=random.uniform(28, 95))
        await element.evaluate("e => e.blur()")

    # ------------------------------------------------------------------ 步骤 4

    async def _step_submit(self, page) -> bool:
        """点击授权并登录。"""
        for frame in await self._frames(page):
            for sel in SUBMIT_SELECTORS:
                try:
                    el = await frame.query_selector(sel)
                    if not el or not await el.is_visible():
                        continue
                    enabled = await el.evaluate("e => !e.disabled")
                    if not enabled:
                        continue
                    await el.click(timeout=6000)
                    self.steps_done.append("点击授权并登录")
                    logger.info(f"【{self.cookie_id}】[自动登录] 4/4 已点击登录")
                    return True
                except Exception:
                    continue
        logger.warning(f"【{self.cookie_id}】[自动登录] 4/4 未找到可点击的登录按钮")
        return False

    # ------------------------------------------------------------------ 结果判定

    async def _await_login_outcome(self, page) -> Dict[str, Any]:
        """等待并判定登录结果（对齐 LoginDoResponse 语义）。

        以前这里 60 秒里每 1 秒静默轮询一次，用户看到的就是"一直转圈"。
        现在：轮询更密、每 5 秒打一条进度（当前 URL + 是否有验证），
        并且一旦发现明确的失败信号立刻返回，不白等满超时。
        """
        deadline = time.time() + self.login_timeout_seconds
        start = time.time()
        last_report = 0.0
        saw_password_error = False
        saw_locked = False
        saw_not_exist = False

        while time.time() < deadline:
            await asyncio.sleep(0.5)

            # 1) 接口响应直判（最快路径）
            verdict = self._judge_payloads()
            if verdict:
                if verdict.get("ok"):
                    logger.info(f"【{self.cookie_id}】[自动登录] 接口返回成功: {verdict.get('reason')}")
                return verdict

            # 2) cookie 直判
            try:
                cookies = await self.session.dump_cookies()
                names = {c.get("name") for c in cookies}
                if "unb" in names and ("cookie2" in names or "sgcookie" in names or "_m_h5_tk" in names):
                    logger.info(f"【{self.cookie_id}】[自动登录] 检测到登录 cookie（unb 已就位）")
                    return {"ok": True, "reason": "cookie_ok"}
                if "x5sec" in names:
                    return {"ok": False, "reason": "verify_required", "detail": "x5sec cookie"}
            except Exception:
                pass

            # 3) 页面文案
            try:
                body = await page.evaluate(
                    "() => (document.body ? document.body.innerText.slice(0, 1200) : '')"
                ) or ""
                url = page.url or ""
                if any(h in url for h in RISK_URL_HINTS) or any(h in body for h in RISK_TEXT_HINTS):
                    return {"ok": False, "reason": "verify_required", "detail": url[:120]}
                if any(h in body for h in LOCKED_HINTS):
                    saw_locked = True
                    return {"ok": False, "reason": "account_locked", "detail": body[:120]}
                if any(h in body for h in PASSWORD_ERROR_HINTS):
                    saw_password_error = True
                    return {"ok": False, "reason": "password_error", "detail": body[:120]}
                if any(h in body for h in NOT_EXIST_HINTS):
                    saw_not_exist = True
                    return {"ok": False, "reason": "account_not_exist", "detail": body[:120]}
                if "登录成功" in body:
                    return {"ok": True, "reason": "page_success"}

                # 进度可见：每 5 秒报一次当前位置
                elapsed = time.time() - start
                if elapsed - last_report >= 5.0:
                    last_report = elapsed
                    short_url = url[:70] if url else "(blank)"
                    logger.info(
                        f"【{self.cookie_id}】[自动登录] 等待结果 {int(elapsed)}s/{int(self.login_timeout_seconds)}s"
                        f"，当前页面: {short_url}"
                    )
            except Exception:
                pass

        detail = []
        if saw_password_error:
            detail.append("password_error")
        if saw_locked:
            detail.append("locked")
        if saw_not_exist:
            detail.append("not_exist")
        return {"ok": False, "reason": "login_timeout", "detail": ",".join(detail)}

    def _judge_payloads(self) -> Optional[Dict[str, Any]]:
        """从拦截到的登录接口响应判断结果。"""
        for item in reversed(self._login_payloads):
            body = item.get("body") or {}
            url = item.get("url") or ""

            # hasLogin.do 语义
            data = body.get("content", {}).get("data") if isinstance(body.get("content"), dict) else None
            if isinstance(data, dict):
                if data.get("redirect") is True:
                    return {"ok": True, "reason": "login_redirect", "url": url}
                title = str(data.get("titleMsg") or "")
                result = str(data.get("loginResult") or "")
                if any(h in title for h in LOCKED_HINTS):
                    return {"ok": False, "reason": "account_locked", "detail": title[:120]}
                if any(h in title for h in PASSWORD_ERROR_HINTS):
                    return {"ok": False, "reason": "password_error", "detail": title[:120]}
                if result and any(h in result for h in RISK_TEXT_HINTS):
                    return {"ok": False, "reason": "verify_required", "detail": result[:120]}

            # 通用成功标志
            for k in ("success", "ret"):
                v = body.get(k)
                if isinstance(v, list):
                    joined = " ".join(str(x) for x in v)
                    if "SUCCESS" in joined:
                        return {"ok": True, "reason": "api_success", "url": url}
                    if any(h in joined for h in LOCKED_HINTS):
                        return {"ok": False, "reason": "account_locked", "detail": joined[:120]}
        return None

    async def _try_slider(self, page) -> bool:
        """同会话页内过验证。"""
        if self.slider is None:
            return False
        try:
            logger.warning(f"【{self.cookie_id}】[自动登录] 命中验证，转页内自动滑块")
            result = await self.slider.solve("", user_id=self.username)
            return bool(result.get("ok"))
        except Exception as exc:
            logger.warning(f"【{self.cookie_id}】[自动登录] 滑块处理异常: {exc}")
            return False

    async def _collect_user_info(self, page) -> Dict[str, Any]:
        """采集账号档案（对齐 BaseUserInfo）。"""
        info: Dict[str, Any] = {}
        try:
            cookies = await self.session.dump_cookies()
            for c in cookies:
                if c.get("name") == "unb":
                    info["platform_shop_id"] = c.get("value")
                    break
        except Exception:
            pass
        try:
            name = await page.evaluate(
                """() => {
                  const sel = ['.nick', '.user-nick', '.shop-name', '[class*=nick]'];
                  for (const s of sel) {
                    const e = document.querySelector(s);
                    if (e && e.innerText) return e.innerText.trim().slice(0, 40);
                  }
                  return '';
                }"""
            )
            if name:
                info["nick_name"] = name
        except Exception:
            pass
        return info

    # ------------------------------------------------------------------ 工具

    async def _frames(self, page) -> List[Any]:
        try:
            return list(page.frames)
        except Exception:
            return [page.main_frame]

    async def _first_visible(self, page, selectors) -> Any:
        for frame in await self._frames(page):
            for sel in selectors:
                try:
                    el = await frame.query_selector(sel)
                    if el and await el.is_visible():
                        return el
                except Exception:
                    continue
        return None

    @staticmethod
    def _mask(s: str) -> str:
        if not s:
            return ""
        if len(s) <= 4:
            return s[0] + "***"
        return s[:3] + "****" + s[-2:]
