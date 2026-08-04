"""Threshold-based SMTP alerts for ChatGPT automatic authentication cycles."""

from __future__ import annotations

from datetime import datetime, timezone
from email.message import EmailMessage
from html import escape
import logging
import re
import smtplib
import ssl
from typing import Mapping
from zoneinfo import ZoneInfo


logger = logging.getLogger(__name__)

DEFAULT_ALERT_THRESHOLD = 20
DEFAULT_SMTP_PORT = 587
SMTP_TIMEOUT_SECONDS = 20
BEIJING_TIMEZONE = ZoneInfo("Asia/Shanghai")


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


def _format_beijing_time(value: datetime | None = None) -> str:
    occurred_at = value or datetime.now(timezone.utc)
    if occurred_at.tzinfo is None:
        occurred_at = occurred_at.replace(tzinfo=timezone.utc)
    return occurred_at.astimezone(BEIJING_TIMEZONE).strftime(
        "%Y-%m-%d %H:%M:%S（北京时间）"
    )


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
    successful_accounts: int,
    invalid_rt_count: int,
    relogin_failed_count: int,
    threshold: int,
) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = (
        "[Any Auto Register] ChatGPT 重登失败账号告警"
        f"（{relogin_failed_count} 个）"
    )
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    occurred_at = _format_beijing_time()
    message.set_content(
        "ChatGPT 自动认证周期触发了邮件告警。\n\n"
        f"任务 ID：{task_id}\n"
        f"账号总数：{total_accounts}\n"
        f"成功账号：{successful_accounts}\n"
        f"鉴权失败：{invalid_rt_count}\n"
        f"重登失败：{relogin_failed_count}\n"
        f"告警阈值：{threshold}\n"
        f"完成时间：{occurred_at}\n\n"
        "鉴权失败数仅用于展示；重登失败数是本邮件的触发依据。"
        "两项为过程指标，可能包含同一账号，四项统计不应相加核对总数。\n\n"
        "请在 Any Auto Register 的“任务运行”页面查看本轮详细记录。\n"
    )
    escaped_task_id = escape(task_id)
    escaped_occurred_at = escape(occurred_at)
    message.add_alternative(
        f"""<!DOCTYPE html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8">
    <style>
      .metric-cell {{ padding: 16px 10px; text-align: center; vertical-align: top; }}
      .metric-total {{ background: #e8eef5; color: #425466; }}
      .metric-success {{ background: #e8f5e9; color: #2e7d32; }}
      .metric-invalid {{ background: #fff3e0; color: #e67e22; }}
      .metric-failed {{ background: #ffebee; color: #c62828; }}
      @media only screen and (max-width: 600px) {{
        .metric-cell {{ display: block !important; width: 100% !important; box-sizing: border-box; }}
      }}
    </style>
  </head>
  <body style="margin:0; padding:0; color:#1f2937; font-family:Arial, 'Microsoft YaHei', sans-serif;">
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0"><tr><td align="center">
      <table role="presentation" width="640" cellspacing="0" cellpadding="0" border="0" style="max-width:640px; width:100%;">
        <tr><td style="padding:24px 20px 12px;"><h2 style="margin:0;">ChatGPT 重登失败账号告警</h2></td></tr>
        <tr><td style="padding:0 20px 20px;">任务 ID：{escaped_task_id}</td></tr>
        <tr><td style="padding:0 20px 20px;">
          <table role="presentation" width="100%" cellspacing="4" cellpadding="0" border="0"><tr>
            <td class="metric-cell metric-total" width="25%"><strong>账号总数</strong><br>{total_accounts}</td>
            <td class="metric-cell metric-success" width="25%"><strong>成功账号</strong><br>{successful_accounts}</td>
            <td class="metric-cell metric-invalid" width="25%"><strong>鉴权失败</strong><br>{invalid_rt_count}</td>
            <td class="metric-cell metric-failed" width="25%"><strong>重登失败</strong><br>{relogin_failed_count}</td>
          </tr></table>
        </td></tr>
        <tr><td style="padding:0 20px 12px;">告警阈值：{threshold}</td></tr>
        <tr><td style="padding:0 20px 20px;">完成时间：{escaped_occurred_at}</td></tr>
        <tr><td style="padding:0 20px 24px;">鉴权失败数仅用于展示；重登失败数是本邮件的触发依据。两项为过程指标，可能包含同一账号，四项统计不应相加核对总数。</td></tr>
      </table>
    </td></tr></table>
  </body>
</html>""",
        subtype="html",
    )
    return message


def _build_test_message(
    *,
    sender: str,
    recipients: list[str],
) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = "[Any Auto Register] SMTP 测试成功"
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    occurred_at = _format_beijing_time()
    message.set_content(
        "这是一封 Any Auto Register SMTP 测试邮件。\n\n"
        "SMTP 邮件配置可用，自动认证告警可以正常投递。\n"
        f"测试时间：{occurred_at}\n"
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


def _send_message(
    *,
    snapshot: Mapping[str, object],
    message: EmailMessage,
    sender: str,
    recipients: list[str],
) -> dict[str, object]:
    host = _text(snapshot.get("smtp_host"))
    port = _positive_int(snapshot.get("smtp_port"), DEFAULT_SMTP_PORT)
    username = _text(snapshot.get("smtp_username"))
    password = str(snapshot.get("smtp_password") or "")
    use_ssl = _to_bool(snapshot.get("smtp_use_ssl"), default=True)
    force_auth_login = _to_bool(snapshot.get("smtp_force_auth_login"))

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
            "ChatGPT 自动认证邮件发送失败（%s）",
            error_type,
        )
        return {
            "sent": False,
            "reason": "send_failed",
            "error_type": error_type,
        }

    return {"sent": True, "reason": "sent"}


def send_auto_relogin_alert(
    *,
    task_id: str,
    total_accounts: int,
    successful_accounts: int,
    invalid_rt_count: int,
    relogin_failed_count: int,
    config: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Send one alert when relogin failures reach the configured threshold."""

    snapshot = dict(config) if config is not None else _get_config()
    threshold = _positive_int(
        snapshot.get("chatgpt_auto_relogin_alert_threshold"),
        DEFAULT_ALERT_THRESHOLD,
    )
    invalid_count = _non_negative_int(invalid_rt_count)
    failed_count = _non_negative_int(relogin_failed_count)
    if failed_count < threshold:
        return {
            "sent": False,
            "reason": "below_threshold",
            "threshold": threshold,
        }

    host = _text(snapshot.get("smtp_host"))
    username = _text(snapshot.get("smtp_username"))
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

    message = _build_message(
        sender=sender,
        recipients=recipients,
        task_id=_text(task_id),
        total_accounts=_non_negative_int(total_accounts),
        successful_accounts=_non_negative_int(successful_accounts),
        invalid_rt_count=invalid_count,
        relogin_failed_count=failed_count,
        threshold=threshold,
    )

    result = _send_message(
        snapshot=snapshot,
        message=message,
        sender=sender,
        recipients=recipients,
    )
    result["threshold"] = threshold
    return result


def send_smtp_test_email(
    *,
    config: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Send a dedicated test message without applying the alert threshold."""

    snapshot = dict(config) if config is not None else _get_config()
    host = _text(snapshot.get("smtp_host"))
    username = _text(snapshot.get("smtp_username"))
    sender = _text(snapshot.get("smtp_sender_email")) or username
    recipients = _mailboxes(snapshot.get("smtp_recipient_email"))
    if not recipients:
        recipients = _mailboxes(username or sender)
    if not host or not sender or not recipients:
        return {"sent": False, "reason": "smtp_not_configured"}

    message = _build_test_message(
        sender=sender,
        recipients=recipients,
    )
    result = _send_message(
        snapshot=snapshot,
        message=message,
        sender=sender,
        recipients=recipients,
    )
    if bool(result.get("sent")):
        result["recipient_count"] = len(recipients)
    return result


__all__ = ["send_auto_relogin_alert", "send_smtp_test_email"]
