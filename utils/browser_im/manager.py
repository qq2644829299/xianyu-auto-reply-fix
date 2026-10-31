"""按账号管理浏览器 IM 会话（等价 `AgisoIdleClient` 的 per-account CefSharp 实例池）。"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

from loguru import logger

from .browser_session import BrowserIMSession, BrowserSessionError
from .transport import BrowserIMTransport, BrowserWebSocket
from .ws_watchdog import WsDisconnectWatcher
from .auto_slider import AutoSlider
from .auto_login import BrowserAutoLogin
from .account_state import AccountStateKeeper


class AccountRuntime:
    """一个账号的浏览器运行时（会话 / 传输 / 看门狗 / 滑块 / 登录 / 状态机）。"""

    __slots__ = ("cookie_id", "session", "transport", "watchdog", "slider",
                 "state", "username", "password", "ready")

    def __init__(self, cookie_id: str) -> None:
        self.cookie_id = cookie_id
        self.session: Optional[BrowserIMSession] = None
        self.transport: Optional[BrowserIMTransport] = None
        self.watchdog: Optional[WsDisconnectWatcher] = None
        self.slider: Optional[AutoSlider] = None
        self.state: Optional[AccountStateKeeper] = None
        self.username: str = ""
        self.password: str = ""
        self.ready = False

    async def start(
        self,
        cookies_str: str = "",
        *,
        headless: bool = True,
        proxy: Optional[Dict[str, Any]] = None,
        slider_options: Optional[Dict[str, Any]] = None,
        state_options: Optional[Dict[str, Any]] = None,
        prefer_persistent: bool = True,
        executable_path: Optional[str] = None,
        username: str = "",
        password: str = "",
    ) -> None:
        if self.ready:
            return

        self.username = (username or "").strip()
        self.password = password or ""
        self.state = AccountStateKeeper(self.cookie_id, **(state_options or {}))

        # ---------------------------------------------------------------
        # 优先复用项目已有的账号级常驻浏览器（utils/persistent_browser.py）。
        # 它在服务器上已被 cookie 刷新链路使用；再为同一账号开第二个 Chromium
        # 既浪费内存（容器常只有 2G），又会让两个浏览器抢同一份登录态。
        # ---------------------------------------------------------------
        attach = None
        if prefer_persistent:
            attach = await self._try_attach_persistent(cookies_str, headless)

        self.session = BrowserIMSession(
            self.cookie_id,
            cookies_str=cookies_str,
            headless=headless,
            proxy=proxy,
            attach_context=attach,
            executable_path=executable_path,
        )
        await self.session.start()
        self.transport = BrowserIMTransport(self.session)
        await self.transport.start()
        self.watchdog = WsDisconnectWatcher(self.session, self.transport)
        self.watchdog.start()
        self.slider = AutoSlider(self.session, **(slider_options or {}))
        self.ready = True
        logger.info(
            f"【{self.cookie_id}】浏览器 IM 运行时就绪"
            f"（含页内自动滑块/自动登录，复用常驻浏览器={attach is not None}）"
        )

    def has_credentials(self) -> bool:
        return bool(self.username and self.password)

    async def auto_login(self) -> Dict[str, Any]:
        """执行四步自动登录并更新状态机。"""
        if self.session is None or self.state is None:
            return {"ok": False, "reason": "runtime_not_ready"}

        if not self.has_credentials():
            self.state.mark("need_qr", "missing_credentials")
            return {"ok": False, "reason": "missing_credentials", "state": self.state.state}

        if not self.state.can_attempt_login():
            return {
                "ok": False,
                "reason": "backoff",
                "state": self.state.state,
                "stats": self.state.stats(),
            }

        login = BrowserAutoLogin(
            self.session, self.username, self.password, slider=self.slider
        )
        result = await login.login()

        if result.get("ok"):
            self.state.mark_logged_in(result.get("reason") or "ok")
            try:
                await self.session.apply_cookies(
                    result.get("cookies") or "", reload_if_changed=False
                )
            except Exception:
                pass
            if self.watchdog is not None:
                self.watchdog.on_reconnected()
        else:
            reason = str(result.get("reason") or "unknown")
            self.state.mark_from_login_reason(reason)
            if self.state.state == "verify_required":
                self.state.record_verify_failure()
            else:
                self.state.record_login_failure(reason)

        out = dict(result)
        out["state"] = self.state.state
        out["stats"] = self.state.stats()
        return out

    def state_stats(self) -> Dict[str, Any]:
        return self.state.stats() if self.state is not None else {}

    async def _try_attach_persistent(self, cookies_str: str, headless: bool):
        """尝试拿到账号级常驻 context；拿不到返回 None。"""
        try:
            from utils.persistent_browser import persistent_browsers
        except Exception:
            return None
        try:
            ctx = await persistent_browsers.get_context(
                self.cookie_id,
                headless=headless,
                initial_cookies=cookies_str or None,
            )
            if ctx is not None:
                logger.info(f"【{self.cookie_id}】复用常驻浏览器上下文（不新开 Chromium）")
            return ctx
        except Exception as exc:
            logger.warning(f"【{self.cookie_id}】获取常驻浏览器失败，将自建: {exc}")
            return None

    async def stop(self) -> None:
        if self.watchdog is not None:
            await self.watchdog.stop()
        if self.transport is not None:
            await self.transport.stop()
        if self.session is not None:
            await self.session.close()
        self.watchdog = None
        self.transport = None
        self.session = None
        self.slider = None
        self.state = None
        self.ready = False
        logger.info(f"【{self.cookie_id}】浏览器 IM 运行时已停止")

    def make_websocket(self, name: str = "primary", predicate=None, shared: bool = True) -> BrowserWebSocket:
        if self.transport is None:
            raise BrowserSessionError(f"【{self.cookie_id}】浏览器运行时未启动")
        return BrowserWebSocket(self.transport, name=name, predicate=predicate, shared=shared)

    def stats(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"cookie_id": self.cookie_id, "ready": self.ready}
        if self.session is not None:
            out["session"] = self.session.stats()
        if self.transport is not None:
            out["transport"] = self.transport.stats()
        if self.watchdog is not None:
            out["watchdog"] = self.watchdog.stats()
        if self.state is not None:
            out["account"] = self.state.stats()
        out["has_credentials"] = self.has_credentials()
        return out


class BrowserIMRuntimeManager:
    """进程内注册表：cookie_id -> AccountRuntime。"""

    def __init__(self) -> None:
        self._items: Dict[str, AccountRuntime] = {}
        self._lock = asyncio.Lock()

    async def get_or_start(
        self,
        cookie_id: str,
        cookies_str: str = "",
        *,
        headless: bool = True,
        proxy: Optional[Dict[str, Any]] = None,
        slider_options: Optional[Dict[str, Any]] = None,
        state_options: Optional[Dict[str, Any]] = None,
        prefer_persistent: bool = True,
        executable_path: Optional[str] = None,
        username: str = "",
        password: str = "",
    ) -> AccountRuntime:
        key = str(cookie_id)
        async with self._lock:
            rt = self._items.get(key)
            if rt is not None and rt.ready:
                return rt
            rt = AccountRuntime(key)
            await rt.start(
                cookies_str=cookies_str,
                headless=headless,
                proxy=proxy,
                slider_options=slider_options,
                state_options=state_options,
                prefer_persistent=prefer_persistent,
                executable_path=executable_path,
                username=username,
                password=password,
            )
            self._items[key] = rt
            return rt

    def get(self, cookie_id: str) -> Optional[AccountRuntime]:
        return self._items.get(str(cookie_id))

    async def stop(self, cookie_id: str) -> None:
        key = str(cookie_id)
        rt = self._items.pop(key, None)
        if rt is not None:
            await rt.stop()

    async def stop_all(self) -> None:
        for key in list(self._items.keys()):
            await self.stop(key)

    def all_stats(self) -> Dict[str, Any]:
        return {k: v.stats() for k, v in self._items.items()}


runtime_manager = BrowserIMRuntimeManager()
