"""验证 `_m_h5_tk` 握手修复：用本地已登录的 cookie 实测。

背景：扫码成功后 cookie 里没有 `_m_h5_tk`，导致所有 mtop 调用报
FAIL_SYS_TOKEN_EMPTY、IM 通道注册不上。本脚本验证 _bootstrap_mtop_token
能否在真实环境把 `_m_h5_tk` 拿下来，并进一步换到 IM token。
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loguru import logger

from db_manager import db_manager
from utils.browser_im import BrowserIMSession
from utils.browser_im.browser_qr_login import BrowserQRLogin


async def main():
    print("=" * 62)
    print("_m_h5_tk 握手修复验证")
    print("=" * 62)

    rows = db_manager.get_all_cookies()
    if not rows:
        print("[X] 本地库没有账号")
        return 1
    cid = list(rows.keys())[0]
    cookie_str = rows[cid]
    print(f"账号: {cid}  cookie 长度: {len(cookie_str)}")

    has_tk_before = "_m_h5_tk" in cookie_str
    print(f"握手前有 _m_h5_tk: {has_tk_before}")

    sess = BrowserIMSession(
        cookie_id=f"__tk_test_{cid}",
        cookies_str=cookie_str,
        headless=True,
        navigate_home=False,
        executable_path=None,
    )
    rc = 0
    try:
        await sess.start()
        page = await sess._ensure_page()

        # 打开主站，让 cookie 域生效
        await page.goto("https://www.goofish.com/", wait_until="domcontentloaded", timeout=45000)
        await asyncio.sleep(1.0)

        qr = BrowserQRLogin(sess, timeout_seconds=30, login_urls=())
        qr._page = page
        qr._owns_page = False

        print("\n[1] 执行 _m_h5_tk 握手 ...")
        ok = await qr._bootstrap_mtop_token()
        print(f"    握手结果: {'成功' if ok else '失败'}")

        cookies = await sess.dump_cookies()
        names = {c.get("name") for c in cookies}
        print(f"[2] 握手后字段: _m_h5_tk={'有' if '_m_h5_tk' in names else '无'}, "
              f"_m_h5_tk_enc={'有' if '_m_h5_tk_enc' in names else '无'}")

        fresh = await sess.cookies_to_string()

        if "_m_h5_tk" in names:
            print("\n[3] 尝试换取 IM token ...")
            info = await sess.get_im_token(device_id="")
            print(f"    ok={info.get('ok')} reason={info.get('reason')} ret={info.get('ret')}")
            if info.get("ok"):
                print("    >>> 全链路打通：_m_h5_tk → IM token")
            else:
                print("    >>> _m_h5_tk 已拿到，但 IM token 未取到（账号可能仍需完整登录）")
            rc = 0
        else:
            print("\n    >>> 握手仍失败，需要排查 h5api 是否可达")
            rc = 1

        # 对比 cookie 变化
        print(f"\n[4] cookie 长度: {len(cookie_str)} -> {len(fresh)}")
    finally:
        try:
            await sess.close()
        except Exception:
            pass

    print("=" * 62)
    return rc


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
