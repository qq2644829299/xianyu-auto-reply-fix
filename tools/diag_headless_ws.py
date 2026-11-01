"""验证：无头 vs 有头 vs 有头但窗口移出屏幕，哪种能让闲鱼 IM 页面建 WS。

结论决定桌面版该怎么配：
  - 无头若能建 WS，就用无头（无窗口，最干净）
  - 若只有有头能建，就用有头 + --window-position=-32000,-32000
    （窗口存在但用户看不见，既保留真实渲染又不干扰）
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.browser_im.browser_session import BrowserIMSession, pick_browser_executable

COOKIE_ID = '3823821587'


async def try_mode(label: str, headless: bool, extra_args=None):
    print(f'\n--- {label} (headless={headless}) ---')
    sess = BrowserIMSession(COOKIE_ID, cookies_str='', headless=headless, navigate_home=False)
    if extra_args:
        # 通过环境变量传递额外参数（browser_session 会读）
        os.environ['XY_EXTRA_BROWSER_ARGS'] = ' '.join(extra_args)
    try:
        await sess.start()
        page = await sess._ensure_page()
        await page.goto('https://www.goofish.com/im', wait_until='domcontentloaded', timeout=45000)

        # 等最多 40 秒，看页面是否建 WS
        deadline = 40
        for i in range(deadline // 2):
            await asyncio.sleep(2)
            st = await sess.im_ws_state()
            rs = st.get('readyState', -1)
            if st.get('exists') and rs in (0, 1):
                print(f'    [OK] {i*2+2}s 时页面已建 WS: exists={st.get("exists")} readyState={rs}')
                # 再确认 cookie 是否登录态
                cookies = await sess.dump_cookies()
                names = {c.get('name') for c in cookies}
                print(f'    cookie 关键字段: unb={"有" if "unb" in names else "无"} '
                      f'_m_h5_tk={"有" if "_m_h5_tk" in names else "无"}')
                return True
            if i % 5 == 4:
                print(f'    ...{i*2+2}s: exists={st.get("exists")} readyState={rs}')
        print(f'    [FAIL] {deadline}s 内页面未建 WS')
        # 看页面内容线索
        try:
            body = await page.evaluate("() => (document.body?document.body.innerText.slice(0,200):'')")
            url = page.url
            print(f'    URL: {url[:90]}')
            print(f'    正文: {(body or "").replace(chr(10)," ")[:150]}')
        except Exception:
            pass
        return False
    except Exception as exc:
        print(f'    [ERR] {exc}')
        return False
    finally:
        try:
            await sess.close()
        except Exception:
            pass
        os.environ.pop('XY_EXTRA_BROWSER_ARGS', None)


async def main():
    print('=' * 62)
    print('IM 页面建 WS 的条件验证')
    print('  浏览器:', pick_browser_executable())
    print('=' * 62)

    r_headless = await try_mode('无头', True)
    r_headful = await try_mode('有头', False)

    print('\n' + '=' * 62)
    print(f'无头建 WS: {"是" if r_headless else "否"}')
    print(f'有头建 WS: {"是" if r_headful else "否"}')
    print('=' * 62)
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
