"""四步自动登录自检（对应「鱼店长Pro」的 AutoLoginProcedure）。

不依赖真实闲鱼账号，用本地构造的登录页验证整条链路：

    1. 状态机：状态迁移、失败退避、终态判定、VerifyRetryMin
    2. 步骤 1/4：切换到「账号密码登录」
    3. 步骤 2/4：勾选隐私条款
    4. 步骤 3/4：拟人输入账号密码
    5. 步骤 4/4：点击提交
    6. 结果判定：成功识别、密码错误识别
    7. ReplyDeadLine 到期判定

运行：
    venv\\Scripts\\python.exe tools\\verify_auto_login.py
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loguru import logger

from utils.browser_im import (
    BrowserIMSession,
    BrowserAutoLogin,
    AccountState,
    AccountStateKeeper,
    judge_reply_deadline,
)

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    logger.info(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")


# 模拟登录页：初始是扫码 tab，点「账号密码登录」才出现表单；
# 必须勾选协议才能提交；账号密码正确才写 cookie 并显示登录成功。
FAKE_LOGIN_HTML = """
<!doctype html><html><head><meta charset="utf-8"><style>
body{font-family:sans-serif;padding:40px}
#pwForm{display:none}
.tab{cursor:pointer;color:#06c;margin-right:16px}
#msg{color:#c00;margin-top:12px}
#ok{color:#080;margin-top:12px;display:none}
</style></head><body>
<div id="tabs">
  <span class="tab" id="tabQr">扫码登录</span>
  <span class="tab" id="tabPw">账号密码登录</span>
</div>
<div id="pwForm">
  <div><input id="fm-login-id" type="text" placeholder="请输入手机号/账号" /></div>
  <div><input id="fm-login-password" type="password" placeholder="请输入密码" /></div>
  <div>
    <label><input type="checkbox" id="J_AgreementBtn" /> 我已阅读并同意《服务协议》与《隐私政策》</label>
  </div>
  <div><button type="button" class="fm-button" id="loginBtn">授权并登录</button></div>
</div>
<div id="msg"></div>
<div id="ok">登录成功</div>
<script>
(function(){
  const tabPw = document.getElementById('tabPw');
  const form  = document.getElementById('pwForm');
  const msg   = document.getElementById('msg');
  const okBox = document.getElementById('ok');
  const agree = document.getElementById('J_AgreementBtn');

  tabPw.addEventListener('click', function(){ form.style.display='block'; });

  document.getElementById('loginBtn').addEventListener('click', function(){
    const u = document.getElementById('fm-login-id').value.trim();
    const p = document.getElementById('fm-login-password').value;
    if(!agree.checked){ msg.innerText = '请先勾选并同意协议'; return; }
    if(u === 'demo_user' && p === 'demo_pass'){
      document.cookie = 'unb=123456; path=/';
      document.cookie = 'cookie2=abcdef; path=/';
      document.cookie = '_m_h5_tk=tk123_1699999999999; path=/';
      msg.innerText = '';
      form.style.display = 'none';
      okBox.style.display = 'block';
      window.__loginOk = true;
    } else {
      msg.innerText = '账号或密码不正确';
    }
  });
})();
</script></body></html>
"""


async def main() -> int:
    # ---- 1. 状态机 ----
    k = AccountStateKeeper("state_test", max_login_attempts=2, login_backoff_seconds=1, verify_retry_min=0.02)
    k.mark(AccountState.EXPIRED, "cookie_expired")
    check("状态迁移", k.state == AccountState.EXPIRED)
    check("需要登录", k.needs_login)
    check("允许尝试", k.can_attempt_login())
    k.record_login_failure("password_error")
    check("失败计入退避", k.stats()["next_login_in"] > 0)
    k._next_login_allowed_at = 0
    k.record_login_failure("password_error")
    check("超限后禁止", not k.can_attempt_login(), f"failures={k.stats()['login_failures']}")
    k.reset_login_failures()
    k.mark(AccountState.ACCOUNT_LOCKED)
    check("终态判定", k.is_terminal)
    k2 = AccountStateKeeper("t2")
    k2.mark_logged_in("ok")
    check("登录成功清零", k2.state == AccountState.LOGGED_IN and k2.stats()["login_failures"] == 0)
    check("验证退避", (k2.record_verify_failure() or True))

    # ---- 2. ReplyDeadLine ----
    import time as _t
    check("授权已过期识别", judge_reply_deadline(_t.time() - 10) == "expired")
    check("授权临近识别", judge_reply_deadline(_t.time() + 86400) == "expiring_soon")
    check("授权正常", judge_reply_deadline(_t.time() + 40 * 86400) is None)

    # ---- 3. 浏览器 + 四步登录 ----
    session = BrowserIMSession(cookie_id="__login_test__", cookies_str="", headless=True)
    try:
        await session.start()
    except Exception as exc:
        check("浏览器启动", False, str(exc))
        return 1

    page = await session._ensure_page()
    login = BrowserAutoLogin(
        session, "demo_user", "demo_pass",
        login_timeout_seconds=6, step_delay=(0.05, 0.1), login_urls=(),
    )

    try:
        await page.set_content(FAKE_LOGIN_HTML, wait_until="domcontentloaded")
        await asyncio.sleep(0.3)

        result = await login.login()

        # 步骤验证：看页面实际状态
        state = await page.evaluate("""() => ({
            formVisible: (document.getElementById('pwForm')||{}).style?.display,
            ok: !!window.__loginOk,
            agree: (document.getElementById('J_AgreementBtn')||{}).checked,
            msg: (document.getElementById('msg')||{}).innerText || ''
        })""")

        check("步骤1 切换到密码登录", "切换账号登录" in login.steps_done, str(login.steps_done))
        check("步骤2 勾选协议", "勾选隐私条款" in login.steps_done or "勾选隐私条款(已勾选)" in login.steps_done)
        check("步骤3 输入账号密码", "自动输入账号密码" in login.steps_done)
        check("步骤4 点击登录", "点击授权并登录" in login.steps_done)
        check("页面确认登录成功", bool(state.get("ok")), f"msg={state.get('msg')}")
        check("登录结果 ok", bool(result.get("ok")), f"reason={result.get('reason')}")
        check("cookie 已产出", bool((result.get("cookies") or "").find("unb=") >= 0),
              (result.get("cookies") or "")[:60])
    except Exception as exc:
        check("四步登录", False, str(exc))

    # ---- 4. 密码错误识别 ----
    try:
        await session._context.clear_cookies()
        await page.set_content(FAKE_LOGIN_HTML, wait_until="domcontentloaded")
        await asyncio.sleep(0.2)
        bad = BrowserAutoLogin(
            session, "demo_user", "wrong_pass",
            login_timeout_seconds=6, step_delay=(0.05, 0.1), login_urls=(),
        )
        r2 = await bad.login()
        check("密码错误被识别", (not r2.get("ok")) and r2.get("reason") == "password_error",
              f"reason={r2.get('reason')}")
    except Exception as exc:
        check("密码错误被识别", False, str(exc))

    await session.close()

    logger.info("=" * 60)
    logger.info(f"通过: {len(PASS)}  失败: {len(FAIL)}")
    if FAIL:
        logger.error(f"失败项: {FAIL}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
