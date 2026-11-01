# -*- coding: utf-8 -*-
"""诊断浏览器通道下 listNewestPagination 的真实返回。

目的：
  在线客服只显示几条会话 -> 已确认 remote_sessions=0。
  这里直接在真实页面 WS 上发一次请求，把回包原样打出来，
  判断是「请求没发出去」「回包没被认领」还是「服务端就是返回空」。
"""
import asyncio
import json
import os
import sys
import time
import uuid

REPO = r'D:\deepseek-workSpace\xianyu-auto-reply-fix-main\xianyu-auto-reply-fix-main'
sys.path.insert(0, REPO)
os.chdir(REPO)
os.environ.setdefault('DEPLOY_MODE', 'desktop')

import sqlite3

DB = os.path.join(os.environ['APPDATA'], 'FishCloudLocal', 'data', 'xianyu_data.db')
COOKIE_ID = '3823821587'


def load_cookie(cid: str) -> str:
    conn = sqlite3.connect(DB)
    row = conn.execute('SELECT value FROM cookies WHERE id=?', (cid,)).fetchone()
    conn.close()
    return row[0] if row else ''


def gen_mid() -> str:
    return f'{int(time.time() * 1000)}{uuid.uuid4().hex[:8]}'


async def main() -> None:
    cookies = load_cookie(COOKIE_ID)
    if not cookies:
        print('[FATAL] 本地库没有该账号 Cookie')
        return
    print('Cookie 长度:', len(cookies))

    from utils.browser_im.browser_session import BrowserIMSession

    session = BrowserIMSession(
        COOKIE_ID,
        cookies,
        headless=False,
        navigate_home=True,
    )
    print('启动浏览器...')
    await session.start()

    # 页面 WS 是页面自己连的，冷启动可能要几十秒，这里轮询等它就绪。
    print('等待 IM WS 就绪...')
    ready = False
    last_state = None
    for attempt in range(40):
        state = await session.im_ws_state()
        rs = state.get('readyState')
        if rs != last_state:
            print('  [{:>4.0f}s] readyState={} index={}'.format(attempt * 1.5, rs, state.get('index')))
            last_state = rs
        if rs == 1:
            ready = True
            break
        # 顺便把事件读掉，避免队列阻塞
        await session.next_event(0.05)
        await asyncio.sleep(1.5)
    print('IM WS 就绪:', ready)
    if not ready:
        print('[WARN] 未拿到就绪的 IM WS，后续仍尝试发送')

    # 清掉历史事件，避免看到旧帧
    drained = 0
    while await session.next_event(0.2) is not None:
        drained += 1
    print('清理旧事件:', drained)

    mid = gen_mid()
    req = {
        'lwp': '/r/Conversation/listNewestPagination',
        'headers': {'mid': mid},
        'body': [9007199254740991, 20],
    }
    ok = await session.send_im_raw(json.dumps(req))
    print('发送结果:', ok, 'mid:', mid)

    matched = None
    frames = []
    deadline = time.time() + 15
    while time.time() < deadline:
        ev = await session.next_event(3)
        if ev is None:
            continue
        payload = ev.get('data') or ''
        if not isinstance(payload, str):
            payload = str(payload)
        try:
            data = json.loads(payload)
        except Exception:
            frames.append(('[非JSON]', payload[:120]))
            continue
        lwp = data.get('lwp') or ''
        hmid = str((data.get('headers') or {}).get('mid') or '')
        frames.append((lwp or '(无lwp)', hmid))
        if hmid == mid:
            matched = data
            break

    print('--- 观察到的帧（前 25 条）---')
    for lwp, hmid in frames[:25]:
        print('  {:<45} mid={}'.format(lwp, hmid))

    print('--- 目标回包 ---')
    if matched is None:
        print('  [未收到本 mid 的回包]')
    else:
        body = matched.get('body')
        print('  lwp :', matched.get('lwp'))
        print('  body:', json.dumps(body, ensure_ascii=False)[:600] if isinstance(body, (dict, list)) else str(body)[:600])
        if isinstance(body, dict):
            print('  userConvs 数量:', len(body.get('userConvs') or []))
            print('  hasMore:', body.get('hasMore'), ' nextCursor:', body.get('nextCursor'))
            print('  code:', body.get('code'), ' reason:', body.get('reason'))
            convs = body.get('userConvs') or []
            for c in convs[:5]:
                print('    - sample:', json.dumps(c, ensure_ascii=False)[:220])

    await session.close()


if __name__ == '__main__':
    asyncio.run(main())
