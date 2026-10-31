"""账号状态维护（对应「鱼店长Pro」的账号生命周期管理）。

逆向依据
========
`AgisoIdleClient.Model.AccountConfig` 中与"账号状态"相关的开关：

| 配置 | 作用 |
|---|---|
| `EnableAutoLogin` / `UserName` / `Password` | 掉线后自动用账号密码重新登录 |
| `EnableReloadKefuWhenNetBreak` | 断网时重载客服页（而不是干等） |
| `EnableAutoVerify` / `EnableAutoVerifyRetry` / `VerifyRetryMin` | 验证失败的重试节奏 |
| `RepeatVerifyFailTip` | 反复失败的提示 |

`AgisoIdleClient.Interfaces.IUserInfoManager` 负责账号档案的读写：

    CurrentUserInfo                当前账号
    AddOrUpdateAsync(BaseUserInfo) 落账号信息
    GetLoginInfo / SaveLoginInfo   凭据（加密）存取
    UpdateReplyDeadLineAsync       授权到期时间

`BaseUserInfo.ReplyDeadLine` 是关键——账号授权有期限，到期后必须重新登录/续期，
否则消息链路注定失效。这一条以前完全没有被本项目管理。

本模块把上述行为收敛成一个显式状态机：状态可判定、可持久化、动作有退避，
避免"一失效就疯狂重登"这种最容易被风控盯上的行为。
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

from loguru import logger


class AccountState:
    """账号状态枚举（字符串常量，便于直接写入 DB 的 status_note）。"""

    UNKNOWN = "unknown"
    LOGGED_IN = "logged_in"
    EXPIRED = "expired"                     # cookie/会话过期
    VERIFY_REQUIRED = "verify_required"     # 需要过验证（滑块/短信）
    PASSWORD_ERROR = "password_error"       # 账号密码不对
    ACCOUNT_LOCKED = "account_locked"       # 账号被冻结/限制
    ACCOUNT_NOT_EXIST = "account_not_exist"
    REPLY_EXPIRED = "reply_expired"         # 回复授权到期（ReplyDeadLine 已过）
    NEED_QR = "need_qr"                     # 无凭据，只能扫码
    DISCONNECTED = "disconnected"           # 网络/WS 断开

    ALL = (
        UNKNOWN, LOGGED_IN, EXPIRED, VERIFY_REQUIRED, PASSWORD_ERROR,
        ACCOUNT_LOCKED, ACCOUNT_NOT_EXIST, REPLY_EXPIRED, NEED_QR, DISCONNECTED,
    )

    # 这些状态重登也没有意义，必须人工介入
    TERMINAL = (ACCOUNT_LOCKED, ACCOUNT_NOT_EXIST, PASSWORD_ERROR, NEED_QR)


# 登录失败原因 -> 账号状态
_REASON_TO_STATE = {
    "password_error": AccountState.PASSWORD_ERROR,
    "account_locked": AccountState.ACCOUNT_LOCKED,
    "account_not_exist": AccountState.ACCOUNT_NOT_EXIST,
    "verify_required": AccountState.VERIFY_REQUIRED,
    "risk_control": AccountState.VERIFY_REQUIRED,
    "login_timeout": AccountState.EXPIRED,
    "missing_credentials": AccountState.NEED_QR,
    "login_page_unreachable": AccountState.DISCONNECTED,
    "credentials_input_failed": AccountState.EXPIRED,
    "submit_failed": AccountState.EXPIRED,
}


class AccountStateKeeper:
    """单账号状态保持器：记录当前状态、判定是否需要动作、给出带退避的动作计划。"""

    def __init__(
        self,
        cookie_id: str,
        *,
        max_login_attempts: int = 3,
        login_backoff_seconds: float = 180.0,
        login_backoff_max_seconds: float = 3600.0,
        verify_retry_min: float = 15.0,
    ) -> None:
        self.cookie_id = cookie_id
        self.max_login_attempts = max(1, int(max_login_attempts))
        self.login_backoff_seconds = float(login_backoff_seconds)
        self.login_backoff_max_seconds = float(login_backoff_max_seconds)
        self.verify_retry_min = float(verify_retry_min)

        self.state: str = AccountState.UNKNOWN
        self.reason: str = ""
        self.updated_at: float = 0.0

        self._login_failures: int = 0
        self._next_login_allowed_at: float = 0.0
        self._verify_attempts: int = 0
        self._next_verify_allowed_at: float = 0.0

        self.success_count: int = 0
        self.last_success_at: float = 0.0

    # ------------------------------------------------------------------ 状态

    def mark(self, state: str, reason: str = "") -> None:
        if state not in AccountState.ALL:
            state = AccountState.UNKNOWN
        if state != self.state or reason != self.reason:
            logger.info(
                f"【{self.cookie_id}】[账号状态] {self.state} -> {state}"
                + (f"（{reason}）" if reason else "")
            )
        self.state = state
        self.reason = reason
        self.updated_at = time.time()

    def mark_from_login_reason(self, reason: str) -> str:
        state = _REASON_TO_STATE.get(reason, AccountState.EXPIRED)
        self.mark(state, reason)
        return state

    def mark_logged_in(self, reason: str = "ok") -> None:
        self.mark(AccountState.LOGGED_IN, reason)
        self._login_failures = 0
        self._verify_attempts = 0
        self._next_login_allowed_at = 0.0
        self._next_verify_allowed_at = 0.0
        self.success_count += 1
        self.last_success_at = time.time()

    # ------------------------------------------------------------------ 判定

    @property
    def is_terminal(self) -> bool:
        """是否处于必须人工介入的状态。"""
        return self.state in AccountState.TERMINAL

    @property
    def needs_login(self) -> bool:
        return self.state in (
            AccountState.EXPIRED,
            AccountState.DISCONNECTED,
            AccountState.VERIFY_REQUIRED,
            AccountState.UNKNOWN,
        )

    def can_attempt_login(self) -> bool:
        """是否允许现在尝试登录（退避 + 次数双重限制）。"""
        if self.is_terminal:
            return False
        if self._login_failures >= self.max_login_attempts:
            return False
        return time.time() >= self._next_login_allowed_at

    def can_attempt_verify(self) -> bool:
        """验证重试是否到点（对齐 VerifyRetryMin）。"""
        return time.time() >= self._next_verify_allowed_at

    def record_login_failure(self, reason: str = "") -> None:
        """记录一次登录失败并计算退避（指数增长，有上限）。"""
        self._login_failures += 1
        backoff = min(
            self.login_backoff_seconds * (2 ** max(0, self._login_failures - 1)),
            self.login_backoff_max_seconds,
        )
        self._next_login_allowed_at = time.time() + backoff
        logger.warning(
            f"【{self.cookie_id}】[账号状态] 登录失败第 {self._login_failures} 次"
            f"（{reason or self.reason}），{int(backoff)}s 内不再尝试"
        )

    def record_verify_failure(self) -> None:
        """记录一次验证失败，按 VerifyRetryMin（分钟）排队重试。"""
        self._verify_attempts += 1
        wait = self.verify_retry_min * 60.0
        self._next_verify_allowed_at = time.time() + wait
        logger.warning(
            f"【{self.cookie_id}】[账号状态] 验证未通过第 {self._verify_attempts} 次，"
            f"{int(self.verify_retry_min)} 分钟后再试"
        )

    def reset_login_failures(self) -> None:
        self._login_failures = 0
        self._next_login_allowed_at = 0.0

    # ------------------------------------------------------------------ 报告

    def describe(self) -> str:
        """给用户看的一句话状态说明。"""
        mapping = {
            AccountState.LOGGED_IN: "会话正常",
            AccountState.EXPIRED: "会话已过期，将尝试自动重新登录",
            AccountState.VERIFY_REQUIRED: "需要完成安全验证（滑块/短信）",
            AccountState.PASSWORD_ERROR: "账号或密码不正确，请更新凭据",
            AccountState.ACCOUNT_LOCKED: "账号被平台限制，需人工处理",
            AccountState.ACCOUNT_NOT_EXIST: "账号不存在，请检查账号",
            AccountState.REPLY_EXPIRED: "回复授权已到期，请续期",
            AccountState.NEED_QR: "缺少账号密码，只能扫码登录",
            AccountState.DISCONNECTED: "连接已断开，等待重连",
            AccountState.UNKNOWN: "状态未知",
        }
        return mapping.get(self.state, self.state)

    def stats(self) -> Dict[str, Any]:
        now = time.time()
        return {
            "state": self.state,
            "reason": self.reason,
            "describe": self.describe(),
            "login_failures": self._login_failures,
            "max_login_attempts": self.max_login_attempts,
            "next_login_in": max(0.0, self._next_login_allowed_at - now) if self._next_login_allowed_at else 0.0,
            "verify_attempts": self._verify_attempts,
            "next_verify_in": max(0.0, self._next_verify_allowed_at - now) if self._next_verify_allowed_at else 0.0,
            "success_count": self.success_count,
            "last_success_age": (now - self.last_success_at) if self.last_success_at else None,
            "terminal": self.is_terminal,
        }


def judge_reply_deadline(deadline_ts: Optional[float], warn_days: float = 3.0) -> Optional[str]:
    """根据 `ReplyDeadLine` 判断是否到期/临近到期。

    返回 None 表示正常；否则返回 `expired` 或 `expiring_soon`。
    """
    if not deadline_ts:
        return None
    now = time.time()
    if deadline_ts <= now:
        return "expired"
    if deadline_ts - now <= warn_days * 86400:
        return "expiring_soon"
    return None
