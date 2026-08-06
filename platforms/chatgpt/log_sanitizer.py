"""Shared secret redaction for ChatGPT task and Python log sinks."""

from __future__ import annotations

import re


_BEARER_RE = re.compile(
    r"(?i)(\bBearer\s+)([A-Za-z0-9._~+/=-]+)",
)
_OTPAUTH_URI_RE = re.compile(r"(?i)otpauth://[^\s,，;]+")
_SECRET_ASSIGNMENT_RE = re.compile(
    r"""(?ix)
    (?P<prefix>
        ["']?
        (?P<key>
            access[_\s-]?token
            | refresh[_\s-]?token
            | id[_\s-]?token
            | session[_\s-]?token
            | authorization[_\s-]?code
            | auth[_\s-]?code
            | password
            | passwd
            | client[_\s-]?secret
            | totp[_\s-]?secret
            | mfa[_\s-]?pending[_\s-]?secret
            | recovery[_\s-]?codes?
            | activation[_\s-]?code
            | secret
        )
        ["']?\s*[:=]\s*
    )
    (?P<value>
        "[^"]*"
        | '[^']*'
        | [^\s,，;&)）}\]]+
    )
    """,
)
_QUERY_CODE_RE = re.compile(
    r"(?i)(?P<prefix>\bcode\s*=\s*)"
    r"(?P<value>\"[^\"]*\"|'[^']*'|[^\s,，;&)）}\]]+)",
)
_DIRECT_OTP_RE = re.compile(
    r"(?P<prefix>"
    r"(?:成功获取验证码|跳过已尝试验证码|尝试\s*[Oo][Tt][Pp]|"
    r"验证码|verification\s+code|[Oo][Tt][Pp])"
    r"\s*[:：=]?\s*)"
    r"(?P<value>\"?[A-Za-z0-9-]{4,}\"?)",
)
_CACHED_OTP_RE = re.compile(
    r"(?P<prefix>缓存\s*[Oo][Tt][Pp][^:\n：]{0,24}[:：]\s*)"
    r"(?P<value>\"?[A-Za-z0-9-]{4,}\"?)",
)


def _masked_value(raw_value: str, label: str) -> str:
    raw = str(raw_value or "")
    if len(raw) >= 2 and raw[0] in {'"', "'"} and raw[-1] == raw[0]:
        return f"{raw[0]}{label}{raw[0]}"
    return label


def _replace_assignment(match: re.Match[str]) -> str:
    key = str(match.group("key") or "").lower()
    if "password" in key or "passwd" in key or "secret" in key:
        label = "[密码已隐藏]"
    elif "code" in key:
        label = "[授权码已隐藏]"
    else:
        label = "[令牌已隐藏]"
    return match.group("prefix") + _masked_value(match.group("value"), label)


def _replace_otp(match: re.Match[str]) -> str:
    raw_value = str(match.group("value") or "")
    unquoted = raw_value.strip('"\'')
    # Avoid turning ordinary lowercase words after a generic "OTP" label into
    # secrets while still covering numeric, mixed and all-uppercase codes.
    if not any(char.isdigit() for char in unquoted) and not unquoted.isupper():
        return match.group(0)
    return match.group("prefix") + _masked_value(
        raw_value,
        "[验证码已隐藏]",
    )


def sanitize_chatgpt_log_message(message) -> str:
    """Return log-safe text while preserving non-secret diagnostic context."""
    text = str(message or "")
    text = _BEARER_RE.sub(r"\1[令牌已隐藏]", text)
    text = _OTPAUTH_URI_RE.sub("[MFA配置已隐藏]", text)
    text = _SECRET_ASSIGNMENT_RE.sub(_replace_assignment, text)
    text = _QUERY_CODE_RE.sub(
        lambda match: match.group("prefix")
        + _masked_value(match.group("value"), "[授权码已隐藏]"),
        text,
    )
    text = _CACHED_OTP_RE.sub(_replace_otp, text)
    text = _DIRECT_OTP_RE.sub(_replace_otp, text)
    return text


__all__ = ["sanitize_chatgpt_log_message"]
