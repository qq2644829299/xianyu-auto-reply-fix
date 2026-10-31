"""页内自动滑块（对应「鱼店长Pro」的 `EnableAutoVerify` / `SliderVerify` 机制）。

为什么必须是"页内"
==================
逆向 `鱼店长Pro` 得到的配置语义（`AgisoIdleClient.Model.AccountConfig`）：

    EnableAutoVerify        bool    自动验证总开关
    EnableAutoVerifyRetry   bool    失败后自动重试
    VerifyRetryMin          int     重试间隔（分钟）
    RepeatVerifyFailTip     string  反复失败的提示文案
    FrontHanlder.SliderVerify()     滑块事件由**页面 JS 回调**到 C#
    JsWs.RefreshPage(reason)        兜底：整页刷新换新会话

关键点：它的滑块处理发生在**同一条已登录的浏览器会话里**，所以
cookie、`_m_h5_tk`、baxia 指纹、TLS 特征全程一致，平台认可这次验证。

本项目原有 `utils/xianyu_slider_stealth.py` 的轨迹算法很好，但它是
**新开一个浏览器、新装一份 cookie** 去闯验证；会话态与发起请求的会话
不是同一个，即使拖对了也常常不被认可——这正是"验证码经常过不去"的
另一半原因。

本模块因此只做一件事：把滑块操作放在 `BrowserIMSession` 已有的页面上，
用真实指针事件（CDP Input）完成拖动。
"""

from __future__ import annotations

import asyncio
import math
import random
import time
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from .browser_session import BrowserIMSession

# 阿里 nc / baxia 滑块常见容器选择器（多版本并存，按优先级排列）
SLIDER_WRAPPER_SELECTORS = (
    "#nc_1_wrapper",
    ".nc-container",
    "#nocaptcha",
    "#baxia-dialog-content",
    ".baxia-dialog",
    "#J_MIDDLEWARE_FRAME_WIDGET",
    ".nc_wrapper",
    "#nc_1_n1z",
    "#nc_2_n1z",
)

SLIDER_HANDLE_SELECTORS = (
    "#nc_1_n1z",
    ".nc_iconfont.btn_slide",
    ".btn_slide",
    "#nc_2_n1z",
    "span.nc-lang-cnt + span",
)

SLIDER_TRACK_SELECTORS = (
    "#nc_1_n1t",
    ".nc_scale",
    ".nc-track",
    "#nc_1__scale_text",
)

RISK_URL_HINTS = (
    "punish",
    "_____tmd_____",
    "x5secdata",
    "sec.taobao.com",
    "login.taobao.com/member/verify",
)

RISK_TEXT_HINTS = (
    "请按住滑块",
    "向右拖动",
    "拖动滑块",
    "请完成验证",
    "安全验证",
    "滑动验证",
    "verify",
)


def _bezier(p0: float, p1: float, p2: float, p3: float, t: float) -> float:
    """三次贝塞尔插值，用于生成自然的位移曲线。"""
    u = 1.0 - t
    return (u * u * u * p0) + (3 * u * u * t * p1) + (3 * u * t * t * p2) + (t * t * t * p3)


class _Trajectory:
    """拟人化拖动轨迹。

    比线性插值多三件事，都是为了贴合真实手部动作：
      1. 贝塞尔加速段 + 减速段；
      2. 末端轻微过冲再回退（人手几乎不会精确停在目标点）；
      3. 抖动 + 偶发微停顿。
    """

    def __init__(self, distance: float, *, rng: Optional[random.Random] = None) -> None:
        self.distance = max(1.0, float(distance))
        self.rng = rng or random.Random()

    def points(self) -> List[Tuple[float, float]]:
        rng = self.rng
        dist = self.distance
        n = int(max(28, min(96, dist / 3.2)))
        # 控制点让曲线有"先慢后快再慢"的手感
        c1 = dist * rng.uniform(0.16, 0.28)
        c2 = dist * rng.uniform(0.62, 0.82)
        overshoot = dist + rng.uniform(2.0, 7.0)

        out: List[Tuple[float, float]] = []
        for i in range(n + 1):
            t = i / n
            x = _bezier(0.0, c1, c2, overshoot, t)
            # 垂直抖动：幅度随速度变化，中段最大
            wobble = math.sin(t * math.pi) * rng.uniform(-2.6, 2.6)
            # 高频微抖
            wobble += rng.uniform(-0.7, 0.7)
            out.append((x, wobble))

        # 回退到精确落点
        steps_back = max(3, int(n * 0.12))
        for i in range(1, steps_back + 1):
            t = i / steps_back
            x = overshoot + (dist - overshoot) * t
            out.append((x, rng.uniform(-0.5, 0.5)))

        return out

    def delays(self, count: int) -> List[float]:
        """每步停顿：中段快、首尾慢，并夹杂随机长停顿（"犹豫"）。"""
        rng = self.rng
        out: List[float] = []
        for i in range(count):
            t = i / max(1, count - 1)
            base = 0.006 + 0.010 * abs(0.5 - t) * 2
            d = base * rng.uniform(0.6, 1.8)
            if rng.random() < 0.035:
                d += rng.uniform(0.04, 0.13)
            out.append(d)
        return out


class AutoSlider:
    """在已有页面上完成滑块验证。"""

    def __init__(
        self,
        session: BrowserIMSession,
        *,
        max_attempts: int = 3,
        retry_interval_seconds: float = 5.0,
        drag_timeout_seconds: float = 20.0,
        headful_manual_wait_seconds: float = 90.0,
    ) -> None:
        self.session = session
        self.cookie_id = session.cookie_id
        self.max_attempts = max(1, int(max_attempts))
        self.retry_interval_seconds = float(retry_interval_seconds)
        self.drag_timeout_seconds = float(drag_timeout_seconds)
        self.headful_manual_wait_seconds = float(headful_manual_wait_seconds)

        self.last_error: Optional[str] = None
        self.attempts_made = 0

    # ------------------------------------------------------------------ 公共入口

    async def solve(self, verify_url: str = "", *, user_id: str = "") -> Dict[str, Any]:
        """在常驻页面里过滑块。

        返回 ``{"ok": bool, "cookies": str, "reason": str, "attempts": int}``。
        ``ok`` 为真时 ``cookies`` 是验证通过后的最新 cookie 串。
        """
        self.attempts_made = 0
        self.last_error = None

        page = await self.session._ensure_page()

        # 1) 进入验证页（同一 page，保持同 context 会话）
        if verify_url:
            try:
                logger.info(f"【{self.cookie_id}】[滑块] 在同一会话内打开验证页: {verify_url[:120]}")
                await page.goto(verify_url, wait_until="domcontentloaded", timeout=45000)
                await asyncio.sleep(random.uniform(0.8, 1.6))
            except Exception as exc:
                self.last_error = f"goto_failed: {exc}"
                logger.warning(f"【{self.cookie_id}】[滑块] 打开验证页失败: {exc}")

        for attempt in range(1, self.max_attempts + 1):
            self.attempts_made = attempt
            try:
                result = await self._attempt_once(page, attempt)
                if result.get("ok"):
                    cookies = await self.session.cookies_to_string()
                    logger.info(
                        f"【{self.cookie_id}】[滑块] ✅ 第 {attempt} 次尝试通过验证（同会话页内）"
                    )
                    return {
                        "ok": True,
                        "cookies": cookies,
                        "reason": "passed",
                        "attempts": attempt,
                    }
                self.last_error = result.get("reason") or "unknown"
                logger.warning(
                    f"【{self.cookie_id}】[滑块] 第 {attempt}/{self.max_attempts} 次未通过: {self.last_error}"
                )
            except Exception as exc:
                self.last_error = f"attempt_exception: {exc}"
                logger.error(f"【{self.cookie_id}】[滑块] 第 {attempt} 次异常: {exc}")

            if attempt < self.max_attempts:
                wait_s = self.retry_interval_seconds * attempt
                logger.info(f"【{self.cookie_id}】[滑块] {wait_s:.1f}s 后重试")
                await asyncio.sleep(wait_s)
                # 每次重试前刷新验证页，拿一个全新的 puzzle
                try:
                    await page.reload(wait_until="domcontentloaded", timeout=30000)
                    await asyncio.sleep(random.uniform(0.6, 1.2))
                except Exception:
                    pass

        # 2) 有头模式下给人工兜底机会
        if not self.session.headless and self.headful_manual_wait_seconds > 0:
            logger.warning(
                f"【{self.cookie_id}】[滑块] 自动尝试未通过，转为人工处理窗口 "
                f"({int(self.headful_manual_wait_seconds)}s)"
            )
            ok = await self._wait_manual_pass(page, self.headful_manual_wait_seconds)
            if ok:
                cookies = await self.session.cookies_to_string()
                return {"ok": True, "cookies": cookies, "reason": "manual", "attempts": self.attempts_made}

        return {
            "ok": False,
            "cookies": "",
            "reason": self.last_error or "all_attempts_failed",
            "attempts": self.attempts_made,
        }

    # ------------------------------------------------------------------ 单次尝试

    async def _attempt_once(self, page, attempt: int) -> Dict[str, Any]:
        # 已经是验证通过状态（例如上次刷新后自动过了）
        if await self._is_passed(page):
            return {"ok": True, "reason": "already_passed"}

        found = await self._locate_slider(page)
        if not found:
            # 页面没有滑块：可能压根不需要验证
            if await self._is_passed(page):
                return {"ok": True, "reason": "no_slider_but_passed"}
            return {"ok": False, "reason": "slider_not_found"}

        handle_box, track_box = found
        distance = max(60.0, track_box["width"] - handle_box["width"] - random.uniform(2, 8))

        logger.info(
            f"【{self.cookie_id}】[滑块] 定位到滑块 handle={handle_box['width']:.0f}px "
            f"track={track_box['width']:.0f}px 需拖动≈{distance:.0f}px"
        )

        start_x = handle_box["x"] + handle_box["width"] / 2
        start_y = handle_box["y"] + handle_box["height"] / 2

        await self._drag(page, start_x, start_y, distance)

        # 等待验证结果落地
        deadline = time.time() + self.drag_timeout_seconds
        while time.time() < deadline:
            await asyncio.sleep(0.35)
            if await self._is_passed(page):
                return {"ok": True, "reason": "passed_after_drag"}
            state = await self._slider_state(page)
            if state.get("failed"):
                return {"ok": False, "reason": f"slider_rejected:{state.get('text') or ''}"[:200]}
        return {"ok": False, "reason": "verify_timeout"}

    async def _drag(self, page, start_x: float, start_y: float, distance: float) -> None:
        """真实指针拖动（Playwright mouse 底层是 CDP Input.dispatchMouseEvent，
        事件 isTrusted=true，与真人操作同等对待）。"""
        traj = _Trajectory(distance)
        pts = traj.points()
        delays = traj.delays(len(pts))

        await page.mouse.move(start_x, start_y)
        await asyncio.sleep(random.uniform(0.08, 0.22))

        await page.mouse.down()
        await asyncio.sleep(random.uniform(0.06, 0.16))

        for (dx, dy), d in zip(pts, delays):
            await page.mouse.move(start_x + dx, start_y + dy, steps=1)
            await asyncio.sleep(d)

        # 落点停留后再松手
        await asyncio.sleep(random.uniform(0.12, 0.28))
        await page.mouse.up()

    # ------------------------------------------------------------------ 页面探测

    async def _all_frames(self, page) -> List[Any]:
        frames = []
        try:
            for f in page.frames:
                frames.append(f)
        except Exception:
            frames = [page.main_frame]
        return frames

    async def _locate_slider(self, page) -> Optional[Tuple[Dict[str, float], Dict[str, float]]]:
        """在页面（含 iframe）里找滑块手柄与轨道，返回两者的 bounding box。"""
        for frame in await self._all_frames(page):
            for handle_sel in SLIDER_HANDLE_SELECTORS:
                try:
                    el = await frame.query_selector(handle_sel)
                    if not el:
                        continue
                    hbox = await el.bounding_box()
                    if not hbox or hbox["width"] < 8:
                        continue

                    # 找同一容器里的轨道
                    for track_sel in SLIDER_TRACK_SELECTORS:
                        tel = await frame.query_selector(track_sel)
                        if tel:
                            tbox = await tel.bounding_box()
                            if tbox and tbox["width"] > hbox["width"]:
                                return hbox, tbox

                    # 退而求其次：用父容器宽度
                    parent = await el.evaluate_handle(
                        "e => e.closest('.nc-container, #nc_1_wrapper, #nocaptcha, .baxia-dialog') || e.parentElement"
                    )
                    if parent:
                        pbox = await parent.as_element().bounding_box()
                        if pbox and pbox["width"] > hbox["width"]:
                            return hbox, pbox
                except Exception:
                    continue
        return None

    async def _slider_state(self, page) -> Dict[str, Any]:
        """判定滑块是否明确失败（出现"验证失败/请重试"提示）。"""
        for frame in await self._all_frames(page):
            try:
                info = await frame.evaluate(
                    """
                    () => {
                      const sel = '#nc_1__scale_text, .nc-lang-cnt, .nc_scale, #nc_1_wrapper';
                      const el = document.querySelector(sel);
                      const text = el ? (el.innerText || '') : '';
                      const fail = /验证失败|请重试|出错|error|try again/i.test(text);
                      return { fail, text: text.slice(0, 120) };
                    }
                    """
                )
                if isinstance(info, dict) and info.get("fail"):
                    return {"failed": True, "text": info.get("text")}
            except Exception:
                continue
        return {"failed": False}

    async def _is_passed(self, page) -> bool:
        """验证是否已通过：x5sec cookie 出现 / URL 离开风控页 / 滑块容器消失。"""
        # 1) x5sec cookie 是最强信号
        try:
            cookies = await self.session.dump_cookies()
            names = {c.get("name") for c in cookies}
            if "x5sec" in names:
                return True
        except Exception:
            pass

        # 2) URL 与页面文本
        try:
            for frame in await self._all_frames(page):
                try:
                    url = frame.url or ""
                    if any(h in url for h in RISK_URL_HINTS):
                        continue
                    has_slider = await frame.evaluate(
                        """() => !!document.querySelector(
                             '#nc_1_wrapper, .nc-container, #nocaptcha, #nc_1_n1z'
                           )"""
                    )
                    if not has_slider:
                        # 没有滑块容器，且页面不含验证文案
                        body = await frame.evaluate(
                            "() => (document.body ? document.body.innerText.slice(0, 800) : '')"
                        )
                        if not any(t in (body or "") for t in RISK_TEXT_HINTS):
                            return True
                except Exception:
                    continue
        except Exception:
            pass
        return False

    async def _wait_manual_pass(self, page, timeout: float) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            await asyncio.sleep(2.0)
            try:
                if await self._is_passed(page):
                    logger.info(f"【{self.cookie_id}】[滑块] 人工处理后已通过验证")
                    return True
            except Exception:
                continue
        return False
