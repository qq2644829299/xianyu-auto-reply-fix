import queue
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from loguru import logger


_BEIJING_TZ = timezone(timedelta(hours=8))


def _now_beijing_str() -> str:
    """返回北京时间字符串（与 DB 出口转换后的格式保持一致：YYYY-MM-DD HH:MM:SS）。"""
    return datetime.now(_BEIJING_TZ).strftime('%Y-%m-%d %H:%M:%S')


class ChatEventHub:
    """进程内聊天事件中心，按 user_id 广播聊天消息。"""

    def __init__(self):
        self._lock = threading.RLock()
        self._subscribers = defaultdict(set)

    def subscribe(self, user_id: int, maxsize: int = 200):
        subscriber = queue.Queue(maxsize=maxsize)
        with self._lock:
            self._subscribers[user_id].add(subscriber)
        return subscriber

    def unsubscribe(self, user_id: int, subscriber):
        with self._lock:
            subscribers = self._subscribers.get(user_id)
            if not subscribers:
                return
            subscribers.discard(subscriber)
            if not subscribers:
                self._subscribers.pop(user_id, None)

    def publish(self, user_id: int, event: Dict[str, Any]):
        with self._lock:
            subscribers = list(self._subscribers.get(user_id, set()))

        for subscriber in subscribers:
            try:
                subscriber.put_nowait(event)
            except queue.Full:
                try:
                    subscriber.get_nowait()
                except queue.Empty:
                    pass

                try:
                    subscriber.put_nowait(event)
                except queue.Full:
                    logger.warning(f"聊天事件队列仍然已满，丢弃事件: user_id={user_id}")


chat_event_hub = ChatEventHub()


class SelfSendDedup:
    """Web 自发消息去重标记。

    Why: /api/chat/send 在调 send_msg 成功后会立刻落库+publish；但闲鱼服务器
    随后可能把同一条消息以"自己发出"的形式经 WebSocket 回推，触发
    XianyuAutoAsync.handle_message 再落库+publish 一次，导致前端看到两条。
    How to apply: Web 路径 mark()，handle_message 的"手动发出"分支调 consume()，
    命中则跳过本次落库/publish。键采用 (cookie_id, chat_id, sender_id, content)，
    TTL 默认 60s 足以覆盖闲鱼回推延迟。
    """

    def __init__(self, ttl_seconds: float = 60.0):
        self._lock = threading.RLock()
        self._marks: Dict[tuple, float] = {}
        self._ttl = ttl_seconds

    def _gc_locked(self, now: float) -> None:
        expired = [k for k, ts in self._marks.items() if now - ts > self._ttl]
        for k in expired:
            self._marks.pop(k, None)

    @staticmethod
    def _key(cookie_id: str, chat_id: str, sender_id: str, content: str) -> tuple:
        return (str(cookie_id), str(chat_id), str(sender_id), str(content or ''))

    def mark(self, cookie_id: str, chat_id: str, sender_id: str, content: str) -> None:
        now = time.time()
        with self._lock:
            self._gc_locked(now)
            self._marks[self._key(cookie_id, chat_id, sender_id, content)] = now

    def consume(self, cookie_id: str, chat_id: str, sender_id: str, content: str) -> bool:
        """命中返回 True 并消费掉该标记；未命中返回 False。"""
        now = time.time()
        key = self._key(cookie_id, chat_id, sender_id, content)
        with self._lock:
            self._gc_locked(now)
            ts = self._marks.get(key)
            if ts is None:
                return False
            self._marks.pop(key, None)
            return True


self_send_dedup = SelfSendDedup()


def publish_chat_message(cookie_id: str, message_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """发布聊天消息事件到对应的系统用户。"""
    from db_manager import db_manager

    cookie_info = db_manager.get_cookie_details(cookie_id)
    user_id = cookie_info.get('user_id') if cookie_info else None
    if user_id is None:
        return None

    # 兜底补上 created_at（北京时间），避免前端 SSE 落回 toISOString 显示 UTC
    if not message_data.get('created_at'):
        message_data['created_at'] = _now_beijing_str()

    event = {
        'type': 'chat.message',
        'timestamp': int(time.time() * 1000),
        'cookie_id': cookie_id,
        'data': message_data,
    }
    chat_event_hub.publish(user_id, event)
    return event
