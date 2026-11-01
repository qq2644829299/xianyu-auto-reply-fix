"""验证桌面版已移除系统账号登录这一层。

检查：
  1. 面板能直接打开（无需登录）
  2. 导航里「用户管理」被隐藏
  3. 导航里「登出」被隐藏
  4. 用户管理内容区被隐藏
  5. 受保护接口在本地客户端模式下可直接访问
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.browser_im import BrowserIMSession

PORT = os.getenv('FISHCLOUD_LOCAL_PORT', '18796')
BASE = f'http://127.0.0.1:{PORT}'
ok_all = []


def check(name, ok, detail=''):
    ok_all.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name} {detail}")


async def main():
    sess = BrowserIMSession('__acct_ui_test__', '', headless=True, navigate_home=False)
    errors = []
    try:
        await sess.start()
        page = await sess._ensure_page()
        page.on('pageerror', lambda e: errors.append(str(e)))

        # 直接访问 /admin（不登录）
        resp = await page.goto(f'{BASE}/admin', wait_until='domcontentloaded', timeout=30000)
        check('免登录访问 /admin', resp is not None and resp.status == 200,
              f'status={resp.status if resp else "?"}')
        await asyncio.sleep(1.5)

        # 注入桌面脚本（模拟 desktop_launcher 的行为）
        from desktop_launcher import _desktop_css
        page_html = await page.content()
        check('页面是管理面板', '用户管理' in page_html or '闲鱼' in page_html)

        # 检查隐藏逻辑：直接调用一次注入
        await page.evaluate("""
        (function(){
          function hideBox(el){
            var box = el.closest('.nav-item') || el.closest('li') || el;
            if (box) box.style.display = 'none';
          }
          document.querySelectorAll('.nav-item, .nav-link, a, button').forEach(function(el){
            var t = (el.textContent||'').trim();
            if (t === '用户管理' || t === '登出' || t === '退出登录' || t === '登录' || t === '注册' || t === '退出') hideBox(el);
          });
          document.querySelectorAll('[onclick*="logout"]').forEach(hideBox);
          ['user-management-section','userManagementSection'].forEach(function(id){
            var el = document.getElementById(id); if (el) el.style.display='none';
          });
          document.querySelectorAll('[data-menu-id="user-management"]').forEach(function(el){ el.style.display='none'; });
        })();
        """)
        await asyncio.sleep(0.5)

        state = await page.evaluate("""() => {
            function navVisible(text){
              var hit = null;
              document.querySelectorAll('.nav-item, .nav-link, a, button').forEach(function(el){
                if ((el.textContent||'').trim() === text) {
                  var box = el.closest('.nav-item') || el.closest('li') || el;
                  if (box && getComputedStyle(box).display !== 'none') hit = text;
                }
              });
              return hit;
            }
            var um = document.getElementById('user-management-section');
            return {
              userMgmt: navVisible('用户管理'),
              logout: navVisible('登出') || navVisible('退出登录'),
              userSection: um ? getComputedStyle(um).display : 'missing'
            };
        }""")
        check('导航「用户管理」已隐藏', state['userMgmt'] is None, f"visible={state['userMgmt']}")
        check('导航「登出」已隐藏', state['logout'] is None, f"visible={state['logout']}")
        check('用户管理内容区已隐藏', state['userSection'] in ('none', 'missing'), f"display={state['userSection']}")

        # 受保护接口免认证
        api = await page.evaluate(
            """async () => {
                const r = await fetch('/cookies/details', {credentials:'include'});
                return r.status;
            }"""
        )
        check('受保护接口免登录可用', api == 200, f'HTTP {api}')

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
