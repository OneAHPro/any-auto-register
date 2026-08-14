from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
import threading
import time
import uuid

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional


logger = logging.getLogger(__name__)
# LeadBee's own page supports at most five active card tasks.  Keep that
# provider limit while allowing the user's mailbox-login concurrency to flow
# through instead of serializing every card behind a single global lock.
leadbee_phone_flow_lock = threading.BoundedSemaphore(5)
LEADBEE_PROVIDER_SETTLEMENT_MARGIN_SECONDS = 60.0
LEADBEE_PROVIDER_SLOT_WAIT_SECONDS = 30.0
KNOWN_EXCHANGE_CODE_SETTLEMENTS = frozenset(
    {"restored", "consumed", "unusable"}
)
LEADBEE_API_CLIENT_ORDER_RE = re.compile(r"^aar_[0-9a-f]{32}$")


def _truthy_config(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _validate_leadbee_api_config(config: dict[str, Any]) -> None:
    if not _truthy_config(config.get("leadbee_api_enabled")):
        raise ValueError("LeadBee API 未启用")
    if not all(
        str(config.get(key) or "").strip()
        for key in (
            "leadbee_api_key",
            "leadbee_api_secret",
            "leadbee_api_product_id",
        )
    ):
        raise ValueError("LeadBee API 配置不完整")


def _require_leadbee_api_config() -> None:
    from core.config_store import config_store

    _validate_leadbee_api_config(config_store.get_all())


def normalize_e164_phone(value: str) -> str:
    normalized = re.sub(r"[\s()-]+", "", str(value or "").strip())
    if not re.fullmatch(r"\+[1-9]\d{7,14}", normalized):
        raise ValueError("手机号需使用 E.164 国际格式，例如 +447456344799")
    return normalized


def normalize_phone_code(value: str) -> str:
    code = re.sub(r"\s+", "", str(value or ""))
    if not re.fullmatch(r"\d{4,8}", code):
        raise ValueError("请输入 4 至 8 位短信验证码")
    return code


def _redact_secret(value: Any, secret: str, replacement: str = "[卡密已隐藏]") -> str:
    text = str(value or "").strip()
    normalized_secret = str(secret or "").strip()
    return text.replace(normalized_secret, replacement) if normalized_secret else text


def _mask_phone(value: str) -> str:
    phone = str(value or "").strip()
    if len(phone) <= 5:
        return "*" * len(phone)
    return f"{phone[:3]}{'*' * (len(phone) - 5)}{phone[-2:]}"


@dataclass(frozen=True)
class PhoneVerificationCommand:
    id: str
    kind: str
    payload: str = ""


class PhoneVerificationCancelled(RuntimeError):
    """Raised inside a phone flow after its owning task cancels the session."""


class InteractivePhoneVerificationBroker:
    def __init__(
        self,
        *,
        account_id: int,
        phone: str = "",
        provider: str = "manual",
        leadbee_api: bool = False,
        client_order_id: str = "",
        request_fingerprint: str = "",
        ttl_seconds: int = 600,
        resend_cooldown_seconds: int = 60,
        on_provider_start: Optional[Callable[[], None]] = None,
        on_exchange_code_consumed: Optional[Callable[[], None]] = None,
        on_exchange_code_restored: Optional[Callable[[], None]] = None,
    ):
        self.session_id = uuid.uuid4().hex
        self.account_id = int(account_id)
        self.phone = normalize_e164_phone(phone) if str(phone or "").strip() else ""
        self.provider = str(provider or "manual").strip().lower() or "manual"
        self.leadbee_api = bool(leadbee_api)
        self.client_order_id = str(client_order_id or "").strip()
        if self.leadbee_api and not LEADBEE_API_CLIENT_ORDER_RE.fullmatch(
            self.client_order_id
        ):
            raise ValueError("LeadBee API 客户端订单标识无效")
        self._request_fingerprint = str(request_fingerprint or "")
        self.automatic = self.provider == "leadbee"
        self.provider_mode = (
            "api"
            if self.leadbee_api
            else ("exchange_code" if self.automatic else "manual")
        )
        self.created_at = time.time()
        self.expires_at = self.created_at + max(0, int(ttl_seconds))
        self.resend_cooldown_seconds = max(0, int(resend_cooldown_seconds))
        self.resend_available_at = self.created_at
        self.status = "starting"
        self.message = (
            "正在恢复 OpenAI 授权会话并启动 LeadBee API 自动接码"
            if self.leadbee_api
            else "正在恢复 OpenAI 授权会话并请求短信验证码"
        )
        self.phone_verified = False
        self.provider_started = False
        self.provider_error_code = ""
        self.provider_error_message = ""
        self.exchange_code_consumed = False
        self.exchange_code_unusable = False
        self.exchange_code_unusable_reason = ""
        self.exchange_code_restoration_confirmed = False
        self.exchange_code_settlement = ""
        self._on_provider_start = on_provider_start
        self._exchange_code_callback_fired = False
        self._on_exchange_code_consumed = (
            None if self.leadbee_api else on_exchange_code_consumed
        )
        self._restoration_callback_fired = False
        self._on_exchange_code_restored = (
            None if self.leadbee_api else on_exchange_code_restored
        )
        self._logs: list[str] = []
        self.tokens: dict[str, Any] = {}
        self._condition = threading.Condition()
        self._commands: deque[PhoneVerificationCommand] = deque()
        self._command_results: dict[str, tuple[bool, str]] = {}
        self._terminal = False
        self._finalizing = False
        self._completion_settled = True
        self._provider_cleanup_settled = not self.automatic
        self.terminal_at = 0.0
        self._command_pending = False
        self._cancel_event = threading.Event()
        self._append_log_locked(self.message)

    def matches_request(self, provider: str, request_fingerprint: str) -> bool:
        normalized_provider = str(provider or "manual").strip().lower() or "manual"
        return self.provider == normalized_provider and hmac.compare_digest(
            self._request_fingerprint,
            str(request_fingerprint or ""),
        )

    def _append_log_locked(self, message: str) -> None:
        text = str(message or "").strip()
        if not text:
            return
        if self._logs and self._logs[-1].endswith(text):
            return
        self._logs.append(f"[{datetime.now().strftime('%H:%M:%S')}] {text}")

    def _is_expired(self) -> bool:
        return time.time() >= self.expires_at

    def remaining_seconds(self) -> int:
        return max(0, int(self.expires_at - time.time()))

    def resend_after_seconds(self) -> int:
        return max(0, int(self.resend_available_at - time.time() + 0.999))

    def snapshot(self) -> dict[str, Any]:
        with self._condition:
            status = self.status
            message = self.message
            if self._is_expired() and not self._terminal and not self._finalizing:
                status = "expired"
                message = "手机验证会话已过期，请重新获取验证码"
            return {
                "session_id": self.session_id,
                "account_id": self.account_id,
                "phone": _mask_phone(self.phone) if self.leadbee_api else self.phone,
                "provider": self.provider,
                "automatic": self.automatic,
                "leadbee_api": self.leadbee_api,
                "provider_mode": self.provider_mode,
                "client_order_id": self.client_order_id,
                "status": status,
                "message": message,
                "phone_verified": self.phone_verified,
                "provider_started": self.provider_started,
                "provider_error_code": self.provider_error_code,
                "provider_error_message": self.provider_error_message,
                "exchange_code_consumed": self.exchange_code_consumed,
                "exchange_code_unusable": self.exchange_code_unusable,
                "exchange_code_unusable_reason": self.exchange_code_unusable_reason,
                "exchange_code_restoration_confirmed": (
                    self.exchange_code_restoration_confirmed
                ),
                "exchange_code_settlement": self.exchange_code_settlement,
                "provider_cleanup_settled": self._provider_cleanup_settled,
                "logs": list(self._logs),
                "expires_in": self.remaining_seconds(),
                "resend_after": self.resend_after_seconds(),
            }

    def mark_code_sent(self, phone: str) -> None:
        with self._condition:
            self.phone = normalize_e164_phone(phone)
            self.status = "code_sent"
            self.message = "短信验证码已发送"
            self._append_log_locked(self.message)
            self.resend_available_at = time.time() + self.resend_cooldown_seconds
            self._condition.notify_all()

    def mark_progress(self, message: str) -> None:
        with self._condition:
            if self._terminal:
                return
            self.status = "starting"
            self.message = (
                "LeadBee API 自动接码处理中"
                if self.leadbee_api
                else str(message or "正在处理手机验证")
            )
            self._append_log_locked(self.message)
            self._condition.notify_all()

    def mark_phone_acquired(self, phone: str) -> None:
        with self._condition:
            if self._terminal:
                return
            self.phone = normalize_e164_phone(phone)
            self.status = "starting"
            self.message = "已获取手机号，正在向 OpenAI 请求短信验证码"
            self._append_log_locked(self.message)
            self._condition.notify_all()

    def mark_automatic_sms_sent(self, phone: str) -> None:
        with self._condition:
            if self._terminal:
                return
            self.phone = normalize_e164_phone(phone)
            self.status = "starting"
            self.message = "短信验证码已发送，正在通过 LeadBee 自动接收"
            self._append_log_locked(self.message)
            self._condition.notify_all()

    def mark_automatic_code_received(self) -> None:
        consumed_callback = None
        with self._condition:
            if self._terminal:
                return
            if not self.leadbee_api:
                if not self._exchange_code_callback_fired:
                    consumed_callback = self._on_exchange_code_consumed
                    self._exchange_code_callback_fired = True
                self.exchange_code_consumed = True
                self.exchange_code_unusable = True
                self.exchange_code_settlement = "consumed"
            self.status = "verifying"
            self.message = "已自动获取短信验证码，正在提交验证"
            self._append_log_locked(self.message)
            self._condition.notify_all()
        if consumed_callback is not None:
            consumed_callback()

    def mark_provider_started(self) -> None:
        """Report the irreversible provider boundary exactly once."""
        provider_callback = None
        with self._condition:
            if self._cancel_event.is_set() or self._terminal:
                raise PhoneVerificationCancelled(self.message or "手机验证已取消")
            if self.provider_started:
                return
            self.provider_started = True
            provider_callback = self._on_provider_start
            self._condition.notify_all()
        if provider_callback is not None:
            provider_callback()

    def mark_provider_error(self, error_code: str = "", message: str = "") -> None:
        """Preserve the first structured provider failure for task-level recovery."""
        normalized_code = str(error_code or "").strip().upper()
        normalized_message = str(message or "").strip()
        if self.leadbee_api:
            if normalized_code:
                normalized_code = "LEADBEE_API_ERROR"
            if normalized_message:
                normalized_message = "LeadBee API 服务返回错误"
        with self._condition:
            if normalized_code and not self.provider_error_code:
                self.provider_error_code = normalized_code
            if normalized_message and not self.provider_error_message:
                self.provider_error_message = normalized_message
            self._condition.notify_all()

    def mark_exchange_code_restored(self) -> None:
        """Record the provider's explicit confirmation that the code is reusable."""
        restored_callback = None
        with self._condition:
            if self.leadbee_api:
                return
            if self.exchange_code_settlement in {"consumed", "unusable"}:
                return
            if self.exchange_code_restoration_confirmed:
                return
            self.exchange_code_restoration_confirmed = True
            self.exchange_code_settlement = "restored"
            if not self._restoration_callback_fired:
                restored_callback = self._on_exchange_code_restored
                self._restoration_callback_fired = True
            self._condition.notify_all()
        if restored_callback is not None:
            restored_callback()

    def mark_exchange_code_active_unknown(self, message: str = "") -> None:
        """Quarantine a card whose provider session never reached a known end."""
        normalized_message = str(message or "").strip()
        with self._condition:
            if self.leadbee_api:
                return
            if self.exchange_code_settlement in KNOWN_EXCHANGE_CODE_SETTLEMENTS:
                return
            self.exchange_code_settlement = "active_unknown"
            if normalized_message:
                self._append_log_locked(normalized_message)
            self._condition.notify_all()

    def mark_exchange_code_unusable(self, message: str = "") -> None:
        """Record a provider-settled/ambiguous card without relying on log text."""
        consumed_callback = None
        normalized_message = str(message or "").strip()
        with self._condition:
            if self.leadbee_api:
                return
            if self.exchange_code_settlement in {"restored", "consumed"}:
                return
            if not self._exchange_code_callback_fired:
                consumed_callback = self._on_exchange_code_consumed
                self._exchange_code_callback_fired = True
            self.exchange_code_unusable = True
            self.exchange_code_settlement = "unusable"
            if normalized_message:
                self.exchange_code_unusable_reason = normalized_message
                self._append_log_locked(normalized_message)
            self._condition.notify_all()
        if consumed_callback is not None:
            consumed_callback()

    def mark_phone_verified(self) -> None:
        with self._condition:
            if self._terminal:
                return
            self.phone_verified = True
            self.status = "verifying"
            self.message = "手机号验证码已通过，正在完成 OAuth 授权"
            self._append_log_locked(self.message)
            self._condition.notify_all()

    def attach_outcome(self, tokens: dict[str, Any]) -> dict[str, Any]:
        with self._condition:
            result = dict(tokens or {})
            result["_phone_verified"] = self.phone_verified
            result["_exchange_code_consumed"] = self.exchange_code_consumed
            result["_exchange_code_unusable"] = self.exchange_code_unusable
            result["_provider_started"] = self.provider_started
            result["_provider_error_code"] = self.provider_error_code
            result["_exchange_code_restoration_confirmed"] = (
                self.exchange_code_restoration_confirmed
            )
            if self.phone_verified and self.phone:
                result["_verified_phone_number"] = self.phone
            return result

    def begin_persisting(self) -> None:
        with self._condition:
            if self._cancel_event.is_set() or self._terminal:
                raise PhoneVerificationCancelled(self.message or "手机验证已取消")
            self._finalizing = True
            self.status = "persisting"
            self.message = "手机号验证已完成，正在安全保存 Refresh Token"
            self._append_log_locked(self.message)
            self._condition.notify_all()

    def mark_completed(
        self,
        tokens: dict[str, Any],
        *,
        completion_settled: bool = True,
    ) -> None:
        del tokens
        with self._condition:
            if self._cancel_event.is_set() and not self._finalizing:
                return
            self.tokens.clear()
            self.status = "completed"
            if self.phone_verified:
                self.message = "手机验证完成，Refresh Token 已保存"
            elif self.leadbee_api:
                self.message = (
                    "OpenAI 未要求新增手机号，LeadBee API 订单未创建；"
                    "Refresh Token 已保存"
                )
            elif self.automatic:
                self.message = (
                    "OpenAI 未要求新增手机号，LeadBee 兑换码未使用；"
                    "Refresh Token 已保存"
                )
            else:
                self.message = (
                    "OpenAI 未要求新增手机号，所填手机号未使用；"
                    "Refresh Token 已保存"
                )
            self._append_log_locked(self.message)
            self._terminal = True
            self._finalizing = False
            self._completion_settled = bool(completion_settled)
            self.terminal_at = time.time()
            self._condition.notify_all()

    def mark_completion_settled(self) -> None:
        with self._condition:
            self._completion_settled = True
            self._condition.notify_all()

    def append_log(self, message: str) -> None:
        with self._condition:
            self._append_log_locked(
                "LeadBee API 状态已更新" if self.leadbee_api else message
            )
            self._condition.notify_all()

    def mark_failed(self, message: str) -> None:
        with self._condition:
            if self._cancel_event.is_set() and self._terminal:
                return
            self.status = "failed"
            self.message = (
                "LeadBee API 自动接码失败"
                if self.leadbee_api
                else str(message or "手机验证失败")
            )
            self._append_log_locked(self.message)
            self._terminal = True
            self._finalizing = False
            self._completion_settled = True
            self.terminal_at = time.time()
            self._condition.notify_all()

    def cancel(self, message: str = "手机验证已取消") -> dict[str, Any]:
        with self._condition:
            if self._terminal:
                return self.snapshot()
            self._cancel_event.set()
            if self._finalizing:
                finalizing_message = (
                    "Refresh Token 正在保存，已忽略会中断已完成验证的取消请求"
                    if self.leadbee_api
                    else "Refresh Token 正在保存，已忽略会导致已消费卡密误判失败的取消请求"
                )
                self._append_log_locked(finalizing_message)
                self._condition.notify_all()
            else:
                self.status = "failed"
                self.message = (
                    "LeadBee API 自动接码已取消"
                    if self.leadbee_api
                    else str(message or "手机验证已取消")
                )
                self._append_log_locked(self.message)
                self._terminal = True
                self.terminal_at = time.time()
                self._command_pending = False
                self._condition.notify_all()
            return self.snapshot()

    def raise_if_cancelled(self) -> None:
        with self._condition:
            if (
                self._is_expired()
                and not self._terminal
                and not self._finalizing
            ):
                self._cancel_event.set()
                self.status = "failed"
                self.message = "手机验证会话已过期，后台流程已取消"
                self._append_log_locked(self.message)
                self._terminal = True
                self.terminal_at = time.time()
                self._command_pending = False
                self._condition.notify_all()
            cancelled = self._cancel_event.is_set()
            message = self.message
        if cancelled:
            raise PhoneVerificationCancelled(message or "手机验证已取消")

    def wait_for_cancellation(self, timeout: float) -> bool:
        return self._cancel_event.wait(max(0.0, float(timeout or 0)))

    def mark_provider_cleanup_settled(self) -> None:
        with self._condition:
            self._provider_cleanup_settled = True
            self._condition.notify_all()

    def wait_until_provider_cleanup_settled(
        self,
        timeout: float,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + max(0.0, float(timeout or 0))
        with self._condition:
            while not self._provider_cleanup_settled:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(remaining)
            return self.snapshot()

    def wait_until_ready(self, timeout: float) -> dict[str, Any]:
        deadline = time.monotonic() + max(0.0, float(timeout or 0))
        with self._condition:
            while self.status == "starting" and not self._terminal:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(remaining)
            while self.status == "completed" and not self._completion_settled:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(remaining)
            return self.snapshot()

    def wait_until_terminal(self, timeout: float) -> dict[str, Any]:
        deadline = time.monotonic() + max(0.0, float(timeout or 0))
        with self._condition:
            while not self._terminal:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(remaining)
            while self.status == "completed" and not self._completion_settled:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(remaining)
            return self.snapshot()

    def wait_until_completion_settled(self, timeout: float) -> dict[str, Any]:
        deadline = time.monotonic() + max(0.0, float(timeout or 0))
        with self._condition:
            while self.status == "completed" and not self._completion_settled:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(remaining)
            return self.snapshot()

    def request_command(
        self,
        kind: str,
        payload: str = "",
        *,
        timeout: float,
    ) -> tuple[bool, str]:
        normalized_kind = str(kind or "").strip().lower()
        if normalized_kind not in {"submit", "resend"}:
            raise ValueError("未知的手机验证操作")
        with self._condition:
            if self._is_expired():
                raise ValueError("手机验证会话已过期，请重新获取验证码")
            if self._terminal:
                raise ValueError(self.message or "手机验证会话已结束")
            if self.status != "code_sent":
                raise ValueError("短信验证码尚未发送完成")
            if self._command_pending:
                raise ValueError("已有手机验证请求正在处理，请稍候")
            if normalized_kind == "resend" and self.resend_after_seconds() > 0:
                raise ValueError(f"请在 {self.resend_after_seconds()} 秒后重新发送")

            command = PhoneVerificationCommand(
                id=uuid.uuid4().hex,
                kind=normalized_kind,
                payload=str(payload or ""),
            )
            self._commands.append(command)
            self._command_pending = True
            self.status = "verifying" if normalized_kind == "submit" else "resending"
            self.message = "正在校验短信验证码" if normalized_kind == "submit" else "正在重新发送短信验证码"
            self._append_log_locked(self.message)
            self._condition.notify_all()

            deadline = time.monotonic() + max(0.0, float(timeout or 0))
            while command.id not in self._command_results and not self._terminal:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._command_pending = False
                    self.status = "code_sent"
                    self.message = "请求处理超时，请重试"
                    self._append_log_locked(self.message)
                    raise ValueError(self.message)
                self._condition.wait(remaining)

            if command.id in self._command_results:
                ok, message = self._command_results.pop(command.id)
                return ok, message
            return self._terminal and self.status == "completed", self.message

    def wait_for_command(self) -> PhoneVerificationCommand:
        with self._condition:
            while not self._commands:
                if self._is_expired():
                    raise TimeoutError("手机验证会话已过期")
                if self._terminal:
                    raise RuntimeError(self.message or "手机验证会话已结束")
                self._condition.wait(min(1.0, max(0.05, self.expires_at - time.time())))
            return self._commands.popleft()

    def resolve_command(self, command_id: str, *, ok: bool, message: str) -> None:
        with self._condition:
            self._command_pending = False
            if not self._terminal:
                self.status = "code_sent"
                self.message = str(message or ("操作成功" if ok else "操作失败"))
                self._append_log_locked(self.message)
                if ok and "重新发送" in self.message:
                    self.resend_available_at = time.time() + self.resend_cooldown_seconds
            self._command_results[str(command_id)] = (bool(ok), self.message)
            self._condition.notify_all()


def merge_chatgpt_phone_tokens(account: Any, tokens: dict[str, Any]) -> None:
    if hasattr(account, "get_extra") and callable(account.get_extra):
        extra = account.get_extra()
    else:
        extra = dict(getattr(account, "extra", None) or {})

    access_token = str(tokens.get("access_token") or "").strip()
    refresh_token = str(tokens.get("refresh_token") or "").strip()
    if not refresh_token:
        raise ValueError("手机验证完成后未获取 Refresh Token")

    if access_token:
        extra["access_token"] = access_token
        account.token = access_token
    elif not extra.get("access_token") and getattr(account, "token", ""):
        extra["access_token"] = account.token
    extra["refresh_token"] = refresh_token

    for key in ("id_token", "session_token", "workspace_id"):
        value = str(tokens.get(key) or "").strip()
        if value:
            extra[key] = value
    phone_verified = bool(tokens.get("_phone_verified", True))
    exchange_code_consumed = bool(tokens.get("_exchange_code_consumed", False))
    verified_phone = str(tokens.get("_verified_phone_number") or "").strip()
    verification = {
        "status": "completed" if phone_verified else "not_required",
        "phone_verified": phone_verified,
        "exchange_code_consumed": exchange_code_consumed,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    if phone_verified and verified_phone:
        verification["phone_number"] = verified_phone
    extra["chatgpt_phone_verification_required"] = False
    extra["chatgpt_phone_verification"] = verification

    if hasattr(account, "set_extra") and callable(account.set_extra):
        account.set_extra(extra)
    else:
        account.extra = extra


class ChatGPTPhoneVerificationManager:
    def __init__(
        self,
        *,
        flow_runner: Optional[Callable[[int, str, InteractivePhoneVerificationBroker], dict[str, Any]]] = None,
        automatic_flow_runner: Optional[
            Callable[[int, str, InteractivePhoneVerificationBroker], dict[str, Any]]
        ] = None,
        token_persister: Optional[Callable[[int, dict[str, Any]], None]] = None,
        status_refresher: Optional[Callable[[int], None]] = None,
        ttl_seconds: int = 600,
        resend_cooldown_seconds: int = 60,
        start_timeout_seconds: float = 1.0,
        command_timeout_seconds: float = 60.0,
        terminal_retention_seconds: int = 300,
        max_retained_sessions: int = 200,
    ):
        self.flow_runner = flow_runner or run_interactive_phone_oauth_flow
        self.automatic_flow_runner = automatic_flow_runner or run_leadbee_phone_oauth_flow
        self.token_persister = token_persister or persist_phone_verification_tokens
        self.status_refresher = status_refresher or refresh_account_after_phone_verification
        self.ttl_seconds = max(1, int(ttl_seconds))
        self.resend_cooldown_seconds = max(0, int(resend_cooldown_seconds))
        self.start_timeout_seconds = max(0.0, float(start_timeout_seconds))
        self.command_timeout_seconds = max(0.1, float(command_timeout_seconds))
        self.terminal_retention_seconds = max(0, int(terminal_retention_seconds))
        self.max_retained_sessions = max(1, int(max_retained_sessions))
        self._request_fingerprint_key = secrets.token_bytes(32)
        self._lock = threading.Lock()
        self._sessions: dict[str, InteractivePhoneVerificationBroker] = {}
        self._account_sessions: dict[int, str] = {}

    def _fingerprint_request(self, provider: str, value: str) -> str:
        payload = f"{provider}\0{value}".encode("utf-8")
        return hmac.new(
            self._request_fingerprint_key,
            payload,
            hashlib.sha256,
        ).hexdigest()

    def _remove_session_locked(
        self,
        session_id: str,
        *,
        cancel_message: str = "手机验证会话已结束",
    ) -> bool:
        broker = self._sessions.get(session_id)
        if broker is None:
            return True
        if not bool(getattr(broker, "_terminal", False)):
            cancelled = broker.cancel(cancel_message)
            if cancelled.get("status") not in {"completed", "failed", "expired"}:
                return False
        if (
            broker.automatic
            and not bool(
                broker.snapshot().get("provider_cleanup_settled", False)
            )
        ):
            # The provider worker still owns (or is waiting for) its semaphore
            # permit. Never retire the only snapshot that can carry its final
            # restored/active_unknown settlement.
            return False
        self._sessions.pop(session_id, None)
        current = self._account_sessions.get(broker.account_id)
        if current == session_id:
            self._account_sessions.pop(broker.account_id, None)
        return True

    def _begin_persisting_if_owner(
        self,
        broker: InteractivePhoneVerificationBroker,
    ) -> None:
        with self._lock:
            current = self._account_sessions.get(broker.account_id)
            if current != broker.session_id:
                broker.cancel("手机验证会话已被更新的请求替换")
                raise PhoneVerificationCancelled("手机验证会话已被更新的请求替换")
            broker.begin_persisting()

    def _prune_locked(self) -> None:
        now = time.time()
        stale_ids = []
        for session_id, broker in self._sessions.items():
            terminal_at = float(getattr(broker, "terminal_at", 0.0) or 0.0)
            if terminal_at and now - terminal_at >= self.terminal_retention_seconds:
                stale_ids.append(session_id)
                continue
            if (
                not bool(getattr(broker, "_finalizing", False))
                and now >= broker.expires_at + self.terminal_retention_seconds
            ):
                stale_ids.append(session_id)
        for session_id in stale_ids:
            self._remove_session_locked(
                session_id,
                cancel_message="手机验证会话已过期，后台流程已取消",
            )

        excess = len(self._sessions) - self.max_retained_sessions
        if excess <= 0:
            return
        terminal_sessions = sorted(
            (
                (
                    float(getattr(broker, "terminal_at", 0.0) or broker.expires_at),
                    session_id,
                )
                for session_id, broker in self._sessions.items()
                if bool(getattr(broker, "_terminal", False))
                or (
                    not bool(getattr(broker, "_finalizing", False))
                    and now >= broker.expires_at
                )
            ),
        )
        for _, session_id in terminal_sessions[:excess]:
            self._remove_session_locked(
                session_id,
                cancel_message="手机验证会话已过期，后台流程已取消",
            )

    def _get(self, account_id: int, session_id: str) -> InteractivePhoneVerificationBroker:
        with self._lock:
            self._prune_locked()
            broker = self._sessions.get(str(session_id or ""))
            if not broker or broker.account_id != int(account_id):
                raise ValueError("手机验证会话不存在，请重新获取验证码")
            if broker.snapshot()["status"] == "expired":
                broker.cancel(
                    "手机验证会话已过期，后台流程已取消"
                )
            return broker

    def _refresh_status_best_effort(
        self,
        broker: InteractivePhoneVerificationBroker,
    ) -> None:
        try:
            self.status_refresher(broker.account_id)
        except Exception as exc:
            logger.warning(
                "ChatGPT phone verification status refresh deferred (%s)",
                type(exc).__name__,
            )
            broker.append_log("Refresh Token 已保存，账号状态刷新稍后重试")

    def _run(self, broker: InteractivePhoneVerificationBroker) -> None:
        try:
            tokens = self.flow_runner(broker.account_id, broker.phone, broker)
            broker.raise_if_cancelled()
            if not isinstance(tokens, dict) or not str(tokens.get("refresh_token") or "").strip():
                raise RuntimeError("手机验证完成后未获取 Refresh Token")
            completed_tokens = broker.attach_outcome(tokens)
            broker.raise_if_cancelled()
            self._begin_persisting_if_owner(broker)
            self.token_persister(broker.account_id, completed_tokens)
            broker.mark_completed(completed_tokens, completion_settled=False)
            self._refresh_status_best_effort(broker)
            broker.mark_completion_settled()
        except Exception as exc:
            broker.mark_failed(str(exc) or "手机验证失败")
        finally:
            with self._lock:
                current = self._account_sessions.get(broker.account_id)
                if current == broker.session_id and broker.status in {"completed", "failed"}:
                    self._account_sessions.pop(broker.account_id, None)
                self._prune_locked()

    def _run_automatic(
        self,
        broker: InteractivePhoneVerificationBroker,
        exchange_code: str,
        provider_lock_already_held: bool = False,
    ) -> None:
        provider_lock_owned = bool(provider_lock_already_held)
        provider_cleanup_published = False

        def settle_provider_cleanup() -> None:
            nonlocal provider_lock_owned, provider_cleanup_published
            if provider_cleanup_published:
                return
            if provider_lock_owned:
                leadbee_phone_flow_lock.release()
                provider_lock_owned = False
            settlement = broker.snapshot()
            if (
                not broker.leadbee_api
                and bool(settlement.get("provider_started"))
                and str(settlement.get("exchange_code_settlement") or "")
                not in KNOWN_EXCHANGE_CODE_SETTLEMENTS
            ):
                broker.mark_exchange_code_active_unknown(
                    "LeadBee provider worker 已结束或超时，"
                    "服务端未明确确认卡密恢复；卡密保持隔离"
                )
            broker.mark_provider_cleanup_settled()
            provider_cleanup_published = True

        try:
            if not provider_lock_already_held:
                provider_lock_owned = leadbee_phone_flow_lock.acquire(
                    blocking=False
                )
                slot_wait_seconds = min(
                    LEADBEE_PROVIDER_SLOT_WAIT_SECONDS,
                    max(
                        0.0,
                        float(broker.expires_at)
                        - time.time()
                        - LEADBEE_PROVIDER_SETTLEMENT_MARGIN_SECONDS,
                    ),
                )
                slot_deadline = time.monotonic() + slot_wait_seconds
                while not provider_lock_owned:
                    broker.raise_if_cancelled()
                    remaining = slot_deadline - time.monotonic()
                    if remaining <= 0:
                        if broker.leadbee_api:
                            raise RuntimeError("LeadBee API 服务并发槽位排队超时")
                        raise RuntimeError("LeadBee 服务并发槽位排队超时，兑换码尚未激活")
                    provider_lock_owned = leadbee_phone_flow_lock.acquire(
                        timeout=min(0.25, remaining)
                    )
                broker.raise_if_cancelled()
            try:
                # The built-in LeadBee runner applies one absolute deadline to
                # every request, poll and sleep.  Keep it in this worker: a
                # Python thread cannot safely terminate another thread, and
                # publishing cleanup while provider code is still executing
                # would allow more than five live provider operations and late
                # settlement callbacks after the task has already returned.
                tokens = self.automatic_flow_runner(
                    broker.account_id,
                    exchange_code,
                    broker,
                )
            finally:
                # The provider/card lifecycle ends with the runner. Token DB
                # persistence and status refresh must not retain a scarce
                # LeadBee slot if either downstream operation blocks.
                settle_provider_cleanup()
            broker.raise_if_cancelled()
            if not isinstance(tokens, dict) or not str(tokens.get("refresh_token") or "").strip():
                raise RuntimeError("手机验证完成后未获取 Refresh Token")
            completed_tokens = broker.attach_outcome(tokens)
            broker.raise_if_cancelled()
            self._begin_persisting_if_owner(broker)
            self.token_persister(broker.account_id, completed_tokens)
            broker.mark_completed(completed_tokens, completion_settled=False)
            self._refresh_status_best_effort(broker)
            broker.mark_completion_settled()
        except Exception as exc:
            if broker.leadbee_api:
                broker.mark_failed("LeadBee API 自动接码失败")
            else:
                broker.mark_failed(
                    _redact_secret(exc, exchange_code) or "LeadBee 自动接码失败"
                )
        finally:
            # Slot-wait and other pre-run failures also publish cleanup. Both
            # operations stay outside the manager lock, avoiding a lock /
            # broker-condition cycle during expiry or cancellation.
            settle_provider_cleanup()
            with self._lock:
                current = self._account_sessions.get(broker.account_id)
                if (
                    current == broker.session_id
                    and broker.status in {"completed", "failed"}
                ):
                    self._account_sessions.pop(broker.account_id, None)
                self._prune_locked()

    def start(
        self,
        account_id: int,
        phone: Optional[str] = None,
        *,
        leadbee_code: Optional[str] = None,
        leadbee_api: bool = False,
        client_order_id: Optional[str] = None,
        leadbee_base_url: Optional[str] = None,
        on_provider_start: Optional[Callable[[], None]] = None,
        on_exchange_code_consumed: Optional[Callable[[], None]] = None,
        on_exchange_code_restored: Optional[Callable[[], None]] = None,
        provider_lock_already_held: bool = False,
        on_provider_lock_handoff: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        api_mode = bool(leadbee_api)
        if api_mode and (str(phone or "").strip() or leadbee_code is not None):
            raise ValueError("手机号、LeadBee 兑换码和 API 只能选择一种接码方式")
        if not api_mode and client_order_id is not None:
            raise ValueError("客户端订单标识仅用于 LeadBee API 模式")
        if api_mode:
            _require_leadbee_api_config()

        automatic = api_mode or leadbee_code is not None
        exchange_code = str(leadbee_code or "").strip()
        receive_base_url = (
            "" if api_mode else str(leadbee_base_url or "").strip().rstrip("/")
        )
        if api_mode:
            exchange_code = str(client_order_id or "").strip() or (
                f"aar_{secrets.token_hex(16)}"
            )
            if not LEADBEE_API_CLIENT_ORDER_RE.fullmatch(exchange_code):
                raise ValueError("LeadBee API 客户端订单标识无效")
            normalized_phone = ""
            provider = "leadbee"
            request_fingerprint = self._fingerprint_request(provider, "api")
        elif automatic:
            if not exchange_code:
                raise ValueError("请输入 LeadBee 兑换码")
            normalized_phone = ""
            provider = "leadbee"
            request_fingerprint = self._fingerprint_request(
                provider,
                f"{exchange_code}\0{receive_base_url}",
            )
        else:
            normalized_phone = normalize_e164_phone(str(phone or ""))
            provider = "manual"
            request_fingerprint = self._fingerprint_request(provider, normalized_phone)
        account_key = int(account_id)
        with self._lock:
            self._prune_locked()
            existing_id = self._account_sessions.get(account_key)
            if existing_id:
                existing = self._sessions.get(existing_id)
                if existing:
                    existing_snapshot = existing.snapshot()
                    if existing_snapshot["status"] in {
                        "completed",
                        "failed",
                        "expired",
                    }:
                        removed = self._remove_session_locked(
                            existing_id,
                            cancel_message=(
                                "手机验证会话已过期，新请求启动前已安全取消旧流程"
                            ),
                        )
                        if not removed:
                            existing_snapshot = existing.snapshot()
                            if existing.matches_request(
                                provider,
                                request_fingerprint,
                            ):
                                existing_snapshot["reused"] = True
                                existing_snapshot["message"] = (
                                    "当前接码会话仍在清理服务端终态，请稍候"
                                )
                                return existing_snapshot
                            raise ValueError(
                                "该账号上一接码会话仍在清理服务端终态；"
                                "请等待清理完成后再启动新卡"
                            )
                    if existing_snapshot["status"] not in {
                        "completed",
                        "failed",
                        "expired",
                    }:
                        if not existing.matches_request(provider, request_fingerprint):
                            raise ValueError(
                                "该账号已有不同接码请求正在进行；"
                                "为防止卡密错配，请等待当前会话结束或先取消当前会话"
                            )
                        existing_snapshot["reused"] = True
                        existing_snapshot["message"] = (
                            "已恢复当前验证会话，未重复发送短信验证码"
                        )
                        return existing_snapshot
            broker = InteractivePhoneVerificationBroker(
                account_id=account_key,
                phone=normalized_phone,
                provider=provider,
                leadbee_api=api_mode,
                client_order_id=exchange_code if api_mode else "",
                request_fingerprint=request_fingerprint,
                ttl_seconds=self.ttl_seconds,
                resend_cooldown_seconds=self.resend_cooldown_seconds,
                on_provider_start=on_provider_start,
                on_exchange_code_consumed=on_exchange_code_consumed,
                on_exchange_code_restored=on_exchange_code_restored,
            )
            broker.leadbee_base_url = receive_base_url
            self._sessions[broker.session_id] = broker
            self._account_sessions[account_key] = broker.session_id

        try:
            if automatic:
                worker = threading.Thread(
                    target=self._run_automatic,
                    args=(broker, exchange_code, bool(provider_lock_already_held)),
                    daemon=True,
                )
            else:
                worker = threading.Thread(target=self._run, args=(broker,), daemon=True)
            worker.start()
        except Exception:
            # No worker owns this broker or a pre-acquired provider permit.
            # Retire the dead mapping immediately so the same account is not
            # blocked by an uncleanable `starting` session forever.
            broker.mark_failed("手机验证后台线程启动失败")
            if automatic:
                broker.mark_provider_cleanup_settled()
            with self._lock:
                self._sessions.pop(broker.session_id, None)
                if self._account_sessions.get(account_key) == broker.session_id:
                    self._account_sessions.pop(account_key, None)
            raise
        if automatic and provider_lock_already_held and callable(on_provider_lock_handoff):
            # From this point onward the worker owns the pre-acquired permit
            # and releases it before publishing cleanup settlement.
            on_provider_lock_handoff(broker.session_id)
        return broker.wait_until_ready(self.start_timeout_seconds)

    def status(self, account_id: int, session_id: str) -> dict[str, Any]:
        broker = self._get(account_id, session_id)
        snapshot = broker.snapshot()
        if snapshot["status"] == "completed":
            return broker.wait_until_completion_settled(self.start_timeout_seconds)
        if snapshot["status"] != "expired":
            return snapshot

        with self._lock:
            current = self._sessions.get(str(session_id or ""))
            if current is not broker:
                raise ValueError("手机验证会话不存在，请重新获取验证码")
            snapshot = broker.snapshot()
            if snapshot["status"] != "expired":
                return snapshot
            broker.cancel(
                "手机验证会话已过期，后台流程已取消"
            )
            return broker.snapshot()

    def wait_for_provider_cleanup(
        self,
        account_id: int,
        session_id: str,
        timeout: float = 1.0,
    ) -> dict[str, Any]:
        """Wait directly on a broker when normal status observation fails."""
        normalized_session_id = str(session_id or "")
        with self._lock:
            broker = self._sessions.get(normalized_session_id)
        if not broker or broker.account_id != int(account_id):
            # Automatic sessions are removed only after cleanup settlement.
            return {
                "session_id": normalized_session_id,
                "account_id": int(account_id),
                "status": "failed",
                "message": "LeadBee 会话已完成后台清理",
                "provider_cleanup_settled": True,
                "exchange_code_settlement": "active_unknown",
                "exchange_code_unusable": False,
            }
        cleanup = broker.wait_until_provider_cleanup_settled(timeout)
        if (
            bool(cleanup.get("provider_cleanup_settled", False))
            and str(cleanup.get("status") or "")
            not in {"completed", "failed", "expired"}
        ):
            return broker.wait_until_terminal(timeout)
        return cleanup

    def submit_code(self, account_id: int, session_id: str, code: str) -> dict[str, Any]:
        broker = self._get(account_id, session_id)
        normalized_code = normalize_phone_code(code)
        ok, message = broker.request_command(
            "submit",
            normalized_code,
            timeout=self.command_timeout_seconds,
        )
        if not ok:
            snapshot = broker.snapshot()
            snapshot["message"] = message
            return snapshot
        return broker.wait_until_terminal(self.command_timeout_seconds)

    def resend(self, account_id: int, session_id: str) -> dict[str, Any]:
        broker = self._get(account_id, session_id)
        ok, message = broker.request_command(
            "resend",
            timeout=self.command_timeout_seconds,
        )
        snapshot = broker.snapshot()
        snapshot["message"] = message
        if not ok:
            snapshot["status"] = "code_sent"
        return snapshot

    def cancel(
        self,
        account_id: int,
        session_id: str,
        message: str = "手机验证已取消",
    ) -> dict[str, Any]:
        account_key = int(account_id)
        normalized_session_id = str(session_id or "")
        with self._lock:
            self._prune_locked()
            broker = self._sessions.get(normalized_session_id)
        if not broker or broker.account_id != account_key:
            raise ValueError("手机验证会话不存在，请重新获取验证码")
        snapshot = broker.cancel(message)
        if broker.automatic and snapshot.get("status") in {"failed", "expired"}:
            snapshot = broker.wait_until_provider_cleanup_settled(
                self.command_timeout_seconds
            )
        with self._lock:
            current = self._account_sessions.get(account_key)
            if current == normalized_session_id and snapshot.get("status") in {
                "completed",
                "failed",
                "expired",
            } and (
                not broker.automatic
                or bool(snapshot.get("provider_cleanup_settled", False))
            ):
                self._account_sessions.pop(account_key, None)
        return snapshot


def _load_account_and_email_service(account_id: int):
    from sqlalchemy import func
    from sqlmodel import Session, select

    from core.base_mailbox import MailboxAccount, create_mailbox
    from core.db import AccountModel, OutlookAccountModel, engine

    with Session(engine) as session:
        account = session.get(AccountModel, int(account_id))
        if not account or account.platform != "chatgpt":
            raise RuntimeError("ChatGPT 账号不存在")
        account_email = str(account.email or "").strip()
        account_password = str(account.password or "")
        account_extra = account.get_extra()

        mailbox_context = account_extra.get("mailbox_login_context")
        if not isinstance(mailbox_context, dict):
            imported_mailbox = session.exec(
                select(OutlookAccountModel)
                .where(func.lower(OutlookAccountModel.email) == account_email.lower())
                .where(OutlookAccountModel.enabled == True)
            ).first()
            if imported_mailbox is not None:
                mailbox_extra = {
                    "provider": "microsoft",
                    "password": str(imported_mailbox.password or ""),
                    "client_id": str(imported_mailbox.client_id or ""),
                    "refresh_token": str(imported_mailbox.refresh_token or ""),
                    "account_type": str(
                        getattr(imported_mailbox, "account_type", "microsoft_oauth")
                        or "microsoft_oauth"
                    ),
                    "mailapi_url": str(
                        getattr(imported_mailbox, "mailapi_url", "") or ""
                    ),
                }
                mailbox_context = {
                    "provider": "microsoft",
                    "email": str(imported_mailbox.email or account_email).strip(),
                    "account_id": str(imported_mailbox.id or ""),
                    "extra": mailbox_extra,
                }
                account_extra["mailbox_login_context"] = mailbox_context
                account.set_extra(account_extra)
                account.updated_at = datetime.now(timezone.utc)
                session.add(account)
                session.commit()

    if not account_email:
        raise RuntimeError("账号邮箱未填写")
    mailbox_context = account_extra.get("mailbox_login_context")
    if not isinstance(mailbox_context, dict):
        raise RuntimeError(
            f"账号缺少邮箱接码凭据，请重新导入 {account_email} 的邮箱凭据后再接码"
        )
    provider = str(mailbox_context.get("provider") or "").strip().lower()
    if provider == "custom_provider":
        provider = str((mailbox_context.get("extra") or {}).get("provider") or "").strip().lower()
    if provider in {"outlook", "microsoft"}:
        provider = "microsoft"
    if not provider:
        raise RuntimeError("账号邮箱接码来源为空，请重新执行邮箱登录")

    mailbox_extra = dict(mailbox_context.get("extra") or {})
    mailbox = create_mailbox(provider, extra=mailbox_extra)
    mailbox_account = MailboxAccount(
        email=str(mailbox_context.get("email") or account_email).strip(),
        account_id=str(mailbox_context.get("account_id") or ""),
        extra=mailbox_extra,
    )
    before_ids = set(mailbox.get_current_ids(mailbox_account) or [])

    class PersistedEmailService:
        service_type = type("ServiceType", (), {"value": provider})()

        def create_email(self, config=None):
            return {
                "email": mailbox_account.email,
                "service_id": mailbox_account.account_id,
                "token": "",
            }

        def get_verification_code(
            self,
            email=None,
            email_id=None,
            timeout=120,
            pattern=None,
            otp_sent_at=None,
            exclude_codes=None,
        ):
            return mailbox.wait_for_code(
                mailbox_account,
                keyword="",
                timeout=timeout,
                before_ids=before_ids,
                otp_sent_at=otp_sent_at,
                exclude_codes=exclude_codes,
            )

    return account_email, account_password, account_extra, PersistedEmailService()


def _load_account_context(account_id: int) -> tuple[str, str, dict[str, Any]]:
    """Load only the persisted ChatGPT/OAuth state needed by staged phone flows."""
    from sqlmodel import Session

    from core.db import AccountModel, engine

    with Session(engine) as session:
        account = session.get(AccountModel, int(account_id))
        if not account or account.platform != "chatgpt":
            raise RuntimeError("ChatGPT 账号不存在")
        account_email = str(account.email or "").strip()
        account_password = str(account.password or "")
        account_extra = account.get_extra()
    if not account_email:
        raise RuntimeError("账号邮箱未填写")
    return account_email, account_password, account_extra


def _take_phone_oauth_resume_context(email: str, account_extra: dict[str, Any]):
    from platforms.chatgpt.oauth_resume_cache import (
        oauth_resume_cache,
        restore_oauth_resume_context,
    )

    def is_prepared(context: Any) -> bool:
        return bool(
            context is not None
            and str(getattr(context, "code_verifier", "") or "").strip()
            and str(getattr(context, "oauth_state", "") or "").strip()
            and getattr(context, "flow_state", None) is not None
        )

    memory_context = oauth_resume_cache.take(email)
    if is_prepared(memory_context):
        return memory_context, "memory"
    persisted_context = restore_oauth_resume_context(
        account_extra.get("oauth_resume_context")
    )
    if is_prepared(persisted_context):
        return persisted_context, "persisted"
    if memory_context is not None or persisted_context is not None:
        raise RuntimeError(
            "当前 Access Token 来自旧版登录流程，缺少可续接的手机授权事务；"
            "请重新执行一次邮箱登录。本次未获取手机号、未发送短信"
        )
    raise RuntimeError(
        "手机验证授权事务不存在或已过期，请重新执行一次邮箱登录；"
        "本次未获取手机号、未发送短信"
    )


def run_interactive_phone_oauth_flow(
    account_id: int,
    phone: str,
    broker: InteractivePhoneVerificationBroker,
) -> dict[str, Any]:
    from core.config_store import config_store
    from platforms.chatgpt.oauth_client import OAuthClient

    email, password, account_extra = _load_account_context(account_id)
    config = config_store.get_all().copy()
    config.update(
        {
            "chatgpt_phone_number": phone,
            "chatgpt_interactive_phone_broker": broker,
        }
    )
    proxy = str(account_extra.get("proxy_used") or "").strip() or None
    oauth_client = OAuthClient(config, proxy=proxy, verbose=False, browser_mode="protocol")
    resume_context, resume_source = _take_phone_oauth_resume_context(email, account_extra)
    if resume_source == "memory":
        broker.mark_progress("正在续接登录时预建的手机授权事务并请求短信验证码")
    else:
        broker.mark_progress("正在恢复登录时预建的手机授权事务并请求短信验证码")
    oauth_client.adopt_browser_context(
        resume_context.session,
        device_id=resume_context.device_id,
        user_agent=resume_context.user_agent,
        sec_ch_ua=resume_context.sec_ch_ua,
        accept_language=resume_context.accept_language,
    )
    tokens = oauth_client.login_and_get_tokens(
        email,
        password,
        device_id=resume_context.device_id,
        user_agent=resume_context.user_agent,
        sec_ch_ua=resume_context.sec_ch_ua,
        impersonate=resume_context.impersonate,
        skymail_client=None,
        prefer_passwordless_login=False,
        allow_phone_verification=True,
        force_new_browser=False,
        resume_authenticated_session=False,
        force_chatgpt_entry=False,
        screen_hint="login",
        force_password_login=False,
        complete_about_you_if_needed=False,
        login_source="interactive_phone_verification",
        prepared_oauth_context=resume_context,
    )
    if not tokens:
        raise RuntimeError(oauth_client.last_error or "手机验证 OAuth 登录失败")
    result = dict(tokens)
    result.setdefault("workspace_id", str(oauth_client.last_workspace_id or "").strip())
    return result


def run_leadbee_phone_oauth_flow(
    account_id: int,
    exchange_code: str,
    broker: InteractivePhoneVerificationBroker,
) -> dict[str, Any]:
    from core.config_store import config_store
    from platforms.chatgpt.oauth_client import OAuthClient

    config = config_store.get_all().copy()
    api_mode = getattr(broker, "leadbee_api", False) is True
    if api_mode:
        _validate_leadbee_api_config(config)
        client_order_id = str(getattr(broker, "client_order_id", "") or "").strip()
        if not LEADBEE_API_CLIENT_ORDER_RE.fullmatch(client_order_id):
            raise ValueError("LeadBee API 客户端订单标识无效")
    else:
        normalized_code = str(exchange_code or "").strip()
        if not normalized_code:
            raise ValueError("请输入 LeadBee 兑换码")

    email, password, account_extra = _load_account_context(account_id)
    for key in (
        "chatgpt_phone_number",
        "openai_phone_number",
        "phone_number",
        "chatgpt_phone_otp_codes",
        "chatgpt_phone_otp_code",
        "openai_phone_otp_codes",
        "openai_phone_otp_code",
        "phone_otp_codes",
        "phone_otp_code",
        "chatgpt_interactive_phone_broker",
    ):
        config.pop(key, None)
    config.update(
        {
            "chatgpt_phone_provider": "leadbee",
            "chatgpt_phone_progress_broker": broker,
        }
    )
    if api_mode:
        config.pop("leadbee_code", None)
        config.pop("leadbee_base_url", None)
        config.update(
            {
                "leadbee_api_enabled": "1",
                "leadbee_api_client_order_id": client_order_id,
            }
        )
    else:
        config["leadbee_api_enabled"] = "0"
        config["leadbee_code"] = normalized_code
        leadbee_base_url = str(
            getattr(broker, "leadbee_base_url", "") or ""
        ).strip().rstrip("/")
        if leadbee_base_url:
            config["leadbee_base_url"] = leadbee_base_url
    proxy = str(account_extra.get("proxy_used") or "").strip() or None
    oauth_client = OAuthClient(config, proxy=proxy, verbose=False, browser_mode="protocol")
    resume_context, resume_source = _take_phone_oauth_resume_context(email, account_extra)
    if resume_source == "memory":
        broker.mark_progress("正在续接登录时预建的手机授权事务并启动 LeadBee 自动接码")
    else:
        broker.mark_progress("正在恢复登录时预建的手机授权事务并启动 LeadBee 自动接码")
    oauth_client.adopt_browser_context(
        resume_context.session,
        device_id=resume_context.device_id,
        user_agent=resume_context.user_agent,
        sec_ch_ua=resume_context.sec_ch_ua,
        accept_language=resume_context.accept_language,
    )
    tokens = oauth_client.login_and_get_tokens(
        email,
        password,
        device_id=resume_context.device_id,
        user_agent=resume_context.user_agent,
        sec_ch_ua=resume_context.sec_ch_ua,
        impersonate=resume_context.impersonate,
        skymail_client=None,
        prefer_passwordless_login=False,
        allow_phone_verification=True,
        force_new_browser=False,
        resume_authenticated_session=False,
        force_chatgpt_entry=False,
        screen_hint="login",
        force_password_login=False,
        complete_about_you_if_needed=False,
        login_source="automatic_phone_verification",
        prepared_oauth_context=resume_context,
    )
    if not tokens:
        raise RuntimeError(oauth_client.last_error or "LeadBee 自动接码 OAuth 登录失败")
    result = dict(tokens)
    result.setdefault("workspace_id", str(oauth_client.last_workspace_id or "").strip())
    return result


def persist_phone_verification_tokens(account_id: int, tokens: dict[str, Any]) -> None:
    from sqlmodel import Session

    from core.db import AccountModel, engine

    with Session(engine) as session:
        account = session.get(AccountModel, int(account_id))
        if not account or account.platform != "chatgpt":
            raise RuntimeError("ChatGPT 账号不存在")
        merge_chatgpt_phone_tokens(account, tokens)
        account.updated_at = datetime.now(timezone.utc)
        session.add(account)
        session.commit()


def refresh_account_after_phone_verification(account_id: int) -> None:
    from services.chatgpt_account_refresh import refresh_chatgpt_account_by_id

    refresh_chatgpt_account_by_id(account_id)
    sync_account_after_phone_verification(account_id)


def sync_account_after_phone_verification(account_id: int) -> None:
    try:
        from sqlmodel import Session

        from core.db import AccountModel, engine
        from services.external_sync import sync_codex2api_account

        with Session(engine) as session:
            account = session.get(AccountModel, int(account_id))
            if not account or account.platform != "chatgpt":
                raise RuntimeError("ChatGPT 账号不存在")

        sync_codex2api_account(account)
    except Exception as exc:
        message = "手机验证完成后自动同步异常"
        logger.error("%s (%s)", message, type(exc).__name__)


phone_verification_manager = ChatGPTPhoneVerificationManager()
