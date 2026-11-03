# -*- coding: utf-8 -*-
"""AI 配置解析：把平台（云端）AI 配置合进账号级设置。

背景
----
`ai_reply_settings` 是**按账号**存的，`api_key` 也在里面。桌面客户端把密钥改成
云端下发之后，账号级那一行就没有 key 了，`ai_reply_engine.generate_reply()`
直接拿 `settings['api_key']` 去请求，必然 401 / 报错。

这里是唯一的合并入口：
    环境变量 PLATFORM_AI_API_KEY
      -> 云端 /api/cloud/ai-config（只进内存，TTL 5 分钟）
        -> 本地 system_settings.platform_ai_api_key
          -> 账号级 settings 里的 api_key（最后兜底）

平台侧给了 key 就以平台为准（这也是"只有管理员能设 key"的本意），
模型名/地址/接口类型同理只在平台有值时才覆盖。
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any, Dict

from loguru import logger

_CACHE: Dict[str, Any] = {'value': None, 'fetched_at': 0.0}
_CACHE_TTL = 300.0
_CACHE_LOCK = threading.Lock()


def _read_cloud_config() -> Dict[str, Any]:
    """从云端拉平台 AI 配置，只放内存。失败返回 {}。"""
    now = time.time()
    with _CACHE_LOCK:
        cached = _CACHE.get('value')
        if cached and (now - float(_CACHE.get('fetched_at') or 0)) < _CACHE_TTL:
            return dict(cached)

    url = str(os.getenv('CLOUD_AI_CONFIG_URL', '') or '').strip()
    token = str(os.getenv('CLOUD_AI_CONFIG_TOKEN', '') or '').strip()
    if not url:
        try:
            from config import CLOUD_AI as _ca
            url = str(_ca.get('url') or '').strip()
            token = token or str(_ca.get('token') or '').strip()
        except Exception:
            url = ''
    if not url:
        return {}

    try:
        import httpx
        from urllib.parse import quote as _quote
        req_url = url
        if token:
            sep = '&' if '?' in url else '?'
            req_url = f"{url}{sep}token={_quote(token, safe='')}"
        with httpx.Client(timeout=8.0, follow_redirects=True) as client:
            resp = client.get(req_url, headers={'Authorization': f'Bearer {token}'} if token else {})
        if resp.status_code != 200:
            logger.warning(f"云端 AI 配置拉取失败: HTTP {resp.status_code}")
            return {}
        data = resp.json() or {}
        key = str(data.get('api_key') or '').strip()
        if not key:
            return {}
        payload = {
            'api_key': key,
            'model_name': str(data.get('model_name') or '').strip(),
            'base_url': str(data.get('base_url') or '').strip().rstrip('/'),
            'api_type': str(data.get('api_type') or '').strip(),
        }
        with _CACHE_LOCK:
            _CACHE['value'] = payload
            _CACHE['fetched_at'] = now
        logger.info("已从云端获取平台 AI 配置（未落盘）")
        return dict(payload)
    except Exception as exc:
        logger.warning(f"云端 AI 配置拉取异常: {exc}")
        return {}


def _read_local_platform_config() -> Dict[str, Any]:
    """本地库里的平台配置（服务器部署场景 / 离线兜底）。"""
    result: Dict[str, Any] = {}
    try:
        env_key = str(os.getenv('PLATFORM_AI_API_KEY', '') or '').strip()
        if env_key:
            result['api_key'] = env_key
        from db_manager import db_manager
        if not result.get('api_key'):
            result['api_key'] = str(db_manager.get_system_setting('platform_ai_api_key') or '').strip()
        result['model_name'] = str(db_manager.get_system_setting('platform_ai_model_name') or '').strip()
        result['base_url'] = str(db_manager.get_system_setting('platform_ai_base_url') or '').strip().rstrip('/')
        result['api_type'] = str(db_manager.get_system_setting('platform_ai_api_type') or '').strip()
    except Exception as exc:
        logger.debug(f"读取本地平台 AI 配置失败: {exc}")
    return result


def resolve_ai_settings(settings: Dict[str, Any]) -> Dict[str, Any]:
    """把平台配置合并进账号级设置，返回可直接用于请求的 dict。"""
    merged: Dict[str, Any] = dict(settings or {})

    platform: Dict[str, Any] = {}
    env_key = str(os.getenv('PLATFORM_AI_API_KEY', '') or '').strip()
    if env_key:
        platform['api_key'] = env_key
    else:
        platform = _read_cloud_config()
        if not platform.get('api_key'):
            platform = {k: v for k, v in _read_local_platform_config().items() if v}

    if platform.get('api_key'):
        merged['api_key'] = platform['api_key']
    for field in ('model_name', 'base_url', 'api_type'):
        if platform.get(field):
            merged[field] = platform[field]

    if not merged.get('api_key'):
        logger.warning("AI 配置缺少 api_key：账号级与平台级都没有可用密钥")
    return merged


def reset_cache() -> None:
    with _CACHE_LOCK:
        _CACHE['value'] = None
        _CACHE['fetched_at'] = 0.0
