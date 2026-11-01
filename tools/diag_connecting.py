"""诊断「在线客服一直连接中」。

要拿到三样东西才能定位：
  1. 前端读的 connection_state（来自 /cookies/{id}/runtime-status）
  2. browser_im 传输层的 state（transport.stats）
  3. 页面里 __xyWsState() 的真实 readyState
三者对不上时，才知道是传输层判定错，还是页面里确实没建 WS。
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aiohttp

PORT = os.getenv('FISHCLOUD_LOCAL_PORT', '18799')
BASE = f'http://127.0.0.1:{PORT}'


async def main():
    print('=' * 62)
    print('「连接中」诊断')
    print('=' * 62)

    async with aiohttp.ClientSession() as s:
        # 1) runtime-status
        try:
            async with s.get(f'{BASE}/cookies/3823821587/runtime-status', timeout=15) as r:
                data = await r.json()
            print('\n[1] /cookies/3823821587/runtime-status')
            for k in ('connection_state', 'ws_ready', 'ws_transport_ready',
                      'is_running', 'login_status', 'connection_message'):
                if k in data:
                    print(f'    {k} = {data.get(k)}')
            extra = {k: v for k, v in data.items()
                     if k not in ('connection_state', 'ws_ready', 'ws_transport_ready',
                                  'is_running', 'login_status', 'connection_message')}
            if extra:
                print('    其它字段:', json.dumps(extra, ensure_ascii=False)[:300])
        except Exception as exc:
            print('[1] runtime-status 失败:', exc)

        # 2) browser-im status
        try:
            async with s.get(f'{BASE}/api/browser-im/status', timeout=15) as r:
                bi = await r.json()
            print('\n[2] /api/browser-im/status')
            rt = bi.get('runtime') or {}
            print('    available_memory_mb =', rt.get('available_memory_mb'))
            print('    active =', rt.get('active'))
            for cid, acc in (rt.get('accounts') or {}).items():
                print(f'    账号 {cid}:')
                print('      ready     =', acc.get('ready'))
                print('      transport =', json.dumps(acc.get('transport'), ensure_ascii=False))
                print('      watchdog  =', json.dumps(acc.get('watchdog'), ensure_ascii=False))
        except Exception as exc:
            print('[2] browser-im status 失败:', exc)

        # 3) 实例内部状态
        try:
            from XianyuAutoAsync import XianyuLive
            inst = XianyuLive.get_instance('3823821587')
            print('\n[3] XianyuLive 实例内部')
            if inst is None:
                print('    实例不存在')
            else:
                cs = getattr(inst, 'connection_state', None)
                print('    connection_state =', getattr(cs, 'value', cs))
                print('    current_token    =', bool(getattr(inst, 'current_token', None)))
                print('    ws is None       =', getattr(inst, 'ws', None) is None)
                ws = getattr(inst, 'ws', None)
                if ws is not None:
                    print('    ws.closed        =', getattr(ws, 'closed', '?'))
                    print('    ws 类型          =', type(ws).__name__)
                print('    heartbeat_task   =', getattr(inst, 'heartbeat_task', None))
        except Exception as exc:
            print('[3] 实例检查失败:', exc)

    print('\n' + '=' * 62)


if __name__ == '__main__':
    asyncio.run(main())
