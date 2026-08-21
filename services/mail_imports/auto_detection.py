from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from core.applemail_pool import parse_applemail_pool_import_content
from core.mail_import_delimiters import (
    has_mail_import_dash_delimiter,
    mail_import_row_pattern,
    split_mail_import_fields,
    split_mail_import_first_field,
)

from .microsoft_import_rules import AutoDetectRowParser
from .schemas import MailImportAccountType, MailImportProviderType


MICROSOFT_MAIL_DOMAINS = {
    "hotmail.com",
    "hotmail.co.jp",
    "hotmail.co.uk",
    "live.com",
    "live.cn",
    "live.co.uk",
    "msn.com",
    "outlook.com",
    "outlook.jp",
}
APPLE_MAIL_DOMAINS = {"icloud.com", "mac.com", "me.com"}
_MFA_SECRET_RE = re.compile(r"[A-Z2-7]{16,128}", re.IGNORECASE)
_OAUTH_CLIENT_RE = re.compile(r"[0-9a-f-]{32,40}", re.IGNORECASE)
_EMAIL_ADDRESS_RE = re.compile(
    r"^[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9.-]+\.[A-Z]{2,}$",
    re.IGNORECASE,
)
_EMAIL_ROW_RE = re.compile(
    mail_import_row_pattern(_EMAIL_ADDRESS_RE.pattern[:-1]),
    re.IGNORECASE,
)


@dataclass
class AutoDetectedMailRow:
    line_number: int
    email: str
    provider: MailImportProviderType | None
    account_type: MailImportAccountType | None
    resolved: bool
    message: str = ""
    raw_content: str = field(default="", repr=False)

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "line_number": self.line_number,
            "email": self.email,
            "provider": self.provider,
            "account_type": self.account_type,
            "resolved": self.resolved,
            "message": self.message,
        }


@dataclass
class AutoMailImportDetection:
    rows: list[AutoDetectedMailRow]
    duplicate_emails: list[str] = field(default_factory=list)
    _provider_contents: dict[str, str] = field(default_factory=dict, repr=False)

    @property
    def has_duplicates(self) -> bool:
        return bool(self.duplicate_emails)

    @property
    def counts(self) -> dict[str, int]:
        return {
            "microsoft": sum(row.provider == "microsoft" and row.resolved for row in self.rows),
            "applemail": sum(row.provider == "applemail" and row.resolved for row in self.rows),
            "unresolved": sum(not row.resolved for row in self.rows),
        }

    @property
    def can_import(self) -> bool:
        return bool(self.rows) and self.counts["unresolved"] == 0

    def provider_content(self, provider: MailImportProviderType) -> str:
        return self._provider_contents.get(provider, "")

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "counts": self.counts,
            "can_import": self.can_import,
            "has_duplicates": self.has_duplicates,
            "duplicate_emails": list(self.duplicate_emails),
            "rows": [row.to_public_dict() for row in self.rows],
        }


def _looks_like_http_url(value: str) -> bool:
    try:
        parsed = urlsplit(str(value or "").strip())
    except ValueError:
        return False
    return parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc)


def _looks_like_remote_totp_url(value: str) -> bool:
    if not _looks_like_http_url(value):
        return False
    parsed = urlsplit(str(value).strip())
    host = str(parsed.hostname or "").lower().rstrip(".")
    path = str(parsed.path or "").lower()
    return host in {"2fa.nloop.cc", "mfa.nloop.cc"} and (
        "/api/mfa/" in path or path.endswith("/2fa") or path.endswith("/view")
    )


def _looks_like_mfa_secret(value: str) -> bool:
    normalized = re.sub(r"[\s-]+", "", str(value or ""))
    return bool(_MFA_SECRET_RE.fullmatch(normalized))


def _looks_like_oauth_pair(client_id: str, refresh_token: str) -> bool:
    client = str(client_id or "").strip()
    token = str(refresh_token or "").strip()
    return bool((_OAUTH_CLIENT_RE.fullmatch(client) or len(client) >= 24) and len(token) >= 32)


def _is_reset_marker(value: str) -> bool:
    text = str(value or "").strip().lower()
    return any(marker in text for marker in ("忘记密码", "忘記密碼", "forgot password"))


def _email_domain(email: str) -> str:
    return str(email or "").strip().lower().rpartition("@")[2]


def _split_row(line: str) -> list[str]:
    return split_mail_import_fields(line)


def _looks_like_credential_line(line: str) -> bool:
    """Keep malformed credential rows visible without treating prose as an account."""
    if _EMAIL_ROW_RE.match(line) or "@" in line:
        return True
    if not has_mail_import_dash_delimiter(line) and "\t" not in line:
        return False
    first_field = split_mail_import_first_field(line)
    return bool(re.fullmatch(r"[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+\.[A-Z]{2,}", first_field, re.IGNORECASE))


def _resolved_row(
    *,
    line_number: int,
    email: str,
    provider: MailImportProviderType,
    account_type: MailImportAccountType,
    raw_content: str,
) -> AutoDetectedMailRow:
    return AutoDetectedMailRow(
        line_number=line_number,
        email=email,
        provider=provider,
        account_type=account_type,
        resolved=True,
        raw_content=raw_content,
    )


def _unresolved_row(
    *,
    line_number: int,
    email: str,
    message: str,
    raw_content: str,
) -> AutoDetectedMailRow:
    return AutoDetectedMailRow(
        line_number=line_number,
        email=email,
        provider=None,
        account_type=None,
        resolved=False,
        message=message,
        raw_content=raw_content,
    )


def _resolved_apple_row(
    *,
    line_number: int,
    email: str,
    raw_content: str,
    fallback_account_type: MailImportAccountType,
) -> AutoDetectedMailRow:
    try:
        records, errors, _total = parse_applemail_pool_import_content(raw_content)
    except (TypeError, ValueError, json.JSONDecodeError):
        records, errors = [], ["invalid"]
    if len(records) != 1 or errors:
        return _unresolved_row(
            line_number=line_number,
            email=email,
            message="AppleMail 凭据字段不完整或格式无效，请检查该行",
            raw_content=raw_content,
        )
    account_type = str(records[0].get("account_type") or fallback_account_type)
    return _resolved_row(
        line_number=line_number,
        email=email,
        provider="applemail",
        account_type=account_type,  # type: ignore[arg-type]
        raw_content=raw_content,
    )


def _resolved_microsoft_row(
    *,
    line_number: int,
    email: str,
    raw_content: str,
) -> AutoDetectedMailRow:
    try:
        record = AutoDetectRowParser().parse(line_number, raw_content)
    except ValueError:
        return _unresolved_row(
            line_number=line_number,
            email=email,
            message="微软邮箱凭据字段不完整或格式无效，请检查该行",
            raw_content=raw_content,
        )
    return _resolved_row(
        line_number=line_number,
        email=email,
        provider="microsoft",
        account_type=record.account_type,  # type: ignore[arg-type]
        raw_content=raw_content,
    )


def _detect_text_row(line_number: int, line: str) -> AutoDetectedMailRow:
    parts = _split_row(line)
    email = parts[0].strip() if parts else ""
    if re.search(r"\\+@yisen\.uk$", email, re.IGNORECASE):
        email = re.sub(r"\\+@", "@", email)
    if not _EMAIL_ADDRESS_RE.fullmatch(email):
        return _unresolved_row(
            line_number=line_number,
            email=email if "@" in email else "",
            message="未识别到有效邮箱地址，请检查该行格式",
            raw_content=line,
        )

    domain = _email_domain(email)
    if domain == "yisen.uk":
        return _resolved_microsoft_row(
            line_number=line_number,
            email=email,
            raw_content=line,
        )
    if len(parts) == 2 and _looks_like_http_url(parts[1]):
        return _resolved_microsoft_row(
            line_number=line_number,
            email=email,
            raw_content=line,
        )
    if len(parts) == 2 and parts[1].strip():
        return _resolved_apple_row(
            line_number=line_number,
            email=email,
            raw_content=line,
            fallback_account_type="chatgpt_google_password",
        )

    if len(parts) == 3:
        second, third = parts[1:3]
        if _is_reset_marker(second) and _looks_like_http_url(third):
            return _resolved_apple_row(
                line_number=line_number,
                email=email,
                raw_content=line,
                fallback_account_type="chatgpt_password_reset_url_mail",
            )
        if _looks_like_remote_totp_url(third):
            return _resolved_apple_row(
                line_number=line_number,
                email=email,
                raw_content=line,
                fallback_account_type="chatgpt_password_remote_totp",
            )
        if _looks_like_http_url(third) and second:
            return _resolved_microsoft_row(
                line_number=line_number,
                email=email,
                raw_content=line,
            )
        if _looks_like_mfa_secret(third):
            return _resolved_apple_row(
                line_number=line_number,
                email=email,
                raw_content=line,
                fallback_account_type="chatgpt_password_totp",
            )
        if _looks_like_oauth_pair(second, third):
            return _resolved_apple_row(
                line_number=line_number,
                email=email,
                raw_content=line,
                fallback_account_type="applemail_oauth",
            )

    if len(parts) >= 4:
        password, third, fourth = parts[1:4]
        if _looks_like_http_url(third) or _looks_like_http_url(fourth):
            if _looks_like_remote_totp_url(third) and _looks_like_remote_totp_url(fourth):
                fallback_account_type = "chatgpt_password_remote_totp"
            elif _looks_like_remote_totp_url(third) and not _looks_like_http_url(fourth):
                fallback_account_type = "chatgpt_password_remote_totp"
            elif _looks_like_remote_totp_url(fourth) and not _looks_like_http_url(third):
                fallback_account_type = "chatgpt_password_remote_totp"
            else:
                fallback_account_type = (
                    "chatgpt_password_reset_url_mail"
                    if _is_reset_marker(password)
                    else "chatgpt_password_url_otp"
                )
            return _resolved_apple_row(
                line_number=line_number,
                email=email,
                raw_content=line,
                fallback_account_type=fallback_account_type,
            )
        if _looks_like_oauth_pair(third, fourth):
            if domain in MICROSOFT_MAIL_DOMAINS:
                return _resolved_microsoft_row(
                    line_number=line_number,
                    email=email,
                    raw_content=line,
                )
            if domain in APPLE_MAIL_DOMAINS:
                return _resolved_apple_row(
                    line_number=line_number,
                    email=email,
                    raw_content=line,
                    fallback_account_type="applemail_oauth",
                )
            return _unresolved_row(
                line_number=line_number,
                email=email,
                message="该 OAuth 格式无法可靠区分邮箱池，请使用手动类型兜底",
                raw_content=line,
            )

    return _unresolved_row(
        line_number=line_number,
        email=email,
        message="暂未识别该邮箱格式，请检查内容或使用手动类型兜底",
        raw_content=line,
    )


def _detect_json_content(content: str) -> AutoMailImportDetection:
    try:
        records, errors, total = parse_applemail_pool_import_content(content)
    except (TypeError, ValueError, json.JSONDecodeError):
        return AutoMailImportDetection(
            rows=[
                _unresolved_row(
                    line_number=1,
                    email="",
                    message="JSON 邮箱池格式无效，请检查内容",
                    raw_content=content,
                )
            ]
        )

    rows: list[AutoDetectedMailRow] = []
    for index, record in enumerate(records, start=1):
        account_type = str(record.get("account_type") or "applemail_oauth")
        rows.append(
            _resolved_row(
                line_number=index,
                email=str(record.get("email") or ""),
                provider="applemail",
                account_type=account_type,  # type: ignore[arg-type]
                raw_content="",
            )
        )
    for offset in range(len(errors)):
        rows.append(
            _unresolved_row(
                line_number=len(records) + offset + 1,
                email="",
                message="JSON 中存在未能识别的邮箱记录，请检查对应字段",
                raw_content="",
            )
        )
    if total and not rows:
        rows.append(
            _unresolved_row(
                line_number=1,
                email="",
                message="JSON 中没有可导入的邮箱记录",
                raw_content="",
            )
        )
    return AutoMailImportDetection(
        rows=rows,
        _provider_contents={"applemail": content},
    )


def _mark_duplicates(rows: list[AutoDetectedMailRow]) -> list[str]:
    by_email: dict[str, list[AutoDetectedMailRow]] = {}
    for row in rows:
        key = row.email.strip().lower()
        if key:
            by_email.setdefault(key, []).append(row)

    duplicates = sorted(key for key, matching in by_email.items() if len(matching) > 1)
    for email in duplicates:
        for row in by_email[email]:
            row.provider = None
            row.account_type = None
            row.resolved = False
            row.message = "同一批次存在重复邮箱，请删除重复行后再导入"
    return duplicates


def detect_mail_import_content(content: str) -> AutoMailImportDetection:
    text = str(content or "").strip().lstrip("\ufeff")
    if not text:
        return AutoMailImportDetection(rows=[])
    if text[:1] in {"[", "{"}:
        result = _detect_json_content(text)
        result.duplicate_emails = _mark_duplicates(result.rows)
        if result.has_duplicates:
            result._provider_contents = {}
        return result

    lines: list[tuple[int, str]] = []
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip().lstrip("\ufeff")
        if not line or line.startswith("#") or not _looks_like_credential_line(line):
            continue
        lines.append((line_number, line))
    rows = [_detect_text_row(line_number, line) for line_number, line in lines]
    duplicate_emails = _mark_duplicates(rows)

    provider_lines: dict[str, list[str]] = {"microsoft": [], "applemail": []}
    for row in rows:
        if row.resolved and row.provider:
            provider_lines[row.provider].append(row.raw_content)
    provider_contents = {
        provider: "\n".join(values)
        for provider, values in provider_lines.items()
        if values
    }
    return AutoMailImportDetection(
        rows=rows,
        duplicate_emails=duplicate_emails,
        _provider_contents=provider_contents,
    )
