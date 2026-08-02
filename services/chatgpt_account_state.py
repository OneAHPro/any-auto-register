"""ChatGPT 账号状态判定辅助逻辑。"""

from __future__ import annotations

import re
import json
from typing import Any

from core.task_runtime import TaskInterruption


INVALID_ACCOUNT_STATUS = "invalid"
_DEACTIVATION_CODE_PATTERN = re.compile(
    r'''(?ix)
    ["']?(?:error[._ -]?)?code["']?
    \s*[:=]\s*["']?
    (?:account_deactivated|account_deleted)\b
    ''',
)


class ChatGPTAccountDeactivatedError(TaskInterruption):
    """The authentication service explicitly says the account no longer exists."""

    def __init__(self, message: str = "账号已被删除或停用") -> None:
        super().__init__(str(message or "账号已被删除或停用").strip())


def _lower_text(value: Any) -> str:
    return str(value or "").strip().lower()


def chatgpt_account_refresh_token(account: Any) -> str:
    """Return a saved ChatGPT refresh token without trusting stored JSON."""
    extra: Any = getattr(account, "extra", None)
    get_extra = getattr(account, "get_extra", None)
    if not isinstance(extra, dict) and callable(get_extra):
        try:
            extra = get_extra()
        except (TypeError, ValueError, json.JSONDecodeError):
            extra = None
    if not isinstance(extra, dict):
        raw_extra = getattr(account, "extra_json", "")
        try:
            extra = json.loads(raw_extra or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            extra = {}
    if not isinstance(extra, dict):
        return ""
    for key in ("refresh_token", "refreshToken"):
        token = str(extra.get(key) or "").strip()
        if token:
            return token
    return ""


def account_is_visible_in_default_list(account: Any) -> bool:
    """Hide incomplete ChatGPT rows while preserving every other platform."""
    if _lower_text(getattr(account, "platform", "")) != "chatgpt":
        return True
    return bool(chatgpt_account_refresh_token(account))


def is_account_deactivated_message(error_code: Any = "", message: Any = "") -> bool:
    code = _lower_text(error_code)
    text = _lower_text(message)
    if code in {"account_deactivated", "account_deleted"}:
        return True
    if _DEACTIVATION_CODE_PATTERN.search(text):
        return True
    markers = (
        "account has been deleted or deactivated",
        "you do not have an account because it has been deleted or deactivated",
        "your account was deleted or deactivated",
        "你没有账号，因为它已被删除或停用",
        "您没有账号，因为它已被删除或停用",
        "账号已被删除或停用",
        "账号已被停用或删除",
        "帐号已被删除或停用",
        "帐号已被停用或删除",
        "账户已被删除或停用",
        "账户已被停用或删除",
        "帳號已被刪除或停用",
        "帳戶已被刪除或停用",
    )
    return any(marker in text for marker in markers)


def classify_local_probe_state(probe: dict[str, Any] | None) -> str:
    if not isinstance(probe, dict):
        return ""

    auth = probe.get("auth") if isinstance(probe.get("auth"), dict) else {}
    codex = probe.get("codex") if isinstance(probe.get("codex"), dict) else {}

    auth_state = _lower_text(auth.get("state"))
    auth_status = int(auth.get("http_status") or 0)
    auth_error_code = auth.get("error_code")
    auth_message = auth.get("message")

    if auth_status == 401 or auth_state in {"access_token_invalidated", "unauthorized"}:
        return "auth_401"
    if is_account_deactivated_message(auth_error_code, auth_message):
        return "auth_deactivated"
    if auth_status == 403 and auth_state in {"account_deactivated", "banned_like"}:
        return "auth_403"

    codex_state = _lower_text(codex.get("state"))
    codex_status = int(codex.get("http_status") or 0)
    codex_error_code = codex.get("error_code")
    codex_message = codex.get("message")

    if codex_status == 401 or codex_state in {"access_token_invalidated", "unauthorized"}:
        return "codex_401"
    if is_account_deactivated_message(codex_error_code, codex_message):
        return "codex_deactivated"
    if codex_status == 403 and codex_state == "account_deactivated":
        return "codex_403"

    return ""


def classify_remote_sync_state(sync: dict[str, Any] | None) -> str:
    if not isinstance(sync, dict):
        return ""

    remote_state = _lower_text(sync.get("remote_state"))
    status_code = int(sync.get("last_probe_status_code") or 0)
    error_code = sync.get("last_probe_error_code")
    message = sync.get("last_probe_message") or sync.get("status_message") or sync.get("message")

    if status_code == 401 or remote_state in {"access_token_invalidated", "unauthorized"}:
        return "remote_401"
    if is_account_deactivated_message(error_code, message):
        return "remote_deactivated"
    if status_code == 403 and remote_state in {"account_deactivated", "banned_like"}:
        return "remote_403"

    return ""


def apply_chatgpt_status_policy(
    account: Any,
    *,
    local_probe: dict[str, Any] | None = None,
    remote_sync: dict[str, Any] | None = None,
) -> str:
    reason = classify_local_probe_state(local_probe) or classify_remote_sync_state(remote_sync)
    if reason:
        setattr(account, "status", INVALID_ACCOUNT_STATUS)
    return reason
