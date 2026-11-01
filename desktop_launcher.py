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
    # 浏览器默认**无头**：有头会额外弹出一个可见的 Edge 窗口，用户会以为
    # 客户端"又开了一个浏览器"。桌面版靠页内自动滑块处理验证；确实需要人工
    # 介入时，把下面这行改成 '0'（或配置文件 headless: false）即可看到窗口。
    os.environ.setdefault('XY_BROWSER_HEADLESS', '1')
    # 浏览器 profile 落到用户目录，升级/重装不丢登录态。
    os.environ.setdefault('FISHCLOUD_BROWSER_DATA', str(browser_root))

    if str(app_root) not in sys.path:
        sys.path.insert(0, str(app_root))
    return data_root


def _ensure_std_streams(data_root: Path) -> None:
    """--windowed 打包后 sys.stdout / sys.stderr 是 None。

    uvicorn 配置日志 formatter 时会去拿 stderr，None 直接抛
    "Unable to configure formatter 'default'"，服务根本起不来
    （表现就是端口通了但 HTTP 无响应）。这里把它们指向日志文件。
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        log_dir = data_root / 'logs'
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = open(log_dir / 'console.log', 'a', encoding='utf-8', errors='ignore')
        if sys.stdout is None:
            sys.stdout = log_file
        if sys.stderr is None:
            sys.stderr = log_file
    except Exception:
        pass


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

      // 桌面版是单用户本机应用，不需要系统账号这一层：
      // 登录页/注册页/用户管理/登出全部隐藏
      //（闲鱼账号的登录由浏览器内扫码/密码自动登录承担）。
      function hideAccountUi() {
        function hideBox(el) {
          var box = el.closest('.nav-item') || el.closest('li') || el;
          if (box) box.style.display = 'none';
        }
        // 1) 导航项：按文本匹配
        document.querySelectorAll('.nav-item, .nav-link, a, button').forEach(function(el){
          var t = (el.textContent || '').trim();
          if (t === '用户管理' || t === '登出' || t === '退出登录' ||
              t === '登录' || t === '注册' || t === '退出') {
            hideBox(el);
          }
        });
        // 2) onclick 里带 logout 的按钮
        document.querySelectorAll('[onclick*="logout"]').forEach(hideBox);
        // 3) 指向登录/注册页的链接
        document.querySelectorAll('a[href*="login.html"], a[href*="register.html"]').forEach(hideBox);
        // 4) 用户管理内容区（id 用连字符）
        ['user-management-section', 'userManagementSection'].forEach(function(id){
          var el = document.getElementById(id);
          if (el) el.style.display = 'none';
        });
        // 5) 按 data-menu-id 兜底
        document.querySelectorAll('[data-menu-id="user-management"]').forEach(function(el){
          el.style.display = 'none';
        });
      }
      hideAccountUi();
      // 面板是 SPA 式渲染，DOM 变化后再兜一次
      try {
        new MutationObserver(function(){ hideAccountUi(); })
          .observe(document.body, { childList: true, subtree: true });
      } catch (e) {}

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


def _patch_playwright_browser_path() -> None:
    """让**所有** Playwright 调用都使用系统浏览器（Edge/Chrome）。

    打包成 exe 后没有 Playwright 自带的 Chromium（PyInstaller 打不进
    chrome-headless-shell），项目里原有的这些链路会直接抛
    "Executable doesn't exist at ...\\playwright\\driver\\...\\chrome-headless-shell.exe"：
      - XianyuAutoAsync._refresh_cookies_via_browser（Cookie 刷新）
      - utils/order_detail_fetcher（订单详情抓取）
      - utils/xianyu_slider_stealth（滑块）
      - utils/persistent_browser（常驻浏览器）

    之前只有 browser_im 自己做了浏览器探测，其它调用仍在找自带 Chromium。
    这里在进程级别给 BrowserType.launch / launch_persistent_context 注入
    executable_path，一次性覆盖所有调用点。
    """
    try:
        from utils.browser_im.browser_session import pick_browser_executable
        exe = pick_browser_executable()
    except Exception as exc:
        print(f'[WARN] 探测系统浏览器失败: {exc}')
        return
    if not exe:
        print('[WARN] 未找到系统浏览器（Edge/Chrome），部分功能可能不可用')
        return

    patched = []

    def _wrap(cls, label: str) -> None:
        for name in ('launch', 'launch_persistent_context'):
            orig = getattr(cls, name, None)
            if orig is None or getattr(orig, '__fc_patched__', False):
                continue

            def make(orig_fn):
                def wrapper(self, *args, **kwargs):
                    kwargs.setdefault('executable_path', exe)
                    # 关键：默认无头，否则订单抓取 / Cookie 刷新会各拉一个
                    # **可见的** Edge 浏览器窗口出来（用户看到的"多开一个浏览器"
                    # 就是这些进程）。需要人眼介入的滑块会显式传 headless=False。
                    kwargs.setdefault('headless', True)
                    return orig_fn(self, *args, **kwargs)

                wrapper.__fc_patched__ = True
                return wrapper

            try:
                setattr(cls, name, make(orig))
                patched.append(f'{label}.{name}')
            except Exception:
                pass

    try:
        from playwright.async_api import BrowserType as AsyncBrowserType
        _wrap(AsyncBrowserType, 'async')
    except Exception:
        pass
    try:
        from playwright.sync_api import BrowserType as SyncBrowserType
        _wrap(SyncBrowserType, 'sync')
    except Exception:
        pass

    if patched:
        print(f'[INFO] 已统一 Playwright 浏览器路径 -> {exe}')
    else:
        print('[WARN] Playwright 浏览器路径注入未生效')


def _show_fatal_error(message: str, data_root: Path = None) -> None:
    """原生错误提示框（不依赖浏览器）。

    绝不再用 webbrowser.open 兜底 —— 那会让用户看到一个浏览器窗口，
    误以为这个客户端只是个网页壳子，也破坏了桌面应用的观感。
    """
    detail = message
    if data_root is not None:
        detail += f'\n\n日志目录：{data_root / "logs"}'
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror('鱼智云本地客户端', detail)
        root.destroy()
        return
    except Exception:
        pass
    # 最后手段：写文件，避免静默失败
    try:
        if data_root is not None:
            p = data_root / 'logs' / 'desktop_launcher_error.txt'
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(detail, encoding='utf-8')
    except Exception:
        pass


def main() -> None:
    data_root = prepare_local_runtime()
    _ensure_std_streams(data_root)
    _patch_playwright_browser_path()

    # 1) 后端放子线程（Start.py 内部是 asyncio 永久等待，天然适合当服务）
    threading.Thread(target=_run_backend, daemon=True, name='fishcloud-backend').start()

    # 2) 等后端就绪（首次启动要初始化数据库与依赖）
    ready = wait_for_local_server(LOCAL_PORT, timeout=120)
    _BACKEND['ready'] = ready
    # 直接进管理面板：桌面版不需要系统账号登录这一层
    #（'/' 是给搜索引擎看的营销官网，/login.html 是系统账号登录页，都不该出现在客户端里）
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
        _show_fatal_error(
            f'原生窗口启动失败：{type(exc).__name__}: {exc}\n'
            f'请确认已安装 Microsoft Edge WebView2 运行时。',
            data_root,
        )
        return

    # 4) 走到这里说明既没有窗口也没有可用的兜底 —— 明确报错而不是静默打开浏览器
    _show_fatal_error('本地服务未能就绪，窗口无法显示。', data_root)


if __name__ == '__main__':
    main()


