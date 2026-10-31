"""IM 连接看门狗：`WsDisconnectWatcher` + `KefuRefreshManager` 等价实现。

逆向证据（`D:\\Program Files\\鱼店长Pro\\Log\\*\\*.log`）：

    [JsWs#1] IM的 WebSoket 关闭
    [WsDisconnectWatcher#1] 检测到断开事件，开始60秒计时
    [JsWs#1] IM的 WebSoket 打开
    [WsDisconnectWatcher#1] 检测到 Open 事件，取消刷新计时
    ...
    [KefuRefreshManager] 定时刷新已启动，间隔=60分钟
    重新加载客服聊天窗口(定时刷新)

即：**不靠客户端心跳包硬撑**，而是

    断开 -> 给 60 秒自愈窗口（页面 JS 自己重连）-> 仍未恢复则整页刷新
    另外每 60 分钟主动整页刷新一次，换新会话

本项目原本用 15 秒一次 `/!` 心跳去维持连接，并在"只有心跳没有业务包"时
强行重连，这在平台看来是典型非浏览器客户端行为，因此频繁弹验证码。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, Optional

from loguru import logger

from .browser_session import BrowserIMSession
from .transport import BrowserIMTransport


class WsDisconnectWatcher:
    """单账号看门狗。

    参数默认值全部取自鱼店长 Pro 运行时日志。
    """

    def __init__(
        self,
        session: BrowserIMSession,
        transport: BrowserIMTransport,
        *,
        disconnect_grace_seconds: float = 60.0,
        periodic_refresh_seconds: float = 3600.0,
        silence_refresh_seconds: float = 900.0,
        reload_cooldown_seconds: float = 90.0,
        max_reload_per_hour: int = 8,
    ) -> None:
        self.session = session
        self.transport = transport
        self.cookie_id = session.cookie_id

        self.disconnect_grace_seconds = disconnect_grace_seconds
        self.periodic_refresh_seconds = periodic_refresh_seconds
        self.silence_refresh_seconds = silence_refresh_seconds
        self.reload_cooldown_seconds = reload_cooldown_seconds
        self.max_reload_per_hour = max_reload_per_hour

        # 断开计时
        self._disconnected_at: Optional[float] = None
        self._reconnect_timer: Optional[asyncio.Task] = None
        # 最近一次 open 时间
        self._last_open_at: float = 0.0
        self._last_reload_at: float = 0.0
        self._reload_history: list = []

        self._task: Optional[asyncio.Task] = None
        self._running = False

    # ------------------------------------------------------------------ 状态

    @property
    def is_disconnected(self) -> bool:
        return self._disconnected_at is not None

    def on_disconnected(self, reason: str = "") -> None:
        """对应 `WsDisconnectWatcher.OnDisconnected(logger)`：开始 60 秒计时。"""
        if self._disconnected_at is not None:
            return
        self._disconnected_at = time.time()
        logger.warning(
            f"【{self.cookie_id}】[看门狗] 检测到断开事件，开始"
            f"{int(self.disconnect_grace_seconds)}秒计时"
            + (f"（{reason}）" if reason else "")
        )
        if self._reconnect_timer and not self._reconnect_timer.done():
            self._reconnect_timer.cancel()
        self._reconnect_timer = asyncio.create_task(self._grace_countdown())

    def on_reconnected(self) -> None:
        """对应 `WsDisconnectWatcher.OnReconnected()`：取消刷新计时。"""
        if self._disconnected_at is None:
            return
        self._disconnected_at = None
        self._last_open_at = time.time()
        if self._reconnect_timer and not self._reconnect_timer.done():
            self._reconnect_timer.cancel()
        self._reconnect_timer = None
        logger.info(f"【{self.cookie_id}】[看门狗] 检测到 Open 事件，取消刷新计时")

    async def _grace_countdown(self) -> None:
        try:
            await asyncio.sleep(self.disconnect_grace_seconds)
        except asyncio.CancelledError:
            return
        if self._disconnected_at is None:
            return
        # 超时仍未恢复 -> 整页刷新
        await self._reload("断开超过 %ds 未自愈" % int(self.disconnect_grace_seconds))

    # ------------------------------------------------------------------ 刷新

    async def _reload(self, reason: str) -> bool:
        now = time.time()
        if now - self._last_reload_at < self.reload_cooldown_seconds:
            logger.info(
                f"【{self.cookie_id}】[看门狗] 刷新被冷却抑制（距上次 "
                f"{int(now - self._last_reload_at)}s < {int(self.reload_cooldown_seconds)}s）"
            )
            return False

        # 一小时窗口内限流，避免刷新风暴
        self._reload_history = [t for t in self._reload_history if now - t < 3600]
        if len(self._reload_history) >= self.max_reload_per_hour:
            logger.warning(
                f"【{self.cookie_id}】[看门狗] 一小时内刷新已达上限 "
                f"{self.max_reload_per_hour} 次，本次跳过"
            )
            return False

        self._last_reload_at = now
        self._reload_history.append(now)

        ok = await self.session.reload_page(reason=reason)
        if ok:
            self._disconnected_at = None
            self._last_open_at = time.time()
            self.transport._last_state = 1
        return ok

    # ------------------------------------------------------------------ 主循环

    async def _loop(self) -> None:
        logger.info(
            f"【{self.cookie_id}】[看门狗] 已启动 "
            f"(断开宽限={int(self.disconnect_grace_seconds)}s, "
            f"周期刷新={int(self.periodic_refresh_seconds)}s, "
            f"静默阈值={int(self.silence_refresh_seconds)}s)"
        )
        last_periodic = time.time()
        while self._running:
            try:
                await asyncio.sleep(5)
                now = time.time()

                # 1) 周期整页刷新（KefuRefreshManager 语义）
                if self.periodic_refresh_seconds > 0 and now - last_periodic >= self.periodic_refresh_seconds:
                    last_periodic = now
                    logger.info(f"【{self.cookie_id}】[看门狗] 触发周期刷新（客服页定时重载）")
                    await self._reload("周期刷新")

                # 2) 页面 WS 静默检测：连接在但长时间没有任何包
                stats = self.transport.stats()
                age = stats.get("last_message_age")
                if (
                    stats.get("state") == 1
                    and age is not None
                    and age > self.silence_refresh_seconds
                ):
                    logger.warning(
                        f"【{self.cookie_id}】[看门狗] IM 通道静默 {int(age)}s，触发刷新"
                    )
                    await self._reload("静默超时")
                    continue

                # 3) 主动核对页面真实状态，纠正本地状态漂移
                try:
                    state = await self.session.im_ws_state()
                    rs = state.get("readyState", -1)
                    if rs == 1:
                        self.transport._last_state = 1
                        if self._disconnected_at is not None:
                            self.on_reconnected()
                    elif rs in (3, -1):
                        self.transport._last_state = 3
                        self.on_disconnected(f"页面 ws readyState={rs}")
                except Exception as exc:
                    logger.debug(f"【{self.cookie_id}】[看门狗] 探测页面状态失败: {exc}")

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(f"【{self.cookie_id}】[看门狗] 循环异常: {exc}")
                await asyncio.sleep(5)

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._running = False
        if self._reconnect_timer and not self._reconnect_timer.done():
            self._reconnect_timer.cancel()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None

    def stats(self) -> Dict[str, Any]:
        now = time.time()
        return {
            "cookie_id": self.cookie_id,
            "running": self._running,
            "disconnected": self.is_disconnected,
            "disconnected_for": (now - self._disconnected_at) if self._disconnected_at else 0.0,
            "last_open_age": (now - self._last_open_at) if self._last_open_at else None,
            "reload_count_1h": len([t for t in self._reload_history if now - t < 3600]),
            "last_reload_age": (now - self._last_reload_at) if self._last_reload_at else None,
        }
