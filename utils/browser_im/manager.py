"""按账号管理浏览器 IM 会话（等价 `AgisoIdleClient` 的 per-account CefSharp 实例池）。"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

from loguru import logger

from .browser_session import BrowserIMSession, BrowserSessionError
from .transport import BrowserIMTransport, BrowserWebSocket
from .ws_watchdog import WsDisconnectWatcher
from .auto_slider import AutoSlider


class AccountRuntime:
    """一个账号的浏览器运行时四件套。"""

    __slots__ = ("cookie_id", "session", "transport", "watchdog", "slider", "ready")

    def __init__(self, cookie_id: str) -> None:
        self.cookie_id = cookie_id
        self.session: Optional[BrowserIMSession] = None
        self.transport: Optional[BrowserIMTransport] = None
        self.watchdog: Optional[WsDisconnectWatcher] = None
        self.slider: Optional[AutoSlider] = None
        self.ready = False

    async def start(
        self,
        cookies_str: str = "",
        *,
        headless: bool = True,
        proxy: Optional[Dict[str, Any]] = None,
        slider_options: Optional[Dict[str, Any]] = None,
    ) -> None:
        if self.ready:
            return
        self.session = BrowserIMSession(
            self.cookie_id,
            cookies_str=cookies_str,
            headless=headless,
            proxy=proxy,
        )
        await self.session.start()
        self.transport = BrowserIMTransport(self.session)
        await self.transport.start()
        self.watchdog = WsDisconnectWatcher(self.session, self.transport)
        self.watchdog.start()
        self.slider = AutoSlider(self.session, **(slider_options or {}))
        self.ready = True
        logger.info(f"【{self.cookie_id}】浏览器 IM 运行时就绪（含页内自动滑块）")

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
