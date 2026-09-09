"""Background target-health and quota collection jobs."""

from __future__ import annotations

import json
import time
from collections import Counter
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from sqlmodel import Session, select

from core.db import (
    AccountAssignmentEventModel,
    AccountAssignmentModel,
    AccountModel,
    AccountPoolModel,
    AccountTargetBindingModel,
    AccountIdentityModel,
    Codex2APITargetModel,
    CustomerUsageSampleModel,
    PoolTargetPolicyModel,
    engine as default_engine,
)
from services.quota_ledger import merge_remote_rows
from services.codex2api_remote_accounts import (
    remote_account_email,
    remote_account_id,
    remote_account_is_schedulable,
    remote_bool,
    remote_identity_id,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime:
    result = value or _utcnow()
    if result.tzinfo is None:
        return result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


@dataclass(frozen=True)
class TargetHealthResult:
    target_id: int
    health_status: str
    health_success_count: int
    health_failure_count: int
    capabilities: dict[str, Any]
    last_error: str


@dataclass(frozen=True)
class TargetQuotaResult:
    target_id: int
    remote_accounts: int
    collected_accounts: int
    missing_accounts: int
    ambiguous_accounts: int


def _client_for(target_id: int, database_engine, client: Any | None):
    if client is not None:
        return client
    from services.codex2api_target_client import get_target_client

    return get_target_client(target_id, database_engine)


def collect_target_health(
    database_engine=None,
    *,
    target_id: int,
    client: Any | None = None,
    now: datetime | None = None,
) -> TargetHealthResult:
    """Probe one target under the shared target-state lock."""

    from services.chatgpt_account_coordination import codex2api_target_lock

    with codex2api_target_lock(target_id):
        return _collect_target_health_impl(
            database_engine,
            target_id=target_id,
            client=client,
            now=now,
        )


def _collect_target_health_impl(
    database_engine=None,
    *,
    target_id: int,
    client: Any | None = None,
    now: datetime | None = None,
) -> TargetHealthResult:
    """Probe one target and persist the two-success/two-failure gate."""

    target_engine = database_engine or default_engine
    checked_at = _aware(now)
    with Session(target_engine) as session:
        target = session.get(Codex2APITargetModel, int(target_id))
        if target is None:
            raise ValueError("Codex2API target does not exist")
        if not target.enabled:
            return TargetHealthResult(
                target_id=int(target.id),
                health_status="disabled",
                health_success_count=int(target.health_success_count or 0),
                health_failure_count=int(target.health_failure_count or 0),
                capabilities={},
                last_error="",
            )
    resolved_client = _client_for(int(target_id), target_engine, client)
    capabilities: dict[str, Any] = {}
    error = ""
    succeeded = False
    try:
        resolved_client.health()
        raw_capabilities = resolved_client.capabilities()
        capabilities = (
            dict(raw_capabilities)
            if isinstance(raw_capabilities, Mapping)
            else {}
        )
        succeeded = True
    except Exception as exc:
        # Client diagnostics are already redacted.  Persist only the exception
        # class so a provider response can never become a secret-bearing row.
        error = f"target probe failed ({type(exc).__name__})"

    with Session(target_engine) as session:
        target = session.get(Codex2APITargetModel, int(target_id))
        if target is None:
            raise ValueError("Codex2API target does not exist")
        if succeeded:
            target.health_success_count = int(target.health_success_count or 0) + 1
            target.health_failure_count = 0
            target.health_status = (
                "healthy" if target.health_success_count >= 2 else "recovering"
            )
            target.capability_json = json.dumps(
                capabilities,
                ensure_ascii=False,
                sort_keys=True,
            )
            target.last_error = ""
        else:
            target.health_failure_count = int(target.health_failure_count or 0) + 1
            target.health_success_count = 0
            target.health_status = (
                "unreachable" if target.health_failure_count >= 2 else "degraded"
            )
            target.last_error = error
        target.last_health_at = checked_at
        target.updated_at = checked_at
        session.add(target)
        session.commit()
        session.refresh(target)
        try:
            stored_capabilities = json.loads(target.capability_json or "{}")
        except (TypeError, ValueError):
            stored_capabilities = {}
        return TargetHealthResult(
            target_id=int(target.id),
            health_status=str(target.health_status),
            health_success_count=int(target.health_success_count or 0),
            health_failure_count=int(target.health_failure_count or 0),
            capabilities=stored_capabilities,
            last_error=str(target.last_error or ""),
        )


def _remote_id(row: Mapping[str, Any]) -> int:
    raw = row.get("id") or row.get("remote_id") or 0
    if isinstance(raw, bool) or (isinstance(raw, float) and not raw.is_integer()):
        return 0
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 0
    return value if value > 0 else 0


_STABLE_ALIAS_KEYS = {
    "workspace_id",
    "effective_workspace_id",
    "chatgpt_account_id",
    "account_id",
    "user_id",
}


def _stable_aliases(value: Any) -> set[str]:
    if isinstance(value, AccountModel):
        try:
            extra = value.get_extra()
        except Exception:
            extra = {}
        source = dict(extra) if isinstance(extra, Mapping) else {}
        if str(value.user_id or "").strip():
            source["user_id"] = value.user_id
    elif isinstance(value, Mapping):
        source = value
        nested = value.get("credentials")
        if isinstance(nested, Mapping):
            source = {**nested, **value}
    else:
        source = {}
    return {
        str(source.get(key) or "").strip().casefold()
        for key in _STABLE_ALIAS_KEYS
        if str(source.get(key) or "").strip()
    }


def _remote_row_matches_binding(
    item: Mapping[str, Any],
    row: Mapping[str, Any],
    account: AccountModel | None,
) -> bool:
    expected_email = str(item.get("remote_email") or "").strip().casefold()
    actual_email = remote_account_email(row).strip().casefold()
    local_aliases = _stable_aliases(account) if account is not None else set()
    remote_aliases = _stable_aliases(row)
    if expected_email:
        if actual_email and expected_email != actual_email and not (
            local_aliases
            and remote_aliases
            and local_aliases.intersection(remote_aliases)
        ):
            return False
        if not actual_email and not (
            local_aliases
            and remote_aliases
            and local_aliases.intersection(remote_aliases)
        ):
            return False
    return not (local_aliases and remote_aliases and not local_aliases.intersection(remote_aliases))


def _binding_data(database_engine, target_id: int) -> list[dict[str, Any]]:
    with Session(database_engine) as session:
        assignment_state_by_identity = {}
        assignments = session.exec(select(AccountAssignmentModel)).all()
        assignments.sort(
            key=lambda assignment: (
                _aware(assignment.updated_at),
                int(assignment.id or 0),
            ),
            reverse=True,
        )
        for assignment in assignments:
            identity_key = str(assignment.identity_id or "")
            if not identity_key:
                continue
            assignment_state_by_identity.setdefault(
                identity_key,
                (str(assignment.state or "").lower(), int(assignment.target_id or 0)),
            )
        bindings = session.exec(
            select(AccountTargetBindingModel).where(
                AccountTargetBindingModel.target_id == int(target_id)
            )
            .where(AccountTargetBindingModel.enabled == True)  # noqa: E712
        ).all()
        bindings = [
            binding
            for binding in bindings
            if (
                (
                    str(binding.identity_id) not in assignment_state_by_identity
                    and int(binding.local_account_id or 0) > 0
                )
                or (
                    assignment_state_by_identity.get(str(binding.identity_id), ("", 0))[0]
                    in {"active", "draining", "standby"}
                    and assignment_state_by_identity.get(str(binding.identity_id), ("", 0))[1]
                    == int(target_id)
                )
            )
        ]
        return [
            {
                "id": int(binding.id or 0),
                "identity_id": str(binding.identity_id),
                "local_account_id": int(binding.local_account_id),
                "remote_account_id": int(binding.remote_account_id or 0),
                "remote_email": str(binding.remote_email or ""),
            }
            for binding in bindings
        ]


def reconcile_target_bindings(
    database_engine,
    *,
    target_id: int,
    rows: list[dict[str, Any]],
    now: datetime,
    include_remote_only: bool = False,
    ambiguity_keys: set[tuple[str, int]] | None = None,
) -> int:
    """Bootstrap bindings under the same per-target reconciliation fence."""

    from services.chatgpt_account_coordination import codex2api_target_lock

    with codex2api_target_lock(target_id):
        return _reconcile_target_bindings_impl(
            database_engine,
            target_id=target_id,
            rows=rows,
            now=now,
            include_remote_only=include_remote_only,
            ambiguity_keys=ambiguity_keys,
        )


def _reconcile_target_bindings_impl(
    database_engine,
    *,
    target_id: int,
    rows: list[dict[str, Any]],
    now: datetime,
    include_remote_only: bool = False,
    ambiguity_keys: set[tuple[str, int]] | None = None,
) -> int:
    """Bootstrap local binding/assignment rows from a target account list."""

    from services.account_identity import (
        move_assignments_to_standby,
        supersede_other_target_bindings,
    )

    def mark_ambiguity(identity_id: Any, remote_id: Any) -> None:
        if ambiguity_keys is None:
            return
        identity_key = str(identity_id or "").strip()
        remote_key = _remote_id({"id": remote_id})
        if identity_key and remote_key > 0:
            ambiguity_keys.add((identity_key, remote_key))

    with Session(database_engine) as session:
        target = session.get(Codex2APITargetModel, int(target_id))
        if target is None:
            return 0
        accounts = session.exec(
            select(AccountModel).where(
                AccountModel.platform == "chatgpt",
                AccountModel.identity_id != "",
            )
        ).all()
        local_by_email: dict[str, list[AccountModel]] = {}
        for account in accounts:
            local_by_email.setdefault(str(account.email or "").strip().lower(), []).append(account)
        remote_by_email: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            email = remote_account_email(row).strip().lower()
            if email:
                remote_by_email.setdefault(email, []).append(row)
        created = 0
        for email, local_matches in local_by_email.items():
            remote_matches = remote_by_email.get(email, [])
            if len(local_matches) != 1 or len(remote_matches) != 1:
                # Preserve the concrete rows involved in an email collision
                # for the quota result.  The normal reconcile path skips these
                # pairs because it cannot choose a safe identity.
                if remote_matches and (
                    len(local_matches) > 1 or len(remote_matches) > 1
                ):
                    for account in local_matches:
                        for remote in remote_matches:
                            mark_ambiguity(
                                getattr(account, "identity_id", ""),
                                _remote_id(remote),
                            )
                continue
            account = local_matches[0]
            remote = remote_matches[0]
            remote_id = _remote_id(remote)
            if remote_id <= 0:
                continue
            identity = session.get(AccountIdentityModel, str(account.identity_id or ""))
            identity_ambiguous = identity is not None and str(identity.state or "") == "ambiguous"
            local_aliases = _stable_aliases(account)
            remote_aliases = _stable_aliases(remote)
            if local_aliases and remote_aliases and not local_aliases.intersection(remote_aliases):
                mark_ambiguity(account.identity_id, remote_id)
                if identity is not None:
                    identity.state = "ambiguous"
                    identity.updated_at = now
                    session.add(identity)
                move_assignments_to_standby(
                    session,
                    identity_id=str(account.identity_id or ""),
                    reason="remote_identity_alias_mismatch",
                )
                mismatched_binding = session.exec(
                    select(AccountTargetBindingModel)
                    .where(AccountTargetBindingModel.identity_id == str(account.identity_id or ""))
                    .where(AccountTargetBindingModel.target_id == int(target_id))
                ).first()
                if mismatched_binding is not None:
                    mismatched_binding.enabled = False
                    mismatched_binding.sync_status = "ambiguous"
                    mismatched_binding.remote_status = "ambiguous"
                    mismatched_binding.last_error = "本地与远端稳定身份不一致"
                    mismatched_binding.updated_at = now
                    session.add(mismatched_binding)
                continue
            target_enabled = bool(target.enabled)
            remote_schedulable = target_enabled and remote_account_is_schedulable(remote)
            binding = session.exec(
                select(AccountTargetBindingModel)
                .where(AccountTargetBindingModel.identity_id == account.identity_id)
                .where(AccountTargetBindingModel.target_id == int(target_id))
            ).first()
            if binding is None:
                binding = AccountTargetBindingModel(
                    identity_id=str(account.identity_id),
                    local_account_id=int(account.id or 0),
                    target_id=int(target_id),
                    remote_account_id=remote_id,
                    remote_email=email,
                    sync_status="synced",
                    remote_status=str(remote.get("remote_status") or remote.get("status") or ""),
                    enabled=remote_schedulable,
                    last_sync_at=now,
                    created_at=now,
                    updated_at=now,
                )
                session.add(binding)
                created += 1
            else:
                binding.local_account_id = int(account.id or 0)
                binding.remote_account_id = remote_id
                binding.remote_email = email
                binding.remote_status = str(remote.get("remote_status") or remote.get("status") or "")
                binding.enabled = remote_schedulable
                binding.sync_status = "synced"
                binding.last_sync_at = now
                binding.last_error = ""
                binding.updated_at = now
            if not target_enabled:
                binding.sync_status = "target_disabled"
                binding.remote_status = "target_disabled"
                binding.last_error = "目标节点已停用"
            if identity_ambiguous:
                mark_ambiguity(account.identity_id, remote_id)
                binding.enabled = False
                binding.sync_status = "ambiguous"
                binding.remote_status = "ambiguous"
                binding.last_error = "身份存在歧义，等待人工确认"
            session.add(binding)
            supersede_other_target_bindings(
                session,
                identity_id=str(account.identity_id or ""),
                current_target_id=int(target_id),
                reason="target_binding_reconciled",
            )
            if identity_ambiguous:
                move_assignments_to_standby(
                    session,
                    identity_id=str(account.identity_id or ""),
                    reason="identity_ambiguous",
                )
                continue
            if not binding.enabled:
                move_assignments_to_standby(
                    session,
                    identity_id=str(account.identity_id or ""),
                    target_id=int(target_id),
                    reason="remote_account_not_schedulable",
                )
                continue
            assignment = session.exec(
                select(AccountAssignmentModel).where(
                    AccountAssignmentModel.identity_id == account.identity_id,
                    AccountAssignmentModel.state.in_(
                        [
                            "active", "draining", "planned", "locking", "uploading",
                            "pending", "verifying", "assignment_committing",
                            "source_cleaning", "target_enabling", "migrating",
                            "target_disabled", "standby",
                        ]
                    ),
                )
            ).first()
            if assignment is None:
                assignment = AccountAssignmentModel(
                    identity_id=str(account.identity_id),
                    local_account_id=int(account.id or 0),
                    pool_id=str(target.default_pool_id or "PUBLIC_POOL"),
                    target_id=int(target_id),
                    state="active",
                    lease_reason="initial_target_reconcile",
                    lease_started_at=now,
                    assignment_version=1,
                    created_at=now,
                    updated_at=now,
                )
                session.add(assignment)
                session.add(
                    AccountAssignmentEventModel(
                        identity_id=str(account.identity_id),
                        local_account_id=int(account.id or 0),
                        event_type="initial_assignment",
                        to_pool_id=assignment.pool_id,
                        to_target_id=int(target_id),
                        assignment_version=1,
                        reason="initial_target_reconcile",
                        created_at=now,
                    )
                )
            elif assignment.state != "active" or int(assignment.target_id or 0) != int(target_id):
                previous_target = int(assignment.target_id or 0)
                previous_version = int(assignment.assignment_version or 0)
                move_assignments_to_standby(
                    session,
                    identity_id=str(account.identity_id or ""),
                    reason="target_binding_reconciled",
                )
                assignment.state = "active"
                assignment.target_id = int(target_id)
                assignment.assignment_version = max(1, previous_version) + 1
                assignment.lease_owner = ""
                assignment.lease_expires_at = None
                assignment.lease_reason = "target_binding_reconciled"
                assignment.updated_at = now
                session.add(assignment)
                session.add(
                    AccountAssignmentEventModel(
                        identity_id=str(account.identity_id),
                        local_account_id=int(account.id or 0),
                        event_type="target_binding_reconciled",
                        from_target_id=previous_target,
                        to_pool_id=assignment.pool_id,
                        to_target_id=int(target_id),
                        assignment_version=int(assignment.assignment_version or 0),
                        reason="target_binding_reconciled",
                        created_at=now,
                    )
                )

        # JSON-imported accounts can exist only on the Codex2API node. Keep a
        # credential-free identity/binding for each such row so it can be
        # displayed and scheduled by the control plane. Background quota
        # collection leaves this opt-in off so a missing local account does
        # not unexpectedly create a new control-plane asset.
        for remote in rows if include_remote_only else []:
            remote_id = remote_account_id(remote)
            email = remote_account_email(remote).strip().lower()
            if remote_id <= 0:
                continue
            identity_email = email or f"remote:{int(target_id)}:{remote_id}"
            local_matches = local_by_email.get(email, []) if email else []
            if len(local_matches) == 1 and str(local_matches[0].identity_id or "").strip():
                continue
            existing_remote_binding = session.exec(
                select(AccountTargetBindingModel)
                .where(AccountTargetBindingModel.target_id == int(target_id))
                .where(AccountTargetBindingModel.remote_account_id == remote_id)
            ).first()
            if existing_remote_binding is not None:
                bound_account = (
                    session.get(AccountModel, int(existing_remote_binding.local_account_id))
                    if int(existing_remote_binding.local_account_id or 0) > 0
                    else None
                )
                if bound_account is not None:
                    continue
                identity_key = str(existing_remote_binding.identity_id)
            else:
                identity_key = remote_identity_id(int(target_id), remote_id)
            identity = session.get(AccountIdentityModel, identity_key)
            identity_ambiguous = identity is not None and str(identity.state or "") == "ambiguous"
            if identity is None:
                identity = AccountIdentityModel(
                    id=identity_key,
                    platform="chatgpt",
                    canonical_email=identity_email,
                    state="active",
                    current_account_id=0,
                    created_at=now,
                    updated_at=now,
                )
            else:
                identity.canonical_email = identity_email
                identity.platform = "chatgpt"
                identity.current_account_id = 0
                if not identity_ambiguous:
                    identity.state = "active"
                identity.updated_at = now
            session.add(identity)

            target_enabled = bool(target.enabled)
            binding = session.exec(
                select(AccountTargetBindingModel)
                .where(AccountTargetBindingModel.identity_id == identity_key)
                .where(AccountTargetBindingModel.target_id == int(target_id))
            ).first()
            if binding is None:
                binding = AccountTargetBindingModel(
                    identity_id=identity_key,
                    local_account_id=0,
                    target_id=int(target_id),
                    remote_account_id=remote_id,
                    remote_email=identity_email,
                    sync_status="synced",
                    remote_status=str(remote.get("remote_status") or remote.get("status") or ""),
                    enabled=target_enabled and remote_account_is_schedulable(remote),
                    last_sync_at=now,
                    created_at=now,
                    updated_at=now,
                )
            else:
                binding.local_account_id = 0
                binding.remote_account_id = remote_id
                binding.remote_email = identity_email
                binding.remote_status = str(remote.get("remote_status") or remote.get("status") or "")
                binding.enabled = target_enabled and remote_account_is_schedulable(remote)
                binding.sync_status = "synced"
                binding.last_sync_at = now
                binding.last_error = ""
                binding.updated_at = now
            if not target_enabled:
                binding.sync_status = "target_disabled"
                binding.remote_status = "target_disabled"
                binding.last_error = "目标节点已停用"
            if identity_ambiguous:
                mark_ambiguity(identity_key, remote_id)
                binding.enabled = False
                binding.sync_status = "ambiguous"
                binding.remote_status = "ambiguous"
                binding.last_error = "身份存在歧义，等待人工确认"
            session.add(binding)
            supersede_other_target_bindings(
                session,
                identity_id=identity_key,
                current_target_id=int(target_id),
                reason="target_binding_reconciled",
            )

            assignment = session.exec(
                select(AccountAssignmentModel)
                .where(AccountAssignmentModel.identity_id == identity_key)
                .where(AccountAssignmentModel.local_account_id == 0)
                .where(
                    AccountAssignmentModel.state.in_(
                        [
                            "active", "draining", "planned", "locking",
                            "uploading", "pending", "verifying",
                            "assignment_committing", "source_cleaning",
                            "target_enabling", "migrating", "standby",
                            "target_disabled",
                        ]
                    )
                )
                .order_by(AccountAssignmentModel.updated_at.desc())
            ).first()
            if identity_ambiguous:
                move_assignments_to_standby(
                    session,
                    identity_id=identity_key,
                    reason="identity_ambiguous",
                )
            elif target_enabled and remote_account_is_schedulable(remote):
                if assignment is None:
                    assignment = AccountAssignmentModel(
                        identity_id=identity_key,
                        local_account_id=0,
                        pool_id=str(target.default_pool_id or "PUBLIC_POOL"),
                        target_id=int(target_id),
                        state="active",
                        lease_reason="remote_account_reconcile",
                        lease_started_at=now,
                        assignment_version=1,
                        created_at=now,
                        updated_at=now,
                    )
                    session.add(
                        AccountAssignmentEventModel(
                            identity_id=identity_key,
                            local_account_id=0,
                            event_type="initial_assignment",
                            to_pool_id=assignment.pool_id,
                            to_target_id=int(target_id),
                            assignment_version=1,
                            reason="remote_account_reconcile",
                            created_at=now,
                        )
                    )
                else:
                    previous_state = str(assignment.state or "")
                    previous_target = int(assignment.target_id or 0)
                    previous_version = int(assignment.assignment_version or 0)
                    if previous_target != int(target_id) or previous_state not in {"active", "standby"}:
                        move_assignments_to_standby(
                            session,
                            identity_id=identity_key,
                            reason="remote_account_reconcile",
                        )
                    elif previous_state == "standby":
                        assignment.assignment_version = max(1, previous_version) + 1
                    assignment.state = "active"
                    assignment.target_id = int(target_id)
                    assignment.lease_owner = ""
                    assignment.lease_expires_at = None
                    assignment.updated_at = now
                    assignment.lease_reason = "remote_account_reconcile"
                session.add(assignment)
            elif assignment is not None:
                move_assignments_to_standby(
                    session,
                    identity_id=identity_key,
                    target_id=int(target_id),
                    reason="remote_account_not_schedulable",
                )
        session.commit()
        return created


def collect_target_quota(
    database_engine=None,
    *,
    target_id: int,
    client: Any | None = None,
    now: datetime | None = None,
    probe_poll_attempts: int = 30,
    probe_poll_interval_seconds: float = 1,
    freshness_seconds: int | None = None,
    sleep_fn=time.sleep,
) -> TargetQuotaResult:
    """Run one target probe under the shared target reconciliation fence."""

    from services.chatgpt_account_coordination import codex2api_target_lock

    with codex2api_target_lock(target_id):
        return _collect_target_quota_impl(
            database_engine,
            target_id=target_id,
            client=client,
            now=now,
            probe_poll_attempts=probe_poll_attempts,
            probe_poll_interval_seconds=probe_poll_interval_seconds,
            freshness_seconds=freshness_seconds,
            sleep_fn=sleep_fn,
        )


def _collect_target_quota_impl(
    database_engine=None,
    *,
    target_id: int,
    client: Any | None = None,
    now: datetime | None = None,
    probe_poll_attempts: int = 30,
    probe_poll_interval_seconds: float = 1,
    freshness_seconds: int | None = None,
    sleep_fn=time.sleep,
) -> TargetQuotaResult:
    """Run one target-level probe and persist every bound account snapshot."""
    from services.account_identity import move_assignments_to_standby

    target_engine = database_engine or default_engine
    captured_at = _aware(now)
    if freshness_seconds is None:
        configured_minutes = 15
        if target_engine is default_engine:
            try:
                from core.config_store import config_store

                configured_minutes = int(
                    str(
                        config_store.get(
                            "codex2api_scheduler_quota_freshness_minutes",
                            "15",
                        )
                    ).strip()
                )
            except Exception:
                configured_minutes = 15
        freshness_seconds = min(max(configured_minutes, 1), 1440) * 60
    freshness_seconds = min(max(int(freshness_seconds), 1), 86400)
    with Session(target_engine) as session:
        target = session.get(Codex2APITargetModel, int(target_id))
        if target is None:
            raise ValueError("Codex2API target does not exist")
        if not target.enabled:
            raise ValueError("Codex2API target is disabled")

    resolved_client = _client_for(int(target_id), target_engine, client)
    resolved_client.trigger_usage_probe()
    attempts = max(min(int(probe_poll_attempts), 300), 1)
    for attempt in range(attempts):
        runtime = resolved_client.runtime_status()
        probes = runtime.get("probes") if isinstance(runtime, Mapping) else {}
        running = bool(
            isinstance(probes, Mapping)
            and probes.get("usage_probe_running")
        )
        if not running:
            break
        if attempt + 1 < attempts:
            sleep_fn(max(float(probe_poll_interval_seconds), 0))
    else:
        raise RuntimeError("Codex2API usage probe did not finish")

    raw_rows = resolved_client.list_accounts()
    if not isinstance(raw_rows, list) or any(
        not isinstance(row, Mapping) for row in raw_rows
    ):
        raise RuntimeError("Codex2API account list contains malformed rows")
    rows = [dict(row) for row in raw_rows]
    remote_ids = [_remote_id(row) for row in rows]
    if any(remote_id <= 0 for remote_id in remote_ids):
        raise RuntimeError("Codex2API account list contains an invalid remote ID")
    duplicate_ids = {
        remote_id
        for remote_id, count in Counter(remote_ids).items()
        if count > 1
    }
    if duplicate_ids:
        raise RuntimeError(
            "Codex2API account list contains duplicate remote IDs: "
            + ",".join(str(remote_id) for remote_id in sorted(duplicate_ids))
        )
    # Reconciliation may quarantine an ambiguous binding (and thus make it
    # invisible to the enabled-binding query).  Keep the concrete response
    # rows that reconciliation rejects so the result still reports them.
    ambiguous_keys: set[tuple[str, int]] = set()
    reconcile_target_bindings(
        target_engine,
        target_id=int(target_id),
        rows=rows,
        now=captured_at,
        ambiguity_keys=ambiguous_keys,
    )
    binding_data = _binding_data(target_engine, int(target_id))
    with Session(target_engine) as session:
        local_accounts = {
            int(account.id): account
            for account in session.exec(
                select(AccountModel).where(AccountModel.platform == "chatgpt")
            ).all()
            if account.id is not None
        }
    by_id = {_remote_id(row): row for row in rows if _remote_id(row)}
    by_email: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        email = remote_account_email(row).strip().lower()
        if email:
            by_email.setdefault(email, []).append(row)

    collected = 0
    missing = 0
    for item in binding_data:
        status = ""
        row = by_id.get(int(item["remote_account_id"]))
        bound_account = local_accounts.get(int(item.get("local_account_id") or 0))
        if row is not None and not _remote_row_matches_binding(item, row, bound_account):
            # A reused numeric remote ID must never transfer another identity's
            # quota into this binding. Treat an alternate same-email row as an
            # ambiguity and quarantine the binding below.
            row = None
            candidates = by_email.get(str(item["remote_email"]).lower(), [])
            if candidates:
                ambiguous_keys.add((str(item["identity_id"]), int(item["remote_account_id"] or 0)))
                status = "ambiguous"
            else:
                missing += 1
                status = "remote_missing"
        if row is None:
            candidates = by_email.get(str(item["remote_email"]).lower(), [])
            if not status:
                if len(candidates) == 1:
                    if _remote_row_matches_binding(item, candidates[0], bound_account):
                        row = candidates[0]
                    else:
                        ambiguous_keys.add(
                            (
                                str(item["identity_id"]),
                                _remote_id(candidates[0]),
                            )
                        )
                        status = "ambiguous"
                elif len(candidates) > 1:
                    for candidate in candidates:
                        ambiguous_keys.add(
                            (str(item["identity_id"]), _remote_id(candidate))
                        )
                    status = "ambiguous"
                else:
                    missing += 1
                    status = "remote_missing"
            if row is None:
                with Session(target_engine) as session:
                    binding = session.get(AccountTargetBindingModel, int(item["id"]))
                    if binding is not None:
                        binding.sync_status = status
                        binding.enabled = False
                        binding.remote_status = (
                            "ambiguous" if status == "ambiguous" else "remote_missing"
                        )
                        binding.last_error = (
                            "目标节点存在多个同邮箱账号"
                            if status == "ambiguous"
                            else "目标节点未找到账号"
                        )
                        binding.updated_at = captured_at
                        session.add(binding)
                        assignment = session.exec(
                                select(AccountAssignmentModel)
                                .where(AccountAssignmentModel.identity_id == str(item["identity_id"]))
                                .where(
                                    AccountAssignmentModel.state.in_(
                                        [
                                            "active", "draining", "planned", "locking",
                                            "uploading", "pending", "verifying",
                                            "assignment_committing", "source_cleaning",
                                            "target_enabling", "migrating", "target_disabled", "standby",
                                        ]
                                    )
                                )
                            ).first()
                        if assignment is not None:
                            move_assignments_to_standby(
                                session,
                                identity_id=str(item["identity_id"]),
                                target_id=int(target_id),
                                reason=(
                                    "remote_account_ambiguous"
                                    if status == "ambiguous"
                                    else "remote_account_missing"
                                ),
                            )
                        session.commit()
                continue

        remote_id = _remote_id(row)
        merge_remote_rows(
            target_engine,
            identity_id=str(item["identity_id"]),
            local_account_id=int(item["local_account_id"]),
            target_id=int(target_id),
            remote_id=remote_id,
            rows=[row],
            captured_at=captured_at,
            freshness_seconds=freshness_seconds,
        )
        with Session(target_engine) as session:
            binding = session.get(AccountTargetBindingModel, int(item["id"]))
            if binding is not None:
                binding.remote_account_id = remote_id
                observed_email = remote_account_email(row).strip().lower()
                if observed_email:
                    binding.remote_email = observed_email
                binding.remote_status = str(
                    row.get("remote_status") or row.get("status") or ""
                )
                binding.enabled = remote_bool(row.get("enabled"), True) and not remote_bool(
                    row.get("locked"), False
                )
                binding.sync_status = "synced"
                binding.last_sync_at = captured_at
                binding.last_error = ""
                binding.updated_at = captured_at
                session.add(binding)
                session.commit()
        collected += 1

    with Session(target_engine) as session:
        target = session.get(Codex2APITargetModel, int(target_id))
        if target is not None:
            target.last_sync_at = captured_at
            target.updated_at = captured_at
            session.add(target)
            session.commit()

    return TargetQuotaResult(
        target_id=int(target_id),
        remote_accounts=len(rows),
        collected_accounts=collected,
        missing_accounts=missing,
        ambiguous_accounts=len(ambiguous_keys),
    )


def collect_customer_usage(
    database_engine=None,
    *,
    target_id: int,
    client: Any | None = None,
    now: datetime | None = None,
) -> list[CustomerUsageSampleModel]:
    """Collect one hourly API-key usage sample for pools on a target."""

    target_engine = database_engine or default_engine
    captured_at = _aware(now)
    bucket_start = captured_at.replace(minute=0, second=0, microsecond=0)
    with Session(target_engine) as session:
        policies = session.exec(
            select(PoolTargetPolicyModel)
            .where(PoolTargetPolicyModel.target_id == int(target_id))
            .where(PoolTargetPolicyModel.enabled == True)  # noqa: E712
        ).all()
        pools = {
            str(row.id): row
            for row in session.exec(
                select(AccountPoolModel).where(
                    AccountPoolModel.id.in_([str(policy.pool_id) for policy in policies])
                )
            ).all()
        } if policies else {}
    if not policies:
        return []
    resolved_client = _client_for(int(target_id), target_engine, client)
    items = resolved_client.api_key_usage(start=bucket_start, end=captured_at)
    samples: list[CustomerUsageSampleModel] = []
    with Session(target_engine) as session:
        for policy in policies:
            pool = pools.get(str(policy.pool_id))
            if pool is None or not str(pool.customer_id or "").strip():
                continue
            try:
                configured_ids = {
                    int(value)
                    for value in json.loads(policy.remote_api_key_ids_json or "[]")
                    if int(value) > 0
                }
            except (TypeError, ValueError, json.JSONDecodeError):
                configured_ids = set()
            selected = []
            for item in items:
                try:
                    api_key_id = int(item.get("api_key_id") or 0)
                except (TypeError, ValueError):
                    continue
                if configured_ids and api_key_id not in configured_ids:
                    continue
                selected.append(item)
            total = Decimal("0")
            requests = 0
            for item in selected:
                try:
                    billed = Decimal(str(item.get("user_billed") or 0))
                except (InvalidOperation, TypeError, ValueError):
                    continue
                if billed.is_finite() and billed >= 0:
                    total += billed
                try:
                    requests += max(int(item.get("requests") or 0), 0)
                except (TypeError, ValueError):
                    pass
            billed_cents = int(
                (total * 100).to_integral_value(rounding=ROUND_HALF_UP)
            )
            key_scope = min(configured_ids) if len(configured_ids) == 1 else 0
            sample = session.exec(
                select(CustomerUsageSampleModel).where(
                    CustomerUsageSampleModel.customer_id == str(pool.customer_id),
                    CustomerUsageSampleModel.target_id == int(target_id),
                    CustomerUsageSampleModel.remote_api_key_id == key_scope,
                    CustomerUsageSampleModel.bucket_start == bucket_start,
                )
            ).first()
            if sample is None:
                sample = CustomerUsageSampleModel(
                    customer_id=str(pool.customer_id),
                    pool_id=str(pool.id),
                    target_id=int(target_id),
                    remote_api_key_id=key_scope,
                    bucket_start=bucket_start,
                    bucket_end=captured_at,
                    captured_at=captured_at,
                )
            sample.bucket_end = captured_at
            sample.billed_cents = billed_cents
            sample.request_count = requests
            sample.captured_at = captured_at
            session.add(sample)
            session.flush()
            samples.append(sample)
        session.commit()
        for sample in samples:
            session.refresh(sample)
        return samples


__all__ = [
    "TargetHealthResult",
    "TargetQuotaResult",
    "collect_target_health",
    "collect_target_quota",
    "collect_customer_usage",
    "reconcile_target_bindings",
]
