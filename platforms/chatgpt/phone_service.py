from __future__ import annotations

import hashlib
import math
import re
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Optional
from urllib.parse import quote

try:
    from curl_cffi import requests as http_requests
except ImportError:
    import requests as http_requests

from smstome_tool import (
    PhoneEntry,
    get_unused_phone,
    mark_phone_blacklisted,
    parse_country_slugs,
    update_global_phone_list,
    wait_for_otp,
)

from .leadbee_open_api import (
    LeadBeeAPIError,
    LeadBeeOpenAPIClient,
    LeadBeeResponseError,
    LeadBeeTransportError,
)
from .leadbee_runtime import leadbee_api_rate_limiter


def _to_positive_int(value, default: int, *, minimum: int = 1) -> int:
    try:
        parsed = int(str(value).strip())
    except Exception:
        return default
    return parsed if parsed >= minimum else default


def _prefix_hint(phone: str, width: int = 7) -> str:
    value = str(phone or "").strip()
    return value[: min(len(value), width)] if value else ""


def _truthy(value: Any) -> bool:
    if value is True or value == 1:
        return True
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


class SMSToMePhoneService:
    provider_name = "SMSToMe"
    supports_resend = True
    supports_blacklist = True
    requires_explicit_replacement = False
    supports_cancellation = False

    def __init__(self, config: Optional[dict] = None, log_fn: Optional[Callable[[str], None]] = None):
        self.config = dict(config or {})
        self.log_fn = log_fn or (lambda _msg: None)
        self.cookie_header = str(self.config.get("smstome_cookie", "") or "").strip() or None
        self.country_slugs = parse_country_slugs(self.config.get("smstome_country_slugs"))
        self.global_file = Path(str(self.config.get("smstome_global_file") or "smstome_all_numbers.txt"))
        self.used_numbers_dir = Path(str(self.config.get("smstome_used_numbers_dir") or "smstome_used"))
        self.task_name = str(self.config.get("smstome_task_name") or "chatgpt_add_phone").strip() or "chatgpt_add_phone"
        self.max_attempts = _to_positive_int(self.config.get("smstome_phone_attempts"), 3)
        self.otp_timeout_seconds = _to_positive_int(self.config.get("smstome_otp_timeout_seconds"), 45, minimum=10)
        self.poll_interval_seconds = _to_positive_int(self.config.get("smstome_poll_interval_seconds"), 5, minimum=1)
        self.sync_max_pages_per_country = _to_positive_int(
            self.config.get("smstome_sync_max_pages_per_country"),
            5,
        )

    @property
    def enabled(self) -> bool:
        return self._has_pool_file() or bool(self.cookie_header)

    def prefix_hint(self, phone: str) -> str:
        return _prefix_hint(phone)

    def _has_pool_file(self) -> bool:
        try:
            return self.global_file.exists() and self.global_file.stat().st_size > 0
        except OSError:
            return False

    def ensure_pool_ready(self) -> None:
        if self._has_pool_file():
            return
        if not self.cookie_header:
            raise RuntimeError("未找到 SMSToMe 号码池文件，且未配置 smstome_cookie")

        self.log_fn("SMSToMe 号码池不存在，开始自动同步...")
        count = update_global_phone_list(
            cookie_header=self.cookie_header,
            countries=self.country_slugs or None,
            output_path=self.global_file,
            max_pages_per_country=self.sync_max_pages_per_country,
        )
        if count <= 0:
            raise RuntimeError("SMSToMe 号码池同步后为空")
        self.log_fn(f"SMSToMe 号码池同步完成，共 {count} 个号码")

    def acquire_phone(self, *, exclude_prefixes: Optional[Iterable[str]] = None) -> Optional[PhoneEntry]:
        self.ensure_pool_ready()
        return get_unused_phone(
            self.task_name,
            country_slug=self.country_slugs or None,
            global_file=self.global_file,
            used_numbers_dir=self.used_numbers_dir,
            exclude_prefixes=exclude_prefixes,
        )

    def mark_blacklisted(self, phone: str) -> None:
        mark_phone_blacklisted(self.task_name, phone, used_numbers_dir=self.used_numbers_dir)

    def wait_for_code(self, entry: PhoneEntry, *, timeout: Optional[int] = None) -> Optional[str]:
        wait_seconds = _to_positive_int(timeout, self.otp_timeout_seconds, minimum=10)
        return wait_for_otp(
            entry,
            cookie_header=self.cookie_header,
            timeout=wait_seconds,
            poll_interval=self.poll_interval_seconds,
            trace=lambda message: self.log_fn(f"[SMSToMe] {message}"),
            raise_on_timeout=False,
        )


LEADBEE_ERROR_MESSAGES = {
    "ACTIVE_LIMIT_REACHED": "LeadBee 当前最多同时处理 5 张兑换码",
    "CARD_ALREADY_USED": "LeadBee 兑换码已使用",
    "CARD_NOT_FOUND": "LeadBee 兑换码无效",
    "CARD_NOT_IN_SESSION": "LeadBee 兑换码正在另一会话中处理",
}


class LeadBeeOperationRejected(RuntimeError):
    def __init__(self, message: str, *, error_code: str = "") -> None:
        super().__init__(message)
        self.error_code = str(error_code or "").strip()


class _LeadBeeFlowDeadlineExceeded(RuntimeError):
    """The worker-owned provider deadline elapsed before an operation settled."""


class LeadBeePhoneService:
    """LeadBee 兑换码接码服务。

    LeadBee 依靠浏览器 Cookie 绑定兑换码任务，所以激活、取号和收码必须
    复用同一个 HTTP Session。兑换码仅保存在当前服务实例中，不写入账号。
    """

    provider_name = "LeadBee"
    supports_resend = False
    supports_blacklist = True
    requires_explicit_replacement = True
    supports_cancellation = True

    def __init__(
        self,
        config: Optional[dict] = None,
        log_fn: Optional[Callable[[str], None]] = None,
        *,
        session=None,
    ):
        self.config = dict(config or {})
        self.log_fn = log_fn or (lambda _msg: None)
        self.code = str(
            self.config.get("leadbee_code")
            or self.config.get("chatgpt_leadbee_code")
            or ""
        ).strip()
        self.base_url = str(
            self.config.get("leadbee_base_url") or "https://sms.leadbee.cn/smsbox"
        ).strip().rstrip("/")
        self.max_attempts = _to_positive_int(
            self.config.get("leadbee_phone_attempts"), 3
        )
        self.phone_timeout_seconds = _to_positive_int(
            self.config.get("leadbee_phone_timeout_seconds"), 120, minimum=5
        )
        # The provider's queue may legitimately outlive the 120-second phase
        # timeout.  Keep one absolute worker-owned settlement deadline so the
        # flow neither burns the card at the soft boundary nor runs forever.
        self.total_timeout_seconds = min(
            _to_positive_int(
                self.config.get("leadbee_total_timeout_seconds"),
                540,
                minimum=5,
            ),
            540,
        )
        self.otp_timeout_seconds = _to_positive_int(
            self.config.get("leadbee_otp_timeout_seconds"), 120, minimum=10
        )
        self.poll_interval_seconds = _to_positive_int(
            self.config.get("leadbee_poll_interval_seconds"), 4, minimum=1
        )
        self.request_timeout_seconds = _to_positive_int(
            self.config.get("leadbee_request_timeout_seconds"), 20, minimum=5
        )
        self.session = session or http_requests.Session()
        self._card: dict[str, Any] = {}
        # Keep the shape of the last provider response separate from the
        # cached card.  LeadBee uses CARD_NOT_IN_SESSION for two different
        # situations: a response with ``card`` means another session still
        # owns the task, while a response without ``card`` means the queue
        # was released because no number was available.  Looking at the
        # cached card (especially its phone field) cannot distinguish them.
        self._last_response: dict[str, Any] = {}
        self._last_response_has_card = False
        self._activated = False
        self._activation_attempted = False
        self._restoration_confirmed = False
        self._known_unusable = False
        self._replacement_pending = False
        self._rejected_phone = ""
        self.last_cancel_error = ""
        self._progress_broker = self.config.get("chatgpt_phone_progress_broker")
        self._flow_deadline: float | None = None
        self._settlement_active_unknown = False

    @property
    def enabled(self) -> bool:
        return bool(self.code and self.base_url)

    def prefix_hint(self, phone: str) -> str:
        return _prefix_hint(phone)

    @staticmethod
    def _mask_code(code: str) -> str:
        parts = str(code or "").split("-")
        if len(parts) > 1:
            return f"{parts[0]}-****-{parts[-1][-4:]}"
        return "****"

    def _redact_exchange_code(self, value: Any) -> str:
        text = str(value or "")
        if not self.code:
            return text
        return text.replace(self.code, "[LeadBee兑换码已脱敏]")

    @staticmethod
    def _normalize_phone(value: Any) -> str:
        phone = re.sub(r"[\s()-]+", "", str(value or "").strip())
        if not re.fullmatch(r"\+[1-9]\d{7,14}", phone):
            raise RuntimeError(f"LeadBee 返回的手机号格式无效: {phone or '空值'}")
        return phone

    @staticmethod
    def _sms_code(card: dict[str, Any]) -> str:
        code = re.sub(r"\s+", "", str(card.get("sms_code") or ""))
        return code if re.fullmatch(r"\d{4,8}", code) else ""

    @staticmethod
    def _terminal(card: dict[str, Any]) -> bool:
        return bool(card.get("is_terminal"))

    @staticmethod
    def _status_message(card: dict[str, Any]) -> str:
        status = str(card.get("status") or "").strip()
        return {
            "canceled": "任务已取消",
            "expired": "任务已过期",
            "failed": "任务已结束",
            "unavailable": "暂时无可用号码",
            "sms_received": "任务已完成",
        }.get(status, status or "任务已结束")

    def _post(
        self,
        path: str,
        *,
        publish_provider_error: bool = True,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        if not self.enabled:
            raise RuntimeError("未提供 LeadBee 兑换码")
        request_timeout = float(self.request_timeout_seconds)
        if deadline is not None:
            remaining = float(deadline) - time.monotonic()
            if remaining <= 0:
                raise _LeadBeeFlowDeadlineExceeded("LeadBee 结算期限已到")
            request_timeout = min(request_timeout, remaining)
        url = f"{self.base_url}{path}"
        try:
            response = self.session.post(
                url,
                json={"code": self.code},
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                timeout=request_timeout,
            )
        except _LeadBeeFlowDeadlineExceeded:
            raise
        except Exception as exc:
            if deadline is not None and time.monotonic() >= deadline:
                raise _LeadBeeFlowDeadlineExceeded(
                    "LeadBee 请求未在结算期限内完成"
                ) from exc
            raise RuntimeError(
                f"LeadBee 请求失败: {self._redact_exchange_code(exc)}"
            ) from exc

        if int(getattr(response, "status_code", 0) or 0) != 200:
            detail = self._redact_exchange_code(
                getattr(response, "text", "")
            ).strip()[:180]
            raise RuntimeError(
                f"LeadBee 请求失败: HTTP {getattr(response, 'status_code', 0)}"
                + (f" - {detail}" if detail else "")
            )
        try:
            data = response.json()
        except Exception as exc:
            raise RuntimeError("LeadBee 响应不是有效 JSON") from exc
        if not isinstance(data, dict):
            raise RuntimeError("LeadBee 响应格式无效")

        self._last_response = dict(data)
        card = data.get("card")
        self._last_response_has_card = isinstance(card, dict)
        if isinstance(card, dict):
            self._card = dict(card)
        if not data.get("ok"):
            error = str(data.get("error") or "").strip()
            safe_error = self._redact_exchange_code(error)
            message = self._redact_exchange_code(data.get("message")).strip()
            phase_message = ""
            if error == "CARD_NOT_IN_SESSION" and path == "/api/receive-sms":
                if (
                    not self._last_response_has_card
                    or data.get("restored") is True
                    or data.get("released") is True
                ):
                    phase_message = (
                        "LeadBee 当前暂时无可用号码，卡密已自动释放，"
                        "可使用原卡重新排队"
                    )
                else:
                    phase_message = "LeadBee 兑换码正在另一会话中处理"
            elif error == "CARD_NOT_IN_SESSION" and path == "/api/activate":
                phase_message = "LeadBee 兑换码正在另一会话中处理"
            safe_message = (
                phase_message
                or LEADBEE_ERROR_MESSAGES.get(error)
                or message
                or f"LeadBee 操作失败{f': {safe_error}' if safe_error else ''}"
            )
            if publish_provider_error:
                provider_error_marker = getattr(
                    self._progress_broker,
                    "mark_provider_error",
                    None,
                )
                if callable(provider_error_marker):
                    provider_error_marker(error, safe_message)
            raise LeadBeeOperationRejected(
                safe_message,
                error_code=error,
            )
        return data

    def _mark_exchange_code_unusable(self, message: str) -> None:
        safe_message = self._redact_exchange_code(message).strip()
        self._known_unusable = True
        self._settlement_active_unknown = False
        marker = getattr(
            self._progress_broker,
            "mark_exchange_code_unusable",
            None,
        )
        if callable(marker):
            marker(safe_message or "LeadBee 卡密不可复用")

    def _mark_exchange_code_restored(self) -> None:
        self._restoration_confirmed = True
        self._known_unusable = False
        self._settlement_active_unknown = False
        restored_marker = getattr(
            self._progress_broker,
            "mark_exchange_code_restored",
            None,
        )
        if callable(restored_marker):
            restored_marker()

    def _card_not_in_session_was_released(self) -> bool:
        """Return whether the last failed request explicitly released the card.

        The provider's browser client treats a ``receive-sms`` error with no
        ``card`` payload as a no-inventory result and removes that task from
        the active list.  A payload that still contains ``card`` is the
        occupied/foreign-session case and must stay isolated.  Explicit
        release flags are accepted for forward-compatible provider payloads.
        """
        return bool(
            not self._last_response_has_card
            or self._last_response.get("restored") is True
            or self._last_response.get("released") is True
        )

    def _settle_unavailable_card(self, card: dict[str, Any]) -> None:
        """Return a terminal no-inventory card without treating it as consumed."""
        if str(card.get("status") or "").strip().lower() != "unavailable":
            return
        self._activation_attempted = False
        self._activated = False
        self._replacement_pending = False
        self._mark_exchange_code_restored()

    def _settle_terminal_card(self, card: dict[str, Any]) -> None:
        """Publish a structured settlement for every terminal card payload."""
        if not self._terminal(card):
            return
        status = str(card.get("status") or "").strip().lower()
        if status == "unavailable":
            self._settle_unavailable_card(card)
        elif status == "sms_received":
            self._mark_exchange_code_unusable(
                "LeadBee 已完成短信接收，该卡密不可复用"
            )
        elif not self._restoration_confirmed and not self._known_unusable:
            self._mark_exchange_code_active_unknown(
                f"LeadBee 任务以 {status or 'unknown'} 结束，"
                "服务端未明确确认卡密恢复；卡密保持隔离"
            )

    def _ensure_flow_deadline(self, now: float) -> float:
        if self._flow_deadline is None:
            timeout_seconds = float(self.total_timeout_seconds)
            broker_expiry = getattr(self._progress_broker, "expires_at", None)
            try:
                broker_remaining = float(broker_expiry) - time.time()
                has_broker_expiry = broker_expiry is not None
            except (TypeError, ValueError):
                broker_remaining = 0.0
                has_broker_expiry = False
            if has_broker_expiry:
                # Leave a full minute for the manager to publish the terminal
                # snapshot and release its provider permit before broker expiry.
                timeout_seconds = min(
                    timeout_seconds,
                    max(0.0, broker_remaining - 60.0),
                )
            self._flow_deadline = now + timeout_seconds
        return self._flow_deadline

    def _mark_exchange_code_active_unknown(self, message: str) -> None:
        if self._restoration_confirmed or self._known_unusable:
            return
        self._settlement_active_unknown = True
        marker = getattr(
            self._progress_broker,
            "mark_exchange_code_active_unknown",
            None,
        )
        if callable(marker):
            marker(message)

    def _raise_total_deadline(self, phase: str) -> None:
        if self._restoration_confirmed:
            raise RuntimeError(
                f"LeadBee {phase}超过结算期限，卡密已确认恢复"
            )
        if self._known_unusable:
            raise RuntimeError(
                f"LeadBee {phase}超过结算期限，卡密已确认不可复用"
            )
        if not self._activation_attempted:
            raise RuntimeError(
                f"LeadBee {phase}超过结算期限，兑换码尚未提交给服务端"
            )
        message = (
            f"LeadBee {phase}超过结算期限，服务端仍未确认卡密恢复；"
            "卡密保持隔离等待人工核对"
        )
        # No network request is started after the absolute deadline.  A late
        # cancel could outlive the broker TTL and release the outer semaphore
        # while the provider worker is still running.
        self._mark_exchange_code_active_unknown(message)
        raise RuntimeError(message)

    def _ensure_active_card(self, data: dict[str, Any]) -> dict[str, Any]:
        card = data.get("card")
        if not isinstance(card, dict):
            raise RuntimeError("LeadBee 未返回接码任务")
        if self._terminal(card):
            self._settle_terminal_card(card)
            if data.get("restored") or str(card.get("status") or "") == "sms_received":
                raise RuntimeError("LeadBee 兑换码已使用，接码任务已结束")
            raise RuntimeError(f"LeadBee {self._status_message(card)}")
        return card

    def _cancel_active_card(self, *, settle_failure: bool = True) -> bool:
        if self._restoration_confirmed or not self._activation_attempted:
            return False
        if self._activated and self._terminal(self._card):
            self._settle_terminal_card(self._card)
            return False
        try:
            data = self._post(
                "/api/cancel",
                publish_provider_error=settle_failure,
                deadline=self._flow_deadline,
            )
        except _LeadBeeFlowDeadlineExceeded as exc:
            self.last_cancel_error = self._redact_exchange_code(exc).strip()
            self._mark_exchange_code_active_unknown(
                "LeadBee 取消操作未能在结算期限内完成；"
                "卡密保持隔离等待人工核对"
            )
            return False
        except LeadBeeOperationRejected as exc:
            if exc.error_code == "CARD_NOT_IN_SESSION":
                self.last_cancel_error = self._redact_exchange_code(exc).strip()
                self.log_fn(f"[LeadBee] 取消接码任务失败: {self.last_cancel_error}")
                # An error response is not a cancellation confirmation.  In
                # particular, CARD_NOT_IN_SESSION can mean another browser
                # still owns the task; returning it to the local pool causes
                # the next account to see the same occupied-card error.
                self._mark_exchange_code_active_unknown(
                    "LeadBee 任务取消状态未知，卡密保持隔离: "
                    f"{self.last_cancel_error}"
                )
                return False
            self.last_cancel_error = self._redact_exchange_code(exc).strip()
            self.log_fn(f"[LeadBee] 取消接码任务失败: {self.last_cancel_error}")
            self._mark_exchange_code_active_unknown(
                f"LeadBee 任务不可取消，卡密保持隔离: {self.last_cancel_error}"
            )
            return False
        except Exception as exc:
            self.last_cancel_error = self._redact_exchange_code(exc).strip()
            self.log_fn(f"[LeadBee] 取消接码任务失败: {self.last_cancel_error}")
            self._mark_exchange_code_active_unknown(
                f"LeadBee 任务不可取消，卡密保持隔离: {self.last_cancel_error}"
            )
            return False
        if data.get("removed") is not True:
            self.last_cancel_error = (
                self._redact_exchange_code(data.get("message")).strip()
                or "服务端未明确确认卡密已恢复"
            )
            self.log_fn(f"[LeadBee] 取消接码任务未确认恢复: {self.last_cancel_error}")
            self._mark_exchange_code_active_unknown(
                f"LeadBee 任务不可取消，卡密保持隔离: {self.last_cancel_error}"
            )
            return False
        self.last_cancel_error = ""
        self._mark_exchange_code_restored()
        self.log_fn("[LeadBee] 接码任务已取消，兑换码已恢复")
        return True

    @property
    def card_at_risk(self) -> bool:
        """Whether an activated non-terminal card lacks confirmed restoration."""
        return bool(
            self._known_unusable
            or self._settlement_active_unknown
            or (self._activation_attempted and not self._restoration_confirmed)
        )

    def cancel_active(self) -> bool:
        """Cancel a non-terminal card so its exchange code can be reused."""
        if self._settlement_active_unknown:
            return False
        return self._cancel_active_card(
            settle_failure=not self._settlement_active_unknown
        )

    def _raise_if_cancelled(self) -> None:
        broker = self._progress_broker
        checker = getattr(broker, "raise_if_cancelled", None)
        if not callable(checker):
            return
        try:
            checker()
        except Exception:
            self._cancel_active_card()
            raise

    def acquire_phone(
        self, *, exclude_prefixes: Optional[Iterable[str]] = None
    ) -> Optional[PhoneEntry]:
        del exclude_prefixes
        self._raise_if_cancelled()
        started_at = time.monotonic()
        total_deadline = self._ensure_flow_deadline(started_at)
        if started_at >= total_deadline:
            self._raise_total_deadline("获取手机号")
        replacing = self._activated
        if replacing:
            if not self._replacement_pending:
                raise RuntimeError("LeadBee 换号未成功，不能重复提交已拒绝号码")
            card = self._ensure_active_card({"card": self._card})
        else:
            masked = self._mask_code(self.code)
            self.log_fn(f"[LeadBee] 正在激活兑换码 {masked}")
            provider_start_marker = getattr(
                self._progress_broker,
                "mark_provider_started",
                None,
            )
            if callable(provider_start_marker):
                provider_start_marker()
            self._activation_attempted = True
            try:
                data = self._post(
                    "/api/activate",
                    deadline=total_deadline,
                )
            except _LeadBeeFlowDeadlineExceeded:
                self._raise_total_deadline("激活兑换码")
            except LeadBeeOperationRejected as exc:
                if exc.error_code == "ACTIVE_LIMIT_REACHED":
                    # The provider explicitly rejected this session before it
                    # owned the card. Undo the conservative pre-call pool mark.
                    self._activation_attempted = False
                    self._mark_exchange_code_restored()
                elif exc.error_code in {"CARD_ALREADY_USED", "CARD_NOT_FOUND"}:
                    self._mark_exchange_code_unusable(str(exc))
                else:
                    # The provider may have accepted the card before returning
                    # an ambiguous browser/session error. Keep it quarantined.
                    self._mark_exchange_code_active_unknown(str(exc))
                raise
            except Exception as exc:
                if time.monotonic() >= total_deadline:
                    self._raise_total_deadline("激活兑换码")
                failure = (
                    "LeadBee 激活结果未知，卡密保持隔离: "
                    f"{self._redact_exchange_code(exc)}"
                )
                self._mark_exchange_code_active_unknown(failure)
                raise RuntimeError(failure) from exc
            self._activated = True
            card = self._ensure_active_card(data)
        self._raise_if_cancelled()
        deadline = started_at + self.phone_timeout_seconds
        queue_logged = False

        while True:
            self._raise_if_cancelled()
            now = time.monotonic()
            if now >= total_deadline:
                self._raise_total_deadline(
                    "更换手机号" if replacing else "获取手机号"
                )
            terminal = self._terminal(card)
            terminal_status = str(card.get("status") or "").strip().lower()
            if terminal:
                self._settle_terminal_card(card)
                if terminal_status != "sms_received":
                    raise RuntimeError(f"LeadBee {self._status_message(card)}")
            phone_value = str(card.get("phone") or "").strip()
            if phone_value:
                phone = self._normalize_phone(phone_value)
                if not replacing or phone != self._rejected_phone:
                    self._replacement_pending = False
                    self._rejected_phone = ""
                    if replacing:
                        self.log_fn(f"[LeadBee] 已获取更换后的手机号 {phone}")
                    else:
                        self.log_fn(f"[LeadBee] 已获取手机号 {phone}")
                    return PhoneEntry(
                        country_slug="leadbee",
                        phone=phone,
                        detail_url=f"{self.base_url}/",
                    )
            if terminal:
                raise RuntimeError(f"LeadBee {self._status_message(card)}")
            if now >= deadline:
                phase = "更换手机号" if replacing else "获取手机号"
                if self._cancel_active_card(settle_failure=False):
                    raise RuntimeError(f"LeadBee {phase}超时，接码任务已取消")
                if self._activated and not self._terminal(self._card):
                    self.log_fn(
                        f"[LeadBee] {phase}等待超时，但服务端仍在排队且暂不可取消；"
                        "继续等待明确结果"
                    )
                    deadline = now + self.phone_timeout_seconds
                else:
                    raise RuntimeError("LeadBee 获取手机号超时")
            if not queue_logged:
                if replacing:
                    self.log_fn("[LeadBee] 正在排队获取更换后的号码，请稍候")
                else:
                    self.log_fn("[LeadBee] 正在排队获取号码，请稍候")
                queue_logged = True
            time.sleep(
                min(
                    float(self.poll_interval_seconds),
                    max(0.0, total_deadline - now),
                )
            )
            self._raise_if_cancelled()
            after_sleep = time.monotonic()
            if after_sleep >= total_deadline:
                self._raise_total_deadline(
                    "更换手机号" if replacing else "获取手机号"
                )
            if after_sleep >= deadline:
                # Re-enter at the phase boundary so the provisional cancel /
                # continue-waiting policy runs before another provider poll.
                continue
            try:
                data = self._post(
                    "/api/receive-sms",
                    deadline=total_deadline,
                )
            except _LeadBeeFlowDeadlineExceeded:
                self._raise_total_deadline(
                    "更换手机号" if replacing else "获取手机号"
                )
            except LeadBeeOperationRejected as exc:
                if exc.error_code == "CARD_NOT_IN_SESSION":
                    if self._card_not_in_session_was_released():
                        self._activation_attempted = False
                        self._activated = False
                        self._replacement_pending = False
                        self._mark_exchange_code_restored()
                    else:
                        self._mark_exchange_code_active_unknown(str(exc))
                raise
            card = data.get("card")
            if not isinstance(card, dict):
                raise RuntimeError("LeadBee 未返回接码任务")
            if self._terminal(card):
                # A known no-inventory result settles the card even when the
                # response arrives exactly after the acceptance deadline.
                self._settle_terminal_card(card)
                if time.monotonic() >= total_deadline:
                    self._raise_total_deadline(
                        "更换手机号" if replacing else "获取手机号"
                    )

    def wait_for_code(
        self, entry: PhoneEntry, *, timeout: Optional[int] = None
    ) -> Optional[str]:
        self._raise_if_cancelled()
        if not self._activated:
            raise RuntimeError("LeadBee 接码任务尚未激活")
        wait_seconds = _to_positive_int(
            timeout, self.otp_timeout_seconds, minimum=10
        )
        started_at = time.monotonic()
        deadline = started_at + wait_seconds
        total_deadline = self._ensure_flow_deadline(started_at)
        card = self._card

        while True:
            self._raise_if_cancelled()
            now = time.monotonic()
            if now >= total_deadline:
                self._raise_total_deadline("等待短信验证码")
            terminal = self._terminal(card)
            if terminal:
                self._settle_terminal_card(card)
                if str(card.get("status") or "").strip().lower() == "sms_received":
                    code = self._sms_code(card)
                    if code:
                        self.log_fn(
                            f"[LeadBee] 已收到手机号 {entry.phone} 的短信验证码"
                        )
                        return code
                self.log_fn(f"[LeadBee] {self._status_message(card)}，未收到有效验证码")
                return None
            code = self._sms_code(card)
            if code:
                self.log_fn(f"[LeadBee] 已收到手机号 {entry.phone} 的短信验证码")
                return code
            if now >= deadline:
                self.log_fn(f"[LeadBee] 等待手机号 {entry.phone} 的短信验证码超时")
                return None
            try:
                data = self._post(
                    "/api/receive-sms",
                    deadline=total_deadline,
                )
            except _LeadBeeFlowDeadlineExceeded:
                self._raise_total_deadline("等待短信验证码")
            except LeadBeeOperationRejected as exc:
                if exc.error_code == "CARD_NOT_IN_SESSION":
                    if self._card_not_in_session_was_released():
                        self._activation_attempted = False
                        self._activated = False
                        self._replacement_pending = False
                        self._mark_exchange_code_restored()
                    else:
                        self._mark_exchange_code_active_unknown(str(exc))
                raise
            self._raise_if_cancelled()
            card = data.get("card")
            if not isinstance(card, dict):
                raise RuntimeError("LeadBee 未返回接码任务")
            if self._terminal(card):
                self._settle_terminal_card(card)
            now = time.monotonic()
            if now >= total_deadline:
                self._raise_total_deadline("等待短信验证码")
            terminal = self._terminal(card)
            if terminal:
                if str(card.get("status") or "").strip().lower() == "sms_received":
                    code = self._sms_code(card)
                    if code:
                        self.log_fn(
                            f"[LeadBee] 已收到手机号 {entry.phone} 的短信验证码"
                        )
                        return code
                self.log_fn(f"[LeadBee] {self._status_message(card)}，未收到有效验证码")
                return None
            code = self._sms_code(card)
            if code:
                self.log_fn(f"[LeadBee] 已收到手机号 {entry.phone} 的短信验证码")
                return code
            time.sleep(
                min(
                    float(self.poll_interval_seconds),
                    max(0.0, total_deadline - now),
                )
            )

    def request_replacement(self, phone: str, *, reason: str = "") -> bool:
        if not self._activated:
            return False
        if self._terminal(self._card):
            self._settle_terminal_card(self._card)
            return False
        self._rejected_phone = self._normalize_phone(phone)
        self._replacement_pending = False
        if reason == "sms_not_received":
            self.log_fn("[LeadBee] 当前号码未收到短信，正在更换号码")
        else:
            self.log_fn("[LeadBee] 当前号码被 OpenAI 拒绝，正在更换号码")
        now = time.monotonic()
        total_deadline = self._ensure_flow_deadline(now)
        if now >= total_deadline:
            self._raise_total_deadline("更换手机号")
        try:
            data = self._post(
                "/api/replace-number",
                deadline=total_deadline,
            )
        except _LeadBeeFlowDeadlineExceeded:
            self._raise_total_deadline("更换手机号")
        except LeadBeeOperationRejected as exc:
            if exc.error_code == "CARD_NOT_IN_SESSION":
                self._replacement_pending = False
                self._mark_exchange_code_active_unknown(str(exc))
            raise
        if time.monotonic() >= total_deadline:
            self._raise_total_deadline("更换手机号")
        card = self._ensure_active_card(data)
        self._card = dict(card)
        self._replacement_pending = True
        self.log_fn("[LeadBee] 换号请求已提交，继续等待新号码")
        return True

    def mark_blacklisted(self, phone: str) -> None:
        self.request_replacement(phone, reason="openai_rejected")


class LeadBeeOpenAPIPhoneService:
    """Order-backed LeadBee phone service using the signed Open API client."""

    provider_name = "LeadBee API"
    supports_resend = False
    supports_blacklist = True
    requires_explicit_replacement = True
    supports_cancellation = True

    _ACTIVE_STATUSES = frozenset(
        {"PROCESSING", "WAITING_CODE", "REPLACING", "CANCELING"}
    )
    _SETTLED_STATUSES = frozenset({"COMPLETED", "EXPIRED", "CANCELED"})
    _QUARANTINE_STATUSES = frozenset({"UNKNOWN", "MANUAL_REVIEW"})

    def __init__(
        self,
        config: Optional[dict] = None,
        log_fn: Optional[Callable[[str], None]] = None,
        *,
        client=None,
        sleep_fn: Optional[Callable[[float], None]] = None,
        monotonic: Optional[Callable[[], float]] = None,
        rate_limiter=None,
    ) -> None:
        self.config = dict(config or {})
        self.log_fn = log_fn or (lambda _msg: None)
        self.api_enabled = _truthy(self.config.get("leadbee_api_enabled"))
        self.api_key = str(self.config.get("leadbee_api_key") or "").strip()
        self.api_secret = str(self.config.get("leadbee_api_secret") or "").strip()
        self.product_id = str(self.config.get("leadbee_api_product_id") or "").strip()
        self.client_order_id = str(
            self.config.get("leadbee_api_client_order_id") or ""
        ).strip()
        self.max_attempts = _to_positive_int(
            self.config.get("leadbee_phone_attempts"), 3
        )
        self.phone_timeout_seconds = _to_positive_int(
            self.config.get("leadbee_phone_timeout_seconds"), 120, minimum=5
        )
        self.total_timeout_seconds = min(
            _to_positive_int(
                self.config.get("leadbee_total_timeout_seconds"),
                540,
                minimum=5,
            ),
            540,
        )
        self.otp_timeout_seconds = _to_positive_int(
            self.config.get("leadbee_otp_timeout_seconds"), 120, minimum=10
        )
        self.poll_interval_seconds = _to_positive_int(
            self.config.get("leadbee_poll_interval_seconds"), 4, minimum=1
        )
        self.request_timeout_seconds = _to_positive_int(
            self.config.get("leadbee_request_timeout_seconds"), 20, minimum=5
        )
        self._sleep = sleep_fn or time.sleep
        self._monotonic = monotonic or time.monotonic
        self._progress_broker = self.config.get("chatgpt_phone_progress_broker")
        self._provider_started = False
        production_client = client is None
        self._client = client
        if self._client is None and self._configuration_complete:
            # Production always uses the client's fixed official default base.
            self._client = LeadBeeOpenAPIClient(
                api_key=self.api_key,
                api_secret=self.api_secret,
            )
        self._rate_limiter = (
            rate_limiter
            if rate_limiter is not None
            else (leadbee_api_rate_limiter if production_client else None)
        )

        self.order_id = ""
        self._order: dict[str, Any] = {}
        self._current_phone = ""
        self._rejected_phone = ""
        self._replacement_pending = False
        self._replacement_sequence = 0
        self._flow_deadline: float | None = None
        self._create_attempted = False
        self._create_failed = False
        self._create_ambiguous = False
        self._quarantined = False
        self._settled_status = ""
        self.last_cancel_error = ""

        reference = self.client_order_id or "disabled"
        self._reference_digest = hashlib.sha256(reference.encode("utf-8")).hexdigest()[
            :32
        ]
        self._create_idempotency_key = f"leadbee-create-{self._reference_digest}"
        self._cancel_idempotency_key = f"leadbee-cancel-{self._reference_digest}"

    @property
    def _configuration_complete(self) -> bool:
        return bool(
            self.api_enabled
            and self.api_key
            and self.api_secret
            and self.product_id
            and self.client_order_id
        )

    @property
    def enabled(self) -> bool:
        return bool(self._configuration_complete and self._client is not None)

    @property
    def card_at_risk(self) -> bool:
        if self._settled_status in self._SETTLED_STATUSES:
            return False
        return bool(
            self._quarantined
            or self.order_id
            or (self._create_attempted and self._create_ambiguous)
        )

    def prefix_hint(self, phone: str) -> str:
        return _prefix_hint(phone)

    def log_phone_hint(self, phone: str) -> str:
        """Return a masked phone representation safe for application logs."""
        return self._masked_phone(str(phone or "").strip())

    @staticmethod
    def _phone(value: Any) -> str:
        phone = str(value or "").strip()
        if re.fullmatch(r"[1-9]\d{7,14}", phone):
            phone = f"+{phone}"
        return phone if re.fullmatch(r"\+[1-9]\d{7,14}", phone) else ""

    @staticmethod
    def _code(order: dict[str, Any]) -> str:
        for key in ("code", "sms_code", "verification_code"):
            code = str(order.get(key) or "").strip()
            if re.fullmatch(r"\d{4,8}", code):
                return code
        return ""

    @staticmethod
    def _masked_phone(phone: str) -> str:
        normalized = str(phone or "").strip()
        if len(normalized) <= 7:
            return "***"
        return f"{normalized[:3]}***{normalized[-4:]}"

    @staticmethod
    def _status(order: dict[str, Any]) -> str:
        return str(order.get("status") or "").strip().upper()

    @staticmethod
    def _order_payload(data: Any) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise RuntimeError("LeadBee API 返回的订单格式无效")
        nested = data.get("order")
        return dict(nested) if isinstance(nested, dict) else dict(data)

    @staticmethod
    def _positive_delay(value: Any) -> float | None:
        if isinstance(value, bool):
            return None
        try:
            delay = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(delay) or delay <= 0:
            return None
        return delay

    def _poll_delay(self, order: dict[str, Any]) -> float:
        return max(
            4.0,
            float(self.poll_interval_seconds),
            self._positive_delay(order.get("next_poll_after_seconds")) or 0.0,
        )

    def _ensure_enabled(self) -> None:
        if not self.enabled:
            raise RuntimeError("LeadBee API 配置不完整")

    def _ensure_flow_deadline(self) -> float:
        if self._flow_deadline is None:
            self._flow_deadline = self._monotonic() + float(self.total_timeout_seconds)
        return self._flow_deadline

    def _phase_deadline(self, timeout_seconds: int) -> float:
        now = self._monotonic()
        return min(
            self._ensure_flow_deadline(),
            now + float(timeout_seconds),
        )

    def _raise_if_cancelled(self) -> None:
        checker = getattr(self._progress_broker, "raise_if_cancelled", None)
        if callable(checker):
            checker()

    def _mark_provider_started(self) -> None:
        if self._provider_started:
            return
        marker = getattr(self._progress_broker, "mark_provider_started", None)
        if callable(marker):
            marker()
        self._provider_started = True

    def _remaining_request_timeout(self, deadline: float, phase: str) -> float:
        remaining = float(deadline) - self._monotonic()
        if remaining <= 0:
            raise RuntimeError(f"LeadBee API {phase}超过本地期限")
        return min(float(self.request_timeout_seconds), remaining)

    def _sleep_before(
        self,
        delay: float,
        deadline: float,
        phase: str,
        *,
        check_cancellation: bool = True,
    ) -> None:
        if check_cancellation:
            self._raise_if_cancelled()
        remaining = deadline - self._monotonic()
        if remaining <= 0:
            raise RuntimeError(f"LeadBee API {phase}超过本地期限")
        self._sleep(min(float(delay), remaining))
        if check_cancellation:
            self._raise_if_cancelled()
        if self._monotonic() >= deadline:
            raise RuntimeError(f"LeadBee API {phase}超过本地期限")

    @staticmethod
    def _retryable(exc: LeadBeeAPIError) -> bool:
        error_code = exc.error_code
        if (
            error_code.startswith(("AUTH", "PERMISSION", "PRODUCT"))
            or "SIGNATURE" in error_code
            or error_code.endswith("_CONFLICT")
            or error_code == "IP_NOT_ALLOWED"
        ):
            return False
        if isinstance(exc, LeadBeeTransportError):
            return True
        if exc.http_status == 429 or error_code == "RATE_LIMITED":
            return True
        return exc.http_status == 503 or error_code == "REPLAY_PROTECTION_UNAVAILABLE"

    def _retry_delay(self, exc: LeadBeeAPIError) -> float:
        if exc.http_status == 429:
            retry_after = self._positive_delay(exc.retry_after)
            if retry_after is not None:
                return retry_after
        return float(self.poll_interval_seconds)

    def _write_with_retry(
        self,
        operation: Callable[[float], dict[str, Any]],
        *,
        deadline: float,
        phase: str,
        check_cancellation: bool = True,
        create: bool = False,
    ) -> dict[str, Any]:
        while True:
            if check_cancellation:
                self._raise_if_cancelled()
            if self._rate_limiter is not None:
                self._rate_limiter.wait(
                    create=create,
                    deadline=deadline,
                    checkpoint=(
                        self._raise_if_cancelled
                        if check_cancellation
                        else (lambda: None)
                    ),
                )
            request_timeout = self._remaining_request_timeout(deadline, phase)
            result: dict[str, Any] = {}
            retry_delay: float | None = None
            unexpected_failure = False
            try:
                result = operation(request_timeout)
            except LeadBeeAPIError as exc:
                if not self._retryable(exc):
                    raise
                retry_delay = self._retry_delay(exc)
            except Exception:  # noqa: BLE001 - sanitize injected client failures
                unexpected_failure = True

            if unexpected_failure:
                # Raise after leaving the handler so the injected exception is
                # not retained as either __context__ or __cause__.
                raise RuntimeError("LeadBee API 请求失败") from None
            if retry_delay is not None:
                self._sleep_before(
                    retry_delay,
                    deadline,
                    phase,
                    check_cancellation=check_cancellation,
                )
                continue
            if check_cancellation:
                self._raise_if_cancelled()
            if self._monotonic() >= deadline:
                raise RuntimeError(f"LeadBee API {phase}超过本地期限")
            return result

    def _read_order(
        self,
        *,
        deadline: float,
        phase: str,
        check_cancellation: bool = True,
    ) -> dict[str, Any]:
        return self._write_with_retry(
            lambda request_timeout: self._client.get_order(
                self.order_id,
                request_timeout=request_timeout,
            ),
            deadline=deadline,
            phase=phase,
            check_cancellation=check_cancellation,
        )

    def _quarantine(self, status: str) -> None:
        self._quarantined = True
        self._settled_status = ""
        safe_status = (
            status
            if status in self._QUARANTINE_STATUSES | {"ORDER_ID_MISMATCH"}
            else "UNRECOGNIZED"
        )
        raise RuntimeError(f"LeadBee API 订单状态需隔离: {safe_status}")

    def _accept_order(
        self,
        data: Any,
        *,
        require_order_id: bool = False,
    ) -> dict[str, Any]:
        order = self._order_payload(data)
        returned_order_id = str(order.get("order_id") or order.get("id") or "").strip()
        if require_order_id and not returned_order_id:
            self._create_ambiguous = True
            raise RuntimeError("LeadBee API 未返回订单编号")
        if returned_order_id:
            if self.order_id and returned_order_id != self.order_id:
                self._quarantine("ORDER_ID_MISMATCH")
            self.order_id = returned_order_id

        status = self._status(order)
        if status not in self._ACTIVE_STATUSES | self._SETTLED_STATUSES:
            self._order = order
            self._quarantine(status)
        self._order = order
        self.log_fn(f"[LeadBee API] 订单状态: {status}")
        if status in self._SETTLED_STATUSES:
            self._settled_status = status
            self._quarantined = False
        return order

    @staticmethod
    def _create_failure_is_ambiguous(exc: LeadBeeAPIError) -> bool:
        if isinstance(exc, LeadBeeTransportError):
            return True
        if isinstance(exc, LeadBeeResponseError) and 200 <= exc.http_status < 300:
            return True
        if exc.http_status >= 500:
            return True
        return exc.error_code == "IDEMPOTENCY_CONFLICT"

    def _create_order(self, *, deadline: float) -> dict[str, Any]:
        if self.order_id:
            return self._order
        if self._quarantined:
            self._quarantine(self._status(self._order))
        if self._create_failed:
            raise RuntimeError("LeadBee API 创建订单已失败，未重复提交")

        def create(request_timeout: float) -> dict[str, Any]:
            self._create_attempted = True
            return self._client.create_order(
                self.client_order_id,
                self.product_id,
                1,
                idempotency_key=self._create_idempotency_key,
                request_timeout=request_timeout,
            )

        try:
            data = self._write_with_retry(
                create,
                deadline=deadline,
                phase="创建订单",
                create=True,
            )
        except LeadBeeAPIError as exc:
            self._create_ambiguous = self._create_failure_is_ambiguous(exc)
            self._create_failed = True
            raise
        except RuntimeError:
            if self._create_attempted:
                self._create_ambiguous = True
                self._create_failed = True
            raise
        try:
            order = self._accept_order(data, require_order_id=True)
        except RuntimeError:
            self._create_failed = True
            raise
        self._create_ambiguous = False
        return order

    def _poll_order(
        self,
        *,
        deadline: float,
        phase: str,
        check_cancellation: bool = True,
    ) -> dict[str, Any]:
        self._sleep_before(
            self._poll_delay(self._order),
            deadline,
            phase,
            check_cancellation=check_cancellation,
        )
        return self._accept_order(
            self._read_order(
                deadline=deadline,
                phase=phase,
                check_cancellation=check_cancellation,
            )
        )

    def _entry(self, phone: str) -> PhoneEntry:
        self._current_phone = phone
        self.log_fn(f"[LeadBee API] 已获取手机号 {self._masked_phone(phone)}")
        return PhoneEntry(
            country_slug="leadbee-api",
            phone=phone,
            detail_url=(f"leadbee-api://order/{quote(self.order_id, safe='')}"),
        )

    def acquire_phone(
        self, *, exclude_prefixes: Optional[Iterable[str]] = None
    ) -> Optional[PhoneEntry]:
        del exclude_prefixes
        self._raise_if_cancelled()
        self._ensure_enabled()
        if self._quarantined:
            self._quarantine(self._status(self._order))
        replacing = self._replacement_pending
        deadline = self._phase_deadline(self.phone_timeout_seconds)
        if self.order_id:
            order = self._order
        else:
            self._mark_provider_started()
            order = self._create_order(deadline=deadline)

        while True:
            status = self._status(order)
            if status in {"CANCELED", "EXPIRED"}:
                raise RuntimeError(f"LeadBee API 订单已{status.lower()}")
            phone = self._phone(order.get("phone") or order.get("phone_number"))
            if (
                status in {"WAITING_CODE", "COMPLETED"}
                and phone
                and (not replacing or phone != self._rejected_phone)
            ):
                self._replacement_pending = False
                self._rejected_phone = ""
                return self._entry(phone)
            if status == "COMPLETED":
                raise RuntimeError("LeadBee API 订单已完成但未返回有效手机号")
            order = self._poll_order(deadline=deadline, phase="获取手机号")

    def wait_for_code(
        self, entry: PhoneEntry, *, timeout: Optional[int] = None
    ) -> Optional[str]:
        self._raise_if_cancelled()
        self._ensure_enabled()
        if not self.order_id:
            raise RuntimeError("LeadBee API 订单尚未创建")
        if self._quarantined:
            self._quarantine(self._status(self._order))
        entry_phone = self._phone(entry.phone)
        if not entry_phone or entry_phone != self._current_phone:
            raise RuntimeError("LeadBee API 手机号与当前订单不匹配")
        wait_seconds = _to_positive_int(
            timeout,
            self.otp_timeout_seconds,
            minimum=10,
        )
        deadline = self._phase_deadline(wait_seconds)
        order = self._order

        while True:
            status = self._status(order)
            if status == "COMPLETED":
                code = self._code(order)
                if code:
                    self.log_fn(
                        "[LeadBee API] 已收到手机号 "
                        f"{self._masked_phone(entry_phone)} 的短信验证码"
                    )
                return code or None
            if status in {"CANCELED", "EXPIRED"}:
                return None
            try:
                order = self._poll_order(
                    deadline=deadline,
                    phase="等待短信验证码",
                )
            except RuntimeError as exc:
                if "超过本地期限" in str(exc):
                    self.log_fn(
                        "[LeadBee API] 等待手机号 "
                        f"{self._masked_phone(entry_phone)} 的短信验证码超时"
                    )
                    return None
                raise

    def request_replacement(self, phone: str, *, reason: str = "") -> bool:
        del reason
        self._raise_if_cancelled()
        self._ensure_enabled()
        if not self.order_id or self._quarantined:
            return False
        status = self._status(self._order)
        if status in self._SETTLED_STATUSES:
            return False
        rejected_phone = self._phone(phone)
        if not rejected_phone or rejected_phone != self._current_phone:
            raise RuntimeError("LeadBee API 换号请求与当前手机号不匹配")

        self._replacement_sequence += 1
        idempotency_key = (
            f"leadbee-replace-{self._replacement_sequence}-{self._reference_digest}"
        )
        deadline = self._ensure_flow_deadline()
        data = self._write_with_retry(
            lambda request_timeout: self._client.replace_order(
                self.order_id,
                idempotency_key=idempotency_key,
                request_timeout=request_timeout,
            ),
            deadline=deadline,
            phase="更换手机号",
        )
        order = self._accept_order(data)
        if self._status(order) in self._SETTLED_STATUSES:
            return False
        self._rejected_phone = rejected_phone
        self._replacement_pending = True
        self.log_fn("[LeadBee API] 换号请求已提交")
        return True

    def mark_blacklisted(self, phone: str) -> None:
        self.request_replacement(phone, reason="openai_rejected")

    def cancel_active(self) -> bool:
        self.last_cancel_error = ""
        if not self.order_id:
            return False
        status = self._status(self._order)
        if status in self._SETTLED_STATUSES:
            self._settled_status = status
            self._quarantined = False
            return False
        if self._quarantined:
            self.last_cancel_error = "LeadBee API 订单状态需人工核对"
            return False

        deadline = self._ensure_flow_deadline()
        try:
            data = self._write_with_retry(
                lambda request_timeout: self._client.cancel_order(
                    self.order_id,
                    idempotency_key=self._cancel_idempotency_key,
                    request_timeout=request_timeout,
                ),
                deadline=deadline,
                phase="取消订单",
                check_cancellation=False,
            )
            order = self._accept_order(data)
            while True:
                status = self._status(order)
                if status == "CANCELED":
                    self._settled_status = status
                    self.log_fn("[LeadBee API] 订单已取消")
                    return True
                if status in {"COMPLETED", "EXPIRED"}:
                    self._settled_status = status
                    return False
                order = self._poll_order(
                    deadline=deadline,
                    phase="确认取消状态",
                    check_cancellation=False,
                )
        except LeadBeeAPIError as exc:
            self.last_cancel_error = str(exc)
        except RuntimeError as exc:
            self.last_cancel_error = (
                "LeadBee API 取消状态未在期限内确认"
                if "超过本地期限" in str(exc)
                else str(exc)
            )
        self._quarantined = True
        return False


def create_phone_service(
    config: Optional[dict] = None,
    log_fn: Optional[Callable[[str], None]] = None,
):
    resolved = dict(config or {})
    if _truthy(resolved.get("leadbee_api_enabled")):
        return LeadBeeOpenAPIPhoneService(resolved, log_fn=log_fn)
    provider = str(resolved.get("chatgpt_phone_provider") or "auto").strip().lower()
    has_leadbee_code = bool(
        str(
            resolved.get("leadbee_code") or resolved.get("chatgpt_leadbee_code") or ""
        ).strip()
    )
    if provider == "leadbee" or (provider in {"", "auto"} and has_leadbee_code):
        return LeadBeePhoneService(resolved, log_fn=log_fn)
    return SMSToMePhoneService(resolved, log_fn=log_fn)
