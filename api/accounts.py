from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse, StreamingResponse
from sqlmodel import Session, select, func
from sqlalchemy import or_
from pydantic import BaseModel, field_validator
from core.db import (
    AccountAssignmentModel,
    AccountModel,
    AccountPoolModel,
    AccountQuotaSnapshotModel,
    AccountTargetBindingModel,
    CodexInventorySnapshotModel,
    Codex2APITargetModel,
    get_session,
)
from core.mail_import_delimiters import split_mail_import_fields
from core.applemail_pool import _looks_like_mfa_secret, _normalize_mfa_secret
from services.chatgpt_account_state import account_is_visible_in_default_list
from services.chatgpt_account_removal import remove_account
from typing import Mapping, Optional
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import io, csv, json, logging
from types import SimpleNamespace
from urllib.parse import urlsplit, urlunsplit
from services.codex2api_remote_accounts import (
    remote_account_email,
    remote_account_id,
    remote_account_payload,
    remote_identity_id,
    remote_virtual_account_id,
)

_CHATGPT_DIRECT_PASSWORD_DOMAINS = {"icloud.com", "me.com", "mac.com"}

logger = logging.getLogger(__name__)

_ACCOUNT_EXTRA_SECRET_KEYS = {
    "access_token",
    "accesstoken",
    "refresh_token",
    "refreshtoken",
    "id_token",
    "idtoken",
    "session_token",
    "sessiontoken",
    "cookies",
    "password",
    "totp_secret",
    "mfa_secret",
    "recovery_code",
    "mfa_recovery_code",
}


def _is_secret_key(key: object) -> bool:
    normalized = str(key or "").strip().lower().replace("-", "_")
    if normalized in _ACCOUNT_EXTRA_SECRET_KEYS:
        return True
    return any(
        marker in normalized
        for marker in (
            "refresh_token",
            "access_token",
            "session_token",
            "id_token",
            "cookie",
            "password",
            "totp",
            "mfa_secret",
            "recovery_code",
            "private_key",
            "admin_key",
            "api_key",
        )
    )


def _scrub_nested_extra(value: object, key: object = "") -> object:
    """Recursively remove credential-shaped fields from API projections."""

    if isinstance(value, dict):
        cleaned: dict[str, object] = {}
        for child_key, child_value in value.items():
            if _is_secret_key(child_key):
                continue
            cleaned[str(child_key)] = _scrub_nested_extra(child_value, child_key)
        return cleaned
    if isinstance(value, list):
        return [_scrub_nested_extra(item, key) for item in value]
    if isinstance(value, str) and "url" in str(key or "").lower():
        try:
            parsed = urlsplit(value)
            if parsed.query or parsed.fragment:
                # Keep the navigable endpoint/path while dropping query
                # parameters that commonly carry bearer or checkout secrets.
                return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
        except ValueError:
            pass
    return value

router = APIRouter(prefix="/accounts", tags=["accounts"])


def _safe_float(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _sanitize_account_extra_for_api(
    raw_extra: str,
    *,
    strip_credentials: bool = False,
) -> str:
    try:
        extra = json.loads(raw_extra or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        extra = {}
    if not isinstance(extra, dict):
        extra = {}

    mailbox_context = extra.get("mailbox_login_context")
    if isinstance(mailbox_context, dict):
        extra["mailbox_login_context"] = {
            "provider": str(mailbox_context.get("provider") or "").strip(),
            "email": str(mailbox_context.get("email") or "").strip(),
            "account_id": str(mailbox_context.get("account_id") or "").strip(),
            "configured": True,
        }

    resume_context = extra.get("oauth_resume_context")
    if isinstance(resume_context, dict):
        flow_state = resume_context.get("flow_state")
        page_type = (
            str(flow_state.get("page_type") or "").strip()
            if isinstance(flow_state, dict)
            else ""
        )
        version = int(resume_context.get("version") or 0)
        ready = bool(
            version == 2
            and str(resume_context.get("code_verifier") or "").strip()
            and str(resume_context.get("oauth_state") or "").strip()
            and page_type
        )
        extra["oauth_resume_context"] = {
            "version": version,
            "created_at": _safe_float(resume_context.get("created_at")),
            "expires_at": _safe_float(resume_context.get("expires_at")),
            "ready": ready,
            "flow_state": {"page_type": page_type},
        }

    browser_context = extra.get("oauth_browser_context")
    if isinstance(browser_context, dict):
        version = int(browser_context.get("version") or 0)
        extra["oauth_browser_context"] = {
            "version": version,
            "created_at": _safe_float(browser_context.get("created_at")),
            "expires_at": _safe_float(browser_context.get("expires_at")),
            "ready": bool(
                version == 1
                and isinstance(browser_context.get("cookies"), list)
                and browser_context.get("cookies")
            ),
        }

    if strip_credentials:
        extra = _scrub_nested_extra(extra)
    return json.dumps(extra, ensure_ascii=False)


def _empty_control_plane_summary(identity_id: str = "") -> dict[str, object]:
    return {
        "identity_id": identity_id,
        "assignment": None,
        "binding": None,
        "quota": {},
    }


_SUMMARY_AUTH_INVALID_STATES = {
    "invalid",
    "unauthorized",
    "auth_error",
    "invalid_token",
    "account_deactivated",
    "account_deleted",
    "access_token_invalidated",
    "token_invalidated",
    "auth_401",
    "auth_deactivated",
    "auth_403",
    "codex_401",
    "codex_deactivated",
    "codex_403",
    "remote_401",
    "remote_deactivated",
    "remote_403",
    "auth_failed",
}
_SUMMARY_ERROR_STATES = {
    "error",
    "probe_failed",
    "unknown",
    "disabled",
    "locked",
    "missing_access_token",
    "banned_like",
    "expired",
    "failed",
    "quarantined",
    "deleted",
    "remote_missing",
    "ambiguous",
    "deferred",
    "missing",
    "not_found",
    "payment_required",
    "quota_exhausted",
}
_SUMMARY_SCHEDULING_STATES = {
    "draining",
    "planned",
    "locking",
    "uploading",
    "target_disabled",
    "verifying",
    "assignment_committing",
    "source_cleaning",
    "target_enabling",
    "migrating",
    "pending",
}
_SUMMARY_ASSIGNMENT_STATES = {
    "active",
    "draining",
    "standby",
    *_SUMMARY_SCHEDULING_STATES,
}
_SUMMARY_RATE_LIMIT_STATES = {
    "rate_limited",
    "rate_limited_5h",
    "rate_limited_7d",
    "usage_exhausted",
    "usage_limited",
    "quota_paused",
}


def _summary_state(value: object) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def _summary_is_false(value: object) -> bool:
    if isinstance(value, bool):
        return not value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value == 0
    return _summary_state(value) in {"0", "false", "no", "off", "disabled"}


def _summary_is_true(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value != 0
    return _summary_state(value) in {"1", "true", "yes", "on", "locked"}


def _inventory_snapshot_is_stale(snapshot: object) -> bool:
    """Treat an inventory error marker as stale even when the flag is textual."""

    if not isinstance(snapshot, dict):
        return False
    return bool(
        _summary_is_true(snapshot.get("_inventory_stale"))
        or str(snapshot.get("_inventory_error") or "").strip()
    )


def _account_operational_summary(
    accounts: list[AccountModel],
    remote_items: list[dict[str, object]],
    live_display: dict[int | None, dict[str, object]],
    assignment_states: Mapping[str, Mapping[str, object]] | None = None,
) -> dict[str, int]:
    """Summarize every filtered account, independent of pagination."""

    result = {
        "total": 0,
        "normal": 0,
        "scheduling": 0,
        "rate_limited": 0,
        "rate_limited_5h": 0,
        "rate_limited_7d": 0,
        "abnormal": 0,
        "auth_invalid": 0,
        "errors": 0,
    }

    def add_item(item: object, display: dict[str, object] | None = None) -> None:
        display = display if isinstance(display, dict) else {}
        if isinstance(item, AccountModel):
            try:
                extra = item.get_extra()
            except Exception:
                extra = {}
            identity_id = str(item.identity_id or "").strip()
            assignment = (assignment_states or {}).get(identity_id) if identity_id else None
            if assignment is None and int(item.id or 0) > 0:
                assignment = (assignment_states or {}).get(f"account:{int(item.id)}")
            if assignment is None:
                assignment = extra.get("assignment") if isinstance(extra, dict) else None
            if not isinstance(assignment, dict):
                assignment = {}
            snapshot = extra.get("codex_remote_snapshot") if isinstance(extra, dict) else None
            snapshot = snapshot if isinstance(snapshot, dict) else {}
            raw_quota = (
                extra.get("quota")
                if isinstance(extra, dict) and isinstance(extra.get("quota"), dict)
                else snapshot.get("quota")
            )
            inventory_stale = _inventory_snapshot_is_stale(snapshot)
            persisted_status = _summary_state(item.status)
            status_value = _summary_state(
                display.get("remote_status")
                or (extra.get("remote_status") if isinstance(extra, dict) else "")
                or snapshot.get("remote_status")
                or snapshot.get("status")
                or persisted_status
                or ""
            )
            auth_state = _summary_state(
                (extra.get("chatgpt_local", {}).get("auth", {}).get("state", ""))
                if isinstance(extra, dict) and isinstance(extra.get("chatgpt_local"), dict)
                else ""
            )
            codex_state = _summary_state(
                (extra.get("chatgpt_local", {}).get("codex", {}).get("state", ""))
                if isinstance(extra, dict) and isinstance(extra.get("chatgpt_local"), dict)
                else ""
            )
            display_quota_status = _summary_state(display.get("quota_status"))
            assignment_state = _summary_state(assignment.get("state"))
            scheduling = bool(
                assignment_state in _SUMMARY_SCHEDULING_STATES
                or assignment.get("lease_owner")
                or status_value == "scheduling"
                or (isinstance(extra, dict) and extra.get("scheduling") is True)
            )
            remote_disabled = _summary_is_false(display.get("remote_enabled"))
            remote_locked = _summary_is_true(display.get("remote_locked"))
        else:
            item_dict = item if isinstance(item, dict) else {}
            extra = {}
            identity_id = str(item_dict.get("identity_id") or "").strip()
            assignment = (assignment_states or {}).get(identity_id) if identity_id else None
            local_account_id = int(item_dict.get("local_account_id") or 0)
            if assignment is None and local_account_id > 0:
                assignment = (assignment_states or {}).get(f"account:{local_account_id}")
            if assignment is None:
                assignment = item_dict.get("assignment")
            if not isinstance(assignment, dict):
                assignment = {}
            raw_quota = item_dict.get("quota")
            inventory_stale = _inventory_snapshot_is_stale(item_dict)
            persisted_status = _summary_state(
                item_dict.get("remote_status") or item_dict.get("status")
            )
            status_value = _summary_state(
                display.get("remote_status")
                or item_dict.get("remote_status")
                or item_dict.get("status")
                or ""
            )
            auth_state = ""
            codex_state = ""
            display_quota_status = _summary_state(display.get("quota_status"))
            assignment_state = _summary_state(assignment.get("state"))
            scheduling = bool(
                assignment_state in _SUMMARY_SCHEDULING_STATES
                or assignment.get("lease_owner")
                or status_value == "scheduling"
            )
            remote_disabled = (
                _summary_is_false(display.get("remote_enabled"))
                or _summary_is_false(item_dict.get("remote_enabled"))
            )
            remote_locked = (
                _summary_is_true(display.get("remote_locked"))
                or _summary_is_true(item_dict.get("remote_locked"))
            )

        result["total"] += 1
        auth_invalid = (
            status_value in _SUMMARY_AUTH_INVALID_STATES
            or persisted_status in _SUMMARY_AUTH_INVALID_STATES
            or auth_state in _SUMMARY_AUTH_INVALID_STATES
            or codex_state in _SUMMARY_AUTH_INVALID_STATES
        )
        abnormal = (
            auth_invalid
            or status_value in _SUMMARY_ERROR_STATES
            or persisted_status in _SUMMARY_ERROR_STATES
            or auth_state in _SUMMARY_ERROR_STATES
            or codex_state in _SUMMARY_ERROR_STATES
            or display_quota_status in {"error", "not_found"}
            or inventory_stale
            or remote_disabled
            or remote_locked
        )
        if abnormal:
            result["abnormal"] += 1
            result["auth_invalid" if auth_invalid else "errors"] += 1
            return
        if status_value in _SUMMARY_RATE_LIMIT_STATES:
            result["rate_limited"] += 1
            quota = display.get("quota") if isinstance(display.get("quota"), dict) else raw_quota
            window = _summary_state((quota or {}).get("window")) if isinstance(quota, dict) else ""
            if status_value.endswith("_5h"):
                window = "5h"
            elif status_value.endswith("_7d"):
                window = "7d"
            if not window and isinstance(raw_quota, dict):
                if isinstance(raw_quota.get("5h"), dict) or "5h" in raw_quota:
                    window = "5h"
                elif isinstance(raw_quota.get("7d"), dict) or "7d" in raw_quota:
                    window = "7d"
            result["rate_limited_5h" if window == "5h" else "rate_limited_7d"] += 1
            return
        if scheduling:
            result["scheduling"] += 1
            return
        result["normal"] += 1

    for account in accounts:
        add_item(account, live_display.get(account.id))
    for item in remote_items:
        display = item.get("chatgpt_display") if isinstance(item.get("chatgpt_display"), dict) else {}
        add_item(item, display)
    return result


def _account_operational_bucket(
    item: object,
    display: dict[str, object] | None = None,
    assignment_states: Mapping[str, Mapping[str, object]] | None = None,
) -> str:
    """Return the mutually-exclusive bucket used by summary-card filters."""

    display = display if isinstance(display, dict) else {}
    if isinstance(item, AccountModel):
        one = _account_operational_summary(
            [item],
            [],
            {item.id: display},
            assignment_states,
        )
    elif isinstance(item, dict):
        one = _account_operational_summary(
            [],
            [item],
            {None: display},
            assignment_states,
        )
    else:
        return "errors"
    if one["auth_invalid"]:
        return "auth_invalid"
    if one["errors"]:
        return "errors"
    if one["rate_limited"]:
        return "rate_limited"
    if one["scheduling"]:
        return "scheduling"
    return "normal"


def _account_assignment_states(
    accounts: list[AccountModel],
    remote_items: list[dict[str, object]],
    session: Session,
) -> dict[str, dict[str, object]]:
    """Load the latest assignment state for every filtered identity."""

    identity_ids = {
        str(account.identity_id or "").strip()
        for account in accounts
        if str(account.identity_id or "").strip()
    }
    local_account_ids = {
        int(account.id)
        for account in accounts
        if account.id is not None and int(account.id) > 0
    }
    identity_ids.update(
        str(item.get("identity_id") or "").strip()
        for item in remote_items
        if isinstance(item, dict) and str(item.get("identity_id") or "").strip()
    )
    if not identity_ids and not local_account_ids:
        return {}

    predicates = []
    if identity_ids:
        predicates.append(AccountAssignmentModel.identity_id.in_(identity_ids))
    if local_account_ids:
        predicates.append(AccountAssignmentModel.local_account_id.in_(local_account_ids))
    assignments = session.exec(
        select(AccountAssignmentModel)
        .where(or_(*predicates))
        .where(
            AccountAssignmentModel.state.in_(
                _SUMMARY_ASSIGNMENT_STATES
            )
        )
        .order_by(AccountAssignmentModel.updated_at.desc())
    ).all()
    result: dict[str, dict[str, object]] = {}
    for assignment in assignments:
        identity_id = str(assignment.identity_id or "").strip()
        if not identity_id or identity_id in result:
            identity_id = ""
        assignment_data = {
            "state": str(assignment.state or "").strip().lower(),
            "lease_owner": str(assignment.lease_owner or "").strip(),
        }
        if identity_id:
            result.setdefault(identity_id, assignment_data)
        local_account_id = int(assignment.local_account_id or 0)
        if local_account_id > 0:
            result.setdefault(f"account:{local_account_id}", assignment_data)
    return result


def _snapshot_key_from_extra(extra: object) -> tuple[int, int] | None:
    if not isinstance(extra, dict):
        return None
    snapshot = extra.get("codex_remote_snapshot")
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    try:
        target_id = int(
            extra.get("remote_target_id")
            or snapshot.get("target_id")
            or snapshot.get("remote_target_id")
            or 0
        )
        remote_id = int(
            extra.get("remote_id")
            or snapshot.get("remote_id")
            or snapshot.get("id")
            or 0
        )
    except (TypeError, ValueError):
        return None
    if target_id <= 0 or remote_id <= 0:
        return None
    return target_id, remote_id


def _account_snapshot_key(account: AccountModel) -> tuple[int, int] | None:
    """Return the target/remote key for any account with a remote snapshot."""

    try:
        extra = account.get_extra()
    except Exception:
        extra = {}
    return _snapshot_key_from_extra(extra)


def _remote_snapshot_key(account: AccountModel) -> tuple[int, int] | None:
    """Return the target/remote key for a credential-free remote row."""

    try:
        extra = account.get_extra()
    except Exception:
        extra = {}
    if not isinstance(extra, dict) or not bool(extra.get("remote_only")):
        return None
    return _snapshot_key_from_extra(extra)


def _inventory_state(
    session: Session,
) -> tuple[dict[tuple[int, int], CodexInventorySnapshotModel], set[int]]:
    """Return target-scoped inventory rows and targets with a complete sync."""

    try:
        rows = session.exec(select(CodexInventorySnapshotModel)).all()
    except Exception:
        return {}, set()
    by_key: dict[tuple[int, int], CodexInventorySnapshotModel] = {}
    grouped: dict[int, list[CodexInventorySnapshotModel]] = {}
    for row in rows:
        try:
            key = (int(row.target_id), int(row.remote_id))
        except (TypeError, ValueError):
            continue
        if key[0] <= 0 or key[1] <= 0:
            continue
        by_key[key] = row
        grouped.setdefault(key[0], []).append(row)
    complete_targets = {
        target_id
        for target_id, target_rows in grouped.items()
        if target_rows and all(not str(row.error or "").strip() for row in target_rows)
    }
    return by_key, complete_targets


def _hide_missing_remote_only_accounts(
    accounts: list[AccountModel],
    session: Session,
    inventory_by_key: dict[tuple[int, int], CodexInventorySnapshotModel] | None = None,
    complete_targets: set[int] | None = None,
) -> list[AccountModel]:
    """Drop remote-only rows removed by the latest complete inventory sync.

    Local credential-backed rows stay visible even when their remote binding is
    temporarily absent.  A remote-only row has no local credential to fall
    back to, so a successful inventory snapshot is authoritative for it.
    """

    remote_accounts = [account for account in accounts if _remote_snapshot_key(account)]
    if not remote_accounts:
        return accounts
    if inventory_by_key is None or complete_targets is None:
        inventory_by_key, complete_targets = _inventory_state(session)
    if not inventory_by_key or not complete_targets:
        return accounts
    return [
        account
        for account in accounts
        if (key := _remote_snapshot_key(account)) is None
        or key[0] not in complete_targets
        or (
            (row := inventory_by_key.get(key)) is not None
            and not bool(row.missing)
        )
    ]


def _account_control_plane_summaries(
    accounts: list[AccountModel],
    session: Session,
) -> dict[str, dict[str, object]]:
    """Batch-load control-plane projections for one paginated account list."""

    identity_ids = {
        str(account.identity_id or "").strip()
        for account in accounts
        if str(account.identity_id or "").strip()
    }
    summaries = {
        identity_id: _empty_control_plane_summary(identity_id)
        for identity_id in identity_ids
    }
    if not identity_ids:
        return summaries

    assignments = session.exec(
        select(AccountAssignmentModel)
        .where(AccountAssignmentModel.identity_id.in_(identity_ids))
        .where(AccountAssignmentModel.state.in_(_SUMMARY_ASSIGNMENT_STATES))
        .order_by(AccountAssignmentModel.updated_at.desc())
    ).all()
    assignment_by_identity: dict[str, AccountAssignmentModel] = {}
    for assignment in assignments:
        assignment_by_identity.setdefault(str(assignment.identity_id), assignment)
    target_ids = {int(assignment.target_id) for assignment in assignments}
    pool_ids = {str(assignment.pool_id) for assignment in assignments}
    target_names = {
        int(target.id): target.name
        for target in session.exec(
            select(Codex2APITargetModel).where(Codex2APITargetModel.id.in_(target_ids))
        ).all()
        if target.id is not None
    } if target_ids else {}
    pool_names = {
        str(pool.id): pool.name
        for pool in session.exec(
            select(AccountPoolModel).where(AccountPoolModel.id.in_(pool_ids))
        ).all()
    } if pool_ids else {}

    bindings = session.exec(
        select(AccountTargetBindingModel).where(
            AccountTargetBindingModel.identity_id.in_(identity_ids)
        )
    ).all()
    binding_by_key = {
        (str(binding.identity_id), int(binding.target_id)): binding
        for binding in bindings
    }
    snapshots = session.exec(
        select(AccountQuotaSnapshotModel)
        .where(AccountQuotaSnapshotModel.identity_id.in_(identity_ids))
        .where(AccountQuotaSnapshotModel.window.in_(["5h", "7d", "monthly"]))
        .order_by(AccountQuotaSnapshotModel.captured_at.desc())
    ).all()
    snapshot_by_key: dict[tuple[str, str], AccountQuotaSnapshotModel] = {}
    for snapshot in snapshots:
        key = (str(snapshot.identity_id), str(snapshot.window))
        current = snapshot_by_key.get(key)
        assigned_target = assignment_by_identity.get(str(snapshot.identity_id))
        preferred_target_id = (
            int(assigned_target.target_id)
            if assigned_target is not None
            else None
        )
        if current is None or (
            preferred_target_id is not None
            and int(snapshot.target_id or 0) == preferred_target_id
            and int(current.target_id or 0) != preferred_target_id
        ):
            snapshot_by_key[key] = snapshot

    from services.quota_ledger import evaluate_snapshot

    for identity_id, summary in summaries.items():
        assignment = assignment_by_identity.get(identity_id)
        if assignment is not None:
            summary["assignment"] = {
                "pool_id": assignment.pool_id,
                "pool_name": pool_names.get(str(assignment.pool_id), ""),
                "target_id": int(assignment.target_id),
                "target_name": target_names.get(int(assignment.target_id), ""),
                "state": assignment.state,
                "lease_owner": assignment.lease_owner,
                "lease_reason": assignment.lease_reason,
                "lease_started_at": assignment.lease_started_at.isoformat()
                if assignment.lease_started_at
                else None,
                "lease_expires_at": assignment.lease_expires_at.isoformat()
                if assignment.lease_expires_at
                else None,
                "assignment_version": int(assignment.assignment_version or 0),
            }
            binding = binding_by_key.get((identity_id, int(assignment.target_id)))
            if binding is not None:
                summary["binding"] = {
                    "target_id": int(binding.target_id),
                    "remote_account_id": int(binding.remote_account_id or 0),
                    "sync_status": binding.sync_status,
                    "remote_status": binding.remote_status,
                    "enabled": bool(binding.enabled),
                    "last_sync_at": binding.last_sync_at.isoformat()
                    if binding.last_sync_at
                    else None,
                    "last_error": binding.last_error,
                }
        quota: dict[str, dict[str, object]] = {}
        for window in ("5h", "7d", "monthly"):
            snapshot = snapshot_by_key.get((identity_id, window))
            if snapshot is None:
                continue
            evaluated = evaluate_snapshot(snapshot)
            quota[window] = {
                "usage_percent": snapshot.usage_percent,
                "billed_usd": snapshot.billed_usd,
                "continuous_billed_usd": float(evaluated.continuous_billed_usd),
                "remaining_usd": float(evaluated.remaining_usd)
                if evaluated.remaining_usd is not None
                else None,
                "continuous_remaining_usd": float(evaluated.continuous_remaining_usd)
                if evaluated.continuous_remaining_usd is not None
                else None,
                "remaining_scope": evaluated.remaining_scope,
                "reset_at": evaluated.reset_at.isoformat()
                if evaluated.reset_at
                else None,
                "captured_at": evaluated.captured_at.isoformat(),
                "continuity_state": evaluated.continuity_state,
                "fresh": evaluated.fresh,
                "scheduler_eligible": evaluated.scheduler_eligible,
            }
        summary["quota"] = quota
    return summaries


def _account_control_plane_summary(account: AccountModel, session: Session) -> dict:
    identity_id = str(getattr(account, "identity_id", "") or "").strip()
    if not identity_id:
        return _empty_control_plane_summary()
    return _account_control_plane_summaries([account], session).get(
        identity_id,
        _empty_control_plane_summary(identity_id),
    )


def _account_for_response(
    account: AccountModel,
    session: Session | None = None,
    *,
    include_credentials: bool = True,
    control_plane_summary: dict[str, object] | None = None,
) -> dict:
    payload = account.model_dump()
    purchase_cost_cents = payload.pop("purchase_cost_cents", None)
    if not include_credentials:
        payload.pop("password", None)
        payload.pop("token", None)
        cashier_url = str(payload.get("cashier_url") or "")
        if cashier_url:
            payload["cashier_url"] = _scrub_nested_extra(cashier_url, "cashier_url")
    extra = json.loads(_sanitize_account_extra_for_api(
        str(payload.get("extra_json") or "{}"),
        strip_credentials=not include_credentials,
    ))
    # Keep the public JSON protocol while sourcing cost only from its own
    # column. Stale credential metadata must never restore a cleared cost.
    extra.pop("purchase_cost_cny", None)
    if purchase_cost_cents is not None:
        extra["purchase_cost_cny"] = format(Decimal(purchase_cost_cents) / 100, ".2f")
    payload["extra_json"] = json.dumps(extra, ensure_ascii=False)
    if control_plane_summary is not None:
        payload.update(control_plane_summary)
    elif session is not None:
        payload.update(_account_control_plane_summary(account, session))
    return payload


class AccountCreate(BaseModel):
    platform: str
    email: str
    password: str
    status: str = "registered"
    token: str = ""
    cashier_url: str = ""


class AccountUpdate(BaseModel):
    status: Optional[str] = None
    token: Optional[str] = None
    cashier_url: Optional[str] = None
    purchase_cost_cny: Optional[Decimal] = None

    @field_validator("purchase_cost_cny", mode="before")
    @classmethod
    def validate_purchase_cost_cny(cls, value):
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
            raise ValueError("账号成本必须是金额")
        try:
            amount = Decimal(str(value))
        except InvalidOperation as exc:
            raise ValueError("账号成本必须是有效金额") from exc
        if not amount.is_finite() or amount < 0 or amount > Decimal("999999999.99"):
            raise ValueError("账号成本须在 0 至 999999999.99 元之间")
        normalized = amount.quantize(Decimal("0.01"))
        if amount != normalized:
            raise ValueError("账号成本最多保留两位小数")
        return normalized if normalized else Decimal("0.00")


class ImportRequest(BaseModel):
    platform: str
    lines: list[str]
    account_type: Optional[str] = None


class BatchDeleteRequest(BaseModel):
    ids: list[int]


def _build_account_list(
    platform: Optional[str] = None,
    status: Optional[str] = None,
    email: Optional[str] = None,
    created_at_start: Optional[datetime] = None,
    created_at_end: Optional[datetime] = None,
    page: int = 1,
    page_size: int = 20,
    include_live: bool = False,
    refresh_live: bool = False,
    subscription_plan: Optional[str] = None,
    operational_status: Optional[str] = None,
    session: Session = Depends(get_session),
    summary_only: bool = False,
):
    def load_visible_accounts() -> list[AccountModel]:
        query = select(AccountModel)
        if platform:
            query = query.where(AccountModel.platform == platform)
        if status:
            query = query.where(AccountModel.status == status)
        if email:
            query = query.where(AccountModel.email.contains(email))
        if created_at_start:
            query = query.where(AccountModel.created_at >= created_at_start)
        if created_at_end:
            query = query.where(AccountModel.created_at <= created_at_end)
        return [
            account
            for account in session.exec(query).all()
            if account_is_visible_in_default_list(account)
        ]

    visible_accounts = load_visible_accounts()
    live_rows: list[dict[str, object]] | None = None
    live_error = ""
    live_display: dict[int | None, dict[str, object]] = {}
    remote_items: list[dict[str, object]] = []
    inventory_by_key: dict[tuple[int, int], CodexInventorySnapshotModel] = {}
    complete_inventory_targets: set[int] = set()
    # ``refresh_live`` is the explicit live-refresh boundary used by the page
    # on first load, the periodic poll, and the manual refresh action.  Keep
    # the regular list request cache-backed, but make the live path reconcile
    # the remote inventory before reading its local projections.
    if include_live and str(platform or "").strip().lower() == "chatgpt":
        from services.chatgpt_account_display import build_chatgpt_account_display

        if refresh_live:
            try:
                from services.codex_inventory import materialize_inventory, sync_inventory

                inventory_result = sync_inventory(
                    session.get_bind(),
                    refresh=False,
                )
                materialize_inventory(session.get_bind())
                session.expire_all()
                inventory_by_key, complete_inventory_targets = _inventory_state(session)
                visible_accounts = _hide_missing_remote_only_accounts(
                    load_visible_accounts(),
                    session,
                    inventory_by_key,
                    complete_inventory_targets,
                )
                targets = int(inventory_result.get("targets") or 0)
                errors = int(inventory_result.get("errors") or 0)
                if targets > 0 and errors > 0:
                    live_error = (
                        "inventory_sync_failed"
                        if errors >= targets
                        else "inventory_sync_partial_failure"
                    )
            except Exception as exc:
                live_error = type(exc).__name__
                logger.warning("刷新 Codex2API 账号库存失败: %s", live_error)
                session.expire_all()
                visible_accounts = load_visible_accounts()

        # Transitional compatibility for local rows created before the
        # inventory table existed. Once the explicit sync endpoint has run,
        # every row is rendered from its durable local snapshot.
        if not visible_accounts or any(
            not isinstance((account.get_extra() or {}).get("codex_remote_snapshot"), dict)
            for account in visible_accounts
        ):
            try:
                from services.chatgpt_codex2api_health import fetch_codex2api_quota_accounts

                fetched = fetch_codex2api_quota_accounts(
                    database_engine=session.get_bind(),
                    include_display_fields=True,
                    refresh=bool(refresh_live),
                )
                live_rows = [row for row in (fetched or []) if isinstance(row, dict)]
            except Exception as exc:
                live_error = type(exc).__name__
                logger.warning("读取旧账号实时兼容数据失败: %s", live_error)

        inventory_by_key, complete_inventory_targets = _inventory_state(session)
        inventory_exists = bool(inventory_by_key)
        if inventory_exists:
            visible_accounts = _hide_missing_remote_only_accounts(
                visible_accounts,
                session,
                inventory_by_key,
                complete_inventory_targets,
            )
        if live_rows and not inventory_exists:
            from services.control_plane_workers import reconcile_target_bindings
            from services.chatgpt_account_display import build_chatgpt_account_display
            from services.quota_ledger import merge_remote_rows
            fallback_target = session.exec(
                select(Codex2APITargetModel)
                .where(Codex2APITargetModel.enabled == True)  # noqa: E712
                .order_by(Codex2APITargetModel.id)
            ).first()
            for row in live_rows:
                remote_id = remote_account_id(row)
                target_id = int(row.get("target_id") or (fallback_target.id if fallback_target else 0))
                if remote_id <= 0 or target_id <= 0:
                    continue
                remote_email = remote_account_email(row).strip().lower()
                local_emails = {str(account.email or "").strip().lower() for account in visible_accounts}
                if remote_email and remote_email in local_emails:
                    continue
                display_email = remote_email or str(row.get("name") or "").strip() or f"remote-account-{remote_id}"
                requested_plan = str(subscription_plan or "").strip().lower()
                if requested_plan:
                    remote_plan = str(row.get("plan_type") or "").strip().lower().replace("_", "")
                    plan_aliases = {
                        "prolite": {"prolite", "selfservebusinessprolite", "businessprolite"},
                        "pro": {"pro"}, "plus": {"plus"}, "team": {"team"},
                        "k12": {"k12"}, "free": {"free"},
                    }
                    if remote_plan not in plan_aliases.get(requested_plan, {requested_plan}):
                        continue
                normalized_row = {**row, "target_id": target_id}
                reconcile_target_bindings(
                    session.get_bind(), target_id=target_id, rows=[normalized_row],
                    now=datetime.now(timezone.utc), include_remote_only=True,
                )
                identity_id = remote_identity_id(target_id, remote_id)
                merge_remote_rows(
                    session.get_bind(), identity_id=identity_id, local_account_id=0,
                    target_id=target_id, remote_id=remote_id, rows=[normalized_row],
                    captured_at=datetime.now(timezone.utc),
                )
                proxy = SimpleNamespace(
                    id=remote_virtual_account_id(target_id, remote_id),
                    email=display_email, identity_id=identity_id,
                    user_id=str(row.get("chatgpt_account_id") or row.get("effective_workspace_id") or ""),
                    extra_json=json.dumps({"account_source": "codex2api", "remote_only": True}, ensure_ascii=False),
                )
                summary = _account_control_plane_summaries([proxy], session).get(identity_id, _empty_control_plane_summary(identity_id))
                item = remote_account_payload(normalized_row, target_id=target_id, assignment=summary.get("assignment"), binding=summary.get("binding"))
                item["chatgpt_display"] = build_chatgpt_account_display(proxy, [normalized_row], live_available=True)
                item["quota"] = summary.get("quota") or {}
                remote_items.append(item)

            remote_items.sort(key=lambda item: (
                0 if str((item.get("chatgpt_display") or {}).get("remote_status") or "").lower() in {"active", "ready"}
                else 1 if str((item.get("chatgpt_display") or {}).get("remote_status") or "").lower() == "rate_limited" else 2,
                str(item.get("email") or "").lower(),
            ))

        for account in visible_accounts:
            try:
                extra = account.get_extra()
            except Exception:
                extra = {}
            snapshot = extra.get("codex_remote_snapshot") if isinstance(extra, dict) else None
            if isinstance(snapshot, dict):
                row = dict(snapshot)
                row.setdefault("target_id", extra.get("remote_target_id"))
                row.setdefault("remote_id", extra.get("remote_id"))
                snapshot_key = _account_snapshot_key(account)
                inventory_row = (
                    inventory_by_key.get(snapshot_key)
                    if snapshot_key is not None
                    else None
                )
                if inventory_row is not None:
                    # The durable inventory is the freshest projection for a
                    # target/remote key.  Local account extras are retained as
                    # a fallback for older rows, but must not shadow a newer
                    # inventory summary during cached list requests.
                    try:
                        inventory_summary = json.loads(
                            inventory_row.summary_json or "{}"
                        )
                    except (TypeError, ValueError, json.JSONDecodeError):
                        inventory_summary = {}
                    if isinstance(inventory_summary, dict):
                        row.update(inventory_summary)
                    row["target_id"] = int(inventory_row.target_id)
                    row["remote_id"] = int(inventory_row.remote_id)
                    row["_inventory_missing"] = bool(inventory_row.missing)
                    row["_inventory_error"] = str(inventory_row.error or "")
                    row["_inventory_stale"] = bool(inventory_row.error)
                snapshot_stale = _inventory_snapshot_is_stale(row)
                snapshot_missing = bool(
                    snapshot_key is not None
                    and snapshot_key[0] in complete_inventory_targets
                    and (
                        inventory_row is None
                        or bool(inventory_row.missing)
                    )
                    and not snapshot_stale
                )
                if snapshot_missing:
                    live_display[account.id] = build_chatgpt_account_display(
                        account,
                        [],
                        live_available=True,
                    )
                elif snapshot_stale:
                    live_display[account.id] = build_chatgpt_account_display(
                        account,
                        [],
                        live_available=False,
                        live_error=(
                            str(row.get("_inventory_error") or "inventory_sync_failed")
                        ),
                    )
                else:
                    live_rows = (live_rows or []) + [row]
                    live_display[account.id] = build_chatgpt_account_display(
                        account,
                        [row],
                        live_available=True,
                    )
            else:
                matching = []
                email_value = str(account.email or "").strip().lower()
                if live_rows:
                    matching = [
                        row for row in live_rows
                        if str(row.get("email") or row.get("name") or "").strip().lower() == email_value
                    ]
                live_display[account.id] = build_chatgpt_account_display(
                    account,
                    matching,
                    live_available=bool(live_rows),
                    live_error=live_error,
                )

    def subscription_matches(account: AccountModel) -> bool:
        requested = str(subscription_plan or "").strip().lower()
        if not requested or str(platform or "").strip().lower() != "chatgpt":
            return True
        display = live_display.get(account.id, {})
        plan = str(display.get("plan_type") or "").strip().lower().replace("_", "")
        aliases = {
            "prolite": {"prolite", "selfservebusinessprolite", "businessprolite"},
            "pro": {"pro"},
            "plus": {"plus"},
            "team": {"team"},
            "k12": {"k12"},
            "free": {"free"},
        }
        return plan in aliases.get(requested, {requested})

    visible_accounts = [account for account in visible_accounts if subscription_matches(account)]
    if include_live and str(platform or "").strip().lower() == "chatgpt":
        def sort_key(account: AccountModel):
            display = live_display.get(account.id, {})
            state = str(display.get("remote_status") or account.status or "").strip().lower()
            rank = 0 if state in {"active", "ready"} else 1 if state == "rate_limited" else 2
            return (rank, str(account.email or "").lower(), int(account.id or 0))
        visible_accounts.sort(key=sort_key)

    combined: list[tuple[str, object]] = [("local", account) for account in visible_accounts]
    combined.extend(("remote", item) for item in remote_items)
    assignment_states = _account_assignment_states(
        visible_accounts,
        remote_items,
        session,
    )
    operational_summary = _account_operational_summary(
        visible_accounts,
        remote_items,
        live_display,
        assignment_states,
    )
    if summary_only:
        return {
            "total": len(combined),
            "summary": operational_summary,
            "account_ids": [int(account.id) for account in visible_accounts if account.id is not None],
            "remote_count": len(remote_items),
        }
    requested_operational_status = _summary_state(operational_status)
    if requested_operational_status in {"all", "any"}:
        requested_operational_status = ""
    if requested_operational_status in {"abnormal", "auth_invalid", "errors", "normal", "scheduling", "rate_limited"}:
        filtered_combined: list[tuple[str, object]] = []
        for kind, item in combined:
            if kind == "local":
                local_item = item  # type: ignore[assignment]
                display = live_display.get(local_item.id, {})
            else:
                remote_item = item  # type: ignore[assignment]
                display = remote_item.get("chatgpt_display") if isinstance(remote_item.get("chatgpt_display"), dict) else {}
            bucket = _account_operational_bucket(item, display, assignment_states)
            matches = bucket == requested_operational_status
            if requested_operational_status == "abnormal":
                matches = bucket in {"auth_invalid", "errors"}
            if matches:
                filtered_combined.append((kind, item))
        combined = filtered_combined
    total = len(combined)
    page_size = max(1, min(int(page_size or 20), 200))
    page = max(1, int(page or 1))
    start = (page - 1) * page_size
    selected = combined[start:start + page_size]
    local_items = [item for kind, item in selected if kind == "local"]
    response_items = []
    summaries = _account_control_plane_summaries(local_items, session)
    for kind, item in selected:
        if kind == "remote":
            response_items.append(dict(item))  # type: ignore[arg-type]
            continue
        local_item = item  # type: ignore[assignment]
        payload = _account_for_response(
            local_item,
            control_plane_summary=summaries.get(
                str(local_item.identity_id or "").strip(),
                _empty_control_plane_summary(
                    str(local_item.identity_id or "").strip()
                ),
            ),
        )
        if include_live and str(platform or "").strip().lower() == "chatgpt":
            payload["chatgpt_display"] = live_display.get(
                local_item.id,
                {"plan_type": None, "plan_source": "none", "quota": None, "quota_status": "not_configured"},
            )
        response_items.append(payload)
    if include_live and str(platform or "").strip().lower() == "chatgpt":
        # All-time charges come from the same account-usage endpoint as the
        # provider's "All" tab, separately from reset-aligned quota windows.
        # Fetch only the visible page and key by node/remote ID, never email.
        billing_keys = []
        display_by_key: dict[tuple[int, int], list[dict]] = {}
        for payload in response_items:
            display = payload.get("chatgpt_display") or {}
            display["billing"] = {
                "scope": "all", "billed_usd": None,
                "source": "codex2api", "status": "error", "fetched_at": None,
            }
            try:
                key = (int(display.get("target_id") or 0), int(display.get("remote_id") or 0))
            except (TypeError, ValueError):
                continue
            if key[0] <= 0 or key[1] <= 0:
                continue
            if key not in display_by_key:
                billing_keys.append(key)
            display_by_key.setdefault(key, []).append(display)
        if billing_keys:
            try:
                from services.codex_account_billing import fetch_account_billing_summaries

                billing = fetch_account_billing_summaries(
                    session.get_bind(), billing_keys, refresh=bool(refresh_live),
                )
                for key, displays in display_by_key.items():
                    if key in billing:
                        for display in displays:
                            display["billing"] = dict(billing[key])
            except Exception as exc:
                logger.warning("读取账号累计费用失败: %s", type(exc).__name__)
    return {
        "total": total,
        "summary": operational_summary,
        "page": page,
        "items": response_items,
    }


@router.get("")
def list_accounts(
    platform: Optional[str] = None,
    status: Optional[str] = None,
    email: Optional[str] = None,
    created_at_start: Optional[datetime] = None,
    created_at_end: Optional[datetime] = None,
    page: int = 1,
    page_size: int = 20,
    include_live: bool = False,
    refresh_live: bool = False,
    subscription_plan: Optional[str] = None,
    operational_status: Optional[str] = None,
    session: Session = Depends(get_session),
):
    return _build_account_list(
        platform=platform, status=status, email=email,
        created_at_start=created_at_start, created_at_end=created_at_end,
        page=page, page_size=page_size, include_live=include_live,
        refresh_live=refresh_live, subscription_plan=subscription_plan,
        operational_status=operational_status, session=session,
    )


@router.post("")
def create_account(body: AccountCreate, session: Session = Depends(get_session)):
    acc = AccountModel(
        platform=body.platform,
        email=body.email,
        password=body.password,
        status=body.status,
        token=body.token,
        cashier_url=body.cashier_url,
    )
    session.add(acc)
    session.commit()
    session.refresh(acc)
    return _account_for_response(acc, session=session)


@router.get("/stats")
def get_stats(session: Session = Depends(get_session)):
    """统计各平台账号数量和状态分布"""
    accounts = session.exec(select(AccountModel)).all()
    platforms: dict = {}
    statuses: dict = {}
    for acc in accounts:
        platforms[acc.platform] = platforms.get(acc.platform, 0) + 1
        statuses[acc.status] = statuses.get(acc.status, 0) + 1
    return {"total": len(accounts), "by_platform": platforms, "by_status": statuses}


@router.get("/export")
def export_accounts(
    platform: Optional[str] = None,
    status: Optional[str] = None,
    session: Session = Depends(get_session),
):
    q = select(AccountModel)
    if platform:
        q = q.where(AccountModel.platform == platform)
    if status:
        q = q.where(AccountModel.status == status)
    accounts = session.exec(q).all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["platform", "email", "password", "user_id", "region",
                     "status", "cashier_url", "created_at"])
    for acc in accounts:
        writer.writerow([acc.platform, acc.email, acc.password, acc.user_id,
                         acc.region, acc.status, acc.cashier_url,
                         acc.created_at.strftime("%Y-%m-%d %H:%M:%S")])
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=accounts.csv"}
    )


@router.post("/import")
def import_accounts(
    body: ImportRequest,
    session: Session = Depends(get_session),
):
    """批量导入，每行格式: email password [extra]"""
    created = 0
    for line in body.lines:
        parts = split_mail_import_fields(str(line or "").strip())
        if len(parts) < 2:
            continue
        email, password = parts[0], parts[1]
        extra = " ".join(parts[2:]) if len(parts) > 2 else ""
        if extra:
            try:
                json.loads(extra)
            except (json.JSONDecodeError, ValueError):
                if (
                    body.platform.strip().lower() == "chatgpt"
                    and len(parts) == 3
                    and _looks_like_mfa_secret(parts[2])
                ):
                    extra = json.dumps(
                        {
                            "account_type": "chatgpt_password_totp",
                            "totp_secret": _normalize_mfa_secret(parts[2]),
                        },
                        ensure_ascii=False,
                    )
                else:
                    extra = "{}"
        else:
            account_type = str(body.account_type or "").strip()
            if not account_type and body.platform.strip().lower() == "chatgpt":
                domain = str(email or "").strip().lower().rpartition("@")[2]
                account_type = (
                    "chatgpt_password"
                    if domain in _CHATGPT_DIRECT_PASSWORD_DOMAINS
                    else "chatgpt_google_password"
                )
            extra = (
                json.dumps({"account_type": account_type}, ensure_ascii=False)
                if account_type
                else "{}"
            )
        acc = AccountModel(platform=body.platform, email=email,
                           password=password, extra_json=extra)
        session.add(acc)
        created += 1
    session.commit()
    return {"created": created}


@router.post("/batch-delete")
def batch_delete_accounts(
    body: BatchDeleteRequest,
    session: Session = Depends(get_session)
):
    """批量删除账号"""
    if not body.ids:
        raise HTTPException(400, "账号 ID 列表不能为空")
    
    if len(body.ids) > 1000:
        raise HTTPException(400, "单次最多删除 1000 个账号")
    
    unique_ids = list(dict.fromkeys(body.ids))
    database_engine = session.get_bind()
    items: list[dict] = []
    for account_id in unique_ids:
        try:
            result = remove_account(
                account_id,
                database_engine=database_engine,
            )
        except Exception as exc:
            result = {
                "ok": False,
                "account_id": int(account_id),
                "status": "database_error",
                "local_deleted": False,
                "codex2api": {"enabled": False, "status": "not_attempted"},
                "error_code": "database_error",
                "message": f"账号删除异常（{type(exc).__name__}）"[:200],
            }
        items.append(result)

    successful = [item for item in items if bool(item.get("ok"))]
    not_found_ids = [
        int(item.get("account_id") or 0)
        for item in items
        if item.get("status") == "not_found"
    ]
    failed_count = sum(
        1
        for item in items
        if not bool(item.get("ok")) and item.get("status") != "not_found"
    )

    def _remote_count(*statuses: str) -> int:
        expected = set(statuses)
        return sum(
            1
            for item in successful
            if str((item.get("codex2api") or {}).get("status") or "") in expected
        )

    response = {
        "total_requested": len(body.ids),
        "total_unique": len(unique_ids),
        "deleted": len(successful),
        "failed": failed_count,
        "not_found": not_found_ids,
        "remote_deleted": _remote_count("deleted"),
        "remote_already_absent": _remote_count("already_absent"),
        "remote_skipped": _remote_count("skipped_disabled", "not_applicable"),
        "items": items,
    }
    logger.info(
        "批量删除完成: requested=%s unique=%s deleted=%s failed=%s not_found=%s",
        response["total_requested"],
        response["total_unique"],
        response["deleted"],
        response["failed"],
        len(not_found_ids),
    )
    return response


@router.post("/check-all")
def check_all_accounts(platform: Optional[str] = None,
                       background_tasks: BackgroundTasks = None):
    from core.scheduler import scheduler
    background_tasks.add_task(scheduler.check_accounts_valid, platform)
    return {"message": "批量检测任务已启动"}


@router.get("/{account_id}")
def get_account(account_id: int, session: Session = Depends(get_session)):
    acc = session.get(AccountModel, account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    return _account_for_response(acc, session=session)


@router.patch("/{account_id}")
def update_account(account_id: int, body: AccountUpdate,
                   session: Session = Depends(get_session)):
    acc = session.get(AccountModel, account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    if body.status is not None:
        acc.status = body.status
    if body.token is not None:
        acc.token = body.token
    if body.cashier_url is not None:
        acc.cashier_url = body.cashier_url
    if "purchase_cost_cny" in body.model_fields_set:
        cost_cents = None if body.purchase_cost_cny is None else int(body.purchase_cost_cny * 100)
        if acc.platform == "chatgpt":
            from services.account_purchase_costs import update_account_purchase_cost

            update_account_purchase_cost(session, acc, cost_cents)
        else:
            acc.purchase_cost_cents = cost_cents
    acc.updated_at = datetime.now(timezone.utc)
    session.add(acc)
    session.commit()
    session.refresh(acc)
    return _account_for_response(acc, session=session)


@router.delete("/{account_id}")
def delete_account(account_id: int, session: Session = Depends(get_session)):
    try:
        result = remove_account(
            account_id,
            database_engine=session.get_bind(),
        )
    except Exception as exc:
        result = {
            "ok": False,
            "account_id": int(account_id),
            "status": "database_error",
            "local_deleted": False,
            "codex2api": {"enabled": False, "status": "not_attempted"},
            "error_code": "database_error",
            "message": f"账号删除异常（{type(exc).__name__}）"[:200],
        }
    if bool(result.get("ok")):
        return result
    status = str(result.get("status") or "")
    status_code = (
        404
        if status == "not_found"
        else 409
        if status in {"busy", "local_delete_conflict"}
        else 502
        if status == "remote_failed"
        else 500
    )
    return JSONResponse(
        status_code=status_code,
        content={
            **result,
            "detail": str(result.get("message") or "删除失败")[:200],
        },
    )


@router.post("/{account_id}/check")
def check_account(account_id: int, background_tasks: BackgroundTasks,
                  session: Session = Depends(get_session)):
    acc = session.get(AccountModel, account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    background_tasks.add_task(_do_check, account_id)
    return {"message": "检测任务已启动"}


def _do_check(account_id: int):
    from core.db import engine
    from sqlmodel import Session
    with Session(engine) as s:
        acc = s.get(AccountModel, account_id)
    if acc:
        from core.base_platform import Account, RegisterConfig
        from core.registry import get
        try:
            PlatformCls = get(acc.platform)
            plugin = PlatformCls(config=RegisterConfig())
            obj = Account(platform=acc.platform, email=acc.email,
                         password=acc.password, user_id=acc.user_id,
                         region=acc.region, token=acc.token,
                         extra=json.loads(acc.extra_json or "{}"))
            valid = plugin.check_valid(obj)
            with Session(engine) as s:
                a = s.get(AccountModel, account_id)
                if a:
                    if a.platform != "chatgpt":
                        a.status = a.status if valid else "invalid"
                    a.updated_at = datetime.now(timezone.utc)
                    s.add(a)
                    s.commit()
        except Exception:
            logger.exception("检测账号 %s 时出错", account_id)
