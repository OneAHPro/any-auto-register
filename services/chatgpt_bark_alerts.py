"""Bark critical notifications for ChatGPT automatic authentication alerts."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
import logging
from typing import Mapping
from urllib import request
from urllib.parse import urlsplit

from services.chatgpt_codex2api_quota import AvailableQuotaReport


logger = logging.getLogger(__name__)

BARK_TIMEOUT_SECONDS = 20
BARK_MAX_RESPONSE_BYTES = 64 * 1024
BARK_OFFICIAL_HOST = "api.day.app"
BARK_GROUP = "Any Auto Register · Codex"
BARK_SOUND = "alarm"
DEFAULT_ALERT_THRESHOLD = 20
USD_CENT = Decimal("0.01")


class BarkResponseError(RuntimeError):
    """Bark returned a response that does not confirm delivery."""


class BarkEndpointError(ValueError):
    """The configured endpoint is not an official Bark device endpoint."""


class _NoRedirectHandler(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise BarkResponseError("redirect responses are not accepted")


def _text(value: object) -> str:
    return str(value or "").strip()


def _to_bool(value: object, default: bool = False) -> bool:
    normalized = _text(value).lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _positive_int(value: object, default: int) -> int:
    try:
        parsed = int(_text(value))
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _non_negative_int(value: object) -> int:
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return 0


def _quota_alert_threshold(value: object) -> Decimal:
    try:
        parsed = Decimal(_text(value) or "0")
    except (InvalidOperation, ValueError):
        return Decimal("0.00")
    if not parsed.is_finite() or parsed <= 0:
        return Decimal("0.00")
    return parsed.quantize(USD_CENT, rounding=ROUND_HALF_UP)


def _get_config() -> dict[str, object]:
    from core.config_store import config_store

    return dict(config_store.get_all() or {})


def _business_alert_title(
    quota_report: AvailableQuotaReport,
    title: str,
) -> str:
    return (
        f"${quota_report.estimated_remaining_usd:.2f}｜"
        f"正常可用账号 {quota_report.account_count} 个｜{title}"
    )


def normalize_bark_endpoint(value: object) -> str:
    """Validate and normalize an official Bark device endpoint."""

    endpoint = _text(value).rstrip("/")
    try:
        parsed = urlsplit(endpoint)
        hostname = (parsed.hostname or "").lower()
        port = parsed.port
    except (TypeError, ValueError, UnicodeError) as exc:
        raise BarkEndpointError("invalid Bark endpoint") from exc

    path_parts = [part for part in parsed.path.split("/") if part]
    if (
        not endpoint
        or any(ord(character) <= 32 for character in endpoint)
        or parsed.scheme.lower() != "https"
        or hostname != BARK_OFFICIAL_HOST
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or len(path_parts) != 1
    ):
        raise BarkEndpointError("invalid Bark endpoint")
    return endpoint


def _resolve_endpoint(snapshot: Mapping[str, object]) -> tuple[str, str | None]:
    value = snapshot.get("bark_endpoint")
    if not _text(value):
        return "", "bark_not_configured"
    try:
        endpoint = normalize_bark_endpoint(value)
    except BarkEndpointError:
        return "", "invalid_bark_endpoint"
    return endpoint, None


def _critical_payload(*, title: str, body: str) -> dict[str, str]:
    return {
        "title": title,
        "body": body,
        "group": BARK_GROUP,
        "level": "critical",
        "call": "1",
        "sound": BARK_SOUND,
    }


def _open_bark_request(outbound: request.Request, timeout: float):
    opener = request.build_opener(_NoRedirectHandler())
    return opener.open(outbound, timeout=timeout)


def _send_bark(
    *,
    endpoint: str,
    title: str,
    body: str,
) -> dict[str, object]:
    try:
        payload = _critical_payload(title=title, body=body)
        outbound = request.Request(
            endpoint,
            data=json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with _open_bark_request(
            outbound,
            BARK_TIMEOUT_SECONDS,
        ) as response:
            status = int(getattr(response, "status", 0) or 0)
            if not 200 <= status < 300:
                raise BarkResponseError("unexpected HTTP status")
            raw_response = response.read(BARK_MAX_RESPONSE_BYTES + 1)
            if len(raw_response) > BARK_MAX_RESPONSE_BYTES:
                raise BarkResponseError("response payload is too large")
            response_payload = json.loads(raw_response.decode("utf-8"))
            if not isinstance(response_payload, dict):
                raise BarkResponseError("invalid response payload")
            if response_payload.get("code") != 200:
                raise BarkResponseError("delivery was not accepted")
    except Exception as exc:
        error_type = type(exc).__name__
        logger.warning("Bark 强提醒发送失败（%s）", error_type)
        return {
            "sent": False,
            "reason": "send_failed",
            "error_type": error_type,
        }

    return {"sent": True, "reason": "sent"}


def send_bark_relogin_alert(
    *,
    task_id: str,
    quota_report: AvailableQuotaReport,
    quota_eligible_failure_count: int,
    quota_exhausted_failure_count: int,
    relogin_failed_count: int,
    deleted_account_count: int = 0,
    config: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Send a Bark alert when quota-eligible relogin failures reach threshold."""

    snapshot = dict(config) if config is not None else _get_config()
    threshold = _positive_int(
        snapshot.get("chatgpt_auto_relogin_alert_threshold"),
        DEFAULT_ALERT_THRESHOLD,
    )
    eligible_count = _non_negative_int(quota_eligible_failure_count)
    base_result = {"threshold": threshold}
    if eligible_count < threshold:
        return {"sent": False, "reason": "below_threshold", **base_result}
    if not _to_bool(snapshot.get("bark_enabled")):
        return {"sent": False, "reason": "bark_disabled", **base_result}

    endpoint, endpoint_error = _resolve_endpoint(snapshot)
    if endpoint_error:
        return {"sent": False, "reason": endpoint_error, **base_result}

    failed_count = _non_negative_int(relogin_failed_count)
    exhausted_count = min(
        _non_negative_int(quota_exhausted_failure_count),
        failed_count,
    )
    deleted_count = min(
        _non_negative_int(deleted_account_count),
        failed_count,
    )
    body = (
        f"仍有额度的重登失败：{eligible_count}\n"
        f"其中封禁或删除：{deleted_count}\n"
        f"额度已用完的重登失败：{exhausted_count}\n"
        f"正常可用账号：{quota_report.account_count}\n"
        f"当前剩余可用额度：${quota_report.current_remaining_usd:.2f}\n"
        f"任务 ID：{_text(task_id)}"
    )
    result = _send_bark(
        endpoint=endpoint,
        title=_business_alert_title(
            quota_report,
            "Codex 重登失败账号告警",
        ),
        body=body,
    )
    result.update(base_result)
    return result


def send_bark_quota_threshold_alert(
    *,
    task_id: str,
    quota_report: AvailableQuotaReport,
    config: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Send a Bark alert on every cycle whose quota is below threshold."""

    snapshot = dict(config) if config is not None else _get_config()
    threshold_usd = _quota_alert_threshold(
        snapshot.get("chatgpt_auto_relogin_quota_alert_threshold_usd")
    )
    remaining_usd = quota_report.current_remaining_usd.quantize(
        USD_CENT,
        rounding=ROUND_HALF_UP,
    )
    base_result = {
        "threshold_usd": f"{threshold_usd:.2f}",
        "estimated_remaining_usd": f"{remaining_usd:.2f}",
    }
    if threshold_usd <= 0:
        return {
            "sent": False,
            "reason": "quota_alert_disabled",
            **base_result,
        }
    if remaining_usd >= threshold_usd:
        return {
            "sent": False,
            "reason": "quota_not_below_threshold",
            **base_result,
        }
    if not _to_bool(snapshot.get("bark_enabled")):
        return {"sent": False, "reason": "bark_disabled", **base_result}

    endpoint, endpoint_error = _resolve_endpoint(snapshot)
    if endpoint_error:
        return {"sent": False, "reason": endpoint_error, **base_result}

    body = (
        f"当前剩余可用额度：${remaining_usd:.2f}\n"
        f"告警阈值：${threshold_usd:.2f}\n"
        f"正常可用账号：{quota_report.account_count}\n"
        f"账号总数：{quota_report.remote_account_count}\n"
        f"任务 ID：{_text(task_id)}"
    )
    result = _send_bark(
        endpoint=endpoint,
        title=(
            f"${quota_report.current_remaining_usd:.2f}｜"
            f"正常可用账号 {quota_report.account_count} 个｜Codex 剩余额度不足告警"
        ),
        body=body,
    )
    result.update(base_result)
    return result


def send_bark_test_notification(
    *,
    config: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Send a critical test notification without applying business thresholds."""

    snapshot = dict(config) if config is not None else _get_config()
    if not _to_bool(snapshot.get("bark_enabled")):
        return {"sent": False, "reason": "bark_disabled"}
    endpoint, endpoint_error = _resolve_endpoint(snapshot)
    if endpoint_error:
        return {"sent": False, "reason": endpoint_error}
    return _send_bark(
        endpoint=endpoint,
        title="Any Auto Register · Bark 强提醒测试",
        body=(
            "Bark 配置可用。\n"
            "正式业务告警将使用 critical + call=1 持续响铃。"
        ),
    )


__all__ = [
    "send_bark_quota_threshold_alert",
    "send_bark_relogin_alert",
    "send_bark_test_notification",
]
