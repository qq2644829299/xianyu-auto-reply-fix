"""纯 HTTP 拉取闲鱼订单详情（不依赖浏览器）。

为什么要有这个模块
==================
逆向「鱼店长Pro」得到一个关键事实：**它从不打开订单详情页**。
它在全部运行日志里没有任何 `order-detail` / `goto` / 页面导航记录，订单信息
一律来自 IM 推送的卡片消息：

    为商家发送的卡片消息，信息内容：你当前宝贝拍下未付款
    为商家发送的卡片消息，信息内容：我拒绝退款申请
    为商家发送的卡片消息，信息内容：我已发起小法庭维权

本项目则需要更完整的订单详情（金额、SKU、订单状态）才能自动发货，于是
`utils/order_detail_fetcher.py` 用 Playwright 打开
`https://www.goofish.com/order-detail?orderId=...&role=seller` 渲染页面再解析 DOM。
代价很重：

* 每次查询都起一次浏览器上下文，2G 服务器直接被拖死（已经发生过 OOM）；
* 页面结构一变就全挂；
* 多账号并发时内存叠加不可控。

实际上闲鱼有现成的 mtop 接口能直接返回订单 JSON。本项目自己的代码里就能找到
证据：`OrderDetailFetcher._is_trusted_order_detail_response_url()` 把
`mtop.idle.web.trade.order.detail` 列为"可信响应"——说明浏览器抓包时它见过这个
接口，只是从来没主动调用过。

本模块把它改成主动 HTTP 调用：cookie + `_m_h5_tk` + sign 即可，无需浏览器。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

# 接口候选：按优先级依次尝试。都是闲鱼 web 端在用的 mtop 接口。
ORDER_DETAIL_APIS: Tuple[Tuple[str, str], ...] = (
    ("mtop.idle.web.trade.order.detail", "1.0"),
    ("mtop.taobao.idle.trade.order.detail", "1.0"),
    ("mtop.taobao.idle.trade.merchant.sold.get", "1.0"),
)

# 参数名候选：不同接口对订单号的字段名不一样
ORDER_ID_KEYS = ("orderId", "bizOrderId", "tradeOrderId", "order_id")

# appKey 候选：不同 mtop 接口使用不同 appKey，两个都试
APP_KEYS = ("444e9908a51d1cb236a27862abc769c9", "34839810")

DEFAULT_HEADERS = {
    "accept": "application/json",
    "accept-language": "zh-CN,zh;q=0.9",
    "cache-control": "no-cache",
    "origin": "https://www.goofish.com",
    "pragma": "no-cache",
    "referer": "https://www.goofish.com/",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
    ),
}

# 金额字段（与 order_detail_fetcher 的 DOM 解析口径保持一致）
AMOUNT_KEYS = (
    "paidAmount", "actualPay", "payAmount", "realPay", "finalAmount",
    "buyerPayAmount", "dealPrice", "orderAmount", "paymentAmount", "tradeAmount",
)
STATUS_KEYS = ("orderStatus", "orderStatusName", "status", "tradeStatus", "statusText")


def parse_cookies(cookies_str: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for part in str(cookies_str or "").replace("\ufeff", "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        k = k.strip()
        if k:
            out[k] = v.strip()
    return out


def _sign(token: str, t: str, app_key: str, data: str) -> str:
    src = f"{token}&{t}&{app_key}&{data}"
    return hashlib.md5(src.encode("utf-8")).hexdigest()


def _dig_first(payload: Any, keys, depth: int = 0) -> Optional[Any]:
    """在嵌套 dict/list 里找第一个命中的键值。"""
    if depth > 6 or payload is None:
        return None
    if isinstance(payload, dict):
        for k in keys:
            if k in payload and payload[k] not in (None, "", []):
                return payload[k]
        for v in payload.values():
            got = _dig_first(v, keys, depth + 1)
            if got is not None:
                return got
    elif isinstance(payload, list):
        for item in payload:
            got = _dig_first(item, keys, depth + 1)
            if got is not None:
                return got
    return None


class MtopOrderApi:
    """用 mtop HTTP 接口查订单详情。"""

    def __init__(self, cookies_str: str, *, account_id: str = "", timeout: float = 20.0) -> None:
        self.cookies_str = str(cookies_str or "").strip()
        self.cookies = parse_cookies(self.cookies_str)
        self.account_id = account_id or self.cookies.get("unb") or "unknown"
        self.timeout = timeout
        self._last_error: str = ""

    @property
    def last_error(self) -> str:
        return self._last_error

    def _mtop_token(self) -> str:
        full = self.cookies.get("_m_h5_tk", "")
        return full.split("_")[0] if full else ""

    async def _call(
        self,
        session,
        api: str,
        version: str,
        app_key: str,
        data_obj: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        token = self._mtop_token()
        if not token:
            self._last_error = "缺少 _m_h5_tk"
            return None

        data_str = json.dumps(data_obj, separators=(",", ":"), ensure_ascii=False)
        ts = str(int(time.time() * 1000))
        params = {
            "jsv": "2.7.2",
            "appKey": app_key,
            "t": ts,
            "sign": _sign(token, ts, app_key, data_str),
            "api": api,
            "v": version,
            "type": "originaljson",
            "dataType": "json",
            "data": data_str,
        }
        url = f"https://h5api.m.goofish.com/h5/{api}/{version}/"

        try:
            async with session.get(
                url, params=params, headers=DEFAULT_HEADERS, timeout=self.timeout
            ) as resp:
                text = await resp.text()
                if resp.status != 200:
                    self._last_error = f"HTTP {resp.status}"
                    return None
                try:
                    payload = json.loads(text)
                except Exception:
                    self._last_error = f"非 JSON: {text[:120]}"
                    return None
        except Exception as exc:
            self._last_error = f"请求异常: {exc}"
            return None

        ret = payload.get("ret") or []
        ret_text = " ".join(str(x) for x in ret) if isinstance(ret, list) else str(ret)
        if "SUCCESS" not in ret_text.upper():
            self._last_error = ret_text[:160]
            # 令牌过期是常见情况，提示调用方去刷新
            if "TOKEN" in ret_text.upper() or "SESSION" in ret_text.upper():
                self._last_error = f"令牌过期: {ret_text[:120]}"
            return None

        data = payload.get("data")
        return data if isinstance(data, dict) else {"raw": data}

    async def fetch(self, order_id: str) -> Optional[Dict[str, Any]]:
        """查询订单详情，返回标准化字段；失败返回 None。"""
        order_id = str(order_id or "").strip()
        if not order_id:
            self._last_error = "订单号为空"
            return None

        import aiohttp

        try:
            connector = aiohttp.TCPConnector(limit=4)
            async with aiohttp.ClientSession(connector=connector, cookies=self.cookies) as session:
                for api, version in ORDER_DETAIL_APIS:
                    for app_key in APP_KEYS:
                        for id_key in ORDER_ID_KEYS:
                            data = await self._call(
                                session, api, version, app_key, {id_key: order_id}
                            )
                            if not data:
                                continue
                            normalized = self._normalize(data, order_id)
                            if normalized:
                                logger.info(
                                    f"【{self.account_id}】mtop 订单详情查询成功 "
                                    f"({api} / {id_key})"
                                )
                                return normalized
        except Exception as exc:
            self._last_error = f"会话异常: {exc}"
        return None

    def _normalize(self, data: Dict[str, Any], order_id: str) -> Optional[Dict[str, Any]]:
        """把 mtop 返回映射成项目通用的订单字段。"""
        amount = _dig_first(data, AMOUNT_KEYS)
        if amount is None:
            return None  # 金额都拿不到，说明这不是订单详情响应

        # 分单位（整数且较大）转元
        amount_text = str(amount).strip()
        if amount_text.isdigit() and len(amount_text) >= 4:
            try:
                amount_text = f"{int(amount_text) / 100:.2f}"
            except Exception:
                pass

        status = _dig_first(data, STATUS_KEYS)
        item_id = _dig_first(data, ("itemId", "item_id", "idleItemId"))
        buyer_id = _dig_first(data, ("buyerId", "buyerUserId", "buyer_id"))
        title = _dig_first(data, ("itemTitle", "title", "subject"))
        spec_name = _dig_first(data, ("specName", "skuName", "propertyName"))
        spec_value = _dig_first(data, ("specValue", "skuValue", "propertyValue"))

        return {
            "order_id": order_id,
            "amount": amount_text,
            "order_status": str(status or ""),
            "item_id": str(item_id or ""),
            "buyer_id": str(buyer_id or ""),
            "item_title": str(title or ""),
            "spec_name": str(spec_name or ""),
            "spec_value": str(spec_value or ""),
            "source": "mtop_http",
            "raw_keys": sorted(data.keys())[:20],
        }


async def fetch_order_detail_http(
    cookies_str: str, order_id: str, *, account_id: str = ""
) -> Optional[Dict[str, Any]]:
    """便捷函数：纯 HTTP 查订单详情。"""
    api = MtopOrderApi(cookies_str, account_id=account_id)
    return await api.fetch(order_id)
