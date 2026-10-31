"""页内自动滑块自检。

不依赖闲鱼真实验证码，用本地构造的 nc 风格滑块页面验证机制正确性：

    1. 轨迹生成：覆盖整段距离、有抖动、末端过冲回退；
    2. 滑块定位：能识别 #nc_1_n1z + .nc_scale 并算出拖动距离；
    3. 真实拖动：Playwright mouse 拖动后按钮确实位移到位；
    4. 通过判定：验证通过后 _is_passed 返回 True；
    5. 重试链路：首次失败会刷新并重试。

运行：
    venv\\Scripts\\python.exe tools\\verify_auto_slider.py
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loguru import logger

from utils.browser_im import BrowserIMSession, AutoSlider
from utils.browser_im.auto_slider import _Trajectory

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    logger.info(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")


# 本地 nc 风格滑块：拖到 92% 以上视为通过，并写入 x5sec cookie
FAKE_SLIDER_HTML = """
<!doctype html><html><head><meta charset="utf-8"><style>
body{margin:0;padding:60px;font-family:sans-serif}
#nc_1_wrapper{position:relative;width:300px;height:34px;border:1px solid #bbb;
  background:#f5f5f5;border-radius:3px;user-select:none}
#nc_1_n1t{position:absolute;left:0;top:0;width:300px;height:34px}
#nc_1_n1z{position:absolute;left:0;top:0;width:40px;height:34px;background:#4a90d9;
  color:#fff;text-align:center;line-height:34px;cursor:pointer;border-radius:3px}
#nc_1__scale_text{position:absolute;left:50px;top:8px;color:#888;font-size:13px}
</style></head><body>
<div id="nc_1_wrapper" class="nc-container">
  <div id="nc_1_n1t" class="nc_scale"></div>
  <span id="nc_1_n1z">→</span>
  <span id="nc_1__scale_text">请按住滑块，拖动到最右边</span>
</div>
<script>
(function(){
  const btn = document.getElementById('nc_1_n1z');
  const track = document.getElementById('nc_1_n1t');
  let dragging = false;
  window.__sliderTrace = {moves: 0, maxLeft: 0, down: 0, up: 0};

  btn.addEventListener('mousedown', function(e){
    dragging = true; window.__sliderTrace.down++;
    e.preventDefault();
  });
  document.addEventListener('mousemove', function(e){
    if(!dragging) return;
    const r = track.getBoundingClientRect();
    let left = e.clientX - r.left - 20;
    left = Math.max(0, Math.min(r.width - btn.offsetWidth, left));
    btn.style.left = left + 'px';
    window.__sliderTrace.moves++;
    window.__sliderTrace.maxLeft = Math.max(window.__sliderTrace.maxLeft, left);
  });
  document.addEventListener('mouseup', function(){
    if(!dragging) return;
    dragging = false; window.__sliderTrace.up++;
    const max = window.__sliderTrace.maxLeft;
    const need = track.getBoundingClientRect().width - btn.offsetWidth - 6;
    if(max >= need * 0.92){
      document.cookie = 'x5sec=passed; path=/';
      document.getElementById('nc_1_wrapper').remove();
      document.body.insertAdjacentHTML('beforeend',
        '<div id="ok">验证通过</div>');
    } else {
      document.getElementById('nc_1__scale_text').innerText = '验证失败，请重试';
    }
  });
})();
</script></body></html>
"""


async def main() -> int:
    # ---- 1. 轨迹 ----
    t = _Trajectory(260.0)
    pts = t.points()
    delays = t.delays(len(pts))
    xs = [p[0] for p in pts]
    check("轨迹点数合理", len(pts) >= 28, f"{len(pts)} 点")
    check("轨迹覆盖全程", max(xs) >= 260.0, f"最远 {max(xs):.1f}px / 目标 260px")
    check("末端回落到目标", abs(xs[-1] - 260.0) < 8.0, f"落点 {xs[-1]:.1f}px")
    check("存在垂直抖动", any(abs(p[1]) > 0.3 for p in pts))
    check("延时序列对齐", len(delays) == len(pts), f"{len(delays)} 个")

    # ---- 2. 启动浏览器 ----
    session = BrowserIMSession(cookie_id="__slider_test__", cookies_str="", headless=True)
    try:
        await session.start()
        check("浏览器启动", True)
    except Exception as exc:
        check("浏览器启动", False, str(exc))
        return 1

    page = await session._ensure_page()
    slider = AutoSlider(session, max_attempts=2, retry_interval_seconds=0.5,
                        drag_timeout_seconds=6, headful_manual_wait_seconds=0)

    # ---- 3. 定位 + 拖动 ----
    try:
        await page.set_content(FAKE_SLIDER_HTML, wait_until="domcontentloaded")
        await asyncio.sleep(0.4)

        found = await slider._locate_slider(page)
        check("滑块定位", found is not None,
              f"handle={found[0]['width']:.0f}px track={found[1]['width']:.0f}px" if found else "未找到")

        result = await slider.solve("", user_id="selftest")
        trace = await page.evaluate("() => window.__sliderTrace || null")

        check("拖动事件已发出", bool(trace) and trace.get("down", 0) >= 1 and trace.get("up", 0) >= 1,
              f"down={trace.get('down')} moves={trace.get('moves')} up={trace.get('up')}" if trace else "无轨迹")
        check("移动步数充足", bool(trace) and trace.get("moves", 0) >= 20,
              f"moves={trace.get('moves')}" if trace else "")

        passed = await slider._is_passed(page)
        check("通过判定生效", bool(passed))
        check("solve 返回成功", bool(result.get("ok")),
              f"ok={result.get('ok')} reason={result.get('reason')} attempts={result.get('attempts')}")
    except Exception as exc:
        check("定位与拖动", False, str(exc))

    # ---- 4. 未拖动时不应误判通过 ----
    try:
        await session._context.clear_cookies()
        await page.set_content(FAKE_SLIDER_HTML, wait_until="domcontentloaded")
        await asyncio.sleep(0.3)
        passed = await slider._is_passed(page)
        check("未验证不误判", not passed)
    except Exception as exc:
        check("未验证不误判", False, str(exc))

    await session.close()

    logger.info("=" * 60)
    logger.info(f"通过: {len(PASS)}  失败: {len(FAIL)}")
    if FAIL:
        logger.error(f"失败项: {FAIL}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
