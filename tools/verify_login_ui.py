"""验证登录页 tab 合并后的行为。

检查：
  1. 顶层只有 2 个 tab（用户名/密码、邮箱登录）
  2. 选「邮箱登录」后出现内部的「密码登录 / 验证码登录」切换
  3. 切到「验证码登录」时显示验证码表单、隐藏密码表单
  4. 无 JS 报错
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.browser_im import BrowserIMSession

PORT = os.getenv('FISHCLOUD_LOCAL_PORT', '18795')
URL = f'http://127.0.0.1:{PORT}/login.html'

ok_all = []


def check(name, ok, detail=''):
    ok_all.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name} {detail}")


async def main():
    sess = BrowserIMSession('__login_ui_test__', '', headless=True, navigate_home=False)
    errors = []
    try:
        await sess.start()
        page = await sess._ensure_page()
        page.on('pageerror', lambda e: errors.append(str(e)))

        await page.goto(URL, wait_until='domcontentloaded', timeout=30000)
        await asyncio.sleep(1.0)

        # 1) 顶层 tab 数
        tabs = await page.evaluate(
            "() => Array.from(document.querySelectorAll('input[name=\"loginType\"]')).map(e => e.value)"
        )
        check('顶层 tab 只有 2 个', len(tabs) == 2, str(tabs))
        check('包含邮箱登录 tab', 'email-password' in (tabs or []))

        # 2) 切到邮箱登录
        await page.click('label[for="emailLogin"]')
        await asyncio.sleep(0.6)
        wrap_vis = await page.evaluate(
            "() => { const e = document.getElementById('emailLoginWrap'); return e ? getComputedStyle(e).display : 'missing'; }"
        )
        check('邮箱容器已显示', wrap_vis != 'none' and wrap_vis != 'missing', f'display={wrap_vis}')

        mode_tabs = await page.evaluate(
            "() => Array.from(document.querySelectorAll('input[name=\"emailMode\"]')).map(e => e.value)"
        )
        check('出现密码/验证码切换', mode_tabs == ['password', 'code'], str(mode_tabs))

        # 3) 默认应为密码表单
        vis = await page.evaluate(
            """() => ({
                pw: getComputedStyle(document.getElementById('emailPasswordLoginForm')).display,
                code: getComputedStyle(document.getElementById('emailCodeLoginForm')).display
            })"""
        )
        check('默认显示密码表单', vis['pw'] != 'none', str(vis))
        check('默认隐藏验证码表单', vis['code'] == 'none', str(vis))

        # 4) 切到验证码登录
        await page.click('label[for="emailModeCode"]')
        await asyncio.sleep(0.6)
        vis2 = await page.evaluate(
            """() => ({
                pw: getComputedStyle(document.getElementById('emailPasswordLoginForm')).display,
                code: getComputedStyle(document.getElementById('emailCodeLoginForm')).display,
                wrap: getComputedStyle(document.getElementById('emailLoginWrap')).display
            })"""
        )
        check('切到验证码后显示验证码表单', vis2['code'] != 'none', str(vis2))
        check('切到验证码后隐藏密码表单', vis2['pw'] == 'none', str(vis2))
        check('邮箱容器仍可见', vis2['wrap'] != 'none', str(vis2))

        # 5) 切回用户名 tab
        await page.click('label[for="usernameLogin"]')
        await asyncio.sleep(0.5)
        vis3 = await page.evaluate(
            """() => ({
                user: getComputedStyle(document.getElementById('usernameLoginForm')).display,
                wrap: getComputedStyle(document.getElementById('emailLoginWrap')).display
            })"""
        )
        check('切回用户名表单正常', vis3['user'] != 'none' and vis3['wrap'] == 'none', str(vis3))

        # 6) JS 错误
        check('无 JS 报错', len(errors) == 0, str(errors[:2]))
    finally:
        try:
            await sess.close()
        except Exception:
            pass

    print('=' * 50)
    print(f"通过 {sum(ok_all)}/{len(ok_all)}")
    return 0 if all(ok_all) else 1


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
