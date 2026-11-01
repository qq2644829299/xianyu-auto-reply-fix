# -*- coding: utf-8 -*-
"""验证临时直连兜底：不依赖主通道/浏览器，直接拿到会话列表与历史消息。"""
import asyncio
import os
import sqlite3
import sys

REPO = r'D:\deepseek-workSpace\xianyu-auto-reply-fix-main\xianyu-auto-reply-fix-main'
sys.path.insert(0, REPO)
os.chdir(REPO)
os.environ.setdefault('DEPLOY_MODE', 'desktop')

DB = os.path.join(os.environ['APPDATA'], 'FishCloudLocal', 'data', 'xianyu_data.db')
COOKIE_ID = '3823821587'


def load_cookie(cid):
    """Cookie 在库里是加密存的，必须走 db_manager 解密。"""
    from db_manager import db_manager
    value = db_manager.get_cookie(cid)
    return value or ''


async def main():
    cookies = load_cookie(COOKIE_ID)
    print('Cookie 长度:', len(cookies), ' 含 unb:', 'unb=' in cookies)
    if not cookies:
        return

    from XianyuAutoAsync import XianyuLive

    live = XianyuLive(
        cookies_str=cookies,
        cookie_id=COOKIE_ID,
        user_id=1,
        register_instance=False,
    )

    print('--- 1. 临时连接拉会话列表 ---')
    body = await live._request_lwp_via_temp_connection(
        '/r/Conversation/listNewestPagination',
        [9007199254740991, 20],
        timeout=20,
    )
    convs = body.get('userConvs') or []
    print('  code =', body.get('code'), ' count =', len(convs),
          ' hasMore =', body.get('hasMore'), ' nextCursor =', body.get('nextCursor'))
    print('  _lwp_body_failed =', live._lwp_body_failed(body))

    if convs:
        first = convs[0]
        print('  首条会话字段:', list(first.keys())[:12])
        cid = first.get('conversationId') or first.get('cid') or ''
        print('  conversationId =', cid)

        print('--- 2. 临时连接拉历史消息 ---')
        full_cid = cid if '@goofish' in cid else f'{cid}@goofish'
        mbody = await live._request_lwp_via_temp_connection(
            '/r/MessageManager/listUserMessages',
            [full_cid, False, 9007199254740991, 20, False],
            timeout=20,
        )
        msgs = mbody.get('userMessageModels') or []
        print('  code =', mbody.get('code'), ' count =', len(msgs),
              ' hasMore =', mbody.get('hasMore'), ' nextCursor =', mbody.get('nextCursor'))
        print('  _lwp_body_failed =', live._lwp_body_failed(mbody))

    print('--- 3. 失败判定自测 ---')
    samples = [
        ({}, True),
        ({'code': 'timeout'}, True),
        ({'code': 'not_connected'}, True),
        ({'code': 'temp_failed'}, True),
        ({'reason': 'x'}, True),
        ({'userConvs': [1]}, False),
        ({'userMessageModels': [1]}, False),
    ]
    for sample, expect in samples:
        got = live._lwp_body_failed(sample)
        flag = 'OK' if got == expect else 'FAIL'
        print('  {:>28} -> {} ({})'.format(str(sample)[:28], got, flag))


if __name__ == '__main__':
    asyncio.run(main())
