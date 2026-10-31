"""浏览器态 IM 会话（移植自「鱼店长Pro」的 CefSharp + JS 注入方案）。

模块职责
--------
* ``browser_session.BrowserIMSession``  常驻持久化 Chromium，加载闲鱼 IM 页，
  维护真实的 ``_m_h5_tk`` / ``x5sec`` / ``baxia`` 等风控 cookie。
* ``transport.BrowserIMTransport``     把页面里的 IM WebSocket 桥接成
  ``websockets`` 风格的收发通道。
* ``ws_watchdog.WsDisconnectWatcher``  断开 60s 宽限 + 周期 60min 整页重载。
* ``manager``                          按账号管理上述三件套。

设计要旨：**不要在 Python 里直连 wss、不要自己刷 token**。让浏览器维持会话，
Python 只做业务逻辑。这是鱼店长 Pro 长期不出验证码的根本原因。
"""

from .browser_session import BrowserIMSession, BrowserSessionError
from .transport import BrowserIMTransport, BrowserWebSocket, Subscription
from .ws_watchdog import WsDisconnectWatcher
from .auto_slider import AutoSlider
from .auto_login import BrowserAutoLogin, AutoLoginResult
from .browser_qr_login import BrowserQRLogin, BrowserQRLoginManager, qr_login_manager_browser
from .account_state import AccountState, AccountStateKeeper, judge_reply_deadline
from .manager import AccountRuntime, BrowserIMRuntimeManager, runtime_manager

__all__ = [
    "BrowserIMSession",
    "BrowserSessionError",
    "BrowserIMTransport",
    "BrowserWebSocket",
    "Subscription",
    "WsDisconnectWatcher",
    "AutoSlider",
    "BrowserAutoLogin",
    "AutoLoginResult",
    "BrowserQRLogin",
    "BrowserQRLoginManager",
    "qr_login_manager_browser",
    "AccountState",
    "AccountStateKeeper",
    "judge_reply_deadline",
    "AccountRuntime",
    "BrowserIMRuntimeManager",
    "runtime_manager",
]
