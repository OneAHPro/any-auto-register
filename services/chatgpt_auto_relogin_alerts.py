"""Threshold-based SMTP alerts for ChatGPT automatic authentication cycles."""

from __future__ import annotations

from datetime import datetime, timezone
from email.message import EmailMessage
import logging
import re
import smtplib
import ssl
from typing import Mapping


logger = logging.getLogger(__name__)

DEFAULT_ALERT_THRESHOLD = 5
DEFAULT_SMTP_PORT = 587
SMTP_TIMEOUT_SECONDS = 20


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


def _mailboxes(value: object) -> list[str]:
    addresses: list[str] = []
    seen: set[str] = set()
    for raw in re.split(r"[,;]", _text(value)):
        address = raw.replace("\r", "").replace("\n", "").strip()
        if not address or address in seen:
            continue
        seen.add(address)
        addresses.append(address)
    return addresses


def _get_config() -> dict[str, object]:
    from core.config_store import config_store

    return dict(config_store.get_all() or {})


def _build_message(
    *,
    sender: str,
    recipients: list[str],
    task_id: str,
    total_accounts: int,
    invalid_rt_count: int,
    relogin_failed_count: int,
    threshold: int,
) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = "[Any Auto Register] ChatGPT 自动认证告警"
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    occurred_at = (
        datetime.now(timezone.utc)
        .astimezone()
        .isoformat(timespec="seconds")
    )
    message.set_content(
        "ChatGPT 自动认证周期触发了邮件告警。\n\n"
        f"任务 ID：{task_id}\n"
        f"本轮账号总数：{total_accounts}\n"
        f"RT 明确失效：{invalid_rt_count}\n"
        f"完整重登失败：{relogin_failed_count}\n"
        f"告警阈值：{threshold}\n"
        f"完成时间：{occurred_at}\n\n"
        "请在 Any Auto Register 的“任务运行”页面查看本轮详细记录。\n"
    )
    return message


def _authenticate(
    smtp: smtplib.SMTP,
    *,
    username: str,
    password: str,
    force_auth_login: bool,
) -> None:
    if not username or not password:
        return
    if not force_auth_login:
        smtp.login(username, password)
        return

    step = 0

    def _auth_login(_challenge: bytes | None = None) -> str:
        nonlocal step
        value = username if step == 0 else password
        step += 1
        return value

    smtp.auth("LOGIN", _auth_login, initial_response_ok=False)


def send_auto_relogin_alert(
    *,
    task_id: str,
    total_accounts: int,
    invalid_rt_count: int,
    relogin_failed_count: int,
    config: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Send one alert when either cycle counter reaches the configured threshold."""

    snapshot = dict(config) if config is not None else _get_config()
    threshold = _positive_int(
        snapshot.get("chatgpt_auto_relogin_alert_threshold"),
        DEFAULT_ALERT_THRESHOLD,
    )
    invalid_count = _non_negative_int(invalid_rt_count)
    failed_count = _non_negative_int(relogin_failed_count)
    if invalid_count < threshold and failed_count < threshold:
        return {
            "sent": False,
            "reason": "below_threshold",
            "threshold": threshold,
        }

    host = _text(snapshot.get("smtp_host"))
    port = _positive_int(snapshot.get("smtp_port"), DEFAULT_SMTP_PORT)
    username = _text(snapshot.get("smtp_username"))
    password = str(snapshot.get("smtp_password") or "")
    sender = _text(snapshot.get("smtp_sender_email")) or username
    recipients = _mailboxes(snapshot.get("smtp_recipient_email"))
    if not recipients:
        recipients = _mailboxes(username or sender)
    if not host or not sender or not recipients:
        return {
            "sent": False,
            "reason": "smtp_not_configured",
            "threshold": threshold,
        }

    use_ssl = _to_bool(snapshot.get("smtp_use_ssl"), default=True)
    force_auth_login = _to_bool(snapshot.get("smtp_force_auth_login"))
    message = _build_message(
        sender=sender,
        recipients=recipients,
        task_id=_text(task_id),
        total_accounts=_non_negative_int(total_accounts),
        invalid_rt_count=invalid_count,
        relogin_failed_count=failed_count,
        threshold=threshold,
    )

    try:
        tls_context = ssl.create_default_context()
        if use_ssl and port == 465:
            smtp_connection = smtplib.SMTP_SSL(
                host,
                port,
                timeout=SMTP_TIMEOUT_SECONDS,
                context=tls_context,
            )
        else:
            smtp_connection = smtplib.SMTP(
                host,
                port,
                timeout=SMTP_TIMEOUT_SECONDS,
            )

        with smtp_connection as smtp:
            if use_ssl and port != 465:
                smtp.ehlo()
                smtp.starttls(context=tls_context)
                smtp.ehlo()
            _authenticate(
                smtp,
                username=username,
                password=password,
                force_auth_login=force_auth_login,
            )
            smtp.send_message(
                message,
                from_addr=sender,
                to_addrs=recipients,
            )
    except Exception as exc:
        error_type = type(exc).__name__
        logger.warning(
            "ChatGPT 自动认证告警邮件发送失败（%s）",
            error_type,
        )
        return {
            "sent": False,
            "reason": "send_failed",
            "threshold": threshold,
            "error_type": error_type,
        }

    return {"sent": True, "reason": "sent", "threshold": threshold}


__all__ = ["send_auto_relogin_alert"]
