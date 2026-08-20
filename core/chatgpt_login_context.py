"""Pure helpers for normalizing persisted ChatGPT login credentials."""

from __future__ import annotations

from typing import Any, Mapping


def promote_managed_mfa_login_context(
    mailbox_context: Mapping[str, Any],
    *,
    saved_password: str = "",
) -> dict[str, Any]:
    """Prefer a saved password + locally managed TOTP over legacy mail OTP."""

    context = dict(mailbox_context or {})
    raw_extra = context.get("extra")
    if not isinstance(raw_extra, Mapping):
        return context
    extra = dict(raw_extra)
    account_type = str(extra.get("account_type") or "").strip().lower()
    password = str(extra.get("password") or saved_password or "")
    totp_secret = str(
        extra.get("totp_secret")
        or extra.get("mfa_secret")
        or extra.get("totp")
        or ""
    ).strip()
    managed = extra.get("chatgpt_mfa_managed") is True
    promotable_type = account_type in {
        "mailapi_url",
        "chatgpt_password_url_otp",
        "chatgpt_password_reset_url_mail",
    }
    if not password.strip() or not totp_secret:
        return context
    if account_type != "chatgpt_password_totp" and not (
        managed and promotable_type
    ):
        return context

    mail_api_url = str(
        extra.get("mail_api_url") or extra.get("mailapi_url") or ""
    ).strip()
    extra.update(
        {
            "account_type": "chatgpt_password_totp",
            "password": password,
            "totp_secret": totp_secret,
            "password_reset_required": False,
        }
    )
    if mail_api_url:
        extra["mail_api_url"] = mail_api_url
        extra["mailapi_url"] = mail_api_url
    extra.pop("new_password", None)
    context["extra"] = extra
    return context
