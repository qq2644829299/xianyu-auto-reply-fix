"""鱼智云本地客户端启动器。

该启动器只负责在用户电脑上启动现有服务并打开本地管理页。数据库、账号登录
Cookie 与闲鱼 WebSocket 均存放并运行在本机，不会因为桌面启动而传到云服务器。
"""
from __future__ import annotations

import os
import runpy
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path


LOCAL_PORT = int(os.getenv('FISHCLOUD_LOCAL_PORT', '18790'))


def resource_root() -> Path:
    """返回源码目录或打包资源目录。"""
    return Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent)).resolve()


def user_data_root() -> Path:
    """返回当前系统用户可写的专属数据目录。"""
    if sys.platform == 'darwin':
        base = Path.home() / 'Library' / 'Application Support'
    elif sys.platform == 'win32':
        base = Path(os.getenv('APPDATA') or Path.home() / 'AppData' / 'Roaming')
    else:
        base = Path(os.getenv('XDG_DATA_HOME') or Path.home() / '.local' / 'share')
    return base / 'FishCloudLocal'


def wait_for_local_server(port: int, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.25)
    return False


def open_local_console(port: int) -> None:
    if wait_for_local_server(port):
        webbrowser.open(f'http://127.0.0.1:{port}')


def prepare_local_runtime() -> Path:
    app_root = resource_root()
    data_root = user_data_root()
    data_root.mkdir(parents=True, exist_ok=True)
    (data_root / 'data').mkdir(exist_ok=True)
    (data_root / 'logs').mkdir(exist_ok=True)
    browser_root = data_root / 'browser_data' / 'im_sessions'
    browser_root.mkdir(parents=True, exist_ok=True)

    # 已有项目仍有少量相对路径；切换到用户目录能确保其写入本机而不是安装包。
    os.chdir(data_root)
    os.environ['FISHCLOUD_LOCAL_CLIENT'] = '1'
    os.environ['API_HOST'] = '127.0.0.1'
    os.environ['API_PORT'] = str(LOCAL_PORT)
    os.environ['DB_PATH'] = str(data_root / 'data' / 'xianyu_data.db')
    os.environ.setdefault('SQL_LOG_ENABLED', 'false')

    # ---- 形态 B：桌面客户端承担全部浏览器工作 ----
    # desktop 模式允许启用浏览器兜底；server 模式才会禁用。
    os.environ.setdefault('DEPLOY_MODE', 'desktop')
    # 有头运行：验证码/滑块需要人眼确认，且真实窗口的指纹远比无头可信。
    # 用环境变量覆盖打包进 exe 的 global_config.yml，避免每次升级配置被重置。
    os.environ.setdefault('XY_BROWSER_HEADLESS', '0')
    # 浏览器 profile 落到用户目录，升级/重装不丢登录态。
    os.environ.setdefault('FISHCLOUD_BROWSER_DATA', str(browser_root))

    if str(app_root) not in sys.path:
        sys.path.insert(0, str(app_root))
    return data_root


def main() -> None:
    prepare_local_runtime()
    threading.Thread(target=open_local_console, args=(LOCAL_PORT,), daemon=True).start()
    runpy.run_module('Start', run_name='__main__')


if __name__ == '__main__':
    main()
