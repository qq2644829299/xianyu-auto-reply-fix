"""闲鱼业务连接凭证获取。

扫码 / 密码登录取得的是登录 Cookie，不等于 IM WebSocket 可用。这里把后续
token 请求作为单独阶段处理；出现官方安全校验时只保存上下文并等待人工处理。
本模块绝不执行滑块识别、拖动或第三方验证码服务。
"""
from __future__ import annotations

import asyncio
import time
import uuid
import shutil
from urllib.parse import urlparse
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional

from utils.xianyu_utils import generate_device_id, trans_cookies
from utils.xianyu_slider_stealth import probe_cookie_verification_from_cookie


class AcquireStatus(str, Enum):
    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    API_AUTHENTICATED = "API_AUTHENTICATED"
    CREDENTIAL_ACQUIRING = "CREDENTIAL_ACQUIRING"
    VERIFY_REQUIRED = "VERIFY_REQUIRED"
    VERIFYING = "VERIFYING"
    CREDENTIAL_READY = "CREDENTIAL_READY"
    CONNECTING = "CONNECTING"
    ONLINE = "ONLINE"
    LOGIN_EXPIRED = "LOGIN_EXPIRED"
    RETRYABLE_ERROR = "RETRYABLE_ERROR"
    FAILED = "FAILED"


@dataclass
class XianyuConnectionCredential:
    """现有 IM 连接实际使用的认证数据。"""
    user_id: str
    cookie: str
    access_token: str
    device_id: str
    websocket_url: str = "wss://wss-goofish.dingtalk.com/"
    app_key: str = ""


@dataclass
class LoginContext:
    account_id: str
    user_id: int
    cookie: str
    xianyu_user_id: str
    device_id: str
    stage: AcquireStatus = AcquireStatus.API_AUTHENTICATED
    verification_url: Optional[str] = None
    remote_session_id: Optional[str] = None
    remote_control_url: Optional[str] = None
    browser: Any = field(default=None, repr=False, compare=False)
    browser_context: Any = field(default=None, repr=False, compare=False)
    verification_page: Any = field(default=None, repr=False, compare=False)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


@dataclass
class AcquireResult:
    status: AcquireStatus
    credential: Optional[XianyuConnectionCredential] = None
    context: Optional[LoginContext] = None
    message: str = ""


class XianyuCredentialProvider:
    """把 Cookie -> IM accessToken 的真实连接凭证流程统一起来。"""

    def __init__(self):
        self._contexts: Dict[str, LoginContext] = {}
        self._lock = asyncio.Lock()

    def get_context(self, account_id: str) -> Optional[LoginContext]:
        return self._contexts.get(str(account_id))

    def status(self, account_id: str) -> Dict[str, Any]:
        context = self.get_context(account_id)
        if not context:
            return {"status": AcquireStatus.LOGIN_REQUIRED.value}
        return {
            "status": context.stage.value,
            "verification_url": context.verification_url,
            "remote_control_url": context.remote_control_url,
            "updated_at": context.updated_at,
        }

    def mark_verification_required(self, account_id: str, user_id: int, cookie: str,
                                   device_id: str, verification_url: Optional[str]) -> LoginContext:
        """供运行中的连接链路暂停时登记原始上下文，不重新发起 token 请求。"""
        parsed = trans_cookies(cookie or "")
        context = self._contexts.get(str(account_id)) or LoginContext(
            account_id=str(account_id), user_id=int(user_id or 0), cookie=str(cookie or ""),
            xianyu_user_id=str(parsed.get("unb") or ""), device_id=str(device_id or ""),
        )
        context.cookie = str(cookie or context.cookie)
        context.device_id = str(device_id or context.device_id)
        context.stage = AcquireStatus.VERIFY_REQUIRED
        context.verification_url = str(verification_url or "") or None
        context.updated_at = time.time()
        self._contexts[str(account_id)] = context
        return context

    async def start_official_verification(self, context: LoginContext) -> LoginContext:
        """在服务器保留原 Cookie 的浏览器中打开官方页，供用户手工操作。"""
        if context.remote_control_url and context.verification_page:
            return context
        if not context.verification_url:
            return context
        from playwright.async_api import async_playwright
        from utils.captcha_remote_control import captcha_controller

        playwright = await async_playwright().start()
        launch_options = {'headless': True, 'args': ['--no-sandbox', '--disable-dev-shm-usage']}
        # 生产镜像安装的是系统 Chromium，而非 Playwright 下载的浏览器包。
        system_chromium = shutil.which('chromium') or shutil.which('chromium-browser')
        if system_chromium:
            launch_options['executable_path'] = system_chromium
        browser = await playwright.chromium.launch(**launch_options)
        browser_context = await browser.new_context(
            viewport={'width': 1280, 'height': 760},
            user_agent=(
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                '(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36'
            ),
        )
        # verification_url is issued by the login-token request and can redirect
        # between goofish / taobao verification hosts.  The old code injected the
        # fresh session only for .goofish.com, leaving the verification page in a
        # new browser without the very cookies which created the one-time QR.
        # Keep the browser context coherent across the official Alibaba domains;
        # this is only session preservation for a user-completed verification,
        # never an attempt to solve or bypass it.
        parsed_url = urlparse(context.verification_url)
        target_host = (parsed_url.hostname or '').lower()
        cookie_domains = {'.goofish.com', '.taobao.com', '.tmall.com', '.alibaba.com'}
        if target_host:
            labels = target_host.split('.')
            if len(labels) >= 2:
                cookie_domains.add('.' + '.'.join(labels[-2:]))
            cookie_domains.add(target_host)

        cookies = []
        for name, value in trans_cookies(context.cookie).items():
            for domain in cookie_domains:
                cookies.append({'name': name, 'value': str(value), 'domain': domain, 'path': '/'})
        if cookies:
            await browser_context.add_cookies(cookies)
        page = await browser_context.new_page()
        try:
            logger.info('Opening official verification page for account {} on host {}', context.account_id, target_host or 'unknown')
            await page.goto(context.verification_url, wait_until='domcontentloaded', timeout=30000)
            # Wait for the official page to finish its redirect/render cycle
            # before exposing it to the human operator.  Capturing an earlier
            # intermediate page produces a QR that the phone later calls stale.
            await page.wait_for_timeout(2500)
            session_id = f'credential-{context.account_id}-{uuid.uuid4().hex[:12]}'
            await captcha_controller.create_session(session_id, page)
        except Exception:
            await browser.close()
            await playwright.stop()
            raise
        context.browser = (playwright, browser)
        context.browser_context = browser_context
        context.verification_page = page
        context.remote_session_id = session_id
        context.remote_control_url = f'/api/captcha/control/{session_id}'
        context.updated_at = time.time()
        return context

    async def _collect_official_verification_cookies(self, context: LoginContext) -> None:
        if not context.browser_context:
            return
        try:
            browser_cookies = await context.browser_context.cookies()
            merged = trans_cookies(context.cookie)
            merged.update({item['name']: item['value'] for item in browser_cookies if item.get('name')})
            context.cookie = '; '.join(f'{key}={value}' for key, value in merged.items())
        except Exception:
            pass

    def validate_credential(self, credential: Optional[XianyuConnectionCredential]) -> bool:
        return bool(
            credential
            and credential.user_id
            and credential.cookie
            and credential.access_token
            and credential.device_id
        )

    async def acquire(self, account_id: str, user_id: int, cookie: str,
                      *, proxy: Optional[Dict[str, Any]] = None,
                      context: Optional[LoginContext] = None) -> AcquireResult:
        """继续当前 Cookie 对应的 token 请求；不会重新登录或变更设备标识。"""
        cookie = str(cookie or "").strip()
        parsed = trans_cookies(cookie)
        xianyu_user_id = str(parsed.get("unb") or "").strip()
        if not cookie or not xianyu_user_id:
            return AcquireResult(AcquireStatus.LOGIN_EXPIRED, message="登录 Cookie 缺少 unb，需重新登录")

        async with self._lock:
            current = context or self._contexts.get(str(account_id))
            if current is None:
                current = LoginContext(
                    account_id=str(account_id), user_id=int(user_id), cookie=cookie,
                    xianyu_user_id=xianyu_user_id, device_id=generate_device_id(xianyu_user_id),
                )
            elif current.cookie != cookie and context is None:
                # 重新扫码得到的是新的 API 登录态，不能复用上一次验证页。
                # 关闭旧浏览器并只保留新 Cookie 对应的完整上下文。
                if current.browser:
                    try:
                        playwright, browser = current.browser
                        await browser.close()
                        await playwright.stop()
                    except Exception:
                        pass
                current.browser = None
                current.browser_context = None
                current.verification_page = None
                current.remote_session_id = None
                current.remote_control_url = None
                current.verification_url = None
                current.device_id = generate_device_id(xianyu_user_id)
            # 验证后的 Cookie 可能由官方页面刷新；只在传入新值时更新，不生成新设备。
            current.cookie = cookie
            current.stage = AcquireStatus.CREDENTIAL_ACQUIRING
            current.updated_at = time.time()
            self._contexts[str(account_id)] = current

        try:
            probe = await asyncio.to_thread(
                probe_cookie_verification_from_cookie, current.cookie, proxy, 30, current.device_id
            )
        except ValueError as exc:
            current.stage = AcquireStatus.LOGIN_EXPIRED
            current.updated_at = time.time()
            return AcquireResult(AcquireStatus.LOGIN_EXPIRED, context=current, message=str(exc))
        except Exception as exc:
            current.stage = AcquireStatus.RETRYABLE_ERROR
            current.updated_at = time.time()
            return AcquireResult(AcquireStatus.RETRYABLE_ERROR, context=current, message=str(exc))

        # token 请求和验证页可能下发本次会话新增的 Cookie。无论成功或要求验证，
        # 都必须留在 LoginContext 中，恢复时不能退回扫码时的旧 Cookie。
        merged = dict(parsed)
        merged.update(probe.get("session_cookies") or {})
        current.cookie = "; ".join(f"{key}={value}" for key, value in merged.items())

        if probe.get("status") == "verification_required":
            # 关键：Cookie、UNB、deviceId、当前阶段全部保留，仅暂停请求链。
            current.stage = AcquireStatus.VERIFY_REQUIRED
            current.verification_url = str(probe.get("verification_url") or "") or None
            current.updated_at = time.time()
            return AcquireResult(
                AcquireStatus.VERIFY_REQUIRED, context=current,
                message="获取业务连接凭证时需要完成闲鱼官方安全验证",
            )

        payload = probe.get("payload") or {}
        data = payload.get("data") or {}
        token = str(data.get("accessToken") or "").strip()
        if probe.get("status") == "cookie_valid" and token:
            # token 请求可能下发新 Cookie，后续连接须使用合并后的值。
            current.cookie = "; ".join(f"{key}={value}" for key, value in merged.items())
            credential = XianyuConnectionCredential(
                user_id=current.xianyu_user_id, cookie=current.cookie,
                access_token=token, device_id=current.device_id,
            )
            if self.validate_credential(credential):
                current.stage = AcquireStatus.CREDENTIAL_READY
                current.verification_url = None
                current.updated_at = time.time()
                return AcquireResult(AcquireStatus.CREDENTIAL_READY, credential, current,
                                     "已获得 IM WebSocket 初始化所需凭证")

        current.stage = AcquireStatus.FAILED
        current.updated_at = time.time()
        return AcquireResult(AcquireStatus.FAILED, context=current,
                             message="登录已完成，但未取得业务连接 accessToken")

    async def resume(self, account_id: str, *, cookie: Optional[str] = None,
                     proxy: Optional[Dict[str, Any]] = None) -> AcquireResult:
        context = self.get_context(account_id)
        if not context:
            return AcquireResult(AcquireStatus.LOGIN_REQUIRED, message="没有可恢复的登录上下文")
        context.stage = AcquireStatus.VERIFYING
        context.updated_at = time.time()
        await self._collect_official_verification_cookies(context)
        # 不调用 login；从被安全验证打断的 token 请求恢复。
        return await self.acquire(account_id, context.user_id, cookie or context.cookie,
                                  proxy=proxy, context=context)


xianyu_credential_provider = XianyuCredentialProvider()
