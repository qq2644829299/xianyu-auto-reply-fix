"""浏览器态 IM 会话自检。

用途：在不依赖真实账号 cookie 的前提下，验证移植过来的这套机制确实成立：

    1. Playwright 能起持久化 Chromium；
    2. WS 劫持脚本在页面脚本之前注入成功；
    3. `expose_binding` 事件桥可用；
    4. 断线看门狗能正确计时与被取消；
    5. 传输层 `send/closed/async for` 鸭子类型与 `websockets` 对齐。

运行：
    venv\\Scripts\\python.exe tools\\verify_browser_im.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loguru import logger

from utils.browser_im import BrowserIMSession, BrowserIMTransport, WsDisconnectWatcher, BrowserWebSocket

PASS = []
FAIL = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        PASS.append(name)
        logger.info(f"[PASS] {name} {detail}")
    else:
        FAIL.append(name)
        logger.error(f"[FAIL] {name} {detail}")


async def main() -> int:
    session = BrowserIMSession(
        cookie_id="__selftest__",
        cookies_str="",
        headless=True,
    )

    # ---- 1. 启动浏览器 ----
    try:
        await session.start()
        check("浏览器启动", True, f"user_data={session.user_data_dir}")
    except Exception as exc:
        check("浏览器启动", False, str(exc))
        return 1

    # ---- 2. WS 劫持脚本已注入 ----
    try:
        page = await session._ensure_page()
        hijacked = await page.evaluate("() => !!window.__XY_WS_HIJACKED__")
        check("WS 劫持脚本注入", bool(hijacked))

        has_api = await page.evaluate(
            "() => ['__xyWsState','__xyWsSend','__xyWsSendIm','__xyWsList','__xyWsPickIm']"
            ".every(k => typeof window[k] === 'function')"
        )
        check("反向控制 API 就绪", bool(has_api))
    except Exception as exc:
        check("WS 劫持脚本注入", False, str(exc))

    # ---- 3. 在页面里造一个真实 WebSocket，验证 event 桥 ----
    transport = BrowserIMTransport(session)
    await transport.start()

    try:
        # 用 data: URL 的页面不方便建 WS，改用 httpbin 回显服务不保证可达，
        # 因此这里直接用本地 file:// 页面构造 WebSocket 到不可达地址，
        # 只要能观测到 construct/error/close 事件就说明劫持 + 桥接链路通了。
        await page.evaluate(
            """
            () => {
              try {
                window.__testWs = new WebSocket('wss://127.0.0.1:9/__probe__');
                return true;
              } catch (e) { return false; }
            }
            """
        )
        got_event = False
        deadline = time.time() + 8
        while time.time() < deadline:
            ev = await session.next_event(timeout=1.0)
            if ev is None:
                continue
            if ev.get("type") in ("construct", "error", "close"):
                got_event = True
                break
        check("WS 事件桥接", got_event)
    except Exception as exc:
        check("WS 事件桥接", False, str(exc))

    # ---- 4. 传输层鸭子类型 ----
    try:
        ws = transport  # 直接用 transport 检查
        bws = BrowserWebSocket(transport, name="verify")
        await bws.__aenter__()
        ok_types = hasattr(bws, "send") and hasattr(bws, "closed") and hasattr(bws, "__aiter__")
        check("传输层接口兼容", ok_types, "send/closed/__aiter__")
        await bws.__aexit__(None, None, None)
    except Exception as exc:
        check("传输层接口兼容", False, str(exc))

    # ---- 5. 看门狗计时 ----
    try:
        wd = WsDisconnectWatcher(
            session,
            transport,
            disconnect_grace_seconds=2,
            periodic_refresh_seconds=0,
            silence_refresh_seconds=10**9,
            reload_cooldown_seconds=0,
        )
        wd.on_disconnected("selftest")
        check("看门狗开始计时", wd.is_disconnected)
        wd.on_reconnected()
        check("看门狗取消计时", not wd.is_disconnected)
        await asyncio.sleep(2.5)
        check("看门狗计时被正确取消", not wd.is_disconnected)
    except Exception as exc:
        check("看门狗计时", False, str(exc))

    # ---- 6. cookie 解析/回灌 ----
    try:
        parsed = session._parse_cookies_str("unb=123; cookie2=abc; _m_h5_tk=tk_1")
        check("cookie 解析", len(parsed) == 3, f"{len(parsed)} 条")
    except Exception as exc:
        check("cookie 解析", False, str(exc))

    await transport.stop()
    await session.close()

    logger.info("=" * 60)
    logger.info(f"通过: {len(PASS)}  失败: {len(FAIL)}")
    if FAIL:
        logger.error(f"失败项: {FAIL}")
        return 1
    return 0


if __name__ == "__main__":
    rc = asyncio.run(main())
    sys.exit(rc)
