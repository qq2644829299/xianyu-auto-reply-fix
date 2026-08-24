"""闲鱼业务连接凭证获取。

扫码 / 密码登录取得的是登录 Cookie，不等于 IM WebSocket 可用。这里把后续
token 请求作为单独阶段处理；出现官方安全校验时只保存上下文并等待人工处理。
本模块绝不执行滑块识别、拖动或第三方验证码服务。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import uuid
import shutil
from urllib.parse import urlencode, urlparse
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional

from utils.xianyu_utils import generate_device_id, trans_cookies
from utils.xianyu_slider_stealth import probe_cookie_verification_from_cookie
from loguru import logger


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
    active_verification_url: Optional[str] = None
    remote_session_id: Optional[str] = None
    remote_control_url: Optional[str] = None
    verification_message: Optional[str] = None
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
            "verification_message": context.verification_message,
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
        context.verification_message = None
        context.updated_at = time.time()
        self._contexts[str(account_id)] = context
        return context

    async def _close_official_verification(self, context: LoginContext, *, preserve_cookie: bool = True) -> None:
        """关闭一次人工验证浏览器，同时保留它在同一上下文中刷新的 Cookie。"""
        if preserve_cookie:
            await self._collect_official_verification_cookies(context)
        if context.remote_session_id:
            try:
                from utils.captcha_remote_control import captcha_controller
                await captcha_controller.close_session(context.remote_session_id)
            except Exception:
                pass
        if context.browser:
            try:
                playwright, browser = context.browser
                await browser.close()
                await playwright.stop()
            except Exception:
                pass
        context.browser = None
        context.browser_context = None
        context.verification_page = None
        context.remote_session_id = None
        context.remote_control_url = None
        context.active_verification_url = None

    async def start_official_verification(self, context: LoginContext) -> LoginContext:
        """在服务器保留原 Cookie 的浏览器中打开官方页，供用户手工操作。"""
        if not context.verification_url:
            return context
        from playwright.async_api import async_playwright
        from utils.captcha_remote_control import captcha_controller

        # 同一真实滑块会话保持不动；只有该会话已完成、已关闭，或上轮是
        # 官方错误页时，才收集已有 Cookie 后重新建立。绝不能一边保留旧页
        # 一边新建浏览器，二者会变成不同会话。
        if context.remote_session_id and context.remote_control_url and context.verification_page:
            # 闲鱼安全页携带一次性的挑战参数。重新取凭证后拿到的新地址
            # 必须重新打开；继续展示旧窗口只会让用户看到“刷新后重试”。
            same_challenge = context.active_verification_url == context.verification_url
            if same_challenge and captcha_controller.session_exists(context.remote_session_id) and not captcha_controller.is_completed(context.remote_session_id):
                return context
        if context.browser:
            await self._close_official_verification(context, preserve_cookie=True)

        context.verification_message = None

        playwright = await async_playwright().start()
        # 人工官方验证应使用可见的 Chromium 会话。生产容器已配好 Xvfb +
        # noVNC；此前这里固定 headless=True，即使用户手工操作，也可能落入
        # 官方页面的无头降级/错误页，且无法切换到真正实时的同一会话。
        # 本地未启用有头模式时仍保持无头，避免给开发环境增加显示依赖。
        headful_requested = os.environ.get('ENABLE_HEADFUL', '').strip().lower() in {'1', 'true', 'yes', 'on'}
        # 旧运行镜像可能尚未包含 Xvfb。不能仅凭环境变量启动有头浏览器，
        # 否则 Playwright 会在没有 X Server 时直接终止，反而让人工验证不可用。
        headful_enabled = headful_requested and bool(shutil.which('Xvfb') or shutil.which('Xorg'))
        if headful_requested and not headful_enabled:
            logger.warning('已请求有头人工验证，但运行镜像缺少 X Server；本次安全回退为无头会话')
        launch_options = {
            'headless': not headful_enabled,
            'args': [
                '--no-sandbox',
                '--disable-dev-shm-usage',
                '--window-size=1280,760',
            ],
        }
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
        # 验证地址可能跳转到 goofish、淘宝等官方域名。必须把当前会话 Cookie
        # 放进验证页实际使用的官方域名，否则用户在页面完成验证，恢复链路仍然
        # 拿不到同一个会话的结果。
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
        # 预检也必须读取这个刚建立的同一浏览器上下文，不能等到验证页创建
        # 完成后再登记，否则会退回到后台 HTTP 请求。
        context.browser = (playwright, browser)
        context.browser_context = browser_context
        context.verification_page = page
        try:
            logger.info(
                '打开官方验证页: account={}, host={}, mode={}',
                context.account_id,
                target_host or 'unknown',
                'headful' if headful_enabled else 'headless',
            )
            # 直接进入与 token 接口同源的官方入口。IM 首页会长期加载消息资源，
            # 服务器浏览器等待该页会卡住；这里不需要等首页，只需先取得 h5api
            # 官方来源，再由该浏览器发起本次凭证请求。
            official_api_origin = 'https://h5api.m.goofish.com/'
            try:
                await asyncio.wait_for(
                    page.goto(official_api_origin, wait_until='commit', timeout=10000),
                    timeout=12,
                )
            except Exception as navigation_error:
                logger.warning('官方验证来源页打开超时，改用本次官方挑战入口: {}', navigation_error)
                await asyncio.wait_for(
                    page.goto(context.verification_url, wait_until='commit', timeout=10000),
                    timeout=12,
                )
            await page.wait_for_timeout(300)
            # 让官方浏览器本身发起 IM token 请求，安全验证会绑定到用户眼前的
            # 这一个页面，而非早先后台 HTTP 请求下发的一次性挑战链接。
            try:
                browser_probe = await self._probe_im_token_in_official_browser(context, page)
                if browser_probe.get('verification_url'):
                    context.verification_url = str(browser_probe['verification_url'])
                logger.info(
                    '官方浏览器 IM 凭证预检: account={}, status={}',
                    context.account_id, browser_probe.get('status'),
                )
            except Exception as browser_probe_error:
                logger.warning(
                    '官方浏览器 IM 凭证预检失败，保留原验证页兜底: account={}, error={}',
                    context.account_id, browser_probe_error,
                )
            official_dialog_visible = False
            for selector in ('iframe#baxia-dialog-content', '.baxia-dialog', '#nocaptcha', '.nc-container'):
                try:
                    locator = page.locator(selector).first
                    if await locator.is_visible(timeout=500):
                        official_dialog_visible = True
                        break
                except Exception:
                    continue
            if not official_dialog_visible:
                await page.goto(context.verification_url, wait_until='domcontentloaded', timeout=30000)
                await page.wait_for_timeout(2000)
            session_id = f'credential-{context.account_id}-{uuid.uuid4().hex[:12]}'
            await captcha_controller.create_desktop_session(session_id, page)
        except Exception:
            await browser.close()
            await playwright.stop()
            context.browser = None
            context.browser_context = None
            context.verification_page = None
            raise
        context.browser = (playwright, browser)
        context.browser_context = browser_context
        context.verification_page = page
        context.remote_session_id = session_id
        context.active_verification_url = context.verification_url
        # 打开的是 noVNC 直连到同一 Chromium 窗口的入口。所有鼠标和滑动均
        # 由用户直接交给闲鱼官方页面，服务端不再转发或合成滑块轨迹。
        context.remote_control_url = f'/api/captcha/desktop/view/{session_id}'
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

    async def _probe_im_token_in_official_browser(
        self, context: LoginContext, page: Any = None,
    ) -> Dict[str, Any]:
        """在用户正在操作的官方浏览器中请求 IM 登录凭证。

        闲鱼的安全挑战绑定发起请求的浏览器会话。先用后台 HTTP 请求拿挑战，
        再把链接交给另一套浏览器，会让人工滑动落在错误会话中。这里没有任何
        自动验证码处理，只由同一官方页面发起请求并保留其 Cookie。
        """
        if not context.browser_context:
            raise RuntimeError('官方浏览器会话不存在')
        page = page or context.verification_page
        if page is None:
            page = await context.browser_context.new_page()
            context.verification_page = page

        await self._collect_official_verification_cookies(context)
        cookie_values = trans_cookies(context.cookie)
        token_seed = str(cookie_values.get('_m_h5_tk') or '').split('_', 1)[0].strip()
        if not token_seed:
            raise ValueError('官方浏览器会话缺少 _m_h5_tk，需重新扫码登录')

        timestamp = str(int(time.time() * 1000))
        data_value = json.dumps(
            {'appKey': '444e9908a51d1cb236a27862abc769c9', 'deviceId': context.device_id},
            separators=(',', ':'), ensure_ascii=False,
        )
        signature = hashlib.md5(
            f'{token_seed}&{timestamp}&34839810&{data_value}'.encode('utf-8')
        ).hexdigest()
        query = urlencode({
            'jsv': '2.7.2', 'appKey': '34839810', 't': timestamp, 'sign': signature,
            'v': '1.0', 'type': 'originaljson', 'accountSite': 'xianyu',
            'dataType': 'json', 'timeout': '20000',
            'api': 'mtop.taobao.idlemessage.pc.login.token',
            'sessionOption': 'AutoLoginOnly', 'spm_cnt': 'a21ybx.im.0.0',
        })
        endpoint = f'https://h5api.m.goofish.com/h5/mtop.taobao.idlemessage.pc.login.token/1.0/?{query}'
        response = await page.evaluate(
            """async ({ endpoint, dataValue }) => {
                const result = await fetch(endpoint, {
                    method: 'POST', credentials: 'include',
                    headers: {
                        'accept': 'application/json',
                        'content-type': 'application/x-www-form-urlencoded;charset=UTF-8',
                    },
                    body: new URLSearchParams({ data: dataValue }).toString(),
                });
                return { status: result.status, text: await result.text() };
            }""",
            {'endpoint': endpoint, 'dataValue': data_value},
        )
        await self._collect_official_verification_cookies(context)
        try:
            payload = json.loads(str(response.get('text') or '{}'))
        except (TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f'官方浏览器请求返回异常: HTTP {response.get("status")}, {exc}') from exc
        data = payload.get('data') or {}
        verification_url = str(data.get('url') or '').strip() or None
        success_ret = any('SUCCESS::调用成功' in str(item) for item in (payload.get('ret') or []))
        has_token = bool(str(data.get('accessToken') or '').strip())
        return {
            'status': 'verification_required' if verification_url else ('cookie_valid' if success_ret and has_token else 'unknown'),
            'verification_url': verification_url,
            'payload': payload,
            'session_cookies': trans_cookies(context.cookie),
            'success_ret': success_ret,
            'has_token_payload': has_token,
        }

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
                current.active_verification_url = None
                current.verification_message = None
                current.device_id = generate_device_id(xianyu_user_id)
            # 验证后的 Cookie 可能由官方页面刷新；只在传入新值时更新，不生成新设备。
            current.cookie = cookie
            current.stage = AcquireStatus.CREDENTIAL_ACQUIRING
            current.updated_at = time.time()
            self._contexts[str(account_id)] = current

        try:
            if current.browser_context and current.verification_page:
                probe = await self._probe_im_token_in_official_browser(current)
            else:
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
            current.verification_message = None
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
                current.verification_message = None
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
        result = await self.acquire(account_id, context.user_id, cookie or context.cookie,
                                    proxy=proxy, context=context)
        if result.status == AcquireStatus.CREDENTIAL_READY:
            # Cookie 已在 acquire 前从同一官方浏览器上下文收集；这时才关闭
            # 验证页，确保后续账号任务使用的就是人工验证后取得的 Cookie。
            await self._close_official_verification(context, preserve_cookie=False)
        return result


xianyu_credential_provider = XianyuCredentialProvider()
