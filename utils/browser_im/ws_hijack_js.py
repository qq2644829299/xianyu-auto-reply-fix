"""浏览器内 WebSocket 劫持脚本（鱼店长 Pro `AgisoIdleClient.JsObject.JsWs` 的等价实现）。

设计来源：逆向 `鱼店长Pro` 的 `AgisoIdleClient.dll`。

鱼店长 Pro 的做法（运行时日志 + 类型结构还原）：
    1. 用 CefSharp 内嵌真实 Chromium 打开闲鱼客服页；
    2. 页面自身建立到 `wss://wss-goofish.dingtalk.com/` 的 IM WebSocket；
    3. C# 侧只用 `JsWs.Open/Message/Close/Error/RefreshPage` 这些**回调**接收事件；
    4. 断线由 `WsDisconnectWatcher` 计时 60 秒，超时就 `RefreshPage`；
    5. `MessageWatcher` 只做增量 Tick，不自己发心跳。

因此 Python 侧不需要自己连 WSS、不需要自己维护心跳。本模块负责把浏览器里的
WebSocket 事件桥接回 Python，并提供 `send/close` 反向通道。
"""

# 注入到每个 frame 的执行前脚本。必须用 add_init_script 语义：页面脚本之前运行。
WS_HIJACK_JS = r"""
(() => {
  if (window.__XY_WS_HIJACKED__) { return; }
  window.__XY_WS_HIJACKED__ = true;

  const OrigWS = window.WebSocket;
  if (!OrigWS) { return; }

  window.__XY_WS_SEQ__ = 0;
  window.__XY_WS_REGISTRY__ = Object.create(null);

  const IM_URL_HINTS = [
    'wss-goofish.dingtalk.com',
    'wss-goofish',
    'goofish.dingtalk',
    'wss://'
  ];

  function emit(payload) {
    try {
      if (typeof window.__xyWsEvent === 'function') {
        window.__xyWsEvent(JSON.stringify(payload));
      }
    } catch (e) {
      /* 桥接未就绪时静默丢弃，不能打断页面 */
    }
  }

  function isImUrl(url) {
    if (!url) { return false; }
    const s = String(url);
    for (let i = 0; i < IM_URL_HINTS.length; i++) {
      if (s.indexOf(IM_URL_HINTS[i]) >= 0) { return true; }
    }
    return false;
  }

  function wrap(ws, url) {
    const id = 'ws_' + (++window.__XY_WS_SEQ__);
    const rec = { id: id, url: String(url), ws: ws, createdAt: Date.now() };
    window.__XY_WS_REGISTRY__[id] = rec;

    const tag = { id: id, url: rec.url, im: isImUrl(url) };

    emit(Object.assign({ type: 'construct', state: ws.readyState }, tag));

    ws.addEventListener('open', () => {
      rec.openedAt = Date.now();
      emit(Object.assign({ type: 'open' }, tag));
    });

    ws.addEventListener('message', (ev) => {
      const data = ev && ev.data;
      if (typeof data === 'string') {
        emit(Object.assign({ type: 'message', data: data }, tag));
      } else {
        // 二进制帧（IM 心跳/同步包偶发）标记后由 Python 侧忽略
        emit(Object.assign({ type: 'message', binary: true }, tag));
      }
    });

    ws.addEventListener('close', (ev) => {
      rec.closedAt = Date.now();
      emit(Object.assign({
        type: 'close',
        code: ev ? ev.code : 0,
        reason: ev ? String(ev.reason || '') : '',
        clean: ev ? !!ev.wasClean : false
      }, tag));
    });

    ws.addEventListener('error', () => {
      emit(Object.assign({ type: 'error' }, tag));
    });

    const origSend = ws.send;
    ws.send = function (payload) {
      try {
        emit(Object.assign({
          type: 'send',
          data: typeof payload === 'string' ? payload : null
        }, tag));
      } catch (e) { /* ignore */ }
      return origSend.apply(ws, arguments);
    };

    return ws;
  }

  function XYWebSocket(url, protocols) {
    const ws = (protocols === undefined)
      ? new OrigWS(url)
      : new OrigWS(url, protocols);
    try { return wrap(ws, url); } catch (e) { return ws; }
  }

  XYWebSocket.prototype = OrigWS.prototype;
  XYWebSocket.CONNECTING = OrigWS.CONNECTING;
  XYWebSocket.OPEN = OrigWS.OPEN;
  XYWebSocket.CLOSING = OrigWS.CLOSING;
  XYWebSocket.CLOSED = OrigWS.CLOSED;

  try {
    Object.defineProperty(XYWebSocket, 'name', { value: 'WebSocket' });
  } catch (e) { /* ignore */ }

  window.WebSocket = XYWebSocket;

  // ---- 反向控制通道：Python -> 页面 ----

  window.__xyWsList = function () {
    const out = [];
    for (const k in window.__XY_WS_REGISTRY__) {
      const r = window.__XY_WS_REGISTRY__[k];
      out.push({
        id: r.id,
        url: r.url,
        im: isImUrl(r.url),
        readyState: r.ws ? r.ws.readyState : -1,
        createdAt: r.createdAt || 0
      });
    }
    out.sort((a, b) => a.createdAt - b.createdAt);
    return out;
  };

  window.__xyWsPickIm = function (requireOpen) {
    const list = window.__xyWsList();
    let fallback = null;
    for (let i = list.length - 1; i >= 0; i--) {
      const it = list[i];
      if (!it.im) { continue; }
      if (it.readyState === 1) { return it.id; }
      if (!fallback) { fallback = it.id; }
    }
    if (requireOpen === false) { return fallback; }
    return null;
  };

  window.__xyWsSend = function (id, text) {
    const rec = id ? window.__XY_WS_REGISTRY__[id] : null;
    if (!rec || !rec.ws) { return false; }
    if (rec.ws.readyState !== 1) { return false; }
    try {
      rec.ws.send(text);
      return true;
    } catch (e) {
      return false;
    }
  };

  window.__xyWsSendIm = function (text) {
    const id = window.__xyWsPickIm(true);
    if (!id) { return false; }
    return window.__xyWsSend(id, text);
  };

  window.__xyWsClose = function (id) {
    const rec = id ? window.__XY_WS_REGISTRY__[id] : null;
    if (!rec || !rec.ws) { return false; }
    try { rec.ws.close(); return true; } catch (e) { return false; }
  };

  window.__xyWsState = function () {
    const id = window.__xyWsPickIm(false);
    if (!id) { return { exists: false, readyState: -1 }; }
    const rec = window.__XY_WS_REGISTRY__[id];
    return {
      exists: true,
      id: id,
      url: rec.url,
      readyState: rec.ws ? rec.ws.readyState : -1
    };
  };
})();
"""


# 反自动化指纹抹除（与 CefSharp 原生环境对齐，降低风控命中）
STEALTH_JS = r"""
(() => {
  if (window.__XY_STEALTH__) { return; }
  window.__XY_STEALTH__ = true;

  try {
    Object.defineProperty(navigator, 'webdriver', {
      get: () => undefined,
      configurable: true
    });
  } catch (e) {}

  try {
    if (!window.chrome) { window.chrome = {}; }
    if (!window.chrome.runtime) { window.chrome.runtime = {}; }
  } catch (e) {}

  try {
    const origQuery = window.navigator.permissions && window.navigator.permissions.query;
    if (origQuery) {
      window.navigator.permissions.query = function (params) {
        if (params && params.name === 'notifications') {
          return Promise.resolve({ state: Notification.permission });
        }
        return origQuery.apply(window.navigator.permissions, arguments);
      };
    }
  } catch (e) {}

  try {
    Object.defineProperty(navigator, 'languages', {
      get: () => ['zh-CN', 'zh', 'en'],
      configurable: true
    });
  } catch (e) {}

  try {
    Object.defineProperty(navigator, 'plugins', {
      get: () => [1, 2, 3, 4, 5],
      configurable: true
    });
  } catch (e) {}
})();
"""


def build_init_script() -> str:
    """返回需要 add_init_script 的完整脚本。"""
    return STEALTH_JS + "\n" + WS_HIJACK_JS
