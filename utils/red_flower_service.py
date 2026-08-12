"""闲鱼求小红花服务。

参考上游 red_flower_task 的核心请求逻辑，适配当前单体项目：
- 调用 mtop.taobao.idlemessage.red.flower；
- 令牌过期时合并响应 Set-Cookie、保存 Cookie 后重试一次；
- 返回统一结果供实时接口、手动接口和后台补偿任务复用。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Any, Dict, Tuple

import aiohttp
from loguru import logger


APP_KEY = "34839810"
RED_FLOWER_API_URL = "https://h5api.m.goofish.com/h5/mtop.taobao.idlemessage.red.flower/1.0/"
RED_FLOWER_API_NAME = "mtop.taobao.idlemessage.red.flower"


class RedFlowerService:
    """闲鱼求小红花服务。"""

    def __init__(self, cookie_string: str, account_id: str | None = None):
        self.cookie_string = str(cookie_string or "").strip()
        self.account_id = account_id
        self.cookies_dict = self._parse_cookies(self.cookie_string)

    @staticmethod
    def _parse_cookies(cookies_str: str) -> Dict[str, str]:
        cookies: Dict[str, str] = {}
        for part in str(cookies_str or "").replace("\ufeff", "").split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            key, value = part.split("=", 1)
            key = key.strip()
            if key:
                cookies[key] = value.strip()
        return cookies

    @staticmethod
    def _cookie_dict_to_string(cookies: Dict[str, str]) -> str:
        return "; ".join(f"{key}={value}" for key, value in cookies.items())

    @staticmethod
    def _generate_sign(t: str, token: str, data: str) -> str:
        msg = f"{token}&{t}&{APP_KEY}&{data}"
        md5_hash = hashlib.md5()
        md5_hash.update(msg.encode("utf-8"))
        return md5_hash.hexdigest()

    @staticmethod
    def _ret_to_text(ret: Any) -> str:
        if isinstance(ret, list):
            return "; ".join(str(item) for item in ret)
        return str(ret or "")

    @classmethod
    def _is_token_expired(cls, ret: Any) -> bool:
        ret_text = cls._ret_to_text(ret)
        return "FAIL_SYS_TOKEN_EXOIRED" in ret_text or "令牌过期" in ret_text

    @classmethod
    def _is_session_expired(cls, ret: Any) -> bool:
        ret_text = cls._ret_to_text(ret)
        return "FAIL_SYS_SESSION_EXPIRED" in ret_text or "Session过期" in ret_text

    @classmethod
    def _is_already_requested(cls, ret: Any, result: Any) -> bool:
        text = cls._ret_to_text(ret) + " " + json.dumps(result, ensure_ascii=False, default=str)
        return any(keyword in text for keyword in (
            "已送出小红花", "已收下", "已求过", "已赠送", "已经送", "重复", "不能重复",
        ))

    @staticmethod
    def _extract_set_cookies(response: aiohttp.ClientResponse) -> Dict[str, str]:
        new_cookies: Dict[str, str] = {}
        try:
            for cookie_header in response.headers.getall("set-cookie", []):
                first_part = cookie_header.split(";", 1)[0]
                if "=" not in first_part:
                    continue
                name, value = first_part.split("=", 1)
                name = name.strip()
                if name:
                    new_cookies[name] = value.strip()
        except Exception as exc:
            logger.warning(f"提取求小红花接口 Set-Cookie 失败: {exc}")
        return new_cookies

    def _merge_response_cookies(self, response: aiohttp.ClientResponse) -> Tuple[bool, str]:
        new_cookies = self._extract_set_cookies(response)
        if not new_cookies:
            return False, self.cookie_string
        merged = dict(self.cookies_dict)
        merged.update(new_cookies)
        merged_string = self._cookie_dict_to_string(merged)
        logger.info(
            f"【{self.account_id or '未知账号'}】求小红花接口返回新 Cookie，已合并 {len(new_cookies)} 个字段"
        )
        return True, merged_string

    async def _persist_cookie_if_needed(self, new_cookie_string: str) -> None:
        if not self.account_id or not new_cookie_string or new_cookie_string == self.cookie_string:
            return
        try:
            from db_manager import db_manager

            db_manager.save_cookie(self.account_id, new_cookie_string)
            logger.info(f"【{self.account_id}】求小红花接口刷新后的 Cookie 已保存到数据库")
        except Exception as exc:
            logger.warning(f"【{self.account_id}】保存求小红花刷新 Cookie 失败: {exc}")

    async def request_red_flower(self, order_id: str, is_retry: bool = False) -> Dict[str, Any]:
        """对指定订单发送求小红花请求。"""
        order_id = str(order_id or "").strip()
        if not order_id:
            return {"success": False, "message": "缺少订单号"}
        if not self.cookie_string:
            return {"success": False, "message": "账号 Cookie 为空"}

        m_h5_tk = self.cookies_dict.get("_m_h5_tk", "")
        token = m_h5_tk.split("_", 1)[0] if m_h5_tk else ""
        if not token:
            return {"success": False, "message": "Cookie 中缺少 _m_h5_tk，无法生成求小红花签名"}

        timestamp = str(int(time.time() * 1000))
        data_obj = {
            "orderId": order_id,
            "channel": "list",
        }
        data_val = json.dumps(data_obj, separators=(",", ":"), ensure_ascii=False)
        sign = self._generate_sign(timestamp, token, data_val)

        params = {
            "jsv": "2.7.2",
            "appKey": APP_KEY,
            "t": timestamp,
            "sign": sign,
            "v": "4.0",
            "type": "originaljson",
            "accountSite": "xianyu",
            "dataType": "json",
            "timeout": "20000",
            "api": RED_FLOWER_API_NAME,
            "sessionOption": "AutoLoginOnly",
        }
        headers = {
            "accept": "application/json",
            "content-type": "application/x-www-form-urlencoded",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "referer": "https://www.goofish.com/",
            "origin": "https://www.goofish.com",
            "cookie": self.cookie_string,
        }

        try:
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(RED_FLOWER_API_URL, params=params, headers=headers, data={"data": data_val}) as response:
                    try:
                        result = await response.json(content_type=None)
                    except Exception:
                        body = await response.text()
                        return {
                            "success": False,
                            "message": f"求小红花接口返回非 JSON: HTTP {response.status}",
                            "raw": body[:1000],
                        }

                    # 无论是否成功，先合并响应中的新 Cookie，供后续请求使用。
                    has_cookie_update, merged_cookie_string = self._merge_response_cookies(response)
                    if has_cookie_update:
                        await self._persist_cookie_if_needed(merged_cookie_string)
                        self.cookie_string = merged_cookie_string
                        self.cookies_dict = self._parse_cookies(merged_cookie_string)

                    ret = result.get("ret", []) if isinstance(result, dict) else []
                    ret_text = self._ret_to_text(ret) or str(result)
                    retry_tag = "[令牌过期重试] " if is_retry else ""

                    if ret_text == "SUCCESS::调用成功" or "SUCCESS" in ret_text:
                        logger.info(
                            f"【{self.account_id or '未知账号'}】{retry_tag}求小红花成功: order_id={order_id}"
                        )
                        return {"success": True, "message": "求小红花成功", "raw": result}

                    if self._is_already_requested(ret, result):
                        logger.info(
                            f"【{self.account_id or '未知账号'}】订单已求过小红花，按成功处理: order_id={order_id}, ret={ret}"
                        )
                        return {"success": True, "already_red_flower": True, "message": "订单已求过小红花", "raw": result}

                    if not is_retry and self._is_token_expired(ret):
                        if has_cookie_update:
                            return await self.request_red_flower(order_id, is_retry=True)
                        return {"success": False, "message": "令牌过期且响应未返回新 Cookie", "raw": result}

                    if self._is_session_expired(ret):
                        return {"success": False, "session_expired": True, "message": ret_text, "raw": result}

                    logger.warning(
                        f"【{self.account_id or '未知账号'}】{retry_tag}求小红花失败: order_id={order_id}, ret={ret}"
                    )
                    return {"success": False, "message": ret_text, "raw": result}
        except asyncio.TimeoutError:
            return {"success": False, "message": "求小红花接口请求超时"}
        except Exception as exc:
            logger.error(f"【{self.account_id or '未知账号'}】求小红花异常: order_id={order_id}, error={exc}")
            return {"success": False, "message": str(exc)}
