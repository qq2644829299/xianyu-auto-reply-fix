# 鱼店长Pro → xianyu-auto-reply 心跳/验证码机制移植说明

> 目标：解决 `xianyu-auto-reply-fix-main` **心跳一直弹验证码、且验证码经常过不去**的问题。
> 方法：逆向桌面端 `鱼店长Pro`（`D:\Program Files\鱼店长Pro`），把它的会话维持机制整体移植过来。

---

## 一、问题根因（为什么要移植，而不是打补丁）

改造前本项目的 IM 连接是这样维持的：

| 环节 | 原实现 | 平台视角 |
|---|---|---|
| IM 连接 | Python `websockets.connect()` 直连 `wss://wss-goofish.dingtalk.com/` | 非浏览器客户端 |
| 心跳 | 每 **15 秒**发一帧 `{"lwp":"/!"}` | 与页面心跳叠加 → 同会话双客户端 |
| 鉴权 | 进程内调 mtop `mtop.taobao.idlemessage.pc.login.token`，自算 `sign` | 缺 `baxia`/`x5sec`/TLS 指纹 → `FAIL_SYS_USER_VALIDATE` |
| 验证码 | 触发后走 Playwright **页外滑块** | 滑块在页外环境下通过率极低 |
| 发消息 | `send_msg_once` **每次新建一条 WSS** | 同 cookie 多连接抢登录态 |

结论：**验证码不是"验证码过不去"，而是"一直在主动触发风控、然后又用一个过不去的通道去解"**。

## 二、鱼店长Pro 的做法（逆向结论）

样本构成：`.NET 6 WPF + CefSharp 109`（`AgisoIdleClient.exe` / `AgisoIdleClient.dll`，v1.2.4.1）。
方法体被商业混淆器加密（IL 只剩 `nop;ret`），但从**元数据 + 运行时日志**可完整还原行为：

| 证据来源 | 内容 |
|---|---|
| 日志 `Log\3823821587\*.log` | `[JsWs#1] IM的 WebSoket 打开 / 关闭` |
| 同上 | `[WsDisconnectWatcher#1] 检测到断开事件，开始60秒计时` / `检测到 Open 事件，取消刷新计时` |
| 同上 | `[KefuRefreshManager] 定时刷新已启动，间隔=60分钟` |
| 同上 | `重新加载客服聊天窗口(定时刷新)` |
| 同上 | `[MessageWatcher#1] (JsWs#1) 初始化` |
| 元数据 `AgisoIdleClient.JsObject.*` | `JsWs`（Open/Message/Close/Error/RefreshPage/LogRaw）、`MessageWatcher`（Initialize/Tick）、`WsDisconnectWatcher`（OnDisconnected/OnReconnected） |
| 元数据 `MOWMcvTWJOtZwWWYeAs.VY2wYKTmJhYvEBABudV` | `GetTokenFromWebAsync` / `ExecuteScriptsInOrderAsync` / `InjectLoginInfoListenerAsync` / `GetJsonResourceHandles` |

**它从不自己连 WSS、从不自己刷 token、也从不发心跳包。** 具体：

1. 用 CefSharp 打开闲鱼客服页，**页面自己的 JS 建立并维护 IM WebSocket**；
2. C# 只注册回调对象（`JsWs`），把页面 WS 的 open/message/close 事件收进来；
3. token 由 `GetTokenFromWebAsync()` **在网页上下文里换**，请求天然带着
   `_m_h5_tk` / `_m_h5_tk_enc` / `x5sec` / `baxia` 和真实 `sec-ch-ua`、TLS 指纹；
4. 断线不慌：`WsDisconnectWatcher` 给 **60 秒**自愈窗口，超时才 `RefreshPage`；
5. 每 **60 分钟**主动整页重载一次，换新会话；
6. 只有 `MessageWatcher.Tick` 做增量拉取，没有任何进程级心跳。

正因为"一切都发生在真实浏览器里"，它长期不触发滑块。

## 三、移植实现对照表

| 鱼店长Pro（C#） | 本项目（Python） | 文件 |
|---|---|---|
| `ChromiumWebBrowser` + 持久化 CefSharp 上下文 | Playwright `launch_persistent_context` | `utils/browser_im/browser_session.py` |
| 注入页面的 JS（拦 WebSocket、回传事件） | `add_init_script` + `expose_binding` | `utils/browser_im/ws_hijack_js.py` |
| `JsWs.Open/Message/Close/Error` 回调 | `__xyWsEvent` → `asyncio.Queue` | `browser_session._on_ws_event` |
| `JsWs.RefreshPage(reason)` | `BrowserIMSession.reload_page(reason)` | `browser_session.py` |
| `GetTokenFromWebAsync()` | `BrowserIMSession.get_im_token()`（页面内 `fetch` + `crypto.subtle` 算 sign） | `browser_session.py` |
| `JsWs` 复用同一条 WS 发消息 | `BrowserIMTransport.send()` + `BrowserWebSocket` | `utils/browser_im/transport.py` |
| `WsDisconnectWatcher.OnDisconnected/OnReconnected` | `WsDisconnectWatcher`（60s 宽限 + 冷却 + 限流） | `utils/browser_im/ws_watchdog.py` |
| `KefuRefreshManager`（60 分钟刷新） | `WsDisconnectWatcher._loop` 周期刷新 | `ws_watchdog.py` |
| 每账号一个 CefSharp 实例池 | `BrowserIMRuntimeManager`（`cookie_id → AccountRuntime`） | `utils/browser_im/manager.py` |

## 四、对本项目的具体改动

### 新增模块（不改动原业务逻辑）

```
utils/browser_im/
├── __init__.py            统一导出
├── ws_hijack_js.py        WebSocket 劫持 + 反自动化指纹抹除脚本
├── browser_session.py     常驻浏览器会话（CefSharp 等价）
├── transport.py           浏览器 WS ↔ websockets 鸭子类型适配
├── ws_watchdog.py         断线看门狗 + 周期刷新
└── manager.py             按账号管理运行时
tools/verify_browser_im.py 自检脚本（9 项）
```

### `XianyuAutoAsync.py` 的接入点（最小侵入）

| 位置 | 改动 |
|---|---|
| `__init__` | 新增 `browser_im_*` 状态字段 |
| `_create_websocket_connection` | **分流**：浏览器模式 → `BrowserWebSocket`；否则 → 原 `_create_direct_websocket_connection`（原逻辑整体保留为降级路径） |
| `init(ws)` | 浏览器通道**跳过 `/reg`**（页面已注册，重复注册是风控诱因），只补 `/r/SyncStatus/ackDiff`；并做一次 cookie 对齐 |
| `heartbeat_loop(ws)` | 浏览器通道改走 `_browser_channel_monitor`：**不发 `/!` 心跳**，只观测状态喂给看门狗 |
| `refresh_token` | 浏览器模式**优先经页面换取 token**，失败先页面自愈再试一次，之后直接返回，**不进滑块死循环** |
| `_handle_captcha_verification` | **自愈优先**：整页刷新 → 重取 token → 成功即返回；只有都失败才降级页外滑块 |
| `_force_websocket_reconnect` | 浏览器模式改为整页刷新，而不是关闭连接 |
| `send_msg_once` / `send_delivery_steps_once` | 浏览器模式**复用同一条连接**（配 `_has_single_chat_payload` 过滤器分流回包） |
| `main()` | 启动前先拉起浏览器会话 |
| `browser_im_stats()` / `stop_browser_im()` | 状态查询与资源释放 |

### 其它

* `config.py` + `global_config.yml`：新增 `BROWSER_IM` 配置段（含注释）。
* `reply_server.py`：新增 `GET /api/browser-im/status` 运维接口。

## 五、配置说明

`global_config.yml`：

```yaml
BROWSER_IM:
  enabled: true
  headless: true                  # 首次登录/需人工过验证码时改 false
  disconnect_grace_seconds: 60    # 对齐鱼店长 60 秒
  periodic_refresh_seconds: 3600  # 对齐鱼店长 60 分钟
  silence_refresh_seconds: 900
  reload_cooldown_seconds: 90
  max_reload_per_hour: 8
  skip_legacy_heartbeat: true     # 停发 /! 心跳
  reuse_single_connection: true   # 复用同一条 IM 连接
  startup_timeout_seconds: 90
  captcha_selfheal_first: true    # 验证码先自愈，滑块仅保底
  fallback_to_direct_ws: true     # 浏览器起不来时回退原逻辑
```

**建议首次启用时把 `headless` 设为 `false`**：能亲眼看到页面是否正常登录、是否有真人验证弹窗，确认稳定后再转 `true`。

## 六、验证

```bash
# 1) 自检（不依赖真实账号）
venv\Scripts\python.exe tools\verify_browser_im.py

# 2) 语法/导入
venv\Scripts\python.exe -c "import XianyuAutoAsync; print('ok')"

# 3) 运行后看状态
curl http://127.0.0.1:8080/api/browser-im/status
```

自检当前结果：**9 项全通过** —— 浏览器启动、WS 劫持注入、反向控制 API、WS 事件桥接、
传输层接口兼容、看门狗开始/取消/计时、cookie 解析。

运行时日志应出现：

```
【账号】浏览器态会话已启动（headless=True）
【账号】IM 页面加载完成: https://www.goofish.com/im
【账号】[浏览器Ws] IM WebSocket 打开 (wss://wss-goofish.dingtalk.com/...)
【账号】浏览器通道已由页面完成注册，跳过 /reg
【账号】浏览器通道监控已启动（不发送 /! 心跳）
```

而**不应再出现**：`心跳包已发送`、`检测到需要滑块验证`、`滑块验证失败`。

## 七、注意事项

1. **浏览器依赖**：Playwright Chromium 已存在于 `%LOCALAPPDATA%\ms-playwright`（`chromium-1217`）。换机器需 `playwright install chromium`。
2. **profile 目录**：每账号一个 `browser_data/im_sessions/<cookie_id>/`，不要跨账号复用。
3. **首次登录**：项目原有扫码登录照常使用；登录后新 cookie 会自动同步进浏览器会话（`apply_cookies`）。
4. **降级路径**：浏览器连续 3 次启动失败会自动回退到原直连逻辑，并打日志告警——**但回退后仍会出验证码**，这是预期行为，说明需要修浏览器环境。
5. **无头 + 强验证**：无头模式下若页面弹真人验证，代码会选择"跳过滑块、等下一轮刷新自愈"而不是硬闯。
   这种情况下请临时切 `headless: false` 人工过一次。
6. 建议把原 `HEARTBEAT_INTERVAL`（15s）保留不动——它现在只影响**非浏览器模式**的节奏，
   以及浏览器模式下的状态轮询间隔（内部已按 `max(10, heartbeat_interval)` 取值）。
