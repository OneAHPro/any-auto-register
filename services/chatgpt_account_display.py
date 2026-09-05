"""Build a safe, read-only display projection for ChatGPT account cards.

The account manager stores credentials and a long-lived quota ledger locally,
while Codex2API exposes the freshest non-mutating account summary.  This module
joins those two sources without writing either system.  A remote row is only
used when it can be matched by a unique email or a unique ChatGPT account ID;
ambiguous matches are deliberately left unresolved.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping


_AUTH_CLAIM = "https://api.openai.com/auth"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _plan_text(value: Any) -> str:
    """Return a usable normalized plan, discarding probe placeholders."""

    normalized = _text(value).lower()
    if normalized in {"", "unknown", "none", "null", "n/a", "unread", "not_available"}:
        return ""
    return normalized


def _finite_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed == parsed and abs(parsed) != float("inf") else None


def _decode_jwt_payload(token: Any) -> dict[str, Any]:
    try:
        parts = _text(token).split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)
        decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
        parsed = json.loads(decoded)
        return parsed if isinstance(parsed, dict) else {}
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        return {}


def _account_extra(account: Any) -> dict[str, Any]:
    getter = getattr(account, "get_extra", None)
    if callable(getter):
        try:
            value = getter()
            if isinstance(value, dict):
                return value
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    raw = getattr(account, "extra_json", "")
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        value = {}
    return value if isinstance(value, dict) else {}


def _token_claims(account: Any) -> dict[str, str]:
    extra = _account_extra(account)
    payload = _decode_jwt_payload(extra.get("access_token") or getattr(account, "token", ""))
    auth = payload.get(_AUTH_CLAIM)
    if not isinstance(auth, dict):
        auth = {}
    return {
        "plan_type": _plan_text(
            auth.get("chatgpt_plan_type")
            or auth.get("plan_type")
            or payload.get("chatgpt_plan_type")
            or payload.get("plan_type")
        ),
        "chatgpt_account_id": _text(
            auth.get("chatgpt_account_id")
            or auth.get("account_id")
            or payload.get("chatgpt_account_id")
            or payload.get("account_id")
        ),
        "chatgpt_user_id": _text(
            auth.get("chatgpt_user_id")
            or auth.get("user_id")
            or payload.get("chatgpt_user_id")
            or payload.get("user_id")
        ),
    }


def _local_probe_values(account: Any) -> dict[str, str]:
    extra = _account_extra(account)
    local = extra.get("chatgpt_local")
    if not isinstance(local, dict):
        return {"plan_type": "", "subscription_active_until": "", "account_id": ""}
    subscription = local.get("subscription")
    codex = local.get("codex")
    subscription = subscription if isinstance(subscription, dict) else {}
    codex = codex if isinstance(codex, dict) else {}
    return {
        "plan_type": _plan_text(subscription.get("plan")),
        "subscription_active_until": _text(
            subscription.get("subscription_active_until")
            or subscription.get("active_until")
        ),
        "account_id": _text(
            subscription.get("chatgpt_account_id")
            or codex.get("chatgpt_account_id")
        ),
    }


def _account_identity_values(account: Any, claims: Mapping[str, str]) -> tuple[set[str], set[str]]:
    extra = _account_extra(account)
    local = extra.get("chatgpt_local") if isinstance(extra.get("chatgpt_local"), dict) else {}
    subscription = local.get("subscription") if isinstance(local.get("subscription"), dict) else {}
    codex = local.get("codex") if isinstance(local.get("codex"), dict) else {}
    emails = {
        _text(getattr(account, "email", "")).lower(),
    }
    profile = local.get("profile") if isinstance(local.get("profile"), dict) else {}
    if _text(profile.get("email")):
        emails.add(_text(profile.get("email")).lower())
    ids = {
        _text(getattr(account, "user_id", "")),
        _text(extra.get("account_id")),
        _text(extra.get("workspace_id")),
        _text(claims.get("chatgpt_account_id")),
        _text(subscription.get("chatgpt_account_id")),
        _text(codex.get("chatgpt_account_id")),
    }
    return {value for value in emails if value}, {value for value in ids if value}


def _remote_identity_values(row: Mapping[str, Any]) -> tuple[str, set[str]]:
    # `name` is often a generated placeholder for imported rows.  It is not a
    # trustworthy account email when the upstream row omitted `email`.
    email = "" if row.get("_remote_email_missing") or not _text(row.get("email")) else _text(row.get("email")).lower()
    ids = {
        _text(row.get("chatgpt_account_id")),
        _text(row.get("effective_workspace_id")),
        _text(row.get("account_id")),
    }
    return email, {value for value in ids if value}


def _match_remote_row(
    account: Any,
    rows: Iterable[Mapping[str, Any]],
    claims: Mapping[str, str],
) -> tuple[Mapping[str, Any] | None, str]:
    emails, ids = _account_identity_values(account, claims)
    remote_rows = [row for row in rows if isinstance(row, Mapping)]
    exact_email = [row for row in remote_rows if _remote_identity_values(row)[0] in emails]
    if len(exact_email) == 1:
        return exact_email[0], "email"
    if len(exact_email) > 1:
        return None, "ambiguous_email"

    id_matches = [
        row
        for row in remote_rows
        if ids.intersection(_remote_identity_values(row)[1])
    ]
    if len(id_matches) == 1:
        return id_matches[0], "account_id"
    if len(id_matches) > 1:
        return None, "ambiguous_account_id"
    return None, "none"


def _remote_quota(row: Mapping[str, Any]) -> dict[str, Any] | None:
    usage_percent = _finite_number(row.get("usage_percent_7d"))
    if usage_percent is None or usage_percent < 0:
        return None
    usage_percent = min(100.0, usage_percent)
    billed = _finite_number(row.get("billed_7d"))
    if billed is None:
        billed = _finite_number(row.get("display_billed_usd"))
    request_count = _finite_number(row.get("usage_7d_requests"))
    if request_count is None:
        detail = row.get("usage_7d_detail")
        if isinstance(detail, Mapping):
            request_count = _finite_number(detail.get("requests"))
    remote_id = row.get("remote_id") or row.get("id")
    try:
        remote_id = int(remote_id) if remote_id is not None else None
    except (TypeError, ValueError):
        remote_id = None
    return {
        "window": "7d",
        "usage_percent": usage_percent,
        "billed_usd": billed,
        "reset_at": _text(row.get("reset_7d_at")) or None,
        "captured_at": _text(
            row.get("quota_7d_updated_at")
            or row.get("updated_at")
        ) or None,
        "request_count": int(request_count) if request_count is not None else None,
        "remote_status": _text(row.get("remote_status") or row.get("status")).lower() or None,
        "remote_id": remote_id,
        "source": "codex2api_live",
    }


def _now_iso(now: datetime | None = None) -> str:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def build_chatgpt_account_display(
    account: Any,
    remote_rows: Iterable[Mapping[str, Any]] | None = None,
    *,
    live_available: bool = True,
    live_error: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return non-secret display data for one account.

    ``live_available`` distinguishes an empty, successfully-read remote list
    from a transport/configuration failure.  The distinction lets the UI avoid
    presenting an old local quota snapshot as if it were current.
    """

    claims = _token_claims(account)
    local = _local_probe_values(account)
    rows = list(remote_rows or [])
    remote, match = _match_remote_row(account, rows, claims) if live_available else (None, "none")

    if remote is not None:
        remote_plan = _plan_text(remote.get("plan_type"))
        plan_type = remote_plan or claims["plan_type"] or local["plan_type"]
        plan_source = "codex2api_live" if remote_plan else (
            "access_token_claim" if claims["plan_type"] else "local_probe"
        )
        active_until = _text(remote.get("subscription_expires_at")) or local["subscription_active_until"]
        quota = _remote_quota(remote)
        quota_status = "live" if quota is not None else "unavailable"
        remote_status = _text(remote.get("remote_status") or remote.get("status")).lower() or None
        remote_id = remote.get("remote_id") or remote.get("id")
    else:
        plan_type = claims["plan_type"] or local["plan_type"]
        plan_source = "access_token_claim" if claims["plan_type"] else (
            "local_probe" if local["plan_type"] else "none"
        )
        active_until = local["subscription_active_until"]
        quota = None
        quota_status = "error" if live_error else ("not_found" if live_available else "not_configured")
        remote_status = None
        remote_id = None

    return {
        "plan_type": plan_type or None,
        "plan_source": plan_source,
        "chatgpt_account_id": claims["chatgpt_account_id"] or local["account_id"] or None,
        "subscription_active_until": active_until or None,
        "workspace_id": (
            _text(remote.get("effective_workspace_id"))
            if remote is not None
            else _text(_account_extra(account).get("workspace_id"))
        ) or None,
        "workspace_name": _text(remote.get("workspace_name")) if remote is not None else None,
        "quota": quota,
        "quota_status": quota_status,
        "remote_status": remote_status,
        "remote_enabled": bool(remote.get("enabled", True)) if remote is not None else None,
        "remote_locked": bool(remote.get("locked", False)) if remote is not None else None,
        "remote_id": remote_id,
        "match": match if remote is not None else None,
        "fetched_at": _now_iso(now),
        "live_updated_at": (
            _text(remote.get("updated_at") or remote.get("quota_7d_updated_at"))
            if remote is not None
            else None
        ),
    }


def build_chatgpt_account_display_map(
    accounts: Iterable[Any],
    remote_rows: Iterable[Mapping[str, Any]] | None = None,
    *,
    live_available: bool = True,
    live_error: str = "",
    now: datetime | None = None,
) -> dict[int | None, dict[str, Any]]:
    rows = list(remote_rows or [])
    return {
        getattr(account, "id", None): build_chatgpt_account_display(
            account,
            rows,
            live_available=live_available,
            live_error=live_error,
            now=now,
        )
        for account in accounts
    }


__all__ = [
    "build_chatgpt_account_display",
    "build_chatgpt_account_display_map",
]
