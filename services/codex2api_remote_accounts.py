"""Identity and API projection helpers for Codex2API-managed accounts."""

from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping


REMOTE_SCHEDULABLE_STATUSES = frozenset({"active", "ready", "rate_limited"})


def _text(value: Any) -> str:
    return str(value or "").strip()


def positive_remote_id(value: Any) -> int:
    """Coerce one provider ID without lossy numeric conversions."""

    if value in (None, "") or isinstance(value, bool):
        return 0
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            return 0
    if isinstance(value, Decimal):
        if not value.is_finite() or value != value.to_integral_value():
            return 0
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError, InvalidOperation):
        return 0
    return parsed if parsed > 0 else 0


def remote_bool(value: Any, default: bool = False) -> bool:
    """Parse provider boolean fields without treating ``"false"`` as true."""

    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float, Decimal)):
        try:
            return value != 0
        except Exception:
            return bool(default)
    normalized = _text(value).lower()
    if normalized in {"1", "true", "yes", "on", "enabled"}:
        return True
    if normalized in {"0", "false", "no", "off", "disabled"}:
        return False
    return bool(default)


def remote_account_id(row: Mapping[str, Any]) -> int:
    # Treat an explicitly supplied invalid primary field as malformed instead
    # of silently falling through to a second alias.  A missing/NULL
    # ``remote_id`` still permits the normal ``id`` field used by the HTTP API.
    for key in ("remote_id", "id"):
        if key not in row or row.get(key) in (None, ""):
            continue
        return positive_remote_id(row.get(key))
    return 0


def remote_identity_id(target_id: int, remote_id: int) -> str:
    """Return a stable identity key scoped to one Codex2API target."""

    target = positive_remote_id(target_id)
    remote = positive_remote_id(remote_id)
    if target <= 0 or remote <= 0:
        raise ValueError("target_id and remote_id must be positive")
    return f"codex2api:{target}:{remote}"


def remote_virtual_account_id(target_id: int, remote_id: int) -> int:
    """Encode a target/remote pair as a reversible negative list-row ID."""

    target = positive_remote_id(target_id)
    remote = positive_remote_id(remote_id)
    if target <= 0 or remote <= 0 or remote >= 2**32:
        raise ValueError("target_id and remote_id are outside the virtual ID range")
    return -((target << 32) | remote)


def decode_remote_virtual_account_id(value: Any) -> tuple[int, int] | None:
    if isinstance(value, bool) or (
        isinstance(value, float) and not value.is_integer()
    ):
        return None
    try:
        encoded = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if encoded >= 0:
        return None
    raw = -encoded
    target = raw >> 32
    remote = raw & 0xFFFFFFFF
    if target <= 0 or remote <= 0:
        return None
    return target, remote


def remote_account_email(row: Mapping[str, Any]) -> str:
    if row.get("_remote_email_missing"):
        return ""
    return _text(row.get("email") or row.get("name"))


def remote_account_is_schedulable(row: Mapping[str, Any]) -> bool:
    status = _text(row.get("remote_status") or row.get("status")).lower()
    return (
        remote_bool(row.get("enabled"), True)
        and not remote_bool(row.get("locked"), False)
        and status in REMOTE_SCHEDULABLE_STATUSES
    )


def remote_account_payload(
    row: Mapping[str, Any],
    *,
    target_id: int,
    assignment: Mapping[str, Any] | None = None,
    binding: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a credential-free account-list row for a remote-only account."""

    remote_id = remote_account_id(row)
    if remote_id <= 0:
        raise ValueError("remote account row is missing a positive ID")
    email = remote_account_email(row)
    account_id = _text(
        row.get("chatgpt_account_id")
        or row.get("effective_workspace_id")
        or row.get("account_id")
    )
    chatgpt_account_id = _text(
        row.get("chatgpt_account_id") or row.get("account_id") or row.get("user_id")
    )
    workspace_id = _text(
        row.get("workspace_id") or row.get("effective_workspace_id")
    )
    virtual_id = remote_virtual_account_id(target_id, remote_id)
    identity_id = remote_identity_id(target_id, remote_id)
    status = _text(row.get("status") or row.get("remote_status")).lower()
    created_at = row.get("created_at") or row.get("updated_at")
    updated_at = row.get("updated_at") or row.get("codex_usage_updated_at")
    return {
        "id": virtual_id,
        "platform": "chatgpt",
        "email": email or _text(row.get("name")) or f"远端账号 #{remote_id}",
        "user_id": account_id,
        "chatgpt_account_id": chatgpt_account_id,
        "workspace_id": workspace_id,
        "effective_workspace_id": workspace_id,
        "region": "",
        "status": "registered",
        "cashier_url": "",
        "created_at": created_at,
        "updated_at": updated_at,
        "extra_json": "{}",
        "identity_id": identity_id,
        "account_source": "codex2api",
        "remote_only": True,
        "remote_id": remote_id,
        "remote_target_id": int(target_id),
        "remote_status": status,
        "remote_enabled": remote_bool(row.get("enabled"), True),
        "remote_locked": remote_bool(row.get("locked"), False),
        "assignment": dict(assignment) if assignment is not None else None,
        "binding": dict(binding) if binding is not None else None,
    }


__all__ = [
    "REMOTE_SCHEDULABLE_STATUSES",
    "decode_remote_virtual_account_id",
    "positive_remote_id",
    "remote_bool",
    "remote_account_email",
    "remote_account_id",
    "remote_account_is_schedulable",
    "remote_account_payload",
    "remote_identity_id",
    "remote_virtual_account_id",
]
