"""浏览器 IM WebSocket 传输层。

目标：让 `XianyuAutoAsync.main()` 里这一行

    async with await self._create_websocket_connection(headers) as websocket:

换成"走浏览器"的实现，而**其余一万八千行业务代码完全不用改**。

逆向依据
--------
鱼店长 Pro 从不自己连 `wss://wss-goofish.dingtalk.com/`。它只在页面里挂
回调（`JsWs.Message(string msg)`），并且**复用同一条页面 WS** 发所有消息。
本项目原有的 `send_msg_once` / `send_delivery_steps_once` 每次新建一条 WSS，
这正是风控最容易命中的行为，因此这里也一并收敛成复用同一条连接。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Callable, Dict, List, Optional

from loguru import logger

from .browser_session import BrowserIMSession


class Subscription:
    """一条消息订阅（对应原代码里 `async for message in websocket` 的消费方）。"""

    __slots__ = ("queue", "predicate", "name", "_closed")

    def __init__(
        self,
        name: str = "sub",
        predicate: Optional[Callable[[Dict[str, Any]], bool]] = None,
        maxsize: int = 4000,
    ) -> None:
        self.name = name
        self.predicate = predicate
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._closed = False

    def offer(self, event: Dict[str, Any]) -> None:
        if self._closed:
            return
        if self.predicate is not None:
            try:
                if not self.predicate(event):
                    return
            except Exception:
                return
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            try:
                self.queue.get_nowait()
                self.queue.put_nowait(event)
            except Exception:
                pass

    def close(self) -> None:
        self._closed = True

    def __aiter__(self) -> "Subscription":
        return self

    async def __anext__(self) -> str:
        if self._closed:
            raise StopAsyncIteration
        event = await self.queue.get()
        if event.get("__close__"):
            raise StopAsyncIteration
        return event.get("data") or ""


class BrowserIMTransport:
    """绑定到一个账号的浏览器 IM 通道（进程内单例）。

    * `send()` 直接把 LWP JSON 经页面 WS 发出；
    * `subscribe()` 拿到消息流；
    * `closed` 跟随页面 IM WS 的真实状态；
    * 断线不会抛异常给业务层，由 `WsDisconnectWatcher` 负责自愈。
    """

    def __init__(self, session: BrowserIMSession) -> None:
        self.session = session
        self.cookie_id = session.cookie_id
        self._subs: List[Subscription] = []
        self._pump_task: Optional[asyncio.Task] = None
        self._closed_local = False
        self._last_message_ts: float = 0.0
        self._message_count: int = 0
        self._last_state: int = -1
        self._open_waiters: List[asyncio.Future] = []
        self._registered = False

    # -------------------------------------------------------------- 生命周期

    async def start(self) -> None:
        if self._pump_task and not self._pump_task.done():
            return
        self._pump_task = asyncio.create_task(self._pump_loop())
        logger.info(f"【{self.cookie_id}】浏览器 IM 传输层已启动")

    async def stop(self) -> None:
        self._closed_local = True
        for sub in list(self._subs):
            sub.close()
            sub.offer({"__close__": True})
        self._subs.clear()
        if self._pump_task and not self._pump_task.done():
            self._pump_task.cancel()
            try:
                await self._pump_task
            except (asyncio.CancelledError, Exception):
                pass
        self._pump_task = None

    # -------------------------------------------------------------- 事件泵

    async def _pump_loop(self) -> None:
        while not self._closed_local:
            try:
                event = await self.session.next_event(timeout=1.0)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug(f"【{self.cookie_id}】取事件异常: {exc}")
                await asyncio.sleep(0.5)
                continue

            if event is None:
                # 顺带刷新一次连接状态，供 watchdog/业务层读取
                continue

            etype = event.get("type")
            if etype == "open":
                self._last_state = 1
                self._registered = True
                self._notify_open()
                logger.info(f"【{self.cookie_id}】[浏览器Ws] IM WebSocket 打开 ({event.get('url')})")
            elif etype == "message":
                if event.get("binary"):
                    continue
                self._last_message_ts = time.time()
                self._message_count += 1
                body = event.get("data") or ""
                for sub in list(self._subs):
                    sub.offer({"data": body, "ts": event.get("ts")})
            elif etype == "close":
                self._last_state = 3
                self._registered = False
                logger.warning(
                    f"【{self.cookie_id}】[浏览器Ws] IM WebSocket 关闭 "
                    f"code={event.get('code')} reason={event.get('reason')}"
                )
                _ = self.session.on_im_ws_closed  # 由外部 watcher 处理
            elif etype == "error":
                logger.warning(f"【{self.cookie_id}】[浏览器Ws] IM WebSocket 错误")

    def _notify_open(self) -> None:
        for fut in self._open_waiters:
            if not fut.done():
                fut.set_result(True)
        self._open_waiters.clear()

    # -------------------------------------------------------------- 订阅/发送

    def subscribe(self, name: str = "sub", predicate=None) -> Subscription:
        sub = Subscription(name=name, predicate=predicate)
        self._subs.append(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        try:
            self._subs.remove(sub)
        except ValueError:
            pass
        sub.close()

    @property
    def closed(self) -> bool:
        """跟随页面 IM WS 实际状态。"""
        if self._closed_local:
            return True
        return self._last_state not in (0, 1)

    async def wait_open(self, timeout: float = 30.0) -> bool:
        try:
            state = await self.session.im_ws_state()
            if state.get("readyState") == 1:
                return True
        except Exception:
            pass
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._open_waiters.append(fut)
        try:
            return bool(await asyncio.wait_for(fut, timeout=timeout))
        except asyncio.TimeoutError:
            return False

    async def send(self, data: Any) -> None:
        """发送 LWP JSON。串行化避免并发写同一 JS 上下文。"""
        text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
        ok = await self.session.send_im_raw(text)
        if not ok:
            logger.warning(f"【{self.cookie_id}】[浏览器Ws] 发送失败：IM WS 未就绪")
            raise ConnectionError("浏览器 IM WebSocket 未就绪，发送失败")

    # -------------------------------------------------------------- 诊断

    def stats(self) -> Dict[str, Any]:
        now = time.time()
        return {
            "cookie_id": self.cookie_id,
            "state": self._last_state,
            "subs": len(self._subs),
            "message_count": self._message_count,
            "last_message_age": (now - self._last_message_ts) if self._last_message_ts else None,
            "registered": self._registered,
        }


class BrowserWebSocket:
    """`websockets` 连接的鸭子类型替身。

    支持原代码用到的全部接口：
        ``async with`` / ``.send()`` / ``.closed`` / ``async for`` / ``.close()``
    """

    def __init__(
        self,
        transport: BrowserIMTransport,
        *,
        name: str = "primary",
        predicate=None,
        shared: bool = True,
    ) -> None:
        self.transport = transport
        self.name = name
        self._shared = shared
        self._sub: Optional[Subscription] = None
        self._predicate = predicate
        self._closed = False
        self._entered = 0

    # 上下文管理：共享模式下不真正关闭底层通道
    async def __aenter__(self) -> "BrowserWebSocket":
        self._entered += 1
        if self._sub is None:
            self._sub = self.transport.subscribe(name=self.name, predicate=self._predicate)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        self._entered = max(0, self._entered - 1)
        if self._sub is not None:
            self.transport.unsubscribe(self._sub)
            self._sub = None
        if not self._shared:
            self._closed = True
        return False

    @property
    def closed(self) -> bool:
        if self._closed:
            return True
        return self.transport.closed

    async def send(self, data: Any) -> None:
        await self.transport.send(data)

    def close(self) -> None:
        self._closed = True

    async def wait_closed(self) -> None:
        return None

    def __aiter__(self) -> "BrowserWebSocket":
        return self

    async def __anext__(self) -> str:
        if self._sub is None:
            self._sub = self.transport.subscribe(name=self.name, predicate=self._predicate)
        try:
            return await self._sub.__anext__()
        except StopAsyncIteration:
            raise
