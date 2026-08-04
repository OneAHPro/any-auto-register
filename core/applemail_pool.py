from __future__ import annotations

import json
import os
import re
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


_POOL_FILE_LOCK = threading.RLock()

_POOL_STATE_AVAILABLE = "available"
_POOL_STATE_CLAIMED = "claimed"
_POOL_STATE_USED = "used"


@contextmanager
def _pool_transaction_lock(path: Path):
    """Serialize a complete pool read/modify/write transaction across processes."""
    lock_path = path.with_name(f".{path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _POOL_FILE_LOCK:
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(lock_fd, "a+b", closefd=True) as lock_handle:
            if os.name == "nt":
                import msvcrt

                if lock_handle.seek(0, os.SEEK_END) == 0:
                    lock_handle.write(b"\0")
                    lock_handle.flush()
                lock_handle.seek(0)
                msvcrt.locking(lock_handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                lock_handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(lock_handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def _project_root() -> Path:
    return Path.cwd()


def _normalize_pool_dir(pool_dir: str | None = None) -> Path:
    raw = str(pool_dir or "mail").strip() or "mail"
    path = Path(raw)
    if path.is_absolute():
        return path
    return _project_root() / path


def _normalize_filename(filename: str | None = None) -> str:
    raw = Path(str(filename or "").strip() or "").name
    if not raw:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return f"applemail_{timestamp}.json"

    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", raw).strip("._")
    if not safe:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe = f"applemail_{timestamp}"
    if not safe.lower().endswith(".json"):
        safe += ".json"
    return safe


def _extract_first(values: dict[str, Any], *keys: str) -> str:
    for key in keys:
        text = str(values.get(key) or "").strip()
        if text:
            return text
    return ""


def _normalize_mailbox(value: Any) -> str:
    mailbox = str(value or "").strip()
    return mailbox or "INBOX"


def _normalize_enabled(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
        "disabled",
    }


def _copy_pool_metadata(
    record: dict[str, Any],
    entry: dict[str, Any],
) -> dict[str, Any]:
    if "enabled" in entry:
        record["enabled"] = _normalize_enabled(entry.get("enabled"))

    state = str(entry.get("pool_state") or "").strip().lower()
    if state in {
        _POOL_STATE_AVAILABLE,
        _POOL_STATE_CLAIMED,
        _POOL_STATE_USED,
    }:
        record["pool_state"] = state
        record["enabled"] = state == _POOL_STATE_AVAILABLE

    claim_id = str(entry.get("pool_claim_id") or "").strip()
    if claim_id and state == _POOL_STATE_CLAIMED:
        record["pool_claim_id"] = claim_id[:128]
    return record


def _record_is_available(record: dict[str, Any]) -> bool:
    state = str(record.get("pool_state") or "").strip().lower()
    if state in {_POOL_STATE_CLAIMED, _POOL_STATE_USED}:
        return False
    if state == _POOL_STATE_AVAILABLE:
        return True
    return _normalize_enabled(record.get("enabled", True))


def _normalize_mfa_secret(value: Any) -> str:
    secret = re.sub(r"[\s-]+", "", str(value or "")).upper()
    if not re.fullmatch(r"[A-Z2-7]{16,128}", secret):
        raise ValueError("iCloud MFA 秘钥格式无效，需为 Base32 字符串")
    return secret


def _looks_like_mfa_secret(value: Any) -> bool:
    try:
        _normalize_mfa_secret(value)
        return True
    except ValueError:
        return False


def _is_icloud_mail_address(value: Any) -> bool:
    domain = str(value or "").strip().lower().rpartition("@")[2]
    return domain in {"icloud.com", "me.com", "mac.com"}


def _normalize_http_url(value: Any, label: str) -> str:
    text = str(value or "").strip()
    try:
        parsed = urlsplit(text)
    except ValueError as exc:
        raise ValueError(f"{label}格式无效") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{label}格式无效")
    return text


def _looks_like_http_url(value: Any) -> bool:
    try:
        _normalize_http_url(value, "URL")
        return True
    except ValueError:
        return False


def _looks_like_mail_message_url(value: Any) -> bool:
    if not _looks_like_http_url(value):
        return False
    parsed = urlsplit(str(value).strip())
    return "messages" in (
        segment.lower() for segment in parsed.path.split("/") if segment
    )


def _looks_like_direct_totp_url(value: Any) -> bool:
    if not _looks_like_http_url(value):
        return False
    parsed = urlsplit(str(value).strip())
    segments = [segment for segment in parsed.path.split("/") if segment]
    return bool(
        len(segments) == 1
        and re.fullmatch(r"[A-Z2-7]{16,128}", segments[0].upper())
    )


def _resolve_url_credential_roles(third: str, fourth: str) -> tuple[str, str]:
    if _looks_like_direct_totp_url(third) and _looks_like_mail_message_url(fourth):
        return fourth, third
    return third, fourth


def _is_password_reset_marker(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return any(marker in text for marker in ("忘记密码", "忘記密碼", "forgot password"))


def _looks_like_oauth_pair(client_id: Any, refresh_token: Any) -> bool:
    client = str(client_id or "").strip()
    token = str(refresh_token or "").strip()
    return bool(
        (re.fullmatch(r"[0-9a-fA-F-]{32,40}", client) or len(client) >= 24)
        and len(token) >= 32
    )


def _normalize_url_credential(
    *,
    email: str,
    password: str,
    mail_api_url: str,
    totp_url: str = "",
    account_type: str,
    mailbox: str = "INBOX",
    password_reset_required: bool = True,
) -> dict[str, Any]:
    if account_type == "chatgpt_password_reset_url_mail":
        record: dict[str, Any] = {
            "email": email,
            "password": "" if password_reset_required else str(password or "").strip(),
            "mail_api_url": _normalize_http_url(mail_api_url, "收件地址"),
            "account_type": account_type,
            "mailbox": _normalize_mailbox(mailbox),
            "password_reset_required": bool(password_reset_required),
        }
        if str(totp_url or "").strip():
            record["totp_url"] = _normalize_http_url(totp_url, "2FA 地址")
        return record
    if not str(password or "").strip():
        raise ValueError(f"{email} 缺少 ChatGPT 密码")
    return {
        "email": email,
        "password": str(password).strip(),
        "mail_api_url": _normalize_http_url(mail_api_url, "收件地址"),
        "totp_url": _normalize_http_url(totp_url, "2FA 地址"),
        "account_type": "chatgpt_password_url_otp",
        "mailbox": _normalize_mailbox(mailbox),
    }


def _normalize_record(entry: Any) -> dict[str, str]:
    if isinstance(entry, str):
        return _normalize_text_record(entry)
    if isinstance(entry, (list, tuple)):
        return _normalize_sequence_record(entry)
    if not isinstance(entry, dict):
        raise ValueError(f"不支持的邮箱记录格式: {type(entry).__name__}")

    email = _extract_first(entry, "email", "mail", "address", "username")
    client_id = _extract_first(entry, "client_id", "clientId", "clientID")
    refresh_token = _extract_first(
        entry,
        "refresh_token",
        "refreshToken",
        "rt",
    )
    mailbox = _normalize_mailbox(
        _extract_first(entry, "mailbox", "folder", "mail_folder", "mailFolder")
    )
    password = _extract_first(entry, "password", "pass", "pwd")
    account_type = _extract_first(entry, "account_type", "accountType", "type").lower()
    mail_api_url = _extract_first(
        entry,
        "mail_api_url",
        "mailApiUrl",
        "mail_url",
        "mailUrl",
    )
    totp_url = _extract_first(entry, "totp_url", "totpUrl", "mfa_url", "mfaUrl")
    mfa_secret = _extract_first(
        entry,
        "mfa_secret",
        "mfaSecret",
        "mfa",
        "totp_secret",
        "totpSecret",
        "totp",
        "secret",
    )

    if not email:
        raise ValueError("缺少 email")
    reset_marker = _is_password_reset_marker(password)
    if (
        account_type == "chatgpt_password_reset_url_mail" or reset_marker
    ) and mail_api_url:
        reset_required = (
            True
            if reset_marker
            else _normalize_enabled(
                entry.get("password_reset_required", not bool(password))
            )
        )
        return _copy_pool_metadata(_normalize_url_credential(
            email=email,
            password="" if reset_marker else password,
            mail_api_url=mail_api_url,
            totp_url=totp_url,
            account_type="chatgpt_password_reset_url_mail",
            mailbox=mailbox,
            password_reset_required=reset_required,
        ), entry)
    if account_type in {
        "chatgpt_password_url_otp",
        "chatgpt_password_remote_totp",
    } or (mail_api_url and totp_url):
        return _copy_pool_metadata(_normalize_url_credential(
            email=email,
            password=password,
            mail_api_url=mail_api_url,
            totp_url=totp_url,
            account_type="chatgpt_password_url_otp",
            mailbox=mailbox,
        ), entry)
    if password and mfa_secret and account_type == "icloud_web":
        return _copy_pool_metadata({
            "email": email,
            "password": password,
            "mfa_secret": _normalize_mfa_secret(mfa_secret),
            "account_type": "icloud_web",
            "mailbox": mailbox,
        }, entry)
    if password and mfa_secret:
        return _copy_pool_metadata({
            "email": email,
            "password": password,
            "totp_secret": _normalize_mfa_secret(mfa_secret),
            "account_type": "chatgpt_password_totp",
            "mailbox": mailbox,
        }, entry)
    if not client_id or not refresh_token:
        raise ValueError(
            f"{email} 缺少 client_id + refresh_token，"
            "或 password + mfa_secret"
        )

    record = {
        "email": email,
        "client_id": client_id,
        "refresh_token": refresh_token,
        "mailbox": mailbox,
    }
    if password:
        record["password"] = password
    return _copy_pool_metadata(record, entry)


def _normalize_sequence_record(entry: list[Any] | tuple[Any, ...]) -> dict[str, str]:
    values = [str(item or "").strip() for item in entry]
    while values and not values[-1]:
        values.pop()
    if not any(values):
        raise ValueError("空邮箱记录")
    if len(values) < 3:
        raise ValueError("邮箱记录字段不足")
    if len(values) == 3:
        email, second, third = values
        if _looks_like_http_url(third):
            if not _is_password_reset_marker(second):
                raise ValueError(f"{email} URL 收件记录缺少 2FA 地址")
            return _normalize_url_credential(
                email=email,
                password="",
                mail_api_url=third,
                account_type="chatgpt_password_reset_url_mail",
            )
        if _looks_like_oauth_pair(second, third):
            return {
                "email": email,
                "client_id": second,
                "refresh_token": third,
                "mailbox": "INBOX",
            }
        if _looks_like_mfa_secret(third) or _is_icloud_mail_address(email):
            return {
                "email": email,
                "password": second,
                "totp_secret": _normalize_mfa_secret(third),
                "account_type": "chatgpt_password_totp",
                "mailbox": "INBOX",
            }
        return {
            "email": email,
            "client_id": second,
            "refresh_token": third,
            "mailbox": "INBOX",
        }
    if len(values) >= 4:
        email, password, third, fourth = values[:4]
        mail_api_url, totp_url = _resolve_url_credential_roles(third, fourth)
        if _is_password_reset_marker(password) and _looks_like_http_url(mail_api_url):
            return _normalize_url_credential(
                email=email,
                password="",
                mail_api_url=mail_api_url,
                totp_url=totp_url,
                account_type="chatgpt_password_reset_url_mail",
            )
        if _looks_like_http_url(mail_api_url) or _looks_like_http_url(totp_url):
            return _normalize_url_credential(
                email=email,
                password=password,
                mail_api_url=mail_api_url,
                totp_url=totp_url,
                account_type="chatgpt_password_url_otp",
            )
        client_id, refresh_token = third, fourth
        record = {
            "email": email,
            "client_id": client_id,
            "refresh_token": refresh_token,
            "mailbox": "INBOX",
        }
        if password:
            record["password"] = password
        if len(values) >= 5 and values[4]:
            record["mailbox"] = _normalize_mailbox(values[4])
        return record

    email, client_id, refresh_token = values[:3]
    record = {
        "email": email,
        "client_id": client_id,
        "refresh_token": refresh_token,
        "mailbox": "INBOX",
    }
    if len(values) >= 4 and values[3]:
        record["mailbox"] = _normalize_mailbox(values[3])
    return record


def _normalize_text_record(line: str) -> dict[str, str]:
    text = str(line or "").strip()
    if not text:
        raise ValueError("空邮箱记录")

    if "----" in text:
        return _normalize_sequence_record(text.split("----"))
    if "\t" in text:
        return _normalize_sequence_record(text.split("\t"))
    return _normalize_sequence_record(text.split())


def _unwrap_json_records(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "items", "accounts", "list", "emails", "mails"):
            if isinstance(payload.get(key), list):
                return payload[key]
        return [payload]
    raise ValueError(f"JSON 根节点必须是对象或数组，当前为 {type(payload).__name__}")


def parse_applemail_pool_content(
    content: str,
    *,
    allow_empty: bool = False,
) -> list[dict[str, str]]:
    text = str(content or "").strip()
    if not text:
        raise ValueError("邮箱池内容为空")

    if text[:1] in {"[", "{"}:
        payload = json.loads(text)
        items = _unwrap_json_records(payload)
        records = [_normalize_record(item) for item in items]
    else:
        lines = [
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        records = [_normalize_text_record(line) for line in lines]

    if not records and not allow_empty:
        raise ValueError("邮箱池内容为空")
    return records


_EMAIL_ROW_RE = re.compile(
    r"^[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9.-]+\.[A-Z]{2,}(?:----|\t|\s)",
    re.IGNORECASE,
)


def parse_applemail_pool_import_content(
    content: str,
) -> tuple[list[dict[str, str]], list[str], int]:
    """Parse supplier content row-by-row without leaking credential URLs in errors."""
    text = str(content or "").strip()
    if not text:
        raise ValueError("邮箱池内容为空")

    candidates: list[tuple[int, Any]] = []
    if text[:1] in {"[", "{"}:
        payload = json.loads(text)
        candidates = list(enumerate(_unwrap_json_records(payload), start=1))
    else:
        for line_number, raw_line in enumerate(text.splitlines(), start=1):
            line = raw_line.strip()
            if not line or line.startswith("#") or not _EMAIL_ROW_RE.match(line):
                continue
            candidates.append((line_number, line))

    if not candidates:
        raise ValueError("未找到邮箱记录")

    records: list[dict[str, str]] = []
    errors: list[str] = []
    for line_number, item in candidates:
        if isinstance(item, dict):
            safe_email = _extract_first(item, "email", "mail", "address", "username")
        else:
            safe_email = re.split(r"----|\t|\s", str(item), maxsplit=1)[0].strip()
        try:
            records.append(_normalize_record(item))
        except Exception as exc:
            message = str(exc or "导入失败")
            message = re.sub(r"https?://\S+", "[URL已隐藏]", message)
            errors.append(f"行 {line_number}: {safe_email or '未知邮箱'} 导入失败: {message}")
    return records, errors, len(candidates)


def resolve_applemail_pool_path(
    *,
    pool_file: str | None = None,
    pool_dir: str | None = None,
) -> Path:
    base_dir = _normalize_pool_dir(pool_dir)
    base_dir.mkdir(parents=True, exist_ok=True)

    raw_file = str(pool_file or "").strip()
    if raw_file:
        file_path = Path(raw_file)
        if file_path.is_absolute():
            resolved = file_path
        else:
            resolved = base_dir / file_path.name
            if not resolved.exists():
                fallback = _project_root() / raw_file
                if fallback.exists():
                    resolved = fallback
        if not resolved.exists():
            raise RuntimeError(f"小苹果邮箱池文件不存在: {resolved}")
        return resolved

    candidates = [
        path
        for pattern in ("*.json", "*.txt", "*.csv")
        for path in base_dir.glob(pattern)
        if path.is_file()
    ]
    if not candidates:
        raise RuntimeError(f"mail 目录下未找到可用的小苹果邮箱池文件: {base_dir}")
    candidates.sort(key=lambda item: (item.stat().st_mtime, item.name), reverse=True)
    return candidates[0]


def load_applemail_pool_records(
    *,
    pool_file: str | None = None,
    pool_dir: str | None = None,
) -> tuple[Path, list[dict[str, str]]]:
    path = resolve_applemail_pool_path(pool_file=pool_file, pool_dir=pool_dir)
    with _pool_transaction_lock(path):
        content = path.read_text(encoding="utf-8", errors="ignore")
        records = parse_applemail_pool_content(content, allow_empty=True)
    return path, records


def load_applemail_pool_snapshot(
    *,
    pool_file: str | None = None,
    pool_dir: str | None = None,
    preview_limit: int = 100,
) -> dict[str, Any]:
    path, records = load_applemail_pool_records(pool_file=pool_file, pool_dir=pool_dir)
    available_records = [record for record in records if _record_is_available(record)]
    limit = max(int(preview_limit or 0), 0)
    items = [
        {
            "index": idx,
            "email": record["email"],
            "mailbox": record.get("mailbox") or "INBOX",
            "account_type": (
                str(record.get("account_type") or "").strip()
                or "applemail_oauth"
            ),
        }
        for idx, record in enumerate(available_records[:limit], start=1)
    ]
    return {
        "filename": path.name,
        "path": str(path),
        "count": len(available_records),
        "items": items,
        "truncated": (
            len(available_records) > limit
            if limit > 0
            else len(available_records) > 0
        ),
    }


def _atomic_write_pool_records(path: Path, records: list[dict[str, Any]]) -> None:
    temp_path = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        temp_fd = os.open(
            temp_path,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
        )
        with os.fdopen(temp_fd, "w", encoding="utf-8") as handle:
            json.dump(records, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        path.chmod(0o600)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except Exception:
            pass


def _find_claimed_record(
    records: list[dict[str, Any]],
    *,
    claim_id: str = "",
    email: str = "",
) -> dict[str, Any] | None:
    normalized_claim_id = str(claim_id or "").strip()
    normalized_email = str(email or "").strip().lower()
    if normalized_claim_id:
        return next(
            (
                record
                for record in records
                if str(record.get("pool_claim_id") or "").strip()
                == normalized_claim_id
            ),
            None,
        )
    if normalized_email:
        return next(
            (
                record
                for record in records
                if str(record.get("email") or "").strip().lower()
                == normalized_email
                and str(record.get("pool_state") or "").strip().lower()
                == _POOL_STATE_CLAIMED
            ),
            None,
        )
    return None


def take_next_applemail_record(
    *,
    pool_file: str | None = None,
    pool_dir: str | None = None,
    email: str | None = None,
    exclude_emails=None,
) -> tuple[Path, dict[str, str]]:
    path = resolve_applemail_pool_path(pool_file=pool_file, pool_dir=pool_dir)
    target_email = str(email or "").strip().lower()
    exclusion_values = (
        [exclude_emails]
        if isinstance(exclude_emails, str)
        else (exclude_emails or ())
    )
    excluded_emails = {
        str(item or "").strip().lower()
        for item in exclusion_values
        if str(item or "").strip()
    }
    with _pool_transaction_lock(path):
        content = path.read_text(encoding="utf-8", errors="ignore")
        records = parse_applemail_pool_content(content, allow_empty=True)
        record = next(
            (
                item
                for item in records
                if _record_is_available(item)
                and str(item.get("email") or "").strip().lower()
                not in excluded_emails
                and (
                    not target_email
                    or str(item.get("email") or "").strip().lower()
                    == target_email
                )
            ),
            None,
        )
        if record is None:
            if target_email:
                raise RuntimeError(f"指定邮箱不在可用池中: {email}")
            raise RuntimeError("小苹果邮箱账号池没有可用邮箱")
        record["enabled"] = False
        record["pool_state"] = _POOL_STATE_CLAIMED
        record["pool_claim_id"] = uuid.uuid4().hex
        _atomic_write_pool_records(path, records)
        return path, dict(record)


def load_used_applemail_record_by_email(
    *,
    email: str,
    pool_file: str | None = None,
    pool_dir: str | None = None,
) -> tuple[Path, dict[str, Any]]:
    path = resolve_applemail_pool_path(pool_file=pool_file, pool_dir=pool_dir)
    target_email = str(email or "").strip().lower()
    if not target_email:
        raise RuntimeError("指定邮箱为空")
    with _pool_transaction_lock(path):
        content = path.read_text(encoding="utf-8", errors="ignore")
        records = parse_applemail_pool_content(content, allow_empty=True)
        record = next(
            (
                item
                for item in records
                if str(item.get("email") or "").strip().lower() == target_email
                and str(item.get("pool_state") or "").strip().lower()
                == _POOL_STATE_USED
            ),
            None,
        )
        if record is None:
            raise RuntimeError(f"指定邮箱不在已使用凭据池中: {email}")
        return path, dict(record)


def requeue_applemail_record(
    *,
    pool_file: str | None = None,
    pool_dir: str | None = None,
    claim_id: str = "",
    email: str = "",
) -> bool:
    path = resolve_applemail_pool_path(pool_file=pool_file, pool_dir=pool_dir)
    with _pool_transaction_lock(path):
        content = path.read_text(encoding="utf-8", errors="ignore")
        records = parse_applemail_pool_content(content, allow_empty=True)
        record = _find_claimed_record(
            records,
            claim_id=claim_id,
            email=email,
        )
        if record is None:
            return False
        record["enabled"] = True
        record["pool_state"] = _POOL_STATE_AVAILABLE
        record.pop("pool_claim_id", None)
        _atomic_write_pool_records(path, records)
        return True


def mark_applemail_record_used(
    *,
    pool_file: str | None = None,
    pool_dir: str | None = None,
    claim_id: str = "",
    email: str = "",
) -> bool:
    path = resolve_applemail_pool_path(pool_file=pool_file, pool_dir=pool_dir)
    with _pool_transaction_lock(path):
        content = path.read_text(encoding="utf-8", errors="ignore")
        records = parse_applemail_pool_content(content, allow_empty=True)
        record = _find_claimed_record(
            records,
            claim_id=claim_id,
            email=email,
        )
        if record is None:
            return False
        record["enabled"] = False
        record["pool_state"] = _POOL_STATE_USED
        record.pop("pool_claim_id", None)
        _atomic_write_pool_records(path, records)
        return True


def commit_applemail_password_reset(
    *,
    claim_id: str = "",
    email: str = "",
    new_password: str,
    pool_file: str | None = None,
    pool_dir: str | None = None,
) -> bool:
    password = str(new_password or "")
    if len(password) < 12:
        raise ValueError("新密码至少需要 12 个字符")
    path = resolve_applemail_pool_path(pool_file=pool_file, pool_dir=pool_dir)
    with _pool_transaction_lock(path):
        content = path.read_text(encoding="utf-8", errors="ignore")
        records = parse_applemail_pool_content(content, allow_empty=True)
        record = _find_claimed_record(records, claim_id=claim_id)
        if record is None and str(email or "").strip():
            target_email = str(email or "").strip().lower()
            record = next(
                (
                    item
                    for item in records
                    if str(item.get("email") or "").strip().lower()
                    == target_email
                    and str(item.get("pool_state") or "").strip().lower()
                    in {_POOL_STATE_CLAIMED, _POOL_STATE_USED}
                ),
                None,
            )
        if record is None:
            return False
        if str(record.get("account_type") or "").strip() not in {
            "chatgpt_password_url_otp",
            "chatgpt_password_reset_url_mail",
        }:
            return False
        record["password"] = password
        record["password_reset_required"] = False
        _atomic_write_pool_records(path, records)
        return True


def mark_applemail_records_used_by_emails(
    *,
    emails,
    pool_file: str | None = None,
    pool_dir: str | None = None,
) -> int:
    """Persistently archive imported credentials without deleting their secrets."""
    targets = {
        str(email or "").strip().lower()
        for email in (emails or [])
        if str(email or "").strip()
    }
    if not targets:
        return 0

    path = resolve_applemail_pool_path(pool_file=pool_file, pool_dir=pool_dir)
    with _pool_transaction_lock(path):
        content = path.read_text(encoding="utf-8", errors="ignore")
        records = parse_applemail_pool_content(content, allow_empty=True)
        matched = 0
        changed = False
        for record in records:
            record_email = str(record.get("email") or "").strip().lower()
            if record_email not in targets:
                continue
            matched += 1
            if (
                str(record.get("pool_state") or "").strip().lower()
                != _POOL_STATE_USED
                or _normalize_enabled(record.get("enabled", True))
                or "pool_claim_id" in record
            ):
                record["enabled"] = False
                record["pool_state"] = _POOL_STATE_USED
                record.pop("pool_claim_id", None)
                changed = True
        if changed:
            _atomic_write_pool_records(path, records)
        return matched


def delete_applemail_pool_records(
    *,
    items,
    pool_file: str | None = None,
    pool_dir: str | None = None,
) -> tuple[Path, list[str], list[str]]:
    """Delete exact pool rows in the same transaction as concurrent claims."""
    pending = [
        (
            str(
                item.get("email")
                if isinstance(item, dict)
                else getattr(item, "email", "")
            ).strip().lower(),
            str(
                item.get("mailbox", "")
                if isinstance(item, dict)
                else getattr(item, "mailbox", "")
            ).strip().lower(),
        )
        for item in (items or [])
    ]
    pending = [(email, mailbox) for email, mailbox in pending if email]
    path = resolve_applemail_pool_path(pool_file=pool_file, pool_dir=pool_dir)
    with _pool_transaction_lock(path):
        records = parse_applemail_pool_content(
            path.read_text(encoding="utf-8", errors="ignore"),
            allow_empty=True,
        )
        deleted: list[str] = []
        remaining: list[dict[str, Any]] = []
        for record in records:
            record_email = str(record.get("email") or "").strip().lower()
            record_mailbox = str(record.get("mailbox") or "INBOX").strip().lower()
            match_index = next((
                index
                for index, (email, mailbox) in enumerate(pending)
                if email == record_email and (not mailbox or mailbox == record_mailbox)
            ), -1)
            if match_index < 0:
                remaining.append(record)
                continue
            deleted_email, _mailbox = pending.pop(match_index)
            deleted.append(deleted_email)
        errors = [f"未找到要删除的小苹果邮箱: {email}" for email, _ in pending]
        if deleted:
            _atomic_write_pool_records(path, remaining)
        return path, deleted, errors


def save_applemail_pool_json(
    content: str,
    *,
    pool_dir: str | None = None,
    filename: str | None = None,
) -> dict[str, Any]:
    records = parse_applemail_pool_content(content, allow_empty=True)
    return save_applemail_pool_records(
        records,
        pool_dir=pool_dir,
        filename=filename,
    )


def save_applemail_pool_records(
    records: list[dict[str, Any]],
    *,
    pool_dir: str | None = None,
    filename: str | None = None,
) -> dict[str, Any]:
    output_dir = _normalize_pool_dir(pool_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    safe_name = _normalize_filename(filename)
    path = output_dir / safe_name
    with _pool_transaction_lock(path):
        output_records = [dict(record) for record in records]
        if output_records and path.exists():
            existing_records = parse_applemail_pool_content(
                path.read_text(encoding="utf-8", errors="ignore"),
                allow_empty=True,
            )
            existing_by_email: dict[str, list[dict[str, Any]]] = {}
            for existing in existing_records:
                key = str(existing.get("email") or "").strip().lower()
                existing_by_email.setdefault(key, []).append(existing)

            matched_existing_ids: set[int] = set()
            for record in output_records:
                key = str(record.get("email") or "").strip().lower()
                candidates = existing_by_email.get(key, [])
                existing = next(
                    (item for item in candidates if id(item) not in matched_existing_ids),
                    None,
                )
                if existing is None:
                    continue
                matched_existing_ids.add(id(existing))
                state = str(existing.get("pool_state") or "").strip().lower()
                if state in {_POOL_STATE_CLAIMED, _POOL_STATE_USED}:
                    record["pool_state"] = state
                    record["enabled"] = False
                    if state == _POOL_STATE_CLAIMED:
                        claim_id = str(existing.get("pool_claim_id") or "").strip()
                        if claim_id:
                            record["pool_claim_id"] = claim_id
                    else:
                        record.pop("pool_claim_id", None)
                    if (
                        str(existing.get("account_type") or "").strip()
                        == "chatgpt_password_reset_url_mail"
                        and existing.get("password_reset_required") is False
                    ):
                        record["password"] = str(existing.get("password") or "")
                        record["password_reset_required"] = False

            represented_emails = {
                str(record.get("email") or "").strip().lower()
                for record in output_records
            }
            output_records.extend(
                dict(existing)
                for existing in existing_records
                if id(existing) not in matched_existing_ids
                and str(existing.get("email") or "").strip().lower()
                not in represented_emails
                and str(existing.get("pool_state") or "").strip().lower()
                in {_POOL_STATE_CLAIMED, _POOL_STATE_USED}
            )
        _atomic_write_pool_records(path, output_records)
    return {
        "filename": safe_name,
        "path": str(path),
        "count": len(output_records),
    }
