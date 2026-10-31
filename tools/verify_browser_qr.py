"""浏览器内扫码登录自检。

验证「扫码也在常驻浏览器里完成」这条链路：

    1. 二维码元素能被定位并截成 base64
    2. 登录前状态是 waiting，不误判成功
    3. cookie 出现 unb + cookie2 后判定 success 并带出 cookie 串
    4. 超时判定为 expired

运行：
    venv\\Scripts\\python.exe tools\\verify_browser_qr.py
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loguru import logger

from utils.browser_im import BrowserIMSession
from utils.browser_im.browser_qr_login import BrowserQRLogin

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    logger.info(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")


# 模拟登录页：含一个二维码 canvas，扫码后由脚本写入登录 cookie
FAKE_QR_HTML = """
<!doctype html><html><head><meta charset="utf-8"><style>
body{font-family:sans-serif;padding:30px}
#login-qrcode{width:180px;height:180px;background:#fff;border:1px solid #ccc}
</style></head><body>
<div>打开闲鱼App扫一扫</div>
<canvas id="login-qrcode" width="180" height="180"></canvas>
<script>
(function(){
  const c = document.getElementById('login-qrcode');
  const g = c.getContext('2d');
  g.fillStyle = '#fff'; g.fillRect(0,0,180,180);
  // 画个像二维码的方块阵
  g.fillStyle = '#000';
  for (let y=0;y<18;y++){
    for (let x=0;x<18;x++){
      if ((x*7+y*3)%5 < 2) g.fillRect(x*10, y*10, 9, 9);
    }
  }
  window.__markScanned = function(){
    document.body.insertAdjacentHTML('beforeend','<div>请在手机上确认登录</div>');
  };
})();
</script></body></html>
"""


async def main() -> int:
    session = BrowserIMSession(cookie_id="__qr_test__", cookies_str="", headless=True)
    try:
        await session.start()
    except Exception as exc:
        check("浏览器启动", False, str(exc))
        return 1

    page = await session._ensure_page()
    await page.set_content(FAKE_QR_HTML, wait_until="domcontentloaded")
    await asyncio.sleep(0.3)

    qr = BrowserQRLogin(session, timeout_seconds=6, login_urls=())

    # ---- 1. 二维码截取 ----
    try:
        res = await qr.start()
        check("二维码截取成功", bool(res.get("success")), res.get("message", ""))
        img = res.get("image") or ""
        check("返回 data URL", img.startswith("data:image/png;base64,"), f"长度={len(img)}")
        import base64
        raw = base64.b64decode(img.split(",", 1)[1]) if "," in img else b""
        check("PNG 有效", raw[:8] == b"\x89PNG\r\n\x1a\n", f"{len(raw)} 字节")
        check("初始状态 waiting", res.get("status") == "waiting")
    except Exception as exc:
        check("二维码截取成功", False, str(exc))

    # ---- 2. 未登录不误判 ----
    try:
        r = await qr.check()
        check("未登录不误判", r.get("status") in ("waiting", "scanned"), f"status={r.get('status')}")
    except Exception as exc:
        check("未登录不误判", False, str(exc))

    # ---- 3. 扫码确认后判定成功 ----
    try:
        await page.evaluate("() => window.__markScanned && window.__markScanned()")
        await asyncio.sleep(0.2)
        await session._context.add_cookies([
            {"name": "unb", "value": "987654", "domain": ".goofish.com", "path": "/"},
            {"name": "cookie2", "value": "c2value", "domain": ".goofish.com", "path": "/"},
            {"name": "_m_h5_tk", "value": "tk9_1700000000000", "domain": ".goofish.com", "path": "/"},
        ])
        r = await qr.check()
        check("登录后判定成功", r.get("status") == "success", f"status={r.get('status')}")
        ck = r.get("cookies") or ""
        check("带出 cookie 串", "unb=" in ck and "cookie2=" in ck, ck[:50])
    except Exception as exc:
        check("登录后判定成功", False, str(exc))

    # ---- 4. 超时判定 ----
    try:
        await session._context.clear_cookies()
        qr2 = BrowserQRLogin(session, timeout_seconds=1.2, login_urls=())
        s2 = await qr2.start()
        check("第二个会话二维码正常", bool(s2.get("success")), s2.get("message", ""))
        await asyncio.sleep(1.4)
        r2 = await qr2.check()
        check("超时判定 expired", r2.get("status") == "expired", f"status={r2.get('status')}")
    except Exception as exc:
        check("超时判定 expired", False, str(exc))

    try:
        await qr.cancel()
    except Exception:
        pass
    await session.close()

    logger.info("=" * 60)
    logger.info(f"通过: {len(PASS)}  失败: {len(FAIL)}")
    if FAIL:
        logger.error(f"失败项: {FAIL}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
