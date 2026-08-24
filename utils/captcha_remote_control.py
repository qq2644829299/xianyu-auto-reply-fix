"""
刮刮乐远程控制模块
通过 WebSocket 实时传输页面截图到前端，并接收用户操作
"""

import asyncio
import base64
import hashlib
import json
from typing import Optional, Dict, Any
from loguru import logger
from playwright.async_api import Page


class CaptchaRemoteController:
    """刮刮乐远程控制器"""
    
    def __init__(self):
        self.active_sessions: Dict[str, Dict[str, Any]] = {}
        # 桌面接管会话不复制、截图或转发鼠标。浏览器由 noVNC 直接显示，
        # 用户看到和操作的是服务器 Chromium 中的闲鱼官方页面本身。
        self.desktop_sessions: Dict[str, Dict[str, Any]] = {}
        self.websocket_connections: Dict[str, Any] = {}

    async def create_desktop_session(self, session_id: str, page: Page) -> Dict[str, Any]:
        """登记一个可由 noVNC 查看的官方人工验证会话。

        这里刻意不识别滑块、更不模拟拖动。页面中的 baxia 官方验证层由
        闲鱼自行渲染，用户通过远程桌面在原浏览器会话中亲自完成验证。
        """
        self.desktop_sessions[session_id] = {
            'page': page,
            'completed': False,
            'initial_url': str(getattr(page, 'url', '') or ''),
            'initial_cookie_fingerprint': await self._cookie_fingerprint(page),
        }
        logger.info("✅ 创建官方页面桌面验证会话: {}", session_id)
        return self.desktop_sessions[session_id]

    def get_desktop_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        return self.desktop_sessions.get(session_id)
    
    async def create_session(self, session_id: str, page: Page) -> Optional[Dict[str, str]]:
        """
        创建远程控制会话
        
        Args:
            session_id: 会话ID（通常是用户ID）
            page: Playwright Page 对象
            
        Returns:
            包含会话信息的字典
        """
        # 验证框常在页面打开后延迟渲染。这里必须确认存在真正可交互的
        # 滑块；错误页、已过期页没有滑块时不能创建“整页鼠标转发”会话。
        # 后者既无法完成官方验证，还会把错误页误报成可操作验证码。
        captcha_info = await self._wait_for_captcha_info(page)
        if not captcha_info:
            logger.warning("未检测到真实滑块，不创建远程验证会话: {}", session_id)
            return None
        
        # 只截取滑块区域，不截取整个页面（性能优化）
        screenshot_bytes, capture = await self._screenshot_captcha_area(page, captcha_info)
        screenshot_base64 = base64.b64encode(screenshot_bytes).decode('utf-8')
        
        # 获取视口大小
        try:
            viewport = page.viewport_size
            if viewport is None:
                # 如果没有设置viewport，使用默认值或通过JS获取
                viewport = await page.evaluate("() => ({width: window.innerWidth, height: window.innerHeight})")
        except:
            viewport = {'width': 1280, 'height': 720}  # 默认值
        
        # 存储会话
        self.active_sessions[session_id] = {
            'page': page,
            'screenshot': screenshot_base64,
            'captcha_info': captcha_info,
            'capture': capture,
            'input_lock': asyncio.Lock(),
            # 高频 pointermove 不能逐条 await Playwright。网络稍有抖动时，旧
            # move 会排在 mouseup 前面，官方页面收到的是一条滞后的轨迹。只保留
            # 尚未转发的最后一个位置，down/up 仍严格按顺序转发。
            'pending_move': None,
            'move_worker': None,
            'completed': False,
            'completion_candidate': False,
            'initial_url': str(getattr(page, 'url', '') or ''),
            'initial_cookie_fingerprint': await self._cookie_fingerprint(page),
            'viewport': viewport
        }
        
        logger.info(f"✅ 创建远程控制会话: {session_id}")
        
        return {
            'session_id': session_id,
            'screenshot': screenshot_base64,
            'captcha_info': captcha_info,
            'capture': capture,
            'viewport': self.active_sessions[session_id]['viewport']
        }

    async def _wait_for_captcha_info(self, page: Page, timeout_seconds: float = 8.0):
        """等待真实滑块容器，绝不回退到整页截图。"""
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while True:
            captcha_info = await self._get_captcha_info(page)
            if captcha_info:
                return captcha_info
            if asyncio.get_running_loop().time() >= deadline:
                logger.warning("⚠️ 等待真实滑块超时，官方页可能已失效或为错误页")
                return None
            await asyncio.sleep(0.4)
    
    async def _screenshot_captcha_area(self, page: Page, captcha_info: Dict[str, Any]):
        """截取整个验证码容器区域"""
        try:
            if captcha_info and 'x' in captcha_info:
                # 直接截取整个容器，稍微留一点边距
                x = max(0, captcha_info['x'] - 10)
                y = max(0, captcha_info['y'] - 10)
                width = captcha_info['width'] + 20
                height = captcha_info['height'] + 20
                
                # 截取整个验证码容器
                screenshot_bytes = await page.screenshot(
                    type='jpeg',
                    quality=80,  # 验证码区域用高质量
                    clip={
                        'x': x,
                        'y': y,
                        'width': width,
                        'height': height
                    }
                )
                logger.info(f"✅ 截取验证码容器: {width}x{height} (包含完整验证码)")
                return screenshot_bytes, {
                    'origin_x': x,
                    'origin_y': y,
                    'css_width': width,
                    'css_height': height,
                }
            else:
                # create_session 不会传入 None；保留这个分支仅用于截图刷新时
                # 的安全降级，前端会明确提示会话已不可操作。
                logger.warning("未找到滑块位置，验证码会话已不可操作")
                screenshot_bytes = await page.screenshot(type='jpeg', quality=75, full_page=False)
                return screenshot_bytes, {'origin_x': 0, 'origin_y': 0}
                
        except Exception as e:
            logger.warning(f"截取滑块区域失败，使用全页面: {e}")
            screenshot_bytes = await page.screenshot(type='jpeg', quality=75, full_page=False)
            return screenshot_bytes, {'origin_x': 0, 'origin_y': 0}
    
    async def _get_captcha_info(self, page: Page) -> Dict[str, Any]:
        """获取滑块验证码信息（查找整个容器）"""
        try:
            # 优先查找整个验证码容器（不是按钮）。命中容器后还必须确认其中
            # 有可见的拖动控件，避免通用 [id*=captcha] 命中官方错误页。
            container_selectors = [
                '#nocaptcha',  # 完整的验证码容器
                '.nc_1_nocaptcha',
                '#baxia-dialog-content',
                '#baxia-dialog',
                '[id*="baxia"]',
                '[class*="baxia"]',
                '.scratch-captcha-container',
                '[id*="captcha"]',
                '.nc-container',
                '[class*="nc-container"]'
            ]
            
            # 先在主页面查找
            for selector in container_selectors:
                try:
                    element = await page.query_selector(selector)
                    if element:
                        box = await element.bounding_box()
                        if box and box['width'] > 100 and box['height'] > 100 and await self._has_real_slider_control(page):
                            logger.info(f"✅ 在主页面找到验证码容器: {selector}, 大小: {box['width']}x{box['height']}")
                            return {
                                'selector': selector,
                                'x': box['x'],
                                'y': box['y'],
                                'width': box['width'],
                                'height': box['height'],
                                'in_iframe': False
                            }
                except Exception as e:
                    logger.debug(f"检查选择器 {selector} 失败: {e}")
                    continue
            
            # 在 iframe 中查找
            frames = page.frames
            for frame in frames:
                if frame != page.main_frame:
                    for selector in container_selectors:
                        try:
                            element = await frame.query_selector(selector)
                            if element:
                                box = await element.bounding_box()
                                if box and box['width'] > 100 and box['height'] > 100 and await self._has_real_slider_control(frame):
                                    logger.info(f"✅ 在iframe找到验证码容器: {selector}, 大小: {box['width']}x{box['height']}")
                                    return {
                                        'selector': selector,
                                        'x': box['x'],
                                        'y': box['y'],
                                        'width': box['width'],
                                        'height': box['height'],
                                        'in_iframe': True
                                        # 注意：不保存 frame 对象，因为不能被 JSON 序列化
                                    }
                        except Exception as e:
                            logger.debug(f"iframe检查选择器 {selector} 失败: {e}")
                            continue
            
            logger.warning("⚠️ 未找到验证码容器")
            return None
            
        except Exception as e:
            logger.error(f"获取滑块信息失败: {e}")
            return None

    async def _has_real_slider_control(self, frame_or_page) -> bool:
        """确认官方页面确实展示了可拖动滑块，而非只包含验证码文案。"""
        slider_selectors = [
            '#nc_1_n1z', '.nc_scale span', '.nc_iconfont.btn_slide',
            '#scratch-captcha-btn', '.scratch-captcha-slider',
            '[class*="slider"][role="slider"]', '[class*="slider"] [class*="handle"]',
            '[class*="slide"] [class*="handle"]', '[data-testid*="slider"]',
        ]
        for selector in slider_selectors:
            try:
                element = await frame_or_page.query_selector(selector)
                if element and await element.is_visible():
                    box = await element.bounding_box()
                    if box and box['width'] >= 12 and box['height'] >= 12:
                        return True
            except Exception:
                continue
        return False

    async def _cookie_fingerprint(self, page: Page) -> str:
        """只存 Cookie 值的摘要，用于验证完成后的正向状态判断，不记录明文。"""
        try:
            cookies = await page.context.cookies()
            material = '\n'.join(
                f"{item.get('domain', '')}|{item.get('path', '')}|{item.get('name', '')}|{item.get('value', '')}"
                for item in sorted(cookies, key=lambda value: (value.get('domain', ''), value.get('path', ''), value.get('name', '')))
            )
            return hashlib.sha256(material.encode('utf-8')).hexdigest()
        except Exception:
            return ''

    @staticmethod
    def _looks_like_verification_url(url: str) -> bool:
        lowered = str(url or '').lower()
        return any(token in lowered for token in ('punish', 'captcha', 'verify', 'baxia', 'nocaptcha', 'security'))

    async def _has_known_verification_error(self, page: Page) -> bool:
        try:
            text = (await page.locator('body').inner_text(timeout=1500)).lower()
        except Exception:
            return True
        markers = (
            "oops", "something's wrong", "error[", "error [", "please refresh",
            '验证未通过', '验证失败', '二维码已失效', '验证已失效', '请刷新后重试',
        )
        return any(marker in text for marker in markers)
    
    async def update_screenshot(self, session_id: str, quality: int = 75) -> Optional[Dict[str, Any]]:
        """刷新截图，并带回该截图在 Playwright 页面中的精确原点。"""
        if session_id not in self.active_sessions:
            return None
        
        try:
            page = self.active_sessions[session_id]['page']
            # 验证失败后页面可能重排或退化为整页；每次截图重取原点，
            # 不能把后续拖动继续映射到首次截图的位置。
            captcha_info = await self._wait_for_captcha_info(page, timeout_seconds=2.0)
            screenshot_bytes, capture = await self._screenshot_captcha_area(page, captcha_info)
            
            screenshot_base64 = base64.b64encode(screenshot_bytes).decode('utf-8')
            self.active_sessions[session_id]['screenshot'] = screenshot_base64
            self.active_sessions[session_id]['captcha_info'] = captcha_info
            self.active_sessions[session_id]['capture'] = capture
            return {'screenshot': screenshot_base64, 'capture': capture, 'captcha_info': captcha_info}
            
        except Exception as e:
            logger.error(f"更新截图失败: {e}")
            return None
    
    async def handle_mouse_event(self, session_id: str, event_type: str, x: int, y: int) -> bool:
        """
        处理鼠标事件
        
        Args:
            session_id: 会话ID
            event_type: 事件类型 (down/move/up)
            x: X坐标
            y: Y坐标
            
        Returns:
            是否成功
        """
        if session_id not in self.active_sessions:
            logger.warning(f"会话不存在: {session_id}")
            return False
        
        try:
            session = self.active_sessions[session_id]

            if event_type == 'move':
                # move 是可合并事件：调用方无需等待浏览器完成这一步，下一条 move
                # 会覆盖尚未消费的位置。这样 WebSocket 接收循环可以持续读取用户
                # 的拖动，且不会把 mouseup 堵在过期的 move 队列后面。
                self._queue_mouse_move(session_id, x, y)
                return True

            # 释放前先将最后一个位置送达。这里是有意等待的，以保证官方页面实际
            # 收到 down -> move* -> up 的正确顺序。不能在 down 前 flush：那会把
            # 极端网络抖动遗留的上一次 move 放到新一轮按下之前。
            if event_type == 'up':
                await self._flush_pending_moves(session_id)
            elif event_type == 'down':
                session['pending_move'] = None
            page = session['page']
            input_lock = session.setdefault('input_lock', asyncio.Lock())
            async with input_lock:
                return await self._handle_mouse_event_locked(page, event_type, x, y)

        except Exception as e:
            logger.error(f"处理鼠标事件失败: {e}")
            return False

    def _queue_mouse_move(self, session_id: str, x: int, y: int) -> None:
        """合并尚未发送的 move，并在后台顺序转发最新位置。"""
        session = self.active_sessions.get(session_id)
        if not session:
            return

        session['pending_move'] = (x, y)
        worker = session.get('move_worker')
        if worker is None or worker.done():
            session['move_worker'] = asyncio.create_task(
                self._drain_pending_moves(session_id),
                name=f'captcha-move-{session_id}',
            )

    async def _flush_pending_moves(self, session_id: str) -> None:
        """等待已接收的最新 move 送达，供 down/up 保证事件顺序。"""
        session = self.active_sessions.get(session_id)
        if not session:
            return
        worker = session.get('move_worker')
        if worker and not worker.done():
            await worker

        # worker 退出到本次调用之间可能又收到一个 move；补启一次并等待，避免
        # mouseup 早于最后位置抵达官方页面。
        if session.get('pending_move') is not None:
            self._queue_mouse_move(session_id, *session['pending_move'])
            worker = session.get('move_worker')
            if worker:
                await worker

    async def _drain_pending_moves(self, session_id: str) -> None:
        """串行发送合并后的 move，永不在截图流程中等待。"""
        try:
            while True:
                session = self.active_sessions.get(session_id)
                if not session:
                    return
                position = session.get('pending_move')
                if position is None:
                    return
                session['pending_move'] = None

                page = session['page']
                input_lock = session.setdefault('input_lock', asyncio.Lock())
                async with input_lock:
                    await page.mouse.move(*position)

                # 让出一个调度周期，将紧接着到达的 pointermove 合并为最新点。
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(f"转发合并鼠标移动失败: {exc}")

    async def _handle_mouse_event_locked(self, page: Page, event_type: str, x: int, y: int) -> bool:
        """在单会话输入锁内转发事件，防止双 WebSocket 交错操作同一个页面。"""
        try:
            
            if event_type == 'down':
                await page.mouse.move(x, y)
                await page.mouse.down()
                logger.debug(f"鼠标按下: ({x}, {y})")
                
            elif event_type == 'move':
                await page.mouse.move(x, y)
                logger.debug(f"鼠标移动: ({x}, {y})")
                
            elif event_type == 'up':
                # 浏览器端末次 move 可能被节流；释放前务必把终点补送到 Playwright。
                await page.mouse.move(x, y)
                await page.mouse.up()
                logger.debug(f"鼠标释放: ({x}, {y})")
                
            else:
                logger.warning(f"未知事件类型: {event_type}")
                return False
            
            return True
            
        except Exception as e:
            logger.error(f"转发鼠标事件失败: {e}")
            return False
    
    async def check_completion(self, session_id: str) -> bool:
        """只在得到官方页面的正向变化时认定人工验证已完成。

        单纯“滑块元素消失”并不代表成功：官方错误页、超时页同样会让
        ``#nocaptcha`` 消失。完成条件至少包含 Cookie 刷新或离开验证页，且
        页面不能是已知错误状态；最终是否能恢复连接仍由 credential provider
        重新请求业务 token 作权威校验。
        """
        session = self.active_sessions.get(session_id) or self.desktop_sessions.get(session_id)
        if not session:
            return False
        
        try:
            page = session['page']
            
            # 多个选择器检查，确保更准确
            captcha_selectors = [
                '#nocaptcha',
                '#scratch-captcha-btn',
                '.scratch-captcha-container',
                '.scratch-captcha-slider'
            ]
            
            found_visible_captcha = False
            
            # 检查主页面
            for selector in captcha_selectors:
                try:
                    element = await page.query_selector(selector)
                    if element:
                        is_visible = await element.is_visible()
                        if is_visible:
                            logger.debug(f"主页面发现可见滑块: {selector}")
                            found_visible_captcha = True
                            break
                except:
                    continue
            
            if found_visible_captcha:
                return False
            
            # 检查所有 iframe
            frames = page.frames
            for frame in frames:
                if frame != page.main_frame:
                    for selector in captcha_selectors:
                        try:
                            element = await frame.query_selector(selector)
                            if element:
                                is_visible = await element.is_visible()
                                if is_visible:
                                    logger.debug(f"iframe中发现可见滑块: {selector}")
                                    found_visible_captcha = True
                                    break
                        except:
                            continue
                    if found_visible_captcha:
                        break
            
            if found_visible_captcha:
                return False
            
            # 额外检查：看页面内容是否还包含滑块相关文字
            try:
                page_content = await page.content()
                captcha_keywords = ['scratch-captcha', 'nocaptcha', 'slider-btn']
                
                # 如果页面中仍然有大量滑块相关内容，可能还未完成
                keyword_count = sum(1 for kw in captcha_keywords if kw in page_content)
                if keyword_count >= 2:
                    logger.debug(f"页面中仍有 {keyword_count} 个滑块关键词")
                    return False
            except:
                pass

            if await self._has_known_verification_error(page):
                logger.info(f"官方验证页仍为错误/失效状态，不认定完成: {session_id}")
                return False

            cookie_changed = bool(session.get('initial_cookie_fingerprint')) and (
                await self._cookie_fingerprint(page) != session.get('initial_cookie_fingerprint')
            )
            left_verification_page = (
                self._looks_like_verification_url(session.get('initial_url', ''))
                and not self._looks_like_verification_url(getattr(page, 'url', ''))
            )
            if not (cookie_changed or left_verification_page):
                logger.debug(f"滑块已隐藏但尚无官方成功证据: {session_id}")
                return False

            # 所有检查都通过，作为“可恢复”候选。resume 会在同一浏览器上下文
            # 采集 Cookie 并重试 token 请求，避免前端把视觉变化伪装成登录成功。
            logger.success(f"✅ 官方验证页面已出现成功变化: {session_id}")
            session['completion_candidate'] = True
            session['completed'] = True
            return True
            
        except Exception as e:
            logger.error(f"检查完成状态失败: {e}")
            # 出错时返回 False，不要误判为成功
            return False
    
    def is_completed(self, session_id: str) -> bool:
        """检查会话是否已完成"""
        session = self.active_sessions.get(session_id) or self.desktop_sessions.get(session_id)
        if not session:
            return False
        return session.get('completed', False)
    
    def session_exists(self, session_id: str) -> bool:
        """检查会话是否存在"""
        return session_id in self.active_sessions or session_id in self.desktop_sessions
    
    async def close_session(self, session_id: str):
        """关闭会话"""
        if session_id in self.active_sessions:
            worker = self.active_sessions[session_id].get('move_worker')
            if worker and not worker.done():
                worker.cancel()
            del self.active_sessions[session_id]
            logger.info(f"🔒 关闭远程控制会话: {session_id}")
        if session_id in self.desktop_sessions:
            del self.desktop_sessions[session_id]
            logger.info(f"🔒 关闭官方页面桌面验证会话: {session_id}")
    
    async def auto_refresh_screenshot(self, session_id: str, interval: float = 1.0):
        """自动刷新截图（优化版：按需更新）"""
        last_update_time = asyncio.get_event_loop().time()
        
        while session_id in self.active_sessions and not self.is_completed(session_id):
            try:
                current_time = asyncio.get_event_loop().time()
                
                # 使用自适应刷新：空闲时降低频率
                if current_time - last_update_time >= interval:
                    snapshot = await self.update_screenshot(session_id, quality=55)  # 降低质量提升性能
                    
                    if snapshot and session_id in self.websocket_connections:
                        try:
                            ws = self.websocket_connections[session_id]
                            await ws.send_json({
                                'type': 'screenshot_update',
                                **snapshot,
                            })
                            last_update_time = current_time
                        except:
                            # WebSocket 可能已断开
                            break
                
                # 降低检查频率，减少 CPU 使用
                await asyncio.sleep(0.5)
                
            except Exception as e:
                logger.error(f"自动刷新截图失败: {e}")
                await asyncio.sleep(1)  # 出错时等待更长时间


# 全局实例
captcha_controller = CaptchaRemoteController()
