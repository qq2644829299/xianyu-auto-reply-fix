"""鱼智云本地客户端启动器。

该启动器只负责在用户电脑上启动现有服务并打开本地管理页。数据库、账号登录
Cookie 与闲鱼 WebSocket 均存放并运行在本机，不会因为桌面启动而传到云服务器。

界面形态：**原生应用窗口**（pywebview + WebView2），不是浏览器标签页。
对齐鱼店长的 WPF 窗口体验：没有地址栏、没有标签页、关窗即退出。
"""
from __future__ import annotations

import os
import runpy
import socket
import sys
import threading
import time
from pathlib import Path


LOCAL_PORT = int(os.getenv('FISHCLOUD_LOCAL_PORT', '18790'))

# 后端线程状态（给窗口显示错误用）
_BACKEND = {'error': None, 'ready': False}


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


ERROR_HTML = """<!doctype html><html><head><meta charset="utf-8"><style>
body{margin:0;font-family:"Microsoft YaHei",sans-serif;background:#0f172a;color:#e2e8f0;
display:flex;align-items:center;justify-content:center;height:100vh;flex-direction:column;gap:12px;padding:32px;text-align:center}
h1{font-size:20px;margin:0;color:#f87171}p{color:#94a3b8;font-size:13px;margin:0;line-height:1.7}
code{background:#1e293b;padding:2px 6px;border-radius:4px;color:#7dd3fc;font-size:12px}
</style></head><body>
<h1>本地服务启动失败</h1>
<p>后端没有在预期时间内就绪，请查看日志：</p>
<p><code id="logpath"></code></p>
<p>常见原因：端口被占用、首次启动较慢、依赖缺失。</p>
<script>
document.getElementById('logpath').textContent = "%LOGPATH%";
</script>
</body></html>"""


def _run_backend() -> None:
    """在子线程里跑完整后端（Start.py 的 main 会永久阻塞，正好当服务）。"""
    try:
        runpy.run_module('Start', run_name='__main__')
    except SystemExit:
        pass
    except Exception as exc:  # noqa: BLE001
        _BACKEND['error'] = f'{type(exc).__name__}: {exc}'


def _open_fallback_browser(port: int) -> None:
    """原生窗口不可用时的兜底：用系统浏览器打开本地管理页。"""
    try:
        import webbrowser
        webbrowser.open(f'http://127.0.0.1:{port}')
    except Exception:
        pass


def _desktop_css() -> str:
    """读取桌面专用样式（去网页化）。打包后从 _MEIPASS/desktop_ui 读。"""
    candidates = [
        resource_root() / 'desktop_ui' / 'desktop.css',
        Path(__file__).resolve().parent / 'desktop_ui' / 'desktop.css',
    ]
    for p in candidates:
        try:
            if p.is_file():
                return p.read_text(encoding='utf-8')
        except Exception:
            continue
    return ''


def _inject_desktop_look(window) -> None:
    """页面加载完成后注入桌面化样式与行为。

    - 细圆角滚动条、禁选文本、禁图片拖拽、系统字体、去焦点虚线圈
    - 禁用右键菜单与 F5/Ctrl+R 刷新（桌面应用不该有浏览器菜单）
    """
    css = _desktop_css()
    js_css = ''
    if css:
        # 用 JSON 编码避免引号/换行破坏注入
        import json as _json
        js_css = (
            "(function(){var s=document.getElementById('fc-desktop-style')||document.createElement('style');"
            "s.id='fc-desktop-style';s.textContent=" + _json.dumps(css) + ";"
            "document.head.appendChild(s);})();"
        )

    js_behavior = """
    (function(){
      if (window.__fcDesktopHooked) return;
      window.__fcDesktopHooked = true;
      // 右键菜单：输入框内保留，其它位置屏蔽（桌面应用没有"网页右键"）
      document.addEventListener('contextmenu', function(e){
        var t = e.target;
        var editable = t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable);
        if (!editable) e.preventDefault();
      }, true);
      // 屏蔽刷新快捷键，避免误刷新丢失当前页面状态
      document.addEventListener('keydown', function(e){
        var k = (e.key || '').toLowerCase();
        if (k === 'f5' || ((e.ctrlKey || e.metaKey) && k === 'r')) e.preventDefault();
      }, true);
    })();
    """
    try:
        if js_css:
            window.evaluate_js(js_css)
        window.evaluate_js(js_behavior)
    except Exception as exc:
        print(f'[WARN] 注入桌面样式失败（不影响使用）: {exc}')


def main() -> None:
    data_root = prepare_local_runtime()

    # 1) 后端放子线程（Start.py 内部是 asyncio 永久等待，天然适合当服务）
    threading.Thread(target=_run_backend, daemon=True, name='fishcloud-backend').start()

    # 2) 等后端就绪（首次启动要初始化数据库与依赖）
    ready = wait_for_local_server(LOCAL_PORT, timeout=120)
    _BACKEND['ready'] = ready
    # 直接进管理面板：'/' 是给搜索引擎看的营销官网（landing.html），
    # 桌面客户端进去会看到"官网/注册/登录"，非常网页化。
    url = f'http://127.0.0.1:{LOCAL_PORT}/admin'

    # 3) 原生窗口（pywebview + WebView2），不是浏览器标签页
    try:
        import webview

        if ready:
            window = webview.create_window(
                '鱼智云本地客户端',
                url=url,
                width=1280,
                height=820,
                min_size=(1024, 700),
                background_color='#0f172a',
            )
        else:
            log_hint = str(data_root / 'logs').replace('\\', '\\\\')
            window = webview.create_window(
                '鱼智云本地客户端',
                html=ERROR_HTML.replace('%LOGPATH%', log_hint),
                width=900,
                height=520,
                background_color='#0f172a',
            )

        if ready:
            # 页面每次加载完都注入一次（面板有 SPA 式重渲染）
            try:
                window.events.loaded += lambda: _inject_desktop_look(window)
            except Exception:
                pass

        # gui='edgechromium' 强制 WebView2，避免落到 mshtml 等老后端
        webview.start(gui='edgechromium', debug=False)
        return
    except Exception as exc:  # noqa: BLE001
        _BACKEND['window_error'] = f'{type(exc).__name__}: {exc}'
        print(f'[WARN] 原生窗口启动失败，退回系统浏览器: {exc}')

    # 4) 兜底：系统浏览器
    _open_fallback_browser(LOCAL_PORT)
    # 阻止主线程退出，保持后端继续运行
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
