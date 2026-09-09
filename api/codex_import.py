"""Project-owned Codex2API-compatible credential import and inventory sync."""

from __future__ import annotations

import json
import hashlib
import threading
from contextlib import ExitStack
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlmodel import Session, select

from core.db import (
    AccountAssignmentModel,
    AccountIdentityModel,
    AccountIdentityAliasModel,
    AccountModel,
    AccountPoolModel,
    AccountTargetBindingModel,
    Codex2APITargetModel,
    PoolTargetPolicyModel,
    get_session,
)
from services.codex_import_parser import ImportFormatError, parse_import_content, parse_import_files
from services.codex_inventory import materialize_inventory, read_inventory, sync_inventory
from services.codex2api_remote_accounts import remote_account_email, remote_bool


router = APIRouter(prefix="/codex-import", tags=["codex-import"])
_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="codex-import")
_LOCK = threading.Lock()


class ImportFile(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    content: str = Field(max_length=20 * 1024 * 1024)


class CodexImportRequest(BaseModel):
    pool_id: str = "PUBLIC_POOL"
    target_id: int | None = None
    format: str = "txt"
    files: list[ImportFile] = Field(min_length=1, max_length=1000)
    purchase_cost_cny: Decimal | None = None
    purchase_batch_key: UUID | None = None

    @field_validator("purchase_cost_cny", mode="before")
    @classmethod
    def validate_purchase_cost(cls, value):
        from services.account_purchase_costs import validate_purchase_cost_cny
        return validate_purchase_cost_cny(value)

    @model_validator(mode="after")
    def validate_purchase_batch(self):
        if self.purchase_cost_cny is not None and self.purchase_batch_key is None:
            raise ValueError("填写购号成本时需要批次请求标识")
        return self


@dataclass
class _ImportJob:
    id: str
    request_fingerprint: str = ""
    status: str = "queued"
    total: int = 0
    processed: int = 0
    success: int = 0
    updated: int = 0
    duplicate: int = 0
    failed: int = 0
    error: str = ""
    items: list[dict[str, Any]] = field(default_factory=list)

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "total": self.total,
            "processed": self.processed,
            "success": self.success,
            "updated": self.updated,
            "duplicate": self.duplicate,
            "failed": self.failed,
            "items": list(self.items[-1000:]),
            "error": self.error or None,
        }


_JOBS: dict[str, _ImportJob] = {}


def _job_get(job_id: str) -> _ImportJob | None:
    with _LOCK:
        return _JOBS.get(str(job_id))


def _target_for_pool(session: Session, pool_id: str, target_id: int | None) -> Codex2APITargetModel | None:
    if target_id is not None:
        target = session.get(Codex2APITargetModel, int(target_id))
        if target is None or not target.enabled:
            raise HTTPException(status_code=409, detail="目标节点不可用")
        belongs_to_pool = str(target.default_pool_id or "") == str(pool_id)
        if not belongs_to_pool:
            belongs_to_pool = session.exec(
                select(PoolTargetPolicyModel)
                .where(PoolTargetPolicyModel.pool_id == str(pool_id))
                .where(PoolTargetPolicyModel.target_id == int(target.id))
                .where(PoolTargetPolicyModel.enabled == True)  # noqa: E712
            ).first() is not None
        if not belongs_to_pool:
            raise HTTPException(status_code=409, detail="目标节点不属于所选号池")
        return target
    policies = session.exec(
        select(PoolTargetPolicyModel)
        .where(PoolTargetPolicyModel.pool_id == pool_id)
        .where(PoolTargetPolicyModel.enabled == True)  # noqa: E712
        .order_by(PoolTargetPolicyModel.priority, PoolTargetPolicyModel.id)
    ).all()
    if policies:
        target = session.get(Codex2APITargetModel, int(policies[0].target_id))
        if target is not None and target.enabled:
            return target
    target = session.exec(
        select(Codex2APITargetModel)
        .where(Codex2APITargetModel.default_pool_id == pool_id)
        .where(Codex2APITargetModel.enabled == True)  # noqa: E712
        .order_by(Codex2APITargetModel.id)
    ).first()
    return target


def _ensure_pool(session: Session, pool_id: str) -> AccountPoolModel:
    normalized = str(pool_id or "PUBLIC_POOL").strip().upper() or "PUBLIC_POOL"
    pool = session.get(AccountPoolModel, normalized)
    if pool is None or not pool.enabled:
        raise HTTPException(status_code=404, detail="号池不存在或已停用")
    return pool


def _credential_payload(row: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "name", "email", "refresh_token", "session_token", "access_token",
        "id_token", "account_id", "chatgpt_account_id", "plan_type", "expires_at",
        "chatgpt_user_id", "agent_runtime_id", "agent_private_key", "agent_task_id",
    )
    return {key: str(row[key]).strip() for key in keys if str(row.get(key) or "").strip()}


_IMPORT_TOKEN_FIELDS = {
    "refresh_token",
    "session_token",
    "access_token",
    "id_token",
}


def _replace_retry_credentials(extra: dict[str, Any], row: Mapping[str, Any]) -> None:
    """Replace credential material without retaining an omitted old token."""

    provided = {
        key
        for key in _IMPORT_TOKEN_FIELDS
        if str(row.get(key) or "").strip()
    }
    # Refresh/session/access imports represent different credential families;
    # an omitted sibling from the new file must not silently survive from a
    # previous failed attempt.  Agent Identity credentials likewise replace
    # the token family entirely.
    if provided:
        if "refresh_token" in provided:
            extra.pop("session_token", None)
            extra.pop("access_token", None)
            extra.pop("id_token", None)
        elif "session_token" in provided:
            extra.pop("refresh_token", None)
            extra.pop("access_token", None)
            extra.pop("id_token", None)
        elif "access_token" in provided:
            extra.pop("refresh_token", None)
            extra.pop("session_token", None)
            extra.pop("id_token", None)
    elif str(row.get("agent_runtime_id") or "").strip():
        for key in _IMPORT_TOKEN_FIELDS:
            extra.pop(key, None)
    for key in _IMPORT_TOKEN_FIELDS | {
        "agent_runtime_id",
        "agent_private_key",
        "agent_task_id",
    }:
        value = str(row.get(key) or "").strip()
        if value:
            extra[key] = row[key]


def _clear_remote_projection_for_pending_import(
    extra: dict[str, Any],
    *,
    target_id: int,
) -> None:
    """Remove an unverified/old target projection before a new upload."""

    old_snapshot = extra.get("codex_remote_snapshot")
    old_target = extra.get("remote_target_id")
    if isinstance(old_snapshot, Mapping) and old_target in (None, ""):
        old_target = old_snapshot.get("target_id")
    try:
        target_changed = int(old_target or 0) != int(target_id)
    except (TypeError, ValueError):
        target_changed = True
    # A retry on the same target also needs a clean projection: the previous
    # remote row may have been disabled or matched ambiguously.
    if old_snapshot or extra.get("codex2api_remote") or extra.get("remote_id"):
        history = extra.get("codex2api_remote_history")
        if not isinstance(history, list):
            history = []
        if isinstance(old_snapshot, Mapping):
            history.append({
                "target_id": old_target,
                "remote_id": old_snapshot.get("remote_id") or extra.get("remote_id"),
                "retired_at": datetime.now(timezone.utc).isoformat(),
                "reason": "pending_import_target_change" if target_changed else "pending_import_retry",
            })
        extra["codex2api_remote_history"] = history[-50:]
        for key in (
            "codex2api_remote",
            "remote_target_id",
            "remote_id",
            "codex_remote_snapshot",
        ):
            extra.pop(key, None)


def _identity_has_strong_alias_conflict(session: Session, identity_id: str) -> bool:
    """Return whether a stable alias is still claimed by another identity."""

    aliases = session.exec(
        select(AccountIdentityAliasModel).where(
            AccountIdentityAliasModel.identity_id == str(identity_id)
        )
    ).all()
    strong_types = {"workspace_id", "chatgpt_account_id", "credential_fingerprint"}
    for alias in aliases:
        if str(alias.alias_type or "") not in strong_types:
            continue
        conflicting = session.exec(
            select(AccountIdentityAliasModel)
            .join(
                AccountIdentityModel,
                AccountIdentityModel.id == AccountIdentityAliasModel.identity_id,
            )
            .where(AccountIdentityModel.platform == "chatgpt")
            .where(AccountIdentityAliasModel.alias_type == alias.alias_type)
            .where(AccountIdentityAliasModel.normalized_value == alias.normalized_value)
            .where(AccountIdentityAliasModel.identity_id != str(identity_id))
        ).first()
        if conflicting is not None:
            return True
    return False


def _identity_and_assignment(
    session: Session,
    account: AccountModel,
    pool: AccountPoolModel,
    target: Codex2APITargetModel | None,
    *,
    initial_state: str = "active",
) -> None:
    identity_id = str(account.identity_id or "").strip()
    identity = session.get(AccountIdentityModel, identity_id) if identity_id else None
    if identity is None or str(identity.platform or "").strip().lower() != "chatgpt":
        # A dangling or cross-platform identity reference must not be reused
        # for a ChatGPT import.  Create a fresh stable row and leave the old
        # row untouched for audit/recovery.
        identity_id = str(uuid4())
        identity = AccountIdentityModel(
            id=identity_id,
            platform="chatgpt",
            canonical_email=str(account.email or "").strip().lower(),
            current_account_id=int(account.id or 0),
        )
        account.identity_id = identity_id
        session.add(identity)
    if target is None:
        return
    from services.account_identity import (
        move_assignments_to_standby,
        supersede_other_target_bindings,
    )

    supersede_other_target_bindings(
        session,
        identity_id=str(account.identity_id or ""),
        current_target_id=int(target.id),
        reason="codex_import_target_changed",
    )
    assignment_states = {
        "active",
        "draining",
        "planned",
        "locking",
        "uploading",
        "pending",
        "verifying",
        "assignment_committing",
        "source_cleaning",
        "target_enabling",
        "migrating",
        "target_disabled",
        "standby",
    }
    assignment_rows = session.exec(
        select(AccountAssignmentModel).where(
            AccountAssignmentModel.identity_id == account.identity_id
        )
    ).all()
    current_rows = [row for row in assignment_rows if row.state in assignment_states]
    def _assignment_stamp(row):
        value = row.updated_at
        if value is None:
            return datetime.min.replace(tzinfo=timezone.utc)
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    current_rows.sort(
        key=lambda row: (
            _assignment_stamp(row),
            int(row.id or 0),
        ),
        reverse=True,
    )
    assignment = current_rows[0] if current_rows else None
    for stale_assignment in current_rows[1:]:
        stale_assignment.state = "superseded"
        stale_assignment.assignment_version = max(
            1, int(stale_assignment.assignment_version or 0)
        ) + 1
        stale_assignment.lease_owner = ""
        stale_assignment.lease_expires_at = None
        stale_assignment.lease_reason = "codex_import_duplicate_assignment"
        stale_assignment.updated_at = datetime.now(timezone.utc)
        session.add(stale_assignment)
    if assignment is None:
        session.add(AccountAssignmentModel(
            identity_id=account.identity_id,
            local_account_id=int(account.id or 0),
            pool_id=str(pool.id),
            target_id=int(target.id),
            state=str(initial_state or "active"),
            lease_reason=(
                "codex_import_pending"
                if str(initial_state or "active") == "standby"
                else "codex_import"
            ),
            lease_started_at=datetime.now(timezone.utc),
            assignment_version=1,
        ))
    else:
        target_changed = int(assignment.target_id or 0) != int(target.id)
        if str(initial_state or "active") == "standby":
            move_assignments_to_standby(
                session,
                identity_id=str(account.identity_id or ""),
                reason="codex_import_pending",
            )
        elif target_changed:
            # Invalidate any migration/lease plan that still carries the old
            # target and CAS version before moving the durable assignment.
            move_assignments_to_standby(
                session,
                identity_id=str(account.identity_id or ""),
                reason="codex_import_target_changed",
            )
            assignment.lease_reason = "codex_import_target_changed"
            assignment.state = str(initial_state or "active")
        assignment.pool_id = str(pool.id)
        assignment.target_id = int(target.id)
        if str(initial_state or "active") == "standby" and not target_changed:
            assignment.state = "standby"
            assignment.lease_reason = "codex_import_pending"
        assignment.updated_at = datetime.now(timezone.utc)
        session.add(assignment)


def _match_remote(client: Any, row: dict[str, Any]) -> dict[str, Any] | None:
    remote, state, _diagnostic = _remote_match_details(client, row)
    return remote if state == "matched" else None


def _positive_remote_id(row: Mapping[str, Any]) -> int:
    """Return the first positive provider ID, rejecting booleans and junk."""

    for key in ("id", "remote_id"):
        value = row.get(key)
        if isinstance(value, bool) or (
            isinstance(value, float) and not value.is_integer()
        ):
            return 0
        try:
            parsed = int(value or 0)
        except (TypeError, ValueError, OverflowError):
            if value not in (None, ""):
                return 0
            continue
        if parsed > 0:
            return parsed
    return 0


def _remote_identity_aliases(row: Mapping[str, Any]) -> set[str]:
    return {
        str(row.get(key) or "").strip().casefold()
        for key in ("chatgpt_account_id", "effective_workspace_id", "account_id")
        if str(row.get(key) or "").strip()
    }


def _remote_match_details(
    client: Any,
    row: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, str, dict[str, Any]]:
    """Resolve exactly one remote row and return safe matching diagnostics.

    ``state`` is one of ``matched``, ``ambiguous``, ``missing``,
    ``invalid_id`` or ``lookup_failed``.  A successful import response alone
    is deliberately insufficient: the provider list must identify one row
    with a positive numeric ID before a local binding can be activated.
    """

    try:
        remote_rows = client.list_accounts()
    except Exception as exc:
        return None, "lookup_failed", {
            "candidate_count": 0,
            "error_code": "remote_list_failed",
            "exception": type(exc).__name__,
        }
    if not isinstance(remote_rows, list):
        return None, "lookup_failed", {
            "candidate_count": 0,
            "error_code": "remote_list_invalid",
            "actual_type": type(remote_rows).__name__,
        }
    if any(not isinstance(item, Mapping) for item in remote_rows):
        return None, "lookup_failed", {
            "candidate_count": 0,
            "error_code": "remote_list_invalid_row",
        }

    candidates = [dict(item) for item in remote_rows if isinstance(item, Mapping)]
    remote_id_counts: dict[int, int] = {}
    for item in candidates:
        remote_id = _positive_remote_id(item)
        if remote_id > 0:
            remote_id_counts[remote_id] = remote_id_counts.get(remote_id, 0) + 1
    duplicate_remote_ids = sorted(
        remote_id for remote_id, count in remote_id_counts.items() if count > 1
    )
    if duplicate_remote_ids:
        return None, "ambiguous", {
            "candidate_count": len(candidates),
            "error_code": "duplicate_remote_ids",
            "candidate_remote_ids": duplicate_remote_ids,
        }
    wanted_id = str(
        row.get("chatgpt_account_id") or row.get("account_id") or ""
    ).strip().casefold()
    wanted_email = str(row.get("email") or "").strip().casefold()

    id_matches = [
        item
        for item in candidates
        if wanted_id and wanted_id in _remote_identity_aliases(item)
    ]
    if wanted_id and len(id_matches) > 1:
        return None, "ambiguous", {
            "candidate_count": len(id_matches),
            "match_basis": "account_id",
            "candidate_remote_ids": [_positive_remote_id(item) for item in id_matches],
        }
    if len(id_matches) == 1:
        matches = id_matches
    else:
        # A legacy row may omit all stable identity aliases.  It is safe to
        # use an exact email only for that alias-less row; a row carrying a
        # different stable ID is an identity mismatch and needs confirmation.
        email_matches = [
            item
            for item in candidates
            if wanted_email
            and remote_account_email(item).strip().casefold() == wanted_email
        ]
        if len(email_matches) > 1:
            return None, "ambiguous", {
                "candidate_count": len(email_matches),
                "match_basis": "email",
                "candidate_remote_ids": [
                    _positive_remote_id(item) for item in email_matches
                ],
            }
        if len(email_matches) == 1 and (
            not wanted_id or not _remote_identity_aliases(email_matches[0])
        ):
            matches = email_matches
        else:
            return None, "missing", {
                "candidate_count": len(email_matches),
                "match_basis": "account_id" if wanted_id else "email",
                "candidate_remote_ids": [
                    _positive_remote_id(item) for item in email_matches
                ],
            }

    remote = matches[0]
    remote_id = _positive_remote_id(remote)
    diagnostic = {
        "candidate_count": 1,
        "match_basis": "account_id" if id_matches else "email",
        "remote_id": remote_id,
    }
    if remote_id <= 0:
        return remote, "invalid_id", diagnostic
    return remote, "matched", diagnostic


def _remote_import_confirmed(result: Any) -> bool:
    """Accept only an explicit successful/updated/duplicate import result."""

    if not isinstance(result, Mapping):
        return False
    try:
        if int(result.get("failed") or 0) > 0:
            return False
    except (TypeError, ValueError):
        return False
    for key in ("success", "updated", "duplicate", "imported"):
        value = result.get(key)
        if isinstance(value, bool) and value:
            return True
        try:
            if int(value or 0) > 0:
                return True
        except (TypeError, ValueError):
            continue
    if result.get("ok") is True:
        return True
    # The Agent Identity endpoint returns ``{message, id, email}`` rather than
    # the counters used by the token import endpoints. A positive remote ID is
    # an explicit creation acknowledgement and is safe to accept.
    for key in ("id", "remote_id"):
        value = result.get(key)
        if isinstance(value, bool):
            continue
        try:
            if int(value or 0) > 0:
                return True
        except (TypeError, ValueError, OverflowError):
            continue
    account_value = str(result.get("account_id") or "").strip()
    if account_value and str(result.get("email") or result.get("name") or "").strip():
        return True
    status = str(result.get("status") or "").strip().lower()
    return status in {"ok", "success", "imported", "updated", "duplicate"}


def _quarantine_import_state(
    session: Session,
    account: AccountModel,
    target: Codex2APITargetModel,
    *,
    sync_status: str,
    reason: str,
    identity_state: str | None = None,
) -> None:
    """Keep a failed import out of scheduling while preserving retry state."""

    now = datetime.now(timezone.utc)
    account.status = "invalid"
    identity_id = str(account.identity_id or "").strip()
    if identity_id:
        identity = session.get(AccountIdentityModel, identity_id)
        if identity is not None and identity_state:
            identity.state = identity_state
            identity.updated_at = now
            session.add(identity)
        bindings = session.exec(
            select(AccountTargetBindingModel)
            .where(AccountTargetBindingModel.identity_id == identity_id)
            .where(AccountTargetBindingModel.target_id == int(target.id))
        ).all()
        for binding in bindings:
            binding.enabled = False
            binding.sync_status = str(sync_status)
            binding.remote_status = "ambiguous" if sync_status == "ambiguous" else "sync_failed"
            binding.last_error = str(reason)[:240]
            binding.updated_at = now
            session.add(binding)
        from services.account_identity import move_assignments_to_standby

        move_assignments_to_standby(
            session,
            identity_id=identity_id,
            target_id=int(target.id),
            reason=f"codex_import_{sync_status}",
        )


def _is_retryable_import_account(
    session: Session,
    account: AccountModel,
    target: Codex2APITargetModel | None,
) -> bool:
    """Return whether a prior Codex2API failure may be retried in place."""
    if target is None or account.id is None:
        return False
    try:
        extra = account.get_extra()
    except (TypeError, ValueError, json.JSONDecodeError):
        extra = {}
    sync = extra.get("codex2api_sync") if isinstance(extra, Mapping) else None
    if isinstance(sync, Mapping) and str(sync.get("status") or "").lower() in {
        "failed",
        "ambiguous",
    }:
        return True
    identity_id = str(account.identity_id or "").strip()
    if not identity_id:
        return False
    binding = session.exec(
        select(AccountTargetBindingModel)
        .where(AccountTargetBindingModel.identity_id == identity_id)
        .where(AccountTargetBindingModel.target_id == int(target.id))
    ).first()
    if binding is None:
        # The same credential may already be active on another Codex2API
        # target.  An explicit import target is a request to establish that
        # target binding, so treating the account as a duplicate here would
        # silently skip the remote upload.
        return True
    sync_status = str(binding.sync_status or "").lower()
    return (
        not binding.enabled
        or sync_status in {"failed", "ambiguous", "remote_missing", "unknown"}
        or sync_status != "synced"
    )


def _credential_identity(row: Mapping[str, Any]) -> str:
    for key in (
        "agent_runtime_id", "chatgpt_account_id", "account_id",
        "refresh_token", "session_token", "access_token",
    ):
        value = str(row.get(key) or "").strip()
        if value:
            return f"{key}:{value}"
    return ""


def _import_lock_keys(request: CodexImportRequest) -> list[str]:
    """Derive stable credential locks before the worker opens its DB session."""

    try:
        files = {item.name: item.content for item in request.files}
        rows = (
            parse_import_files(files)
            if request.format == "auto"
            else [
                row
                for item in request.files
                for row in parse_import_content(item.content, request.format)
            ]
        )
    except Exception:
        rows = []
    keys = sorted(
        {
            _credential_identity(row)
            for row in rows
            if _credential_identity(row)
        }
    )
    if not keys:
        keys = [
            "request:" + hashlib.sha256(request.model_dump_json().encode()).hexdigest()
        ]
    return keys


def _import_job(job: _ImportJob, request: CodexImportRequest, database_engine) -> None:
    """Run one import under deterministic credential-level idempotency locks."""

    from services.chatgpt_account_coordination import codex2api_credential_lock

    with ExitStack() as lock_stack:
        for key in _import_lock_keys(request):
            lock_stack.enter_context(codex2api_credential_lock(key))
        _import_job_impl(job, request, database_engine)


def _import_job_impl(job: _ImportJob, request: CodexImportRequest, database_engine) -> None:
    from services.account_purchase_costs import create_purchase_batch, bind_purchase_slot
    from services.account_identity import move_assignments_to_standby

    try:
        job.status = "running"
        files = {item.name: item.content for item in request.files}
        rows = (
            parse_import_files(files)
            if request.format == "auto"
            else [row for item in request.files for row in parse_import_content(item.content, request.format)]
        )
        job.total = len(rows)
        if not rows:
            raise ImportFormatError("文件中未找到有效的 Refresh Token、Session Token 或 Access Token")
        # The target lock is entered after the selected target is known, so
        # imports for independent enterprise nodes can proceed concurrently
        # while all local lookup, remote mutation, and binding confirmation for
        # one node remain serialized.
        with ExitStack() as import_stack:
            session = import_stack.enter_context(Session(database_engine))
            pool = _ensure_pool(session, request.pool_id)
            target = _target_for_pool(session, str(pool.id), request.target_id)
            from services.chatgpt_account_coordination import codex2api_target_lock

            locked_target_id = int(getattr(target, "id", 0) or 0)
            import_stack.enter_context(
                codex2api_target_lock(locked_target_id)
            )
            # Target selection above opens a short read transaction.  Another
            # job may have committed the same credential while we waited for
            # the lock; refresh the transaction before doing duplicate lookup
            # so the critical section observes that commit.
            session.rollback()
            pool = _ensure_pool(session, request.pool_id)
            target = _target_for_pool(session, str(pool.id), request.target_id)
            refreshed_target_id = int(getattr(target, "id", 0) or 0)
            if refreshed_target_id != locked_target_id:
                # Never acquire target locks in descending order.  A pool
                # policy update can change the selected node while this job
                # waits; rejecting that race lets the stack release the old
                # lock and a retry reselects the current target without an
                # ABBA deadlock between two imports.
                if refreshed_target_id < locked_target_id:
                    raise RuntimeError("导入目标在等待锁期间发生变化，请重试")
                import_stack.enter_context(codex2api_target_lock(refreshed_target_id))
            purchase_batch = None
            if request.purchase_cost_cny is not None:
                purchase_batch = create_purchase_batch(
                    session, int(request.purchase_cost_cny * 100), len(rows),
                    "json_import", str(request.purchase_batch_key),
                )
                # The expense already happened: later target/credential failures
                # must not roll it back or discard unprocessed paid slots.
                session.commit()
            client = None
            if target is not None:
                from services.codex2api_target_client import get_target_client
                client = get_target_client(int(target.id), database_engine)
            seen: set[str] = set()
            for index, row in enumerate(rows, start=1):
                # Token credentials are case-sensitive. Keep their identity
                # byte-for-byte stable so distinct tokens differing only in
                # case are imported as separate accounts.
                identity = _credential_identity(row)
                item_result = {"index": index, "file": request.files[min(index - 1, len(request.files) - 1)].name, "status": "failed"}
                if not identity or identity in seen:
                    job.duplicate += 1
                    job.processed += 1
                    item_result["status"] = "duplicate"
                    job.items.append(item_result)
                    continue
                seen.add(identity)
                email = str(row.get("email") or row.get("name") or f"import-{index}@codex2api.local").strip()
                previous_sync_error_code = ""
                email_candidates = session.exec(
                    select(AccountModel)
                    .where(AccountModel.platform == "chatgpt")
                    .where(AccountModel.email == email)
                ).all()
                existing = None
                candidate_identities: dict[int, str] = {}
                for candidate in email_candidates:
                    try:
                        candidate_extra = candidate.get_extra()
                    except Exception:
                        candidate_extra = {}
                    candidate_identity = _credential_identity(candidate_extra)
                    if candidate.id is not None:
                        candidate_identities[int(candidate.id)] = candidate_identity
                    if candidate_identity == identity:
                        existing = candidate
                        break
                if existing is None and len(email_candidates) == 1:
                    # An old local row with no credential fingerprint can be
                    # safely adopted.  A row carrying a different token must
                    # remain a separate account even when the provider reused
                    # its email; otherwise importing the second credential
                    # silently overwrites the first one.
                    only_candidate = email_candidates[0]
                    if not candidate_identities.get(int(only_candidate.id or 0)):
                        existing = only_candidate
                retry_existing = _is_retryable_import_account(session, existing, target) if existing is not None else False
                if existing is not None and not retry_existing:
                    if purchase_batch is not None:
                        bind_purchase_slot(session, purchase_batch.id, index - 1, existing)
                        session.commit()
                    job.duplicate += 1
                    job.processed += 1
                    item_result.update({"status": "duplicate", "email": email})
                    job.items.append(item_result)
                    continue
                if retry_existing:
                    account = existing
                    try:
                        extra = account.get_extra()
                    except (TypeError, ValueError, json.JSONDecodeError):
                        extra = {}
                    if not isinstance(extra, dict):
                        extra = {}
                    previous_sync = extra.get("codex2api_sync")
                    previous_sync_error_code = (
                        str(previous_sync.get("error_code") or "").strip().lower()
                        if isinstance(previous_sync, Mapping)
                        else ""
                    )
                    # Replace the credential fields while retaining operator
                    # metadata such as purchase-cost links and import history.
                    _replace_retry_credentials(extra, row)
                    if target is not None:
                        _clear_remote_projection_for_pending_import(
                            extra,
                            target_id=int(target.id),
                        )
                    for key, value in row.items():
                        if (
                            key not in {"name", "email", *_IMPORT_TOKEN_FIELDS,
                                        "agent_runtime_id", "agent_private_key", "agent_task_id"}
                            and str(value or "").strip()
                        ):
                            extra[key] = value
                    extra["account_source"] = "project_import"
                    extra["import_format"] = str(request.format)
                    account.email = email
                    account.token = str(row.get("access_token") or "")
                    account.status = "registered"
                else:
                    extra = {key: value for key, value in row.items() if key not in {"name", "email", "access_token"}}
                    extra["account_source"] = "project_import"
                    extra["import_format"] = str(request.format)
                    if row.get("access_token"):
                        extra["access_token"] = row["access_token"]
                    account = AccountModel(
                        platform="chatgpt",
                        email=email,
                        password="",
                        token=str(row.get("access_token") or ""),
                        status="registered",
                        extra_json=json.dumps(extra, ensure_ascii=False),
                    )
                account.set_extra(extra)
                session.add(account)
                session.flush()
                _identity_and_assignment(
                    session,
                    account,
                    pool,
                    target,
                    initial_state="standby" if target is not None else "active",
                )
                if purchase_batch is not None:
                    bind_purchase_slot(session, purchase_batch.id, index - 1, account)
                # Establish a durable pending/quarantined state before making
                # the network call.  A worker crash after this commit leaves
                # no unverified active assignment for the scheduler to lease.
                session.commit()
                if client is not None:
                    payload = _credential_payload({**row, "email": email, "name": email})
                    try:
                        if row.get("agent_runtime_id") and row.get("agent_private_key"):
                            importer = getattr(client, "import_agent_identity", None)
                            remote_result = (
                                importer(payload)
                                if callable(importer)
                                else client.import_full_json(payload)
                            )
                        elif row.get("refresh_token") and row.get("access_token"):
                            remote_result = client.import_full_json(payload)
                        elif row.get("refresh_token") or row.get("session_token"):
                            remote_result = client.import_refresh_token(payload)
                        else:
                            remote_result = client.import_access_token(payload)
                        if not _remote_import_confirmed(remote_result):
                            result_keys = (
                                sorted(str(key) for key in remote_result.keys())
                                if isinstance(remote_result, Mapping)
                                else []
                            )
                            diagnostic = {
                                "candidate_count": 0,
                                "result_keys": result_keys,
                            }
                            reason = "目标节点未确认导入"
                            _quarantine_import_state(
                                session,
                                account,
                                target,
                                sync_status="failed",
                                reason=reason,
                            )
                            extra["codex2api_sync"] = {
                                "status": "failed",
                                "error_code": "remote_import_unconfirmed",
                                "diagnostic": diagnostic,
                            }
                            account.set_extra(extra)
                            session.commit()
                            item_result.update({
                                "status": "failed",
                                "needs_confirmation": False,
                                "sync_status": "failed",
                                "error_code": "remote_import_unconfirmed",
                                "diagnostic": diagnostic,
                                "message": reason,
                                "email": email,
                            })
                            job.failed += 1
                            job.processed += 1
                            job.items.append(item_result)
                            continue

                        remote, match_state, diagnostic = _remote_match_details(client, row)
                        if match_state != "matched" or remote is None:
                            is_ambiguous = match_state in {"ambiguous", "missing"}
                            sync_status = "ambiguous" if is_ambiguous else "failed"
                            error_code = {
                                "ambiguous": "remote_identity_ambiguous",
                                "missing": "remote_identity_not_found",
                                "invalid_id": "remote_id_missing",
                                "lookup_failed": "remote_list_failed",
                            }.get(match_state, "remote_match_failed")
                            reason = {
                                "ambiguous": "目标节点账号身份不唯一，等待确认",
                                "missing": "目标节点未找到唯一匹配账号",
                                "invalid_id": "目标节点账号缺少有效远端 ID",
                                "lookup_failed": "读取目标节点账号清单失败",
                            }.get(match_state, "目标节点账号匹配失败")
                            _quarantine_import_state(
                                session,
                                account,
                                target,
                                sync_status=sync_status,
                                reason=reason,
                                identity_state="ambiguous" if is_ambiguous else None,
                            )
                            extra["codex2api_sync"] = {
                                "status": sync_status,
                                "error_code": error_code,
                                "diagnostic": diagnostic,
                            }
                            account.set_extra(extra)
                            session.commit()
                            item_result.update({
                                "status": "needs_confirmation" if is_ambiguous else "failed",
                                "needs_confirmation": is_ambiguous,
                                "sync_status": sync_status,
                                "error_code": error_code,
                                "diagnostic": diagnostic,
                                "message": reason,
                                "email": email,
                            })
                            job.failed += 1
                            job.processed += 1
                            job.items.append(item_result)
                            continue

                        remote_id = _positive_remote_id(remote)
                        summary = {
                            key: value
                            for key, value in remote.items()
                            if key not in {
                                "credentials", "refresh_token", "access_token",
                                "session_token", "id_token", "password", "cookies",
                            }
                        }
                        extra["codex2api_remote"] = {
                            "target_id": int(target.id),
                            "remote_id": remote_id,
                            "summary": summary,
                        }
                        extra["remote_target_id"] = int(target.id)
                        extra["remote_id"] = remote_id
                        extra["codex_remote_snapshot"] = {
                            **summary,
                            "target_id": int(target.id),
                            "remote_id": remote_id,
                        }
                        extra["codex2api_sync"] = {
                            "status": "synced",
                            "error_code": "",
                            "diagnostic": diagnostic,
                        }
                        account.set_extra(extra)

                        # Reuse the identity/target row when a retry or an
                        # earlier reconciliation already created it.  Free a
                        # conflicting remote slot before claiming the unique
                        # (target_id, remote_account_id) key.
                        binding = session.exec(
                            select(AccountTargetBindingModel)
                            .where(AccountTargetBindingModel.identity_id == account.identity_id)
                            .where(AccountTargetBindingModel.target_id == int(target.id))
                        ).first()
                        conflicting = session.exec(
                            select(AccountTargetBindingModel)
                            .where(AccountTargetBindingModel.target_id == int(target.id))
                            .where(AccountTargetBindingModel.remote_account_id == remote_id)
                        ).first()
                        if conflicting is not None and (
                            binding is None
                            or int(conflicting.id or 0) != int(binding.id or 0)
                        ):
                            conflicting_identity_id = str(
                                conflicting.identity_id or ""
                            ).strip()
                            conflicting.remote_account_id = 0
                            conflicting.remote_email = ""
                            conflicting.enabled = False
                            conflicting.sync_status = "superseded"
                            conflicting.remote_status = "superseded"
                            conflicting.last_error = "远端账号身份已转移到当前导入账号"
                            conflicting.updated_at = datetime.now(timezone.utc)
                            session.add(conflicting)
                            session.flush()
                            if conflicting_identity_id:
                                # The old owner must leave the active pool at
                                # the same time its remote slot is released;
                                # otherwise the scheduler can keep leasing an
                                # account whose binding is now superseded.
                                move_assignments_to_standby(
                                    session,
                                    identity_id=conflicting_identity_id,
                                    target_id=int(target.id),
                                    reason="codex_import_remote_binding_superseded",
                                )
                        if binding is None:
                            binding = AccountTargetBindingModel(
                                identity_id=account.identity_id,
                                local_account_id=int(account.id or 0),
                                target_id=int(target.id),
                            )
                        binding.local_account_id = int(account.id or 0)
                        binding.remote_account_id = remote_id
                        binding.remote_email = str(
                            remote.get("email") or remote.get("name") or email
                        ).strip().lower()
                        binding.sync_status = "synced"
                        binding.remote_status = str(
                            remote.get("remote_status") or remote.get("status") or ""
                        )
                        binding.enabled = remote_bool(remote.get("enabled"), True) and not remote_bool(
                            remote.get("locked"), False
                        )
                        binding.last_sync_at = datetime.now(timezone.utc)
                        binding.last_error = ""
                        binding.updated_at = datetime.now(timezone.utc)
                        session.add(binding)
                        identity_row = session.get(
                            AccountIdentityModel,
                            str(account.identity_id or ""),
                        )
                        if (
                            identity_row is not None
                            and identity_row.state == "ambiguous"
                            and not (
                                previous_sync_error_code
                                in {"remote_identity_ambiguous", "remote_identity_not_found"}
                                and not _identity_has_strong_alias_conflict(
                                    session,
                                    str(account.identity_id or ""),
                                )
                            )
                        ):
                            binding.enabled = False
                            binding.sync_status = "ambiguous"
                            binding.remote_status = "ambiguous"
                            binding.last_error = "身份存在歧义，等待人工确认"
                            _clear_remote_projection_for_pending_import(
                                extra,
                                target_id=int(target.id),
                            )
                            extra["codex2api_sync"] = {
                                "status": "ambiguous",
                                "error_code": "local_identity_ambiguous",
                                "diagnostic": diagnostic,
                            }
                            account.set_extra(extra)
                            move_assignments_to_standby(
                                session,
                                identity_id=str(account.identity_id or ""),
                                target_id=int(target.id),
                                reason="codex_import_identity_ambiguous",
                            )
                            session.add(binding)
                            session.commit()
                            item_result.update({
                                "status": "needs_confirmation",
                                "needs_confirmation": True,
                                "sync_status": "ambiguous",
                                "error_code": "local_identity_ambiguous",
                                "diagnostic": diagnostic,
                                "message": "本地账号身份存在歧义，等待确认",
                                "email": email,
                            })
                            job.failed += 1
                            job.processed += 1
                            job.items.append(item_result)
                            continue
                        if identity_row is not None:
                            identity_row.state = "active"
                            identity_row.current_account_id = int(account.id or 0)
                            identity_row.updated_at = datetime.now(timezone.utc)
                            session.add(identity_row)
                        if binding.enabled:
                            assignment = session.exec(
                                select(AccountAssignmentModel)
                                .where(AccountAssignmentModel.identity_id == str(account.identity_id or ""))
                                .where(AccountAssignmentModel.target_id == int(target.id))
                            ).first()
                            if assignment is not None and assignment.state == "standby":
                                assignment.state = "active"
                                assignment.assignment_version = max(1, int(assignment.assignment_version or 0)) + 1
                                assignment.lease_reason = "codex_import_synced"
                                assignment.updated_at = datetime.now(timezone.utc)
                                session.add(assignment)
                        else:
                            move_assignments_to_standby(
                                session,
                                identity_id=str(account.identity_id or ""),
                                target_id=int(target.id),
                                reason="codex_import_remote_disabled",
                            )
                        # Commit the verified binding and assignment before
                        # reporting this item as successful.  A process exit
                        # between the remote response and the job's final
                        # commit must not leave an unverified local lease.
                        session.commit()
                        item_result.update({
                            "remote_id": remote_id,
                            "sync_status": "synced",
                            "diagnostic": diagnostic,
                        })
                    except Exception as exc:
                        reason = f"目标同步失败（{type(exc).__name__}）"
                        _quarantine_import_state(
                            session,
                            account,
                            target,
                            sync_status="failed",
                            reason=reason,
                        )
                        extra["codex2api_sync"] = {
                            "status": "failed",
                            "error_code": "remote_sync_exception",
                            "diagnostic": {"exception": type(exc).__name__},
                        }
                        account.set_extra(extra)
                        session.commit()
                        item_result.update({
                            "status": "failed",
                            "needs_confirmation": False,
                            "sync_status": "failed",
                            "error_code": "remote_sync_exception",
                            "diagnostic": {"exception": type(exc).__name__},
                            "message": reason,
                        })
                        job.failed += 1
                        job.processed += 1
                        item_result["email"] = email
                        job.items.append(item_result)
                        continue
                job.success += 1
                job.processed += 1
                item_result.update({
                    "status": "success",
                    "email": email,
                    "needs_confirmation": False,
                })
                job.items.append(item_result)
            session.commit()
        job.status = "completed"
    except Exception as exc:
        job.status = "failed"
        job.error = str(exc)[:240]


@router.get("/options")
def import_options(session: Session = Depends(get_session)):
    from services.pool_scheduler import ensure_default_pools

    ensure_default_pools(session.get_bind())
    pools = session.exec(select(AccountPoolModel).where(AccountPoolModel.enabled == True)).all()  # noqa: E712
    pools.sort(key=lambda pool: (0 if str(pool.id) == "PUBLIC_POOL" else 1, str(pool.id)))
    targets = session.exec(select(Codex2APITargetModel).where(Codex2APITargetModel.enabled == True).order_by(Codex2APITargetModel.id)).all()  # noqa: E712
    policies = session.exec(select(PoolTargetPolicyModel).where(PoolTargetPolicyModel.enabled == True)).all()  # noqa: E712
    return {
        "default_pool_id": "PUBLIC_POOL",
        "pools": [{
            "id": pool.id,
            "name": pool.name,
            "targets": [
                {"id": int(target.id), "name": target.name, "enabled": bool(target.enabled)}
                for target in targets
                if int(target.id) in {
                    int(policy.target_id) for policy in policies if str(policy.pool_id) == str(pool.id)
                } or str(target.default_pool_id) == str(pool.id)
            ],
        } for pool in pools],
    }


@router.post("", status_code=status.HTTP_202_ACCEPTED)
def start_import(body: CodexImportRequest, session: Session = Depends(get_session)):
    from services.pool_scheduler import ensure_default_pools

    ensure_default_pools(session.get_bind())
    pool = _ensure_pool(session, body.pool_id)
    _target_for_pool(session, str(pool.id), body.target_id)
    job_id = f"codex-import-{body.purchase_batch_key.hex}" if body.purchase_cost_cny is not None else f"codex-import-{uuid4().hex}"
    fingerprint = hashlib.sha256(body.model_dump_json().encode()).hexdigest()
    with _LOCK:
        existing = _JOBS.get(job_id)
        if existing is not None:
            if existing.request_fingerprint != fingerprint:
                raise HTTPException(409, "购号请求标识已用于其他导入内容")
            return {"job_id": existing.id, "status": existing.status}
        job = _ImportJob(id=job_id, request_fingerprint=fingerprint)
        _JOBS[job.id] = job
    _EXECUTOR.submit(_import_job, job, body, session.get_bind())
    return {"job_id": job.id, "status": job.status}


@router.get("/jobs/{job_id}")
def get_import_job(job_id: str):
    job = _job_get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="导入任务不存在")
    return job.public()


@router.post("/sync")
def sync_import_inventory(session: Session = Depends(get_session)):
    # First capture the complete account inventory so remote-only rows have a
    # local identity/binding before the usage probe writes quota snapshots.
    result = sync_inventory(session.get_bind(), refresh=False)
    materialized = materialize_inventory(session.get_bind())
    quota_results: list[dict[str, Any]] = []
    try:
        from services.control_plane_workers import collect_target_quota
        from services.codex2api_target_client import get_target_client

        targets = session.exec(
            select(Codex2APITargetModel)
            .where(Codex2APITargetModel.enabled == True)  # noqa: E712
            .order_by(Codex2APITargetModel.id)
        ).all()
        for target in targets:
            try:
                client = get_target_client(int(target.id), session.get_bind())
                quota = collect_target_quota(
                    session.get_bind(), target_id=int(target.id), client=client,
                )
                quota_results.append({"target_id": quota.target_id, "collected_accounts": quota.collected_accounts})
            except Exception as exc:
                quota_results.append({"target_id": int(target.id), "error": type(exc).__name__})
    except Exception as exc:
        quota_results.append({"error": type(exc).__name__})
    result_after_quota = sync_inventory(session.get_bind(), refresh=False)
    materialized_after_quota = materialize_inventory(session.get_bind())
    return {
        "status": "completed",
        "inventory": result_after_quota,
        "initial_inventory": result,
        "materialized": materialized_after_quota,
        "initial_materialized": materialized,
        "quota": quota_results,
    }


__all__ = ["router"]
