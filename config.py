import os
import yaml
from typing import Dict, Any

class Config:
    """配置管理类
    
    用于加载和管理全局配置文件(global_config.yml)。
    支持配置的读取、修改和保存。
    """
    
    _instance = None
    _config = {}

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(Config, cls).__new__(cls)
            cls._instance._load_config()
        return cls._instance

    def _load_config(self):
        """加载配置文件
        
        从global_config.yml文件中加载配置信息。
        如果文件不存在则抛出FileNotFoundError异常。
        """
        config_path = os.path.join(os.path.dirname(__file__), 'global_config.yml')
        if not os.path.exists(config_path):
            raise FileNotFoundError(f"配置文件不存在: {config_path}")

        with open(config_path, 'r', encoding='utf-8') as f:
            self._config = yaml.safe_load(f)

    def get(self, key: str, default: Any = None) -> Any:
        """获取配置项
        
        Args:
            key: 配置项的键，支持点号分隔的多级键
            default: 当配置项不存在时返回的默认值
            
        Returns:
            配置项的值或默认值
        """
        keys = key.split('.')
        value = self._config
        for k in keys:
            if isinstance(value, dict):
                value = value.get(k)
            else:
                return default
            if value is None:
                return default
        return value

    def set(self, key: str, value: Any) -> None:
        """设置配置项
        
        Args:
            key: 配置项的键，支持点号分隔的多级键
            value: 要设置的值
        """
        keys = key.split('.')
        config = self._config
        for k in keys[:-1]:
            if k not in config:
                config[k] = {}
            config = config[k]
        config[keys[-1]] = value

    def save(self) -> None:
        """保存配置到文件
        
        将当前配置保存回global_config.yml文件
        """
        config_path = os.path.join(os.path.dirname(__file__), 'global_config.yml')
        with open(config_path, 'w', encoding='utf-8') as f:
            yaml.safe_dump(self._config, f, allow_unicode=True, default_flow_style=False)

    @property
    def config(self) -> Dict[str, Any]:
        """获取完整配置
        
        Returns:
            包含所有配置项的字典
        """
        return self._config

# 创建全局配置实例
config = Config()

# 导出常用配置项
COOKIES_STR = config.get('COOKIES.value', '')
COOKIES_LAST_UPDATE = config.get('COOKIES.last_update_time', '')
WEBSOCKET_URL = config.get('WEBSOCKET_URL', 'wss://wss-goofish.dingtalk.com/')
HEARTBEAT_INTERVAL = config.get('HEARTBEAT_INTERVAL', 15)
HEARTBEAT_TIMEOUT = config.get('HEARTBEAT_TIMEOUT', 30)
TOKEN_REFRESH_INTERVAL = config.get('TOKEN_REFRESH_INTERVAL', 72000)
TOKEN_RETRY_INTERVAL = config.get('TOKEN_RETRY_INTERVAL', 7200)
SESSION_KEEPALIVE_INTERVAL = config.get('SESSION_KEEPALIVE_INTERVAL', 600)
SESSION_KEEPALIVE_RETRY_INTERVAL = config.get('SESSION_KEEPALIVE_RETRY_INTERVAL', 180)
MESSAGE_EXPIRE_TIME = config.get('MESSAGE_EXPIRE_TIME', 300000)
SLIDER_VERIFICATION = config.get('SLIDER_VERIFICATION', {
    'max_concurrent': 3,
    'wait_timeout': 60
})
API_ENDPOINTS = config.get('API_ENDPOINTS', {})
DEFAULT_HEADERS = config.get('DEFAULT_HEADERS', {})
WEBSOCKET_HEADERS = config.get('WEBSOCKET_HEADERS', {})
APP_CONFIG = config.get('APP_CONFIG', {})
AUTO_REPLY = config.get('AUTO_REPLY', {
    'enabled': True,
    'default_message': '亲爱的"{send_user_name}" 老板你好！所有宝贝都可以拍，秒发货的哈~不满意的话可以直接申请退款哈~',
    'api': {
        'enabled': False,
        'url': 'http://localhost:8090/xianyu/reply',
        'timeout': 10
    }
})
MANUAL_MODE = config.get('MANUAL_MODE', {})
LOG_CONFIG = config.get('LOG_CONFIG', {})
YIFAN_API = config.get('YIFAN_API', {
    'callback_url': 'http://116.196.116.76/yifan.php',
    'query_url': 'http://116.196.116.76/yifan.php'
})
RISK_CONTROL = config.get('RISK_CONTROL', {
    'night_mode_enabled': False,
    'night_start_hour': 1,
    'night_end_hour': 6,
    'qr_login_grace_minutes': 0,
    'night_keepalive_multiplier': 3,
    'night_cookie_refresh_multiplier': 2,
    'backoff_escalation_factor': 1.5,
    'backoff_max_cap_seconds': 3600,
    'consecutive_failure_protection_threshold': 5,
    'post_slider_retry_delay_min': 5.0,
    'post_slider_retry_delay_max': 10.0,
    'token_refresh_dedup_window_seconds': 60,
    'token_retry_min_wait_seconds': 180,
    'max_post_slider_session_retries': 1,
    'soft_auth_token_preflight_enabled': True,
    'soft_auth_token_preflight_timeout_seconds': 5.0,
    'soft_auth_token_preflight_qr_enabled': False,
})
# ======================================================================
# 浏览器态 IM 会话（移植自「鱼店长Pro」的 CefSharp + JS 注入方案）
# ======================================================================
# 开启后：IM WebSocket 由常驻浏览器页面建立并维护，Python 只做业务收发。
# 这是消除"心跳一直出验证码 / 验证码过不去"的根本手段。
BROWSER_IM = config.get('BROWSER_IM', {
    'enabled': True,                    # 总开关
    'headless': True,                   # 无头运行；首次登录/需人工过验证码时设 false
    'disconnect_grace_seconds': 60,     # 断开后给页面自愈的宽限（鱼店长=60秒）
    'periodic_refresh_seconds': 3600,   # 客服页周期整页重载（鱼店长=60分钟）
    'silence_refresh_seconds': 900,     # IM 通道静默多久判定假在线
    'reload_cooldown_seconds': 90,      # 两次刷新最小间隔
    'max_reload_per_hour': 8,           # 每小时刷新上限，防刷新风暴
    'skip_legacy_heartbeat': True,      # 浏览器模式下停发 /! 心跳（页面自己维持）
    'reuse_single_connection': True,    # 发送消息复用同一条连接，不再每次新建 WSS
    'startup_timeout_seconds': 90,      # 等待页面建立 IM WS 的上限
    'captcha_selfheal_first': True,     # 出验证码时先浏览器自愈，滑块仅作保底
    'fallback_to_direct_ws': True,      # 浏览器起不来时回退原直连 WSS 逻辑
    'reuse_persistent_browser': True,   # 复用 utils/persistent_browser 的常驻浏览器，避免重复开 Chromium
    'executable_path': '',
    'qr_login_in_browser': True,          # 扫码走常驻浏览器（同 profile，避免取 cookie 失败）
    'qr_timeout_seconds': 180,            # 扫码有效期              # 浏览器路径；留空自动探测（容器内通常为 /usr/bin/chromium）
})

# ======================================================================
# 页内自动滑块（对应「鱼店长Pro」的 EnableAutoVerify / SliderVerify）
# ======================================================================
# 关键：滑块在**同一个已登录浏览器会话**里完成，而不是另开浏览器闯验证。
# 会话态一致，平台才认这次验证；这也是"验证码老过不去"的另一半原因。
AUTO_SLIDER = config.get('AUTO_SLIDER', {
    'enabled': True,                       # 自动滑块总开关（对齐 EnableAutoVerify）
    'max_attempts': 3,                     # 单轮最多尝试次数
    'retry_interval_seconds': 5,           # 首次重试间隔（逐次递增）
    'drag_timeout_seconds': 20,            # 单次拖动后等待判定时长
    'headful_manual_wait_seconds': 90,     # 有头模式下自动失败后留给人工的时长
    'notify_on_repeat_fail': True,         # 反复失败时通知（对齐 RepeatVerifyFailTip）
    'rotate_page_before_retry': True,      # 每次重试前刷新验证页拿新 puzzle
})

# ======================================================================
# 账号自动登录与状态维护
# ======================================================================
# 对应「鱼店长Pro」的 EnableAutoLogin / EnableReloadKefuWhenNetBreak /
# VerifyRetryMin / ReplyDeadLine：登录在常驻浏览器内完成（填账号密码 → 勾协议
# → 点登录），凭据加密存 cookies.username/password；会话失效时按退避策略重登。
ACCOUNT_STATE = config.get('ACCOUNT_STATE', {
    'auto_login_enabled': True,             # EnableAutoLogin
    'auto_login_on_startup': True,          # 启动时无有效 cookie 就用凭据登录
    'relogin_on_session_invalid': True,     # 会话失效时自动重登
    'max_login_attempts': 3,                # 连续失败上限，超过转人工
    'login_backoff_seconds': 180,           # 首次失败退避，之后翻倍
    'login_backoff_max_seconds': 3600,      # 退避上限
    'verify_retry_min': 15,                 # VerifyRetryMin（分钟）
    'reload_kefu_when_net_break': True,     # EnableReloadKefuWhenNetBreak
    'reply_deadline_warn_days': 3,          # ReplyDeadLine 临近提醒
    'login_timeout_seconds': 60,            # 单次登录等待上限
})

_cookies_raw = config.get('COOKIES', [])
if isinstance(_cookies_raw, list):
    COOKIES_LIST = _cookies_raw
else:
    # 兼容旧格式，仅有 value 字段
    val = _cookies_raw.get('value') if isinstance(_cookies_raw, dict) else None
    COOKIES_LIST = [{'id': 'default', 'value': val}] if val else []



# ======================================================================
# 部署形态（形态 B：混合部署）
# ======================================================================
# server  : 服务器只做账号/数据/面板/回复决策，不启动任何浏览器
# desktop : 桌面客户端承担浏览器相关全部工作（登录/扫码/滑块/抓页面）
# 由环境变量 DEPLOY_MODE 或本配置控制，默认 desktop（保持原行为）。
DEPLOY_MODE = (os.environ.get('DEPLOY_MODE') or config.get('DEPLOY_MODE', 'desktop') or 'desktop').strip().lower()
