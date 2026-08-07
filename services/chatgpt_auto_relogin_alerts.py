"""Threshold-based SMTP alerts for ChatGPT automatic authentication cycles."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from email.message import EmailMessage
from html import escape
import logging
import re
import smtplib
import ssl
from typing import Iterable, Mapping
from zoneinfo import ZoneInfo

from services.chatgpt_codex2api_quota import (
    AvailableQuotaReport,
    summarize_available_quota,
)


logger = logging.getLogger(__name__)

DEFAULT_ALERT_THRESHOLD = 20
DEFAULT_SMTP_PORT = 587
SMTP_TIMEOUT_SECONDS = 20
BEIJING_TIMEZONE = ZoneInfo("Asia/Shanghai")
USD_CENT = Decimal("0.01")


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


def _business_alert_subject(
    quota_report: AvailableQuotaReport,
    title: str,
) -> str:
    return (
        f"${quota_report.estimated_remaining_usd:.2f}｜"
        f"正常可用账号 {quota_report.account_count} 个｜{title}"
    )


def _quota_alert_threshold(value: object) -> Decimal:
    try:
        parsed = Decimal(_text(value) or "0")
    except (InvalidOperation, ValueError):
        return Decimal("0.00")
    if not parsed.is_finite() or parsed <= 0:
        return Decimal("0.00")
    return parsed.quantize(USD_CENT, rounding=ROUND_HALF_UP)


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
    deleted_account_count: int = 0,
    quota_eligible_failure_count: int | None = None,
    quota_exhausted_failure_count: int = 0,
    quota_report: AvailableQuotaReport | None = None,
) -> EmailMessage:
    if quota_eligible_failure_count is None:
        quota_eligible_failure_count = relogin_failed_count
    quota_report = quota_report or AvailableQuotaReport(
        account_count=0,
        estimated_remaining_usd=0,
        accounts=(),
    )
    message = EmailMessage()
    message["Subject"] = _business_alert_subject(
        quota_report,
        "ChatGPT 重登失败账号告警",
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
        f"其中已删除或停用账号：{deleted_account_count}\n"
        f"仍有额度的重登失败：{quota_eligible_failure_count}\n"
        f"额度已用完的重登失败：{quota_exhausted_failure_count}\n"
        f"正常可用账号：{quota_report.account_count}\n"
        f"Codex2API 账号总数：{quota_report.remote_account_count}\n"
        f"当前估算剩余额度：${quota_report.estimated_remaining_usd:.2f}\n"
        f"告警阈值：{threshold}\n"
        f"完成时间：{occurred_at}\n\n"
        "已删除或停用账号属于重登失败账号的子集。"
        "鉴权失败数仅用于展示；仍有额度的重登失败数是本邮件的触发依据。"
        "两项为过程指标，可能包含同一账号，四项统计不应相加核对总数。\n\n"
        "正常可用账号额度为按 7 天用量百分比和已用成本计算的估算值，金额单位为美元。\n\n"
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
        <tr><td style="padding:0 20px 20px;">本轮自动鉴权已完成，重登失败账号数已达到告警阈值。</td></tr>
        <tr><td style="padding:0 20px 20px;">任务 ID：{escaped_task_id}</td></tr>
        <tr><td style="padding:0 20px 20px;">
          <table role="presentation" width="100%" cellspacing="4" cellpadding="0" border="0"><tr>
            <td class="metric-cell metric-total" width="25%"><strong>账号总数</strong><br>{total_accounts}</td>
            <td class="metric-cell metric-success" width="25%"><strong>成功账号</strong><br>{successful_accounts}</td>
            <td class="metric-cell metric-invalid" width="25%"><strong>鉴权失败</strong><br>{invalid_rt_count}</td>
            <td class="metric-cell metric-failed" width="25%"><strong>重登失败</strong><br>{relogin_failed_count}</td>
          </tr></table>
        </td></tr>
        <tr><td style="padding:0 20px 12px;"><strong>其中已删除或停用账号：{deleted_account_count}</strong>（属于重登失败账号的子集）</td></tr>
        <tr><td style="padding:0 20px 12px;">仍有额度的重登失败：{quota_eligible_failure_count}</td></tr>
        <tr><td style="padding:0 20px 12px;">额度已用完的重登失败：{quota_exhausted_failure_count}</td></tr>
        <tr><td style="padding:0 20px 12px;">正常可用账号：{quota_report.account_count}</td></tr>
        <tr><td style="padding:0 20px 12px;">Codex2API 账号总数：{quota_report.remote_account_count}</td></tr>
        <tr><td style="padding:0 20px 12px;"><strong>当前估算剩余额度：${quota_report.estimated_remaining_usd:.2f}</strong>（美元）</td></tr>
        <tr><td style="padding:0 20px 12px;">告警阈值：{threshold}</td></tr>
        <tr><td style="padding:0 20px 20px;">完成时间：{escaped_occurred_at}</td></tr>
        <tr><td style="padding:0 20px 24px;">已删除或停用账号属于重登失败账号的子集。鉴权失败数仅用于展示；仍有额度的重登失败数是本邮件的触发依据。剩余额度为按 7 天用量百分比和已用成本计算的美元估算值。</td></tr>
        <tr><td style="padding:0 20px 24px;">请在 Any Auto Register 的“任务运行”页面查看本轮详细记录。</td></tr>
      </table>
    </td></tr></table>
  </body>
</html>""",
        subtype="html",
    )
    return message


def _build_quota_threshold_message(
    *,
    sender: str,
    recipients: list[str],
    task_id: str,
    quota_report: AvailableQuotaReport,
    threshold_usd: Decimal,
    quota_eligible_failure_count: int,
    quota_exhausted_failure_count: int,
    relogin_failed_count: int,
    deleted_account_count: int,
) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = _business_alert_subject(
        quota_report,
        "Codex2API 剩余额度不足告警",
    )
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    occurred_at = _format_beijing_time()
    message.set_content(
        "Codex2API 当前正常可用账号的估算剩余额度低于告警阈值。\n\n"
        f"任务 ID：{task_id}\n"
        f"当前估算剩余额度：${quota_report.estimated_remaining_usd:.2f}\n"
        f"额度告警阈值：${threshold_usd:.2f}\n"
        f"正常可用账号：{quota_report.account_count}\n"
        f"Codex2API 账号总数：{quota_report.remote_account_count}\n"
        f"仍有额度的重登失败：{quota_eligible_failure_count}\n"
        f"额度已用完的重登失败：{quota_exhausted_failure_count}\n"
        f"重登失败：{relogin_failed_count}\n"
        f"其中已删除或停用账号：{deleted_account_count}\n"
        f"完成时间：{occurred_at}\n\n"
        "剩余额度为按正常账号的 7 天用量百分比和已用成本计算的美元估算值。\n"
        "自动化流程每次检测到低于阈值时都会发送本告警。\n"
    )
    message.add_alternative(
        f"""<!DOCTYPE html>
<html lang="zh-CN">
  <head><meta charset="utf-8"></head>
  <body style="margin:0;padding:0;color:#1f2937;font-family:Arial,'Microsoft YaHei',sans-serif;">
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0"><tr><td align="center">
      <table role="presentation" width="640" cellspacing="0" cellpadding="0" border="0" style="max-width:640px;width:100%;">
        <tr><td style="padding:24px 20px 12px;"><h2 style="margin:0;">Codex2API 剩余额度不足告警</h2></td></tr>
        <tr><td style="padding:0 20px 20px;">当前正常可用账号的估算剩余额度低于告警阈值。</td></tr>
        <tr><td style="padding:0 20px 12px;">任务 ID：{escape(task_id)}</td></tr>
        <tr><td style="padding:0 20px 12px;"><strong>当前估算剩余额度：${quota_report.estimated_remaining_usd:.2f}</strong></td></tr>
        <tr><td style="padding:0 20px 12px;">额度告警阈值：${threshold_usd:.2f}</td></tr>
        <tr><td style="padding:0 20px 12px;">正常可用账号：{quota_report.account_count}</td></tr>
        <tr><td style="padding:0 20px 12px;">Codex2API 账号总数：{quota_report.remote_account_count}</td></tr>
        <tr><td style="padding:0 20px 12px;">仍有额度的重登失败：{quota_eligible_failure_count}</td></tr>
        <tr><td style="padding:0 20px 12px;">额度已用完的重登失败：{quota_exhausted_failure_count}</td></tr>
        <tr><td style="padding:0 20px 12px;">重登失败：{relogin_failed_count}</td></tr>
        <tr><td style="padding:0 20px 12px;">其中已删除或停用账号：{deleted_account_count}</td></tr>
        <tr><td style="padding:0 20px 20px;">完成时间：{escape(occurred_at)}</td></tr>
        <tr><td style="padding:0 20px 24px;">剩余额度为按正常账号的 7 天用量百分比和已用成本计算的美元估算值。自动化流程每次检测到低于阈值时都会发送本告警。</td></tr>
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
    deleted_account_count: int = 0,
    quota_eligible_failure_count: int | None = None,
    quota_exhausted_failure_count: int = 0,
    quota_accounts: Iterable[Mapping[str, object]] = (),
    quota_report: AvailableQuotaReport | None = None,
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
    eligible_count = (
        failed_count
        if quota_eligible_failure_count is None
        else _non_negative_int(quota_eligible_failure_count)
    )
    exhausted_count = min(
        _non_negative_int(quota_exhausted_failure_count),
        failed_count,
    )
    deleted_count = min(
        _non_negative_int(deleted_account_count),
        failed_count,
    )
    if eligible_count < threshold:
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

    resolved_quota_report = (
        quota_report
        if quota_report is not None
        else summarize_available_quota(quota_accounts)
    )
    message = _build_message(
        sender=sender,
        recipients=recipients,
        task_id=_text(task_id),
        total_accounts=_non_negative_int(total_accounts),
        successful_accounts=_non_negative_int(successful_accounts),
        invalid_rt_count=invalid_count,
        relogin_failed_count=failed_count,
        quota_eligible_failure_count=eligible_count,
        quota_exhausted_failure_count=exhausted_count,
        quota_report=resolved_quota_report,
        threshold=threshold,
        deleted_account_count=deleted_count,
    )

    result = _send_message(
        snapshot=snapshot,
        message=message,
        sender=sender,
        recipients=recipients,
    )
    result["threshold"] = threshold
    return result


def send_quota_threshold_alert(
    *,
    task_id: str,
    quota_report: AvailableQuotaReport,
    quota_eligible_failure_count: int,
    quota_exhausted_failure_count: int,
    relogin_failed_count: int,
    deleted_account_count: int = 0,
    config: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Send a fresh quota alert whenever the configured threshold is crossed."""

    snapshot = dict(config) if config is not None else _get_config()
    threshold_usd = _quota_alert_threshold(
        snapshot.get("chatgpt_auto_relogin_quota_alert_threshold_usd")
    )
    estimated_remaining_usd = quota_report.estimated_remaining_usd.quantize(
        USD_CENT,
        rounding=ROUND_HALF_UP,
    )
    base_result = {
        "threshold_usd": f"{threshold_usd:.2f}",
        "estimated_remaining_usd": f"{estimated_remaining_usd:.2f}",
    }
    if threshold_usd <= 0:
        return {
            "sent": False,
            "reason": "quota_alert_disabled",
            **base_result,
        }
    if estimated_remaining_usd >= threshold_usd:
        return {
            "sent": False,
            "reason": "quota_not_below_threshold",
            **base_result,
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
            **base_result,
        }

    failed_count = _non_negative_int(relogin_failed_count)
    eligible_count = _non_negative_int(quota_eligible_failure_count)
    exhausted_count = min(
        _non_negative_int(quota_exhausted_failure_count),
        failed_count,
    )
    deleted_count = min(_non_negative_int(deleted_account_count), failed_count)
    message = _build_quota_threshold_message(
        sender=sender,
        recipients=recipients,
        task_id=_text(task_id),
        quota_report=quota_report,
        threshold_usd=threshold_usd,
        quota_eligible_failure_count=eligible_count,
        quota_exhausted_failure_count=exhausted_count,
        relogin_failed_count=failed_count,
        deleted_account_count=deleted_count,
    )
    result = _send_message(
        snapshot=snapshot,
        message=message,
        sender=sender,
        recipients=recipients,
    )
    result.update(base_result)
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


__all__ = [
    "send_auto_relogin_alert",
    "send_quota_threshold_alert",
    "send_smtp_test_email",
]
