"""Identity and API projection helpers for Codex2API-managed accounts."""

from __future__ import annotations

from typing import Any, Mapping


REMOTE_SCHEDULABLE_STATUSES = frozenset({"active", "ready", "rate_limited"})


def _text(value: Any) -> str:
    return str(value or "").strip()


def remote_account_id(row: Mapping[str, Any]) -> int:
    try:
        value = int(row.get("remote_id") or row.get("id") or 0)
    except (TypeError, ValueError):
        return 0
    return value if value > 0 else 0


def remote_identity_id(target_id: int, remote_id: int) -> str:
    """Return a stable identity key scoped to one Codex2API target."""

    target = int(target_id or 0)
    remote = int(remote_id or 0)
    if target <= 0 or remote <= 0:
        raise ValueError("target_id and remote_id must be positive")
    return f"codex2api:{target}:{remote}"


def remote_virtual_account_id(target_id: int, remote_id: int) -> int:
    """Encode a target/remote pair as a reversible negative list-row ID."""

    target = int(target_id or 0)
    remote = int(remote_id or 0)
    if target <= 0 or remote <= 0 or remote >= 2**32:
        raise ValueError("target_id and remote_id are outside the virtual ID range")
    return -((target << 32) | remote)


def decode_remote_virtual_account_id(value: Any) -> tuple[int, int] | None:
    try:
        encoded = int(value)
    except (TypeError, ValueError):
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
        bool(row.get("enabled", True))
        and not bool(row.get("locked", False))
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
        "remote_enabled": bool(row.get("enabled", True)),
        "remote_locked": bool(row.get("locked", False)),
        "assignment": dict(assignment) if assignment is not None else None,
        "binding": dict(binding) if binding is not None else None,
    }


__all__ = [
    "REMOTE_SCHEDULABLE_STATUSES",
    "decode_remote_virtual_account_id",
    "remote_account_email",
    "remote_account_id",
    "remote_account_is_schedulable",
    "remote_account_payload",
    "remote_identity_id",
    "remote_virtual_account_id",
]
