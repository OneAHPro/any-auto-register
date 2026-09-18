from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse, StreamingResponse
from sqlmodel import Session, select, func
from sqlalchemy import or_
from pydantic import BaseModel, field_validator
from core.db import (
    AccountAssignmentModel,
    AccountIdentityModel,
    AccountIdentityAliasModel,
    AccountModel,
    AccountPoolModel,
    AccountQuotaSnapshotModel,
    AccountTargetBindingModel,
    CodexInventorySnapshotModel,
    Codex2APITargetModel,
    get_session,
)
from core.operations_models import OperationsBillingSnapshotModel
from core.mail_import_delimiters import split_mail_import_fields
from core.applemail_pool import _looks_like_mfa_secret, _normalize_mfa_secret
from services.chatgpt_account_state import account_is_visible_in_default_list
from services.chatgpt_account_removal import remove_account
from typing import Mapping, Optional
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import io, csv, json, logging
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from urllib.parse import urlsplit, urlunsplit
from services.codex2api_remote_accounts import (
    positive_remote_id,
    remote_account_email,
    remote_account_id,
    remote_account_payload,
    remote_identity_id,
    remote_virtual_account_id,
)

_CHATGPT_DIRECT_PASSWORD_DOMAINS = {"icloud.com", "me.com", "mac.com"}

logger = logging.getLogger(__name__)
_SNAPSHOT_REFRESH_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="accounts-snapshot-refresh")


def _schedule_snapshot_refresh(database_engine) -> None:
    def refresh() -> None:
        try:
            from services.codex_inventory import materialize_inventory, sync_inventory
            from services.codex_account_billing import fetch_account_billing_summaries
            sync_inventory(database_engine, refresh=True)
            materialize_inventory(database_engine)
            # The fast list path only reads the in-memory billing cache. Warm
            # that cache in the same background pass so a restart does not
            # leave every card at “—” until a slower live request is made.
            with Session(database_engine) as background_session:
                bindings = background_session.exec(
                    select(AccountTargetBindingModel)
                    .where(AccountTargetBindingModel.enabled == True)  # noqa: E712
                ).all()
            billing_keys = []
            for binding in bindings:
                try:
                    key = (int(binding.target_id), int(binding.remote_account_id))
                except (TypeError, ValueError):
                    continue
                if key[0] > 0 and key[1] > 0:
                    billing_keys.append(key)
            if billing_keys:
                fetch_account_billing_summaries(
                    database_engine,
                    list(dict.fromkeys(billing_keys)),
                    refresh=True,
                )
        except Exception as exc:
            logger.warning("后台刷新 Codex2API 账号库存或计费失败: %s", type(exc).__name__)
    _SNAPSHOT_REFRESH_EXECUTOR.submit(refresh)
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
            binding_sync_status = _summary_state(assignment.get("binding_sync_status"))
            binding_remote_status = _summary_state(assignment.get("binding_remote_status"))
            binding_error = bool(str(assignment.get("binding_error") or "").strip())
            binding_disabled = (
                "binding_enabled" in assignment
                and not bool(assignment.get("binding_enabled"))
            )
            scheduling = bool(
                assignment_state in _SUMMARY_SCHEDULING_STATES
                or assignment.get("lease_owner")
                or status_value == "scheduling"
                or (isinstance(extra, dict) and extra.get("scheduling") is True)
            )
            # A disabled binding intentionally has no live display match. Its
            # inventory flags still determine health, independent of row order.
            remote_disabled = _summary_is_false(
                display.get("remote_enabled")
                if display.get("remote_enabled") is not None
                else snapshot.get("enabled", snapshot.get("remote_enabled"))
            )
            remote_locked = _summary_is_true(
                display.get("remote_locked")
                if display.get("remote_locked") is not None
                else snapshot.get("locked", snapshot.get("remote_locked"))
            )
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
            binding_sync_status = _summary_state(assignment.get("binding_sync_status"))
            binding_remote_status = _summary_state(assignment.get("binding_remote_status"))
            binding_error = bool(str(assignment.get("binding_error") or "").strip())
            binding_disabled = (
                "binding_enabled" in assignment
                and not bool(assignment.get("binding_enabled"))
            )
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
            or binding_sync_status in {
                "failed",
                "ambiguous",
                "remote_missing",
                "target_disabled",
                "sync_failed",
                "quarantined",
            }
            or binding_remote_status in {
                "ambiguous",
                "remote_missing",
                "target_disabled",
                "sync_failed",
            }
            or binding_error
            or binding_disabled
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
            "target_id": int(assignment.target_id or 0),
        }
        if identity_id:
            result.setdefault(identity_id, assignment_data)
        local_account_id = int(assignment.local_account_id or 0)
        if local_account_id > 0:
            result.setdefault(f"account:{local_account_id}", assignment_data)
    # A standby assignment can be intentional, but a disabled/failed binding
    # must still count as an operational error in the global summary. Attach
    # the current target binding metadata to the same projection object used
    # by both identity and local-account lookups.
    if result:
        binding_predicates = []
        if identity_ids:
            binding_predicates.append(AccountTargetBindingModel.identity_id.in_(identity_ids))
        if local_account_ids:
            binding_predicates.append(AccountTargetBindingModel.local_account_id.in_(local_account_ids))
        try:
            bindings = session.exec(
                select(AccountTargetBindingModel)
                .where(or_(*binding_predicates))
            ).all() if binding_predicates else []
        except Exception:
            # Keep the legacy account list usable while a rolling deployment
            # is creating the optional binding table.
            bindings = []
        for binding in bindings:
            identity_key = str(binding.identity_id or "").strip()
            assignment_data = result.get(identity_key)
            if assignment_data is None:
                local_key = int(binding.local_account_id or 0)
                assignment_data = result.get(f"account:{local_key}") if local_key > 0 else None
            if assignment_data is None:
                continue
            if int(assignment_data.get("target_id") or 0) != int(binding.target_id or 0):
                continue
            assignment_data.update(
                {
                    "binding_sync_status": str(binding.sync_status or "").strip().lower(),
                    "binding_enabled": bool(binding.enabled),
                    "binding_remote_status": str(binding.remote_status or "").strip().lower(),
                    "binding_error": str(binding.last_error or "").strip(),
                }
            )
    return result


def _snapshot_key_from_extra(extra: object) -> tuple[int, int] | None:
    if not isinstance(extra, dict):
        return None
    snapshot = extra.get("codex_remote_snapshot")
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    raw_target = (
        extra.get("remote_target_id")
        or snapshot.get("target_id")
        or snapshot.get("remote_target_id")
    )
    raw_remote = (
        extra.get("remote_id")
        or snapshot.get("remote_id")
        or snapshot.get("id")
    )
    if isinstance(raw_target, bool) or (
        isinstance(raw_target, float) and not raw_target.is_integer()
    ):
        return None
    try:
        target_id = int(raw_target or 0)
    except (TypeError, ValueError, OverflowError):
        return None
    remote_id = positive_remote_id(raw_remote)
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


def _aggregate_billing_summaries(
    summaries: list[Mapping[str, object]],
    *,
    expected_components: int | None = None,
) -> dict[str, object] | None:
    """Sum independent target/remote billing snapshots for one identity.

    ``summaries`` contains only keys returned by the fetch layer.  The caller
    can provide the number of expected target/remote keys so a cache miss is
    surfaced as ``partial`` instead of looking like a complete one-key total.
    """

    expected_count = max(
        len(summaries),
        int(expected_components or 0),
    )
    if expected_count <= 0:
        return None
    total = Decimal("0")
    available_count = 0
    latest: datetime | None = None
    sources: set[str] = set()
    for summary in summaries:
        if not isinstance(summary, Mapping):
            continue
        status = str(summary.get("status") or "").strip().lower()
        amount = summary.get("billed_usd")
        if status == "available" and not isinstance(amount, bool) and isinstance(
            amount, (str, int, float, Decimal)
        ):
            try:
                value = Decimal(str(amount))
            except (InvalidOperation, ValueError, TypeError):
                value = Decimal("-1")
            if value.is_finite() and value >= 0:
                total += value
                available_count += 1
        source = str(summary.get("source") or "").strip()
        if source:
            sources.add(source)
        parsed = summary.get("fetched_at")
        if isinstance(parsed, str):
            try:
                stamp = datetime.fromisoformat(parsed.replace("Z", "+00:00"))
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=timezone.utc)
                stamp = stamp.astimezone(timezone.utc)
                if latest is None or stamp > latest:
                    latest = stamp
            except ValueError:
                pass
    if not available_count:
        # Preserve an upstream error when every component failed.
        first = next((dict(item) for item in summaries if isinstance(item, Mapping)), None)
        if first is not None:
            first.setdefault("component_count", expected_count)
            first.setdefault("available_components", 0)
            first.setdefault("missing_components", expected_count)
        return first
    partial = available_count != expected_count
    return {
        "scope": "all",
        "billed_usd": float(total),
        "source": "codex2api_aggregated" if expected_count > 1 else (next(iter(sources), "codex2api")),
        "status": "available" if not partial else "partial",
        "partial": partial,
        "component_count": expected_count,
        "available_components": available_count,
        "missing_components": max(0, expected_count - available_count),
        "fetched_at": latest.isoformat() if latest is not None else "",
    }


def _inventory_state(
    session: Session,
) -> tuple[dict[tuple[int, int], CodexInventorySnapshotModel], set[int]]:
    """Return target-scoped inventory rows and targets with a complete sync."""

    try:
        rows = session.exec(select(CodexInventorySnapshotModel)).all()
        target_rows = session.exec(select(Codex2APITargetModel)).all()
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
    # A successful empty response has no snapshot rows.  The target's durable
    # sync marker is the authoritative completion record in that case.
    complete_targets.update(
        int(target.id)
        for target in target_rows
        if target.id is not None
        and target.inventory_last_sync_at is not None
        and not str(target.inventory_last_error or "").strip()
    )
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
    if not complete_targets:
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


def _account_display_stable_aliases(
    account: AccountModel,
    aliases_by_identity: Mapping[str, set[tuple[str, str]]],
) -> set[tuple[str, str]]:
    """Return only strong aliases safe for display-side deduplication.

    Email is intentionally excluded: two provider accounts may share one
    mailbox.  Workspace/account IDs and the persisted credential fingerprint
    are the same aliases used by identity resolution and therefore provide a
    conservative bridge for legacy rows that were materialized separately.
    """

    identity_id = str(account.identity_id or "").strip()
    result = set(aliases_by_identity.get(identity_id, set()))
    try:
        extra = account.get_extra()
    except Exception:
        extra = {}
    if not isinstance(extra, Mapping):
        extra = {}
    alias_sources: list[Mapping[str, object]] = []

    def collect_sources(value: object, depth: int = 0) -> None:
        if not isinstance(value, Mapping) or depth > 6:
            return
        alias_sources.append(value)
        for child in value.values():
            if isinstance(child, Mapping):
                collect_sources(child, depth + 1)
            elif isinstance(child, (list, tuple, set)):
                for item in child:
                    if isinstance(item, Mapping):
                        collect_sources(item, depth + 1)

    collect_sources(extra)
    for alias_type, keys in {
        "workspace_id": ("workspace_id", "workspaceId", "effective_workspace_id"),
        "chatgpt_account_id": (
            "chatgpt_account_id", "chatgptAccountId", "account_id", "accountId",
        ),
    }.items():
        for source in alias_sources:
            for key in keys:
                raw_value = source.get(key)
                values = raw_value if isinstance(raw_value, (list, tuple, set)) else (raw_value,)
                for value in values:
                    normalized = str(value or "").strip().casefold()
                    if normalized:
                        result.add((alias_type, normalized))
    user_id = str(account.user_id or "").strip().casefold()
    if user_id:
        result.add(("chatgpt_account_id", user_id))
    # Legacy rows may have no persisted alias record yet.  Derive the same
    # HMAC fingerprint used by identity resolution when a real token is
    # present; never fingerprint email alone, since shared mailboxes are
    # common and must remain separate.
    token = str(account.token or "").strip()
    refresh_token = str(extra.get("refresh_token") or extra.get("refreshToken") or "").strip()
    access_token = str(extra.get("access_token") or extra.get("accessToken") or token).strip()
    session_token = str(extra.get("session_token") or extra.get("sessionToken") or "").strip()
    if token or refresh_token or access_token or session_token:
        try:
            from services.account_identity import credential_fingerprint

            result.add((
                "credential_fingerprint",
                credential_fingerprint(
                    str(account.platform or ""),
                    str(account.email or ""),
                    refresh_token=refresh_token,
                    access_token=access_token,
                    session_token=session_token,
                    workspace_id=str(extra.get("workspace_id") or extra.get("workspaceId") or ""),
                    chatgpt_account_id=str(
                        extra.get("chatgpt_account_id") or extra.get("chatgptAccountId") or ""
                    ),
                ),
            ))
        except Exception:
            pass
    return {
        (str(alias_type), str(value))
        for alias_type, value in result
        if alias_type in {"workspace_id", "chatgpt_account_id", "credential_fingerprint"}
        and str(value).strip()
    }


_DISPLAY_STRONG_ALIAS_TYPES = frozenset(
    {"workspace_id", "chatgpt_account_id", "credential_fingerprint"}
)


def _mapping_display_stable_aliases(value: Mapping[str, object]) -> set[tuple[str, str]]:
    """Extract non-secret strong aliases from a remote projection."""

    result: set[tuple[str, str]] = set()
    sources: list[Mapping[str, object]] = []

    def collect(source: object, depth: int = 0) -> None:
        if not isinstance(source, Mapping) or depth > 6:
            return
        sources.append(source)
        for child in source.values():
            if isinstance(child, Mapping):
                collect(child, depth + 1)
            elif isinstance(child, (list, tuple, set)):
                for item in child:
                    if isinstance(item, Mapping):
                        collect(item, depth + 1)

    collect(value)
    for alias_type, keys in {
        "workspace_id": ("workspace_id", "workspaceId", "effective_workspace_id"),
        "chatgpt_account_id": (
            "chatgpt_account_id", "chatgptAccountId", "account_id", "accountId", "user_id",
        ),
    }.items():
        for source in sources:
            raw_values = [source.get(key) for key in keys]
            for raw_value in raw_values:
                values = raw_value if isinstance(raw_value, (list, tuple, set)) else (raw_value,)
                for item in values:
                    normalized = str(item or "").strip().casefold()
                    if normalized:
                        result.add((alias_type, normalized))
    return result


def _deduplicate_remote_items(
    items: list[dict[str, object]],
) -> tuple[list[dict[str, object]], dict[int, list[dict[str, object]]]]:
    """Collapse duplicate pure-remote cards when a strong alias agrees.

    This is the compatibility path used before inventory materialization. It
    follows the same rule as local rows: aliases may join only compatible
    mailboxes, and email alone never joins. The returned component map lets
    billing include every remote key hidden behind the representative card.
    """

    if len(items) < 2:
        return items, {id(item): [item] for item in items}
    parent = list(range(len(items)))
    emails = [str(item.get("email") or "").strip().casefold() for item in items]
    aliases = [
        _mapping_display_stable_aliases(item)
        for item in items
    ]

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    owners: dict[tuple[str, str], list[int]] = {}
    for index, values in enumerate(aliases):
        for alias in values:
            owners.setdefault(alias, []).append(index)
    for indices in owners.values():
        by_email: dict[str, list[int]] = {}
        for index in indices:
            by_email.setdefault(emails[index], []).append(index)
        # Empty emails are safe to combine only when no known mailbox conflicts.
        groups = [values for key, values in by_email.items() if key]
        for values in groups:
            first = values[0]
            for index in values[1:]:
                parent[find(index)] = find(first)
        empty = by_email.get("", [])
        if empty and len(groups) == 1:
            first = groups[0][0]
            for index in empty:
                parent[find(index)] = find(first)
        elif len(groups) == 0 and empty:
            first = empty[0]
            for index in empty[1:]:
                parent[find(index)] = find(first)

    grouped: dict[int, list[dict[str, object]]] = {}
    for index, item in enumerate(items):
        grouped.setdefault(find(index), []).append(item)
    if all(len(values) == 1 for values in grouped.values()):
        return items, {id(item): [item] for item in items}

    def rank(item: dict[str, object]) -> tuple[int, str, int]:
        status = str(item.get("remote_status") or item.get("status") or "").strip().casefold()
        status_rank = 0 if status in {"active", "ready"} else 1 if status == "rate_limited" else 2
        return (status_rank, str(item.get("updated_at") or ""), int(item.get("id") or 0))

    representatives: list[dict[str, object]] = []
    components: dict[int, list[dict[str, object]]] = {}
    original_order = {id(item): index for index, item in enumerate(items)}
    for values in grouped.values():
        values.sort(key=rank)
        representative = values[0]
        representatives.append(representative)
        components[id(representative)] = list(values)
    representatives.sort(key=lambda item: original_order.get(id(item), 0))
    return representatives, components


def _display_identity_metadata(
    session: Session,
    identity_ids: set[str],
) -> dict[str, dict[str, object]]:
    """Load identity metadata used to validate cross-row alias joins."""

    if not identity_ids:
        return {}
    try:
        rows = session.exec(
            select(AccountIdentityModel).where(AccountIdentityModel.id.in_(identity_ids))
        ).all()
    except Exception:
        return {}
    result: dict[str, dict[str, object]] = {}
    for row in rows:
        identity_id = str(row.id or "").strip()
        if not identity_id:
            continue
        try:
            current_account_id = int(row.current_account_id or 0)
        except (TypeError, ValueError, OverflowError):
            current_account_id = 0
        result[identity_id] = {
            "platform": str(row.platform or "").strip().casefold(),
            "email": str(row.canonical_email or "").strip().casefold(),
            "state": str(row.state or "active").strip().casefold(),
            "current_account_id": current_account_id,
        }
    return result


def _account_display_email(account: AccountModel) -> str:
    """Return a real mailbox for grouping, ignoring remote placeholders."""

    email = str(account.email or "").strip().casefold()
    try:
        extra = account.get_extra()
    except Exception:
        extra = {}
    if not isinstance(extra, Mapping):
        extra = {}
    snapshot = extra.get("codex_remote_snapshot")
    snapshot = snapshot if isinstance(snapshot, Mapping) else {}
    if (
        bool(extra.get("remote_only"))
        or email.startswith("remote-account-")
        or email.startswith("远端账号 #")
    ) and (
        bool(extra.get("remote_email_missing"))
        or bool(snapshot.get("_remote_email_missing"))
        or email.startswith("remote-account-")
        or email.startswith("远端账号 #")
    ):
        return ""
    return email


def _display_account_groups(
    accounts: list[AccountModel],
    session: Session,
) -> tuple[dict[int, list[int]], dict[int, int]]:
    """Build conservative groups for duplicate account projections.

    Strong aliases can bridge rows created for different targets, but an
    alias is not sufficient by itself: a provider can reuse an account or
    workspace identifier for another mailbox.  Email compatibility and the
    identity ambiguity state fence that collision.  Email alone never joins.
    """

    if not accounts:
        return {}, {}
    identity_ids = {
        str(account.identity_id or "").strip()
        for account in accounts
        if str(account.identity_id or "").strip()
    }
    aliases_by_identity: dict[str, set[tuple[str, str]]] = {}
    if identity_ids:
        try:
            alias_rows = session.exec(
                select(AccountIdentityAliasModel).where(
                    AccountIdentityAliasModel.identity_id.in_(identity_ids),
                    AccountIdentityAliasModel.alias_type.in_(tuple(_DISPLAY_STRONG_ALIAS_TYPES)),
                )
            ).all()
        except Exception:
            alias_rows = []
        for row in alias_rows:
            identity_id = str(row.identity_id or "").strip()
            alias_type = str(row.alias_type or "").strip().casefold()
            value = str(row.normalized_value or "").strip().casefold()
            if identity_id and alias_type in _DISPLAY_STRONG_ALIAS_TYPES and value:
                aliases_by_identity.setdefault(identity_id, set()).add((alias_type, value))

    metadata = _display_identity_metadata(session, identity_ids)
    parent = list(range(len(accounts)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    platforms: list[str] = []
    emails: list[str] = []
    identities: list[str] = []
    states: list[str] = []
    aliases: list[set[tuple[str, str]]] = []
    for account in accounts:
        identity_id = str(account.identity_id or "").strip()
        identity_meta = metadata.get(identity_id, {})
        platform = str(account.platform or "").strip().casefold()
        if not platform:
            platform = str(identity_meta.get("platform") or "").strip().casefold()
        email = _account_display_email(account)
        if not email:
            email = str(identity_meta.get("email") or "").strip().casefold()
        platforms.append(platform)
        emails.append(email)
        identities.append(identity_id)
        states.append(str(identity_meta.get("state") or "active").strip().casefold())
        aliases.append(_account_display_stable_aliases(account, aliases_by_identity))

    def compatible(left: int, right: int, *, same_identity: bool = False) -> bool:
        if platforms[left] != platforms[right]:
            return False
        if same_identity:
            # A stable identity remains authoritative through a provider email
            # rotation, so duplicate rows carrying that exact key collapse.
            return bool(identities[left])
        if emails[left] and emails[right] and emails[left] != emails[right]:
            return False
        if states[left] == "ambiguous" or states[right] == "ambiguous":
            return False
        return True

    group_emails: dict[int, set[str]] = {
        index: ({emails[index]} if emails[index] else set())
        for index in range(len(accounts))
    }
    group_identities: dict[int, set[str]] = {
        index: ({identities[index]} if identities[index] else set())
        for index in range(len(accounts))
    }
    group_ambiguous: dict[int, bool] = {
        index: states[index] == "ambiguous"
        for index in range(len(accounts))
    }

    def guarded_union(left: int, right: int, *, same_identity: bool = False) -> None:
        left_root, right_root = find(left), find(right)
        if left_root == right_root:
            return
        # Alias joins must remain globally mailbox-compatible, even when an
        # intermediate legacy row omitted its email. Without this group-level
        # fence, A(email=a, alias=x) + B(no email, alias=x/y) + C(email=c,
        # alias=y) would incorrectly collapse all three rows.
        merged_emails = group_emails[left_root] | group_emails[right_root]
        if not same_identity and (
            len(merged_emails) > 1
            or group_ambiguous[left_root]
            or group_ambiguous[right_root]
        ):
            return
        parent[right_root] = left_root
        group_emails[left_root] = merged_emails
        group_identities[left_root].update(group_identities[right_root])
        group_ambiguous[left_root] = (
            group_ambiguous[left_root] or group_ambiguous[right_root]
        )
        group_emails.pop(right_root, None)
        group_identities.pop(right_root, None)
        group_ambiguous.pop(right_root, None)

    identity_owners: dict[tuple[str, str], list[int]] = {}
    alias_owners: dict[tuple[str, str, str], list[int]] = {}
    for index, identity_id in enumerate(identities):
        if identity_id:
            identity_owners.setdefault((platforms[index], identity_id), []).append(index)
        for alias_type, value in aliases[index]:
            alias_owners.setdefault((platforms[index], alias_type, value), []).append(index)

    for owner_indices in identity_owners.values():
        first = owner_indices[0]
        for index in owner_indices[1:]:
            if compatible(first, index, same_identity=True):
                guarded_union(first, index, same_identity=True)
    for owner_indices in alias_owners.values():
        # Group by mailbox before unioning.  A popular workspace/account alias
        # can occur on thousands of rows; pairwise comparison would turn a
        # normal list request into O(n²) work.
        by_email: dict[str, list[int]] = {}
        for index in owner_indices:
            by_email.setdefault(emails[index], []).append(index)
        nonempty_groups = [members for key, members in by_email.items() if key]
        for members in nonempty_groups:
            first = members[0]
            for index in members[1:]:
                if compatible(first, index):
                    guarded_union(first, index)
        empty_members = by_email.get("", [])
        if empty_members:
            if len(nonempty_groups) == 1:
                # An omitted provider email can safely follow the sole known
                # mailbox carrying this alias.
                first = nonempty_groups[0][0]
                for index in empty_members:
                    if compatible(first, index):
                        guarded_union(first, index)
            else:
                # Multiple known mailboxes make an empty row ambiguous. Keep
                # empty rows together only; never choose one mailbox.
                first = empty_members[0]
                for index in empty_members[1:]:
                    if compatible(first, index):
                        guarded_union(first, index)

    groups: dict[int, list[int]] = {}
    account_to_group: dict[int, int] = {}
    for index in range(len(accounts)):
        root = find(index)
        groups.setdefault(root, []).append(index)
        account_to_group[index] = root
    return groups, account_to_group


def _deduplicate_display_accounts(
    accounts: list[AccountModel],
    session: Session,
) -> list[AccountModel]:
    """Collapse duplicate local rows while retaining one canonical card.

    The durable bindings and inventory snapshots remain untouched.  This is a
    presentation-level grouping so one account present in several pools is
    counted once and can receive an aggregate billing value later in the
    response builder.
    """

    if len(accounts) < 2:
        return accounts
    grouped, _account_to_group = _display_account_groups(accounts, session)
    if all(len(rows) == 1 for rows in grouped.values()):
        return accounts

    identity_current: dict[str, int] = {}
    identity_ids = {
        str(account.identity_id or "").strip()
        for account in accounts
        if str(account.identity_id or "").strip()
    }
    if identity_ids:
        identity_current = {
            identity_id: int(metadata.get("current_account_id") or 0)
            for identity_id, metadata in _display_identity_metadata(session, identity_ids).items()
        }

    def stamp(account: AccountModel) -> datetime:
        value = account.updated_at or account.created_at
        if value is None:
            return datetime.min.replace(tzinfo=timezone.utc)
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    result: list[AccountModel] = []
    for member_indices in grouped.values():
        rows = [accounts[index] for index in member_indices]
        if len(rows) == 1:
            result.append(rows[0])
            continue
        rows.sort(
            key=lambda account: (
                int(account.id or 0) == identity_current.get(str(account.identity_id or ""), 0),
                not _account_is_remote_only_for_display(account),
                bool(str(account.password or "").strip() or str(account.token or "").strip()),
                stamp(account),
                int(account.id or 0),
            ),
            reverse=True,
        )
        result.append(rows[0])
    # Preserve the database order so pagination remains stable.
    order = {id(account): index for index, account in enumerate(accounts)}
    result.sort(key=lambda account: order.get(id(account), 0))
    return result


def _display_representatives_with_members(
    accounts: list[AccountModel],
    session: Session,
) -> tuple[list[AccountModel], dict[int, list[AccountModel]]]:
    """Return representatives plus the rows hidden behind each card."""

    if not accounts:
        return [], {}
    grouped, _account_to_group = _display_account_groups(accounts, session)
    representatives = _deduplicate_display_accounts(accounts, session)
    by_object_id: dict[int, list[AccountModel]] = {}
    for member_indices in grouped.values():
        members = [accounts[index] for index in member_indices]
        member_ids = {id(member) for member in members}
        representative = next(
            (item for item in representatives if id(item) in member_ids),
            None,
        )
        if representative is not None:
            by_object_id[id(representative)] = members
    for representative in representatives:
        by_object_id.setdefault(id(representative), [representative])
    return representatives, by_object_id


def _account_is_remote_only_for_display(account: AccountModel) -> bool:
    if _account_source(account) == "codex2api":
        return True
    try:
        extra = account.get_extra()
    except Exception:
        extra = {}
    return isinstance(extra, Mapping) and bool(extra.get("remote_only"))


def _safe_identity_groups_for_billing(
    session: Session,
    identity_ids: set[str],
) -> dict[str, set[str]]:
    """Return billing groups using the same collision fences as the list.

    This wrapper intentionally delegates to ``_display_account_groups`` so a
    strong alias shared by different mailboxes (or an ambiguous identity) is
    never allowed to pull another account's billing into the card.
    """

    result: dict[str, set[str]] = {
        identity_id: {identity_id} for identity_id in identity_ids
    }
    if not identity_ids:
        return result
    try:
        accounts = session.exec(
            select(AccountModel).where(AccountModel.platform == "chatgpt")
        ).all()
        known_identity_ids = {
            str(account.identity_id or "").strip()
            for account in accounts
            if str(account.identity_id or "").strip()
        }
        # A remote-only binding can exist for a short period before its local
        # projection is materialized. Add a credential-free synthetic row so
        # its persisted aliases still participate in the same group lookup.
        identities = session.exec(select(AccountIdentityModel)).all()
        for identity in identities:
            identity_key = str(identity.id or "").strip()
            if not identity_key or identity_key in known_identity_ids:
                continue
            accounts.append(
                AccountModel(
                    platform=str(identity.platform or "chatgpt"),
                    email=str(identity.canonical_email or ""),
                    password="",
                    identity_id=identity_key,
                    extra_json="{}",
                )
            )
    except Exception:
        return result
    groups, _account_to_group = _display_account_groups(accounts, session)
    for indices in groups.values():
        members = {
            str(accounts[index].identity_id or "").strip()
            for index in indices
            if str(accounts[index].identity_id or "").strip()
        }
        for identity_id in members.intersection(identity_ids):
            result[identity_id] = set(members)
    return result


# Keep the old private symbol compatible for callers/tests from the previous
# list implementation while routing it through the fenced grouping logic.
_identity_group_ids_for_billing = _safe_identity_groups_for_billing


def _billing_binding_is_current(
    binding: AccountTargetBindingModel,
    target_enabled_by_id: Mapping[int, bool] | None = None,
    inventory_by_key: Mapping[tuple[int, int], CodexInventorySnapshotModel] | None = None,
    persisted_billing_keys: set[tuple[int, int]] | None = None,
) -> bool:
    """Return whether a binding can contribute current or historical billing."""

    try:
        target_id = int(binding.target_id or 0)
        remote_id = int(binding.remote_account_id or 0)
    except (TypeError, ValueError, OverflowError):
        return False
    if target_id <= 0 or remote_id <= 0:
        return False
    status = str(binding.sync_status or "").strip().casefold()
    if status in {"", "unknown", "synced"}:
        if not bool(binding.enabled):
            return False
        key = (target_id, remote_id)
        if status in {"", "unknown"} and not (
            (persisted_billing_keys and key in persisted_billing_keys)
            or (
                inventory_by_key is not None
                and key in inventory_by_key
                and not bool(inventory_by_key[key].missing)
                and not str(inventory_by_key[key].error or "").strip()
            )
        ):
            # Legacy rows without a confirmed sync state are not enough to
            # trigger a new remote read or inflate an aggregate.
            return False
        # A disabled target can still have a valid historical billing segment;
        # the target registry is therefore only a guard for live rows.
        if target_enabled_by_id is not None and target_id in target_enabled_by_id:
            return bool(target_enabled_by_id[target_id]) or (
                inventory_by_key is not None
                and (target_id, remote_id) in inventory_by_key
                and not bool(inventory_by_key[(target_id, remote_id)].missing)
                and not str(inventory_by_key[(target_id, remote_id)].error or "").strip()
            )
        return True
    if status not in {"superseded", "remote_missing", "target_disabled", "failed"}:
        return False
    key = (target_id, remote_id)
    if persisted_billing_keys and key in persisted_billing_keys:
        return True
    # ``remote_missing`` is an explicit negative observation. A stale
    # inventory row must not resurrect it; only a previously captured billing
    # snapshot can contribute historical value for that key.
    if status == "remote_missing":
        return False
    if inventory_by_key is not None:
        row = inventory_by_key.get(key)
        if row is not None and not bool(row.missing) and not str(row.error or "").strip():
            return True
    return False


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
    payload["account_source"] = _account_source(account)
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


def _account_source(account: AccountModel) -> str:
    """Return the durable account origin with legacy extra-json fallback."""

    source = str(getattr(account, "account_source", "") or "").strip().lower()
    if source in {"local", "codex2api"}:
        return source
    try:
        extra = account.get_extra()
    except Exception:
        extra = {}
    if isinstance(extra, Mapping) and bool(extra.get("remote_only")):
        return "codex2api"
    return "local"


class AccountCreate(BaseModel):
    platform: str
    email: str
    password: str
    status: str = "registered"
    token: str = ""
    cashier_url: str = ""

    @field_validator("platform", "email", "password")
    @classmethod
    def validate_required_text(cls, value: str, info):
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError(f"{info.field_name} 不能为空")
        return normalized

    @field_validator("platform")
    @classmethod
    def normalize_platform(cls, value: str):
        return value.lower()

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str):
        normalized = value.lower()
        if "@" not in normalized or normalized.startswith("@") or normalized.endswith("@"):
            raise ValueError("邮箱格式不正确")
        return normalized


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
    account_source: Optional[str] = None,
    session: Session = Depends(get_session),
    summary_only: bool = False,
    snapshot_only: bool = False,
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

    # Keep the raw rows through live projection/filtering.  A duplicate row
    # can carry the only matching plan/status; collapsing it before filters
    # would make a valid account disappear from a filtered result.  We collapse
    # once the predicates have been evaluated below.
    requested_source = str(account_source or "").strip().lower()
    if requested_source not in {"", "local", "codex2api"}:
        requested_source = ""

    visible_accounts = load_visible_accounts()
    live_rows: list[dict[str, object]] | None = None
    live_error = ""
    live_display: dict[int | None, dict[str, object]] = {}
    remote_items: list[dict[str, object]] = []
    remote_item_components: dict[int, list[dict[str, object]]] = {}
    # Legacy live rows can be hidden behind an already materialized local
    # card.  Keep their natural keys so the billing pass still includes every
    # target/remote component even when inventory materialization is delayed.
    compatibility_hidden_keys_by_account_id: dict[int, set[tuple[int, int]]] = {}
    inventory_by_key: dict[tuple[int, int], CodexInventorySnapshotModel] = {}
    complete_inventory_targets: set[int] = set()
    bindings = session.exec(select(AccountTargetBindingModel)).all()
    identity_states = {
        str(identity.id): str(identity.state or "").strip().lower()
        for identity in session.exec(select(AccountIdentityModel)).all()
        if str(identity.id or "").strip()
    }
    # ``refresh_live`` is the explicit live-refresh boundary used by the page
    # on first load, the periodic poll, and the manual refresh action.  Keep
    # the regular list request cache-backed, but make the live path reconcile
    # the remote inventory before reading its local projections.
    if include_live and str(platform or "").strip().lower() == "chatgpt":
        from services.chatgpt_account_display import build_chatgpt_account_display

        if snapshot_only and refresh_live:
            _schedule_snapshot_refresh(session.get_bind())

        if refresh_live and not snapshot_only:
            try:
                from services.codex_inventory import materialize_inventory, sync_inventory

                inventory_result = sync_inventory(
                    session.get_bind(),
                    refresh=bool(refresh_live),
                )
                materialize_inventory(session.get_bind())
                session.expire_all()
                bindings = session.exec(select(AccountTargetBindingModel)).all()
                identity_states = {
                    str(identity.id): str(identity.state or "").strip().lower()
                    for identity in session.exec(select(AccountIdentityModel)).all()
                    if str(identity.id or "").strip()
                }
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
        if not snapshot_only and (not visible_accounts or any(
            not isinstance((account.get_extra() or {}).get("codex_remote_snapshot"), dict)
            for account in visible_accounts
        )):
            try:
                from services.chatgpt_codex2api_health import fetch_codex2api_quota_accounts

                fetched = fetch_codex2api_quota_accounts(
                    database_engine=session.get_bind(),
                    include_display_fields=True,
                    refresh=bool(refresh_live),
                )
                live_rows = [row for row in (fetched or []) if isinstance(row, dict)]
                # This is a display-only compatibility reader for rows
                # produced by older health endpoints.  Those projections
                # legitimately omit ``remote_id``; authoritative inventory
                # synchronization validates positive, unique IDs in
                # ``sync_inventory`` before it can mutate bindings or
                # snapshots.  Keep ID-less rows available for an exact email
                # match here, while remote-only materialization below still
                # requires a positive ID.
            except Exception as exc:
                live_error = type(exc).__name__
                logger.warning("读取旧账号实时兼容数据失败: %s", live_error)

        inventory_by_key, complete_inventory_targets = _inventory_state(session)
        # A successful empty inventory has no rows by definition.  The
        # target-level completion marker still makes it authoritative, so
        # apply the same remote-only hiding and fallback suppression based on
        # ``complete_inventory_targets`` rather than row presence.
        inventory_exists = bool(inventory_by_key)
        if inventory_exists or complete_inventory_targets:
            visible_accounts = _hide_missing_remote_only_accounts(
                visible_accounts,
                session,
                inventory_by_key,
                complete_inventory_targets,
            )
        if live_rows:
            from services.control_plane_workers import reconcile_target_bindings
            from services.chatgpt_account_display import build_chatgpt_account_display
            from services.quota_ledger import merge_remote_rows
            fallback_target = session.exec(
                select(Codex2APITargetModel)
                .where(Codex2APITargetModel.enabled == True)  # noqa: E712
                .order_by(Codex2APITargetModel.id)
            ).first()
            local_email_counts: dict[str, int] = {}
            for local_account in visible_accounts:
                local_email = str(local_account.email or "").strip().lower()
                if local_email:
                    local_email_counts[local_email] = local_email_counts.get(local_email, 0) + 1
            bound_local_by_key: dict[tuple[int, int], int] = {}
            for binding in bindings:
                try:
                    binding_key = (int(binding.target_id), int(binding.remote_account_id))
                except (TypeError, ValueError, OverflowError):
                    continue
                if (
                    binding_key[0] > 0
                    and binding_key[1] > 0
                    and bool(binding.enabled)
                    and str(binding.sync_status or "").strip().casefold() == "synced"
                    and int(binding.local_account_id or 0) > 0
                ):
                    bound_local_by_key[binding_key] = int(binding.local_account_id)
            local_snapshot_keys = {
                key
                for key in (_account_snapshot_key(account) for account in visible_accounts)
                if key is not None
            }
            local_aliases = {
                id(account): _account_display_stable_aliases(account, {})
                for account in visible_accounts
            }
            for row in live_rows:
                remote_id = remote_account_id(row)
                target_id = int(row.get("target_id") or (fallback_target.id if fallback_target else 0))
                if remote_id <= 0 or target_id <= 0:
                    continue
                # Inventory completion is target-scoped.  A complete target
                # must not be repopulated from the legacy API compatibility
                # reader, while an independent target may still need that
                # fallback during its first sync.
                if target_id in complete_inventory_targets:
                    continue
                remote_email = remote_account_email(row).strip().lower()
                remote_key = (target_id, remote_id)
                bound_local_id = bound_local_by_key.get(remote_key, 0)
                if bound_local_id > 0:
                    compatibility_hidden_keys_by_account_id.setdefault(
                        bound_local_id, set()
                    ).add(remote_key)
                    continue
                if remote_key in local_snapshot_keys:
                    for local_account in visible_accounts:
                        if _account_snapshot_key(local_account) == remote_key:
                            try:
                                local_id = int(local_account.id or 0)
                            except (TypeError, ValueError, OverflowError):
                                local_id = 0
                            if local_id > 0:
                                compatibility_hidden_keys_by_account_id.setdefault(
                                    local_id, set()
                                ).add(remote_key)
                    continue
                row_aliases = _mapping_display_stable_aliases(row)
                if row_aliases:
                    alias_matches = [
                        account
                        for account in visible_accounts
                        if row_aliases.intersection(local_aliases.get(id(account), set()))
                        and identity_states.get(str(account.identity_id or ""), "active") != "ambiguous"
                        and (
                            not remote_email
                            or not str(account.email or "").strip()
                            or str(account.email or "").strip().casefold() == remote_email
                        )
                    ]
                    if len(alias_matches) == 1:
                        try:
                            local_id = int(alias_matches[0].id or 0)
                        except (TypeError, ValueError, OverflowError):
                            local_id = 0
                        if local_id > 0:
                            compatibility_hidden_keys_by_account_id.setdefault(
                                local_id, set()
                            ).add(remote_key)
                        continue
                if remote_email and local_email_counts.get(remote_email, 0) == 1:
                    matching_local = next(
                        (
                            account
                            for account in visible_accounts
                            if str(account.email or "").strip().casefold() == remote_email
                        ),
                        None,
                    )
                    if matching_local is not None:
                        try:
                            local_id = int(matching_local.id or 0)
                        except (TypeError, ValueError, OverflowError):
                            local_id = 0
                        if local_id > 0:
                            compatibility_hidden_keys_by_account_id.setdefault(
                                local_id, set()
                            ).add(remote_key)
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
                # The reconciliation helper uses its own transaction.  Drop
                # this request session's old snapshot before reading the
                # binding/identity it just repaired (especially on SQLite).
                session.rollback()
                session.expire_all()
                identity_id = remote_identity_id(target_id, remote_id)
                merge_remote_rows(
                    session.get_bind(), identity_id=identity_id, local_account_id=0,
                    target_id=target_id, remote_id=remote_id, rows=[normalized_row],
                    captured_at=datetime.now(timezone.utc),
                )
                session.rollback()
                session.expire_all()
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
            remote_items, remote_item_components = _deduplicate_remote_items(remote_items)

            # The compatibility loop may have reconciled bindings in a
            # separate transaction. Refresh this request session before
            # deciding whether a local snapshot is still current; stale ORM
            # state could otherwise mask a newly quarantined binding.
            session.rollback()
            session.expire_all()
            bindings = session.exec(select(AccountTargetBindingModel)).all()
            identity_states = {
                str(identity.id): str(identity.state or "").strip().lower()
                for identity in session.exec(select(AccountIdentityModel)).all()
                if str(identity.id or "").strip()
            }

        for account in visible_accounts:
            try:
                extra = account.get_extra()
            except Exception:
                extra = {}
            snapshot = extra.get("codex_remote_snapshot") if isinstance(extra, dict) else None
            # A retry/target move can leave a historical projection in an
            # account's extra JSON while its binding has already been
            # superseded.  Only an enabled, synced binding may make that
            # projection visible to cards; otherwise the card stays unknown
            # until the new target is confirmed.
            if isinstance(snapshot, dict):
                snapshot_target = extra.get("remote_target_id") or snapshot.get("target_id")
                snapshot_remote = extra.get("remote_id") or snapshot.get("remote_id")
                try:
                    snapshot_pair = (int(snapshot_target or 0), int(snapshot_remote or 0))
                except (TypeError, ValueError):
                    snapshot_pair = (0, 0)
                binding_matches = [
                    binding
                    for binding in bindings
                    if (
                        str(binding.identity_id or "") == str(account.identity_id or "")
                        and identity_states.get(str(account.identity_id or ""), "active") != "ambiguous"
                        and bool(binding.enabled)
                        and (
                            str(binding.sync_status or "").lower() == "synced"
                            or (
                                str(binding.sync_status or "").lower() in {"", "unknown"}
                                and binding.last_sync_at is None
                            )
                        )
                        and (int(binding.target_id or 0), int(binding.remote_account_id or 0)) == snapshot_pair
                    )
                ]
                if any(
                    str(binding.identity_id or "") == str(account.identity_id or "")
                    for binding in bindings
                ) and not binding_matches:
                    snapshot = None
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
                    active_binding_keys = {
                        (int(binding.target_id or 0), int(binding.remote_account_id or 0))
                        for binding in bindings
                        if (
                            str(binding.identity_id or "") == str(account.identity_id or "")
                            and bool(binding.enabled)
                            and int(binding.remote_account_id or 0) > 0
                        )
                    }
                    has_identity_bindings = any(
                        str(binding.identity_id or "")
                        == str(account.identity_id or "")
                        for binding in bindings
                    )
                    if active_binding_keys:
                        matching = [
                            row for row in live_rows
                            if (
                                int(row.get("target_id") or 0),
                                int(row.get("remote_id") or row.get("id") or 0),
                            ) in active_binding_keys
                        ]
                    elif has_identity_bindings:
                        matching = []
                    else:
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

    # Match live rows against every local credential before filtering by origin.
    # Otherwise a linked remote copy would reappear as a remote-only account.
    if requested_source:
        visible_accounts = [
            account for account in visible_accounts
            if _account_source(account) == requested_source
        ]
    if requested_source == "local":
        remote_items = []

    visible_accounts = [account for account in visible_accounts if subscription_matches(account)]
    # Apply the display grouping after subscription filtering so any matching
    # member of a duplicate pool group keeps the canonical card visible. Keep
    # the member map for billing, including legacy rows without an identity_id.
    visible_accounts, display_group_members = _display_representatives_with_members(
        visible_accounts,
        session,
    )
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
    local_source_accounts = [
        account
        for account in visible_accounts
        if not _account_is_remote_only_for_display(account)
    ]
    remote_source_accounts = [
        account
        for account in visible_accounts
        if _account_is_remote_only_for_display(account)
    ]
    source_summary = {
        "local": _account_operational_summary(
            local_source_accounts,
            [],
            live_display,
            assignment_states,
        ),
        "remote": _account_operational_summary(
            remote_source_accounts,
            remote_items,
            live_display,
            assignment_states,
        ),
    }
    if summary_only:
        return {
            "total": len(combined),
            "summary": operational_summary,
            "source_summary": source_summary,
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
    local_group_key_by_id: dict[int, str] = {}
    local_group_key_by_member_id: dict[int, str] = {}
    local_group_identity_ids: dict[str, set[str]] = {}
    for representative in visible_accounts:
        try:
            representative_id = int(representative.id or 0)
        except (TypeError, ValueError, OverflowError):
            continue
        identity_key = str(representative.identity_id or "").strip()
        group_key = identity_key or f"account:{representative_id}"
        local_group_key_by_id[representative_id] = group_key
        local_group_identity_ids.setdefault(group_key, set())
        for member in display_group_members.get(id(representative), [representative]):
            try:
                member_id = int(member.id or 0)
            except (TypeError, ValueError, OverflowError):
                member_id = 0
            if member_id > 0:
                local_group_key_by_member_id[member_id] = group_key
            member_identity = str(member.identity_id or "").strip()
            if member_identity:
                local_group_identity_ids[group_key].add(member_identity)
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
        display_by_identity: dict[str, list[dict]] = {}
        keys_by_identity: dict[str, set[tuple[int, int]]] = {}
        remote_components_by_identity: dict[str, list[dict[str, object]]] = {}
        pending_remote_component_keys: dict[str, set[tuple[int, int]]] = {}
        for remote_item in remote_items:
            remote_identity = str(remote_item.get("identity_id") or "").strip()
            if remote_identity:
                remote_components_by_identity[remote_identity] = remote_item_components.get(
                    id(remote_item), [remote_item]
                )
        for payload in response_items:
            display = payload.get("chatgpt_display") or {}
            display["billing"] = {
                "scope": "all", "billed_usd": None,
                "source": "codex2api", "status": "error", "fetched_at": None,
            }
            identity_id = str(payload.get("identity_id") or "").strip()
            if not identity_id:
                # Keep a deterministic card key for legacy rows that have not
                # received an AccountIdentityModel yet.  Their hidden pool
                # members are added after binding evidence is evaluated below.
                try:
                    local_id = int(payload.get("id") or 0)
                except (TypeError, ValueError, OverflowError):
                    local_id = 0
                identity_id = local_group_key_by_id.get(
                    local_id,
                    f"account:{local_id}",
                )
            display_by_identity.setdefault(identity_id, []).append(display)
            keys_by_identity.setdefault(identity_id, set())
            for component in remote_components_by_identity.get(identity_id, []):
                try:
                    component_key = (
                        int(component.get("remote_target_id") or 0),
                        int(component.get("remote_id") or 0),
                    )
                except (TypeError, ValueError, OverflowError):
                    continue
                if component_key[0] > 0 and component_key[1] > 0:
                    keys_by_identity[identity_id].add(component_key)
                    pending_remote_component_keys.setdefault(identity_id, set()).add(
                        component_key
                    )
            try:
                key = (int(display.get("target_id") or 0), int(display.get("remote_id") or 0))
            except (TypeError, ValueError):
                continue
            if key[0] <= 0 or key[1] <= 0:
                continue
            if key not in display_by_key:
                billing_keys.append(key)
            display_by_key.setdefault(key, []).append(display)
            keys_by_identity.setdefault(identity_id, set()).add(key)
            if identity_id and identity_id in remote_components_by_identity:
                pending_remote_component_keys.setdefault(identity_id, set()).add(key)
        # One stable identity may have bindings in multiple enterprise pools.
        # Keep each durable snapshot keyed by target/remote, then aggregate the
        # values only for the account card so usage is not shown as two rows.
        identity_ids = set(display_by_identity)
        real_identity_ids = {
            value for value in identity_ids if not value.startswith("account:")
        }
        billing_identity_groups = _safe_identity_groups_for_billing(
            session,
            real_identity_ids,
        )
        for group_key, member_identity_ids in local_group_identity_ids.items():
            if group_key.startswith("account:"):
                billing_identity_groups[group_key] = set(member_identity_ids)
            elif member_identity_ids:
                billing_identity_groups.setdefault(group_key, set()).update(
                    member_identity_ids
                )
        try:
            target_rows = session.exec(select(Codex2APITargetModel)).all()
        except Exception:
            target_rows = []
        target_enabled_by_id: dict[int, bool] = {}
        for target in target_rows:
            try:
                target_id_value = int(target.id)
            except (TypeError, ValueError, OverflowError):
                continue
            if target_id_value > 0:
                target_enabled_by_id[target_id_value] = bool(target.enabled)
        try:
            persisted_billing_keys: set[tuple[int, int]] = set()
            for row in session.exec(select(OperationsBillingSnapshotModel)).all():
                try:
                    key = (int(row.target_id), int(row.remote_id))
                except (TypeError, ValueError, OverflowError):
                    continue
                try:
                    amount = int(row.total_billed_micros)
                except (TypeError, ValueError, OverflowError):
                    continue
                if key[0] > 0 and key[1] > 0 and amount >= 0:
                    persisted_billing_keys.add(key)
        except Exception:
            persisted_billing_keys = set()
        current_binding_keys: set[tuple[int, int]] = set()
        bindings_by_key: dict[tuple[int, int], list[AccountTargetBindingModel]] = {}
        for binding in bindings:
            try:
                binding_key = (int(binding.target_id), int(binding.remote_account_id))
            except (TypeError, ValueError, OverflowError):
                continue
            if binding_key[0] <= 0 or binding_key[1] <= 0:
                continue
            # Keep identityless legacy bindings in the evidence index too.
            # They cannot participate in identity-owner joins, but their
            # disabled/superseded state must still fence a stale snapshot key.
            bindings_by_key.setdefault(binding_key, []).append(binding)
            if _billing_binding_is_current(
                binding,
                target_enabled_by_id,
                inventory_by_key,
                persisted_billing_keys,
            ):
                current_binding_keys.add(binding_key)
        # Compatibility remote cards may describe a locked/unauthorized row
        # whose binding is deliberately disabled.  Do not fetch or aggregate
        # those component keys merely because the remote projection exists;
        # the same evidence fence as local bindings must be satisfied first.
        for card_identity, component_keys in pending_remote_component_keys.items():
            for key in component_keys:
                key_bindings = bindings_by_key.get(key, [])
                if not any(
                    _billing_binding_is_current(
                        binding,
                        target_enabled_by_id,
                        inventory_by_key,
                        persisted_billing_keys,
                    )
                    and str(identity_states.get(str(binding.identity_id or "").strip(), "active")).casefold() != "ambiguous"
                    for binding in key_bindings
                ):
                    continue
                keys_by_identity.setdefault(card_identity, set()).add(key)
                if key not in display_by_key:
                    billing_keys.append(key)
        # A presentation group can contain legacy rows with different (or
        # missing) identity IDs.  Include their keys only after applying the
        # same binding/evidence fence used by the identity-based pass, so a
        # stale hidden row cannot inflate the representative card.
        for representative in visible_accounts:
            try:
                representative_id = int(representative.id or 0)
            except (TypeError, ValueError, OverflowError):
                continue
            group_key = local_group_key_by_id.get(representative_id)
            if not group_key or group_key not in display_by_identity:
                continue
            members = display_group_members.get(id(representative), [representative])
            member_account_ids = {
                int(member.id)
                for member in members
                if member.id is not None and int(member.id) > 0
            }
            member_identity_ids = {
                str(member.identity_id or "").strip()
                for member in members
                if str(member.identity_id or "").strip()
            }
            for member in members:
                key = _account_snapshot_key(member)
                if key is None:
                    continue
                if key in bindings_by_key and key not in current_binding_keys:
                    continue
                keys_by_identity.setdefault(group_key, set()).add(key)
                if key not in display_by_key:
                    billing_keys.append(key)
            for binding in bindings:
                if (
                    str(binding.identity_id or "").strip() not in member_identity_ids
                    and int(binding.local_account_id or 0) not in member_account_ids
                ):
                    continue
                if not _billing_binding_is_current(
                    binding,
                    target_enabled_by_id,
                    inventory_by_key,
                    persisted_billing_keys,
                ):
                    continue
                if str(identity_states.get(str(binding.identity_id or "").strip(), "active")).casefold() == "ambiguous":
                    continue
                try:
                    key = (int(binding.target_id), int(binding.remote_account_id))
                except (TypeError, ValueError):
                    continue
                if key[0] <= 0 or key[1] <= 0:
                    continue
                keys_by_identity.setdefault(group_key, set()).add(key)
                if key not in display_by_key:
                    billing_keys.append(key)
        # The pre-inventory compatibility reader may have hidden a remote row
        # behind a local card above.  Attach those keys to the same display
        # group, subject to the normal binding/evidence fence.
        for local_id, hidden_keys in compatibility_hidden_keys_by_account_id.items():
            group_key = local_group_key_by_member_id.get(local_id)
            if not group_key or group_key not in display_by_identity:
                continue
            for key in hidden_keys:
                key_bindings = bindings_by_key.get(key, [])
                if key_bindings and key not in current_binding_keys:
                    continue
                keys_by_identity.setdefault(group_key, set()).add(key)
                if key not in display_by_key:
                    billing_keys.append(key)
        if identity_ids:
            # A legacy row can retain a remote snapshot even when its binding
            # was never backfilled. Include those snapshot keys as well, or
            # collapsing the duplicate cards would silently drop its charges.
            try:
                all_chatgpt_accounts = session.exec(
                    select(AccountModel).where(AccountModel.platform == "chatgpt")
                ).all()
            except Exception:
                all_chatgpt_accounts = []
            for account in all_chatgpt_accounts:
                account_identity = str(account.identity_id or "").strip()
                if not account_identity:
                    continue
                owning_cards = {
                    card_identity
                    for card_identity, group in billing_identity_groups.items()
                    if account_identity in group
                }
                if not owning_cards:
                    continue
                key = _account_snapshot_key(account)
                if key is not None:
                    has_binding = key in bindings_by_key
                    if key not in current_binding_keys and has_binding:
                        continue
                    for card_identity in owning_cards:
                        keys_by_identity.setdefault(card_identity, set()).add(key)
                    if key not in display_by_key:
                        billing_keys.append(key)
            for binding in bindings:
                identity_id = str(binding.identity_id or "").strip()
                if not _billing_binding_is_current(
                    binding,
                    target_enabled_by_id,
                    inventory_by_key,
                    persisted_billing_keys,
                ):
                    continue
                if str(identity_states.get(identity_id, "active")).casefold() == "ambiguous":
                    continue
                owning_cards = {
                    card_identity
                    for card_identity, group in billing_identity_groups.items()
                    if identity_id in group
                }
                if not owning_cards:
                    continue
                try:
                    key = (int(binding.target_id), int(binding.remote_account_id))
                except (TypeError, ValueError):
                    continue
                if key[0] > 0 and key[1] > 0:
                    for card_identity in owning_cards:
                        keys_by_identity.setdefault(card_identity, set()).add(key)
                    if key not in display_by_key:
                        billing_keys.append(key)
        # Bindings and inventory rows are returned in database order that can
        # differ between SQLite/PostgreSQL. Keep the upstream batch stable and
        # avoid fetching one target/remote pair twice.
        billing_keys = sorted(set(billing_keys))
        if billing_keys:
            try:
                from services.codex_account_billing import fetch_account_billing_summaries

                if snapshot_only:
                    from services.codex_account_billing import read_cached_account_billing_summaries
                    billing = read_cached_account_billing_summaries(session.get_bind(), billing_keys)
                else:
                    billing = fetch_account_billing_summaries(
                        session.get_bind(), billing_keys, refresh=bool(refresh_live),
                    )
                # First preserve the exact-key projection for rows without a
                # stable identity (legacy data), then overwrite identity cards
                # with the sum across all of their target/remote keys.
                for key, displays in display_by_key.items():
                    if key in billing:
                        for display in displays:
                            display["billing"] = dict(billing[key])
                for identity_id, displays in display_by_identity.items():
                    summaries = [
                        billing[key]
                        for key in sorted(keys_by_identity.get(identity_id, set()))
                        if key in billing
                    ]
                    aggregate = _aggregate_billing_summaries(
                        summaries,
                        expected_components=len(
                            keys_by_identity.get(identity_id, set())
                        ),
                    )
                    if aggregate is not None:
                        for display in displays:
                            display["billing"] = dict(aggregate)
            except Exception as exc:
                logger.warning("读取账号累计费用失败: %s", type(exc).__name__)
    return {
        "total": total,
        "summary": operational_summary,
        "source_summary": source_summary,
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
    account_source: Optional[str] = None,
    snapshot_only: bool = False,
    session: Session = Depends(get_session),
):
    return _build_account_list(
        platform=platform, status=status, email=email,
        created_at_start=created_at_start, created_at_end=created_at_end,
        page=page, page_size=page_size, include_live=include_live,
        refresh_live=refresh_live, subscription_plan=subscription_plan,
        operational_status=operational_status, session=session,
        account_source=account_source,
        snapshot_only=snapshot_only,
    )


@router.post("")
def create_account(body: AccountCreate, session: Session = Depends(get_session)):
    existing = session.exec(
        select(AccountModel)
        .where(AccountModel.platform == body.platform)
        .where(func.lower(AccountModel.email) == body.email.lower())
    ).all()
    if any(not _account_is_remote_only_for_display(item) for item in existing):
        raise HTTPException(status_code=409, detail="该平台已存在相同邮箱的本地账号")
    acc = AccountModel(
        platform=body.platform,
        email=body.email,
        password=body.password,
        account_source="local",
        status=body.status,
        token=body.token,
        cashier_url=body.cashier_url,
    )
    session.add(acc)
    session.commit()
    session.refresh(acc)
    try:
        from services.account_identity import ensure_identity_for_model

        ensure_identity_for_model(session.get_bind(), acc)
        session.refresh(acc)
    except Exception as exc:
        logger.warning("新增本地账号的稳定身份投影延后: %s", type(exc).__name__)
    return _account_for_response(acc, session=session)


@router.get("/stats")
def get_stats(session: Session = Depends(get_session)):
    """统计各平台账号数量和状态分布"""
    accounts = session.exec(select(AccountModel)).all()
    # Keep the aggregate counters consistent with the account list: duplicate
    # ChatGPT projections from several pools represent one displayed account.
    chatgpt_accounts = [
        account for account in accounts
        if str(account.platform or "").strip().casefold() == "chatgpt"
    ]
    if len(chatgpt_accounts) > 1:
        displayed_chatgpt = _deduplicate_display_accounts(chatgpt_accounts, session)
        displayed_ids = {id(account) for account in displayed_chatgpt}
        accounts = [
            account for account in accounts
            if str(account.platform or "").strip().casefold() != "chatgpt"
            or id(account) in displayed_ids
        ]
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
    created_accounts: list[AccountModel] = []
    for line in body.lines:
        parts = split_mail_import_fields(str(line or "").strip())
        if len(parts) < 2:
            continue
        email, password = parts[0].strip().lower(), parts[1]
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
        acc = AccountModel(
            platform=body.platform.strip().lower(),
            email=email,
            password=password,
            account_source="local",
            extra_json=extra,
        )
        session.add(acc)
        created_accounts.append(acc)
        created += 1
    session.commit()
    from services.account_identity import ensure_identity_for_model

    for account in created_accounts:
        try:
            session.refresh(account)
            ensure_identity_for_model(session.get_bind(), account)
        except Exception as exc:
            logger.warning("批量导入本地账号的稳定身份投影延后: %s", type(exc).__name__)
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
