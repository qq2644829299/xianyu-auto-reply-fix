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
5. **无头 + 强验证**：无头模式下若页面弹真人验证，页内滑块会照常尝试；仍不通过则跳过、等下一轮刷新自愈。
   这种情况下请临时切 `headless: false` 人工过一次。
6. 建议把原 `HEARTBEAT_INTERVAL`（15s）保留不动——它现在只影响**非浏览器模式**的节奏，
   以及浏览器模式下的状态轮询间隔（内部已按 `max(10, heartbeat_interval)` 取值）。

---

# 八、页内自动滑块（第二阶段）

## 8.1 逆向依据

`鱼店长Pro` 的验证相关配置在 `AgisoIdleClient.Model.AccountConfig` 里（元数据可读）：

| 配置项 | 含义 |
|---|---|
| `EnableAutoVerify` | 自动验证总开关 |
| `EnableAutoVerifyRetry` | 失败后自动重试 |
| `VerifyRetryMin` | 重试间隔（分钟） |
| `RepeatVerifyFailTip` | 反复失败的提示文案 |
| `FrontHanlder.SliderVerify()` | 滑块事件由**页面 JS 回调**到 C# |
| `JsWs.RefreshPage(reason)` | 兜底：整页刷新换新会话 |

`AgisoIdleClient.dll` 的资源区整体加密（内嵌 MZ 保护器 + 随机魔数，方法体被抹成 `nop;ret`），
静态取不到滑块 JS。但配置语义已足够确定设计：**验证发生在同一条已登录的浏览器会话里**，
C# 只在页面报告验证事件后做状态管理与重试，并且保留"刷新页面"作为最后手段。

## 8.2 为什么必须"页内"

本项目原有 `utils/xianyu_slider_stealth.py` 的轨迹算法（含 1D Perlin 噪声）质量很好，
但它是**新开一个浏览器、新装一份 cookie** 去闯验证：

```
发起请求的会话 A  ──触发风控──>  滑块会话 B ──通过──> cookie 回灌 A
```

B 的 canvas/WebGL/TLS 指纹、`_m_h5_tk` 时序、baxia 埋点与 A 不是同一个，
即使拖对了，平台也常判定"验证环境不一致"而不放行——这是"验证码老过不去"的另一半原因。

鱼店长的做法是 **A 自己验证自己**，本模块照此实现。

## 8.3 实现对照

| 鱼店长Pro | 本次实现 | 文件 |
|---|---|---|
| `EnableAutoVerify` | `AUTO_SLIDER.enabled` | `config.py` |
| `EnableAutoVerifyRetry` + `VerifyRetryMin` | `max_attempts` + `retry_interval_seconds`（逐次递增） | `auto_slider.py` |
| `RepeatVerifyFailTip` | 连续失败 ≥2 次发通知，建议切有头人工 | `_browser_auto_slider` |
| `FrontHanlder.SliderVerify()` | `AutoSlider.solve()` 主动驱动 | `auto_slider.py` |
| `JsWs.RefreshPage(reason)` | 自愈前置 + 每次重试前 `reload` 拿新 puzzle | `ws_watchdog.py` / `auto_slider.py` |

## 8.4 关键实现点

1. **同会话**：直接复用 `BrowserIMSession` 的 page/context，不新建浏览器。
2. **真实指针事件**：用 `page.mouse.move/down/up`，底层是 CDP `Input.dispatchMouseEvent`，
   `isTrusted = true`，与真人操作同等对待（不是 JS 里 `dispatchEvent` 的伪事件）。
3. **拟人轨迹**：
   - 三次贝塞尔加速段 + 减速段；
   - 末端轻微过冲再回退（人手不会精确停在目标点）；
   - 水平抖动 + 高频微抖 + 偶发 40~130ms 长停顿（"犹豫"）。
4. **多形态选择器**：`#nc_1_n1z` / `.btn_slide` / `#nc_2_n1z` 等手柄，
   `#nc_1_n1t` / `.nc_scale` 等轨道，并支持 `#baxia-dialog-content` 等 **iframe** 内的滑块。
5. **通过判定**三重保险：`x5sec` cookie 出现 → URL 离开 punish 页 → 滑块容器消失且无验证文案。
6. **失败判定**：出现"验证失败/请重试/出错"即提前退出，不空等。
7. **有头兜底**：自动尝试失败后，有头模式再给 `headful_manual_wait_seconds` 让真人接手。

## 8.5 处理链路（最终形态）

```
mtop 返回 FAIL_SYS_USER_VALIDATE
        │
        ▼
① 浏览器自愈：整页刷新 → 重取 token ──成功──> 返回，完全不用滑块
        │ 失败
        ▼
② 页内自动滑块（同会话 + 真实指针事件 + 拟人轨迹）
        │ 成功 ──> 回灌 cookie + 换新 IM token → 返回
        │ 失败
        ▼
③ 有头模式：留 90s 给人工作业
   无头模式：跳过，等下一轮刷新自愈（不空转、不硬闯）
        │ 全失败
        ▼
④ 降级到原有页外滑块（保留，作为最后保底）
```

## 8.6 配置

```yaml
AUTO_SLIDER:
  enabled: true                    # 自动滑块总开关
  max_attempts: 3                  # 单轮最多尝试次数
  retry_interval_seconds: 5        # 首次重试间隔（逐次递增）
  drag_timeout_seconds: 20         # 单次拖动后等待判定时长
  headful_manual_wait_seconds: 90  # 有头模式下自动失败后留给人工的时长
  notify_on_repeat_fail: true      # 反复失败时通知
  rotate_page_before_retry: true   # 每次重试前刷新验证页拿新 puzzle
```

## 8.7 验证

```bash
venv\Scripts\python.exe tools\verify_auto_slider.py
```

自检用本地构造的 nc 风格滑块页面（拖到 92% 即通过并写 `x5sec` cookie），**12 项全通过**：

```
[PASS] 轨迹点数合理 91 点
[PASS] 轨迹覆盖全程 最远 263.8px / 目标 260px
[PASS] 末端回落到目标 落点 260.0px
[PASS] 存在垂直抖动
[PASS] 延时序列对齐 91 个
[PASS] 浏览器启动
[PASS] 滑块定位 handle=40px track=300px
[PASS] 拖动事件已发出 down=1 moves=90 up=1
[PASS] 移动步数充足 moves=90
[PASS] 通过判定生效
[PASS] solve 返回成功 ok=True reason=passed attempts=1
[PASS] 未验证不误判
```

其中 `down=1 moves=90 up=1` 证明是**真实鼠标事件**驱动页面完成拖动，一次通过。

运行时日志应出现：

```
【账号】[自动滑块] 开始页内验证（同会话）
【账号】[滑块] 定位到滑块 handle=40px track=300px 需拖动≈254px
【账号】[滑块] ✅ 第 1 次尝试通过验证（同会话页内）
【账号】[自动滑块] 已换新 IM token
```

## 8.8 说明

* 页内滑块的**通过率取决于真实滑块难度**，机制正确不等于 100% 通过；偶发难图会重试或转人工。
* 每次都换新 puzzle（`rotate_page_before_retry`）比在同一张图上反复拖更容易过。
* `x5sec` cookie 一旦拿到会被持久化到账号 profile，后续相当长时间不再验证。
* 想进一步提高通过率：把 `headless` 设为 `false`——有头模式下 canvas/WebGL 指纹更完整，
  且失败时能人工兜底。

