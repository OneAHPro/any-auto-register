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


def _build_alert_html(
    *,
    title: str,
    lead: str,
    badge: str,
    badge_color: str,
    badge_border: str,
    badge_background: str,
    amount_label: str,
    amount_usd: Decimal,
    section_label: str,
    metrics: tuple[tuple[str, int], tuple[str, int], tuple[str, int]],
    notice_title: str,
    notice_body: str,
    notice_color: str,
    task_id: str,
    occurred_at: str,
    footer_note: str,
) -> str:
    metric_cells = "".join(
        f"""
            <td class="metric-cell" width="33.33%" valign="middle" style="height:94px;padding:14px 8px;border:1px solid #e1e5e9;border-radius:10px;background:#f6f7f8;text-align:center;vertical-align:middle;">
              <div style="margin:0 0 7px;font-family:-apple-system,BlinkMacSystemFont,'SF Pro Display','Helvetica Neue',Arial,sans-serif;font-size:27px;font-weight:700;line-height:1;color:#161d26;">{escape(str(value))}</div>
              <div style="color:#737e89;font-size:11px;font-weight:500;line-height:1.25;white-space:nowrap;">{escape(label)}</div>
            </td>"""
        for label, value in metrics
    )
    escaped_notice_color = escape(notice_color, quote=True)
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
      @media only screen and (max-width: 600px) {{
        .page-pad {{ padding:12px !important; }}
        .content-pad {{ padding-left:22px !important; padding-right:22px !important; }}
        .metric-cell {{ display:block !important; width:100% !important; height:auto !important; box-sizing:border-box !important; margin-bottom:10px !important; }}
      }}
    </style>
  </head>
  <body style="margin:0;padding:0;background:#eef1f4;color:#161d26;font-family:-apple-system,BlinkMacSystemFont,'SF Pro Text','PingFang SC','Helvetica Neue',Arial,sans-serif;-webkit-font-smoothing:antialiased;">
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" bgcolor="#eef1f4">
      <tr><td class="page-pad" align="center" style="padding:34px 12px;">
        <table role="presentation" width="680" cellspacing="0" cellpadding="0" border="0" bgcolor="#ffffff" style="max-width:680px;width:100%;overflow:hidden;border:1px solid #e1e5e9;border-radius:16px;background:#ffffff;">
          <tr><td height="5" bgcolor="#161d26" style="height:5px;line-height:5px;font-size:0;">&nbsp;</td></tr>
          <tr><td class="content-pad" style="padding:28px 38px 23px;border-bottom:1px solid #eceff2;">
            <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0"><tr>
              <td style="color:#77818d;font-size:10px;font-weight:650;letter-spacing:1.3px;">ANY AUTO REGISTER · CODEX</td>
              <td align="right"><span style="display:inline-block;padding:5px 9px;border:1px solid {escape(badge_border, quote=True)};border-radius:99px;color:{escape(badge_color, quote=True)};background:{escape(badge_background, quote=True)};font-size:10px;font-weight:650;">{escape(badge)}</span></td>
            </tr></table>
          </td></tr>
          <tr><td class="content-pad" style="padding:32px 38px 28px;">
            <h2 style="margin:0 0 9px;font-family:-apple-system,BlinkMacSystemFont,'SF Pro Display','PingFang SC','Helvetica Neue',Arial,sans-serif;font-size:24px;font-weight:700;line-height:1.3;letter-spacing:-0.5px;">{escape(title)}</h2>
            <p style="margin:0 0 28px;color:#707985;font-size:13px;line-height:1.55;">{escape(lead)}</p>
            <div style="margin-bottom:5px;color:#7a8490;font-size:11px;font-weight:500;">{escape(amount_label)}</div>
            <div style="font-family:-apple-system,BlinkMacSystemFont,'SF Pro Display','Helvetica Neue',Arial,sans-serif;font-size:46px;font-weight:750;line-height:1.08;letter-spacing:-2px;color:#161d26;">${amount_usd:.2f}</div>
            <div style="margin:31px 0 11px;color:#858f9a;font-size:11px;font-weight:600;">{escape(section_label)}</div>
            <table role="presentation" width="100%" cellspacing="10" cellpadding="0" border="0"><tr>{metric_cells}
            </tr></table>
            <div style="margin-top:22px;padding:14px 16px;border-left:3px solid {escaped_notice_color};background:#f7f8f9;color:#5f6974;font-size:11px;line-height:1.7;">
              <strong style="color:{escaped_notice_color};font-weight:650;">{escape(notice_title)}</strong><br>{escape(notice_body)}
            </div>
          </td></tr>
          <tr><td class="content-pad" style="padding:18px 38px 22px;border-top:1px solid #eceff2;background:#fbfbfc;color:#9099a3;font-size:9px;line-height:1.85;">
            完成时间：{escape(occurred_at)}<br>
            <span style="color:#77818c;font-family:'SFMono-Regular',SFMono-Regular,ui-monospace,Menlo,monospace;word-break:break-all;">任务 ID：{escape(task_id)}</span><br>
            {escape(footer_note)}
          </td></tr>
        </table>
      </td></tr>
    </table>
  </body>
</html>"""


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
        "Codex 重登失败账号告警",
    )
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    occurred_at = _format_beijing_time()
    message.set_content(
        "Codex 自动认证周期触发了邮件告警。\n\n"
        f"任务 ID：{task_id}\n"
        f"账号总数：{total_accounts}\n"
        f"成功账号：{successful_accounts}\n"
        f"鉴权失败：{invalid_rt_count}\n"
        f"重登失败：{relogin_failed_count}\n"
        f"其中已删除或停用账号：{deleted_account_count}\n"
        f"仍有额度的重登失败：{quota_eligible_failure_count}\n"
        f"额度已用完的重登失败：{quota_exhausted_failure_count}\n"
        f"正常可用账号：{quota_report.account_count}\n"
        f"账号总数：{quota_report.remote_account_count}\n"
        f"当前剩余可用额度：${quota_report.current_remaining_usd:.2f}\n"
        f"告警阈值：{threshold}\n"
        f"完成时间：{occurred_at}\n\n"
        "已删除或停用账号属于重登失败账号的子集。"
        "鉴权失败数仅用于展示；仍有额度的重登失败数是本邮件的触发依据。"
        "两项为过程指标，可能包含同一账号，四项统计不应相加核对总数。\n\n"
        "正常可用账号额度为按 7 天用量百分比和已用成本计算的估算值，金额单位为美元。\n\n"
        "请在 Any Auto Register 的“任务运行”页面查看本轮详细记录。\n"
    )
    message.add_alternative(
        _build_alert_html(
            title="Codex 重登失败账号告警",
            lead="本轮仍有剩余额度的账号重登失败数量已达到你设置的告警阈值。",
            badge="账号异常",
            badge_color="#9a4b10",
            badge_border="#f2d4b5",
            badge_background="#fff7ed",
            amount_label="当前正常账号估算剩余额度",
            amount_usd=quota_report.estimated_remaining_usd,
            section_label="本轮异常概览",
            metrics=(
                ("仍有额度的重登失败", quota_eligible_failure_count),
                ("其中封禁或删除", deleted_account_count),
                ("正常可用账号", quota_report.account_count),
            ),
            notice_title="告警判定规则",
            notice_body=(
                "只有仍有额度的账号重登失败、被封禁或删除达到阈值时才触发；"
                "额度已用完的失败账号不参与告警。"
                f"本轮额度已用完的重登失败为 {quota_exhausted_failure_count} 个，已自动忽略。"
            ),
            notice_color="#7c3d0c",
            task_id=task_id,
            occurred_at=occurred_at,
            footer_note="请在 Any Auto Register 的“任务运行”页面查看本轮详细记录。",
        ),
        subtype="html",
    )
    return message


def _build_quota_threshold_message(
    *,
    sender: str,
    recipients: list[str],
    task_id: str,
    quota_report: AvailableQuotaReport,
    quota_eligible_failure_count: int,
    quota_exhausted_failure_count: int,
    relogin_failed_count: int,
    deleted_account_count: int,
) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = (
        f"${quota_report.current_remaining_usd:.2f}｜"
        f"正常可用账号 {quota_report.account_count} 个｜Codex 剩余额度不足告警"
    )
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    occurred_at = _format_beijing_time()
    message.set_content(
        "Codex 当前剩余可用额度低于告警阈值。\n\n"
        f"任务 ID：{task_id}\n"
        f"当前剩余可用额度：${quota_report.current_remaining_usd:.2f}\n"
        f"正常可用账号：{quota_report.account_count}\n"
        f"账号总数：{quota_report.remote_account_count}\n"
        f"仍有额度的重登失败：{quota_eligible_failure_count}\n"
        f"额度已用完的重登失败：{quota_exhausted_failure_count}\n"
        f"重登失败：{relogin_failed_count}\n"
        f"其中已删除或停用账号：{deleted_account_count}\n"
        f"完成时间：{occurred_at}\n\n"
        "当前剩余可用额度按账号当前适用的额度窗口估算。\n"
        "自动化流程每次检测到低于阈值时都会发送本告警。\n"
    )
    message.add_alternative(
        _build_alert_html(
            title="Codex 剩余额度不足告警",
            lead="当前剩余可用额度已低于告警阈值。",
            badge="额度不足",
            badge_color="#b83b30",
            badge_border="#ffd0ca",
            badge_background="#fff2f0",
            amount_label="当前剩余可用额度",
            amount_usd=quota_report.current_remaining_usd,
            section_label="账号概览",
            metrics=(
                ("正常可用账号", quota_report.account_count),
                ("账号总数", quota_report.remote_account_count),
                ("本轮重登失败", relogin_failed_count),
            ),
            notice_title="持续告警规则",
            notice_body=(
                "自动化流程每轮检测一次；只要当前剩余可用额度仍低于你设置的阈值，"
                "就会继续发送本邮件。"
            ),
            notice_color="#252e38",
            task_id=task_id,
            occurred_at=occurred_at,
            footer_note=(
                "当前剩余可用额度按账号当前适用的额度窗口估算，金额单位为美元。"
            ),
        ),
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
    if not quota_report.current_fresh:
        return {
            "sent": False,
            "reason": "quota_data_stale",
            "threshold_usd": f"{threshold_usd:.2f}",
            "estimated_remaining_usd": f"{quota_report.current_remaining_usd:.2f}",
        }
    estimated_remaining_usd = quota_report.current_remaining_usd.quantize(
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
