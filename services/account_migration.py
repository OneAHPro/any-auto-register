"""Durable cross-target account migration Saga.

The Saga coordinates existing Codex2API admin endpoints.  No transaction is
held while waiting on a remote request; every step is persisted before the
request so a process restart can reconcile and continue safely.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import uuid4

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from core.db import (
    AccountAssignmentModel,
    AccountAssignmentEventModel,
    AccountIdentityAliasModel,
    AccountIdentityModel,
    AccountMigrationModel,
    AccountModel,
    AccountPoolModel,
    AccountTargetBindingModel,
    ChatGPTAuthStateModel,
    engine as default_engine,
)
from services.chatgpt_account_coordination import chatgpt_account_operation_lock


logger = logging.getLogger(__name__)

TERMINAL_STATES = {"committed", "rolled_back", "rollback_required", "cleanup_pending"}
STEP_ORDER = (
    "planned",
    "locking",
    "draining",
    "uploading",
    "target_disabled",
    "verifying",
    "assignment_committing",
    "source_cleaning",
    "target_enabling",
    "committed",
)
_TARGET_LOCKS_GUARD = threading.Lock()
_TARGET_LOCKS: dict[int, threading.RLock] = {}


class MigrationError(RuntimeError):
    """A migration step failed with a safe diagnostic."""


class MigrationConflict(MigrationError):
    """The local assignment or credential version changed during a run."""


@dataclass(frozen=True)
class MigrationResult:
    id: str
    state: str
    step: str
    error: str = ""
    source_remote_id: int = 0
    destination_remote_id: int = 0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def _safe_error(exc: BaseException, account: AccountModel | None = None) -> str:
    message = _safe_text(exc)
    if account is not None:
        secrets = [
            _safe_text(account.password),
            _safe_text(account.token),
        ]
        try:
            extra = account.get_extra()
        except Exception:
            extra = {}
        if isinstance(extra, dict):
            secrets.extend(_safe_text(extra.get(key)) for key in (
                "refresh_token",
                "access_token",
                "session_token",
                "id_token",
            ))
        for secret in secrets:
            if secret:
                message = message.replace(secret, "***")
    return message[:240] or type(exc).__name__


def _result(row: AccountMigrationModel) -> MigrationResult:
    return MigrationResult(
        id=str(row.id),
        state=str(row.state or "planned"),
        step=str(row.step or "planned"),
        error=_safe_text(
            (json.loads(row.error_json or "{}").get("message") if row.error_json else "")
            if isinstance(row.error_json, str)
            else ""
        ),
        source_remote_id=int(row.source_remote_id or 0),
        destination_remote_id=int(row.destination_remote_id or 0),
    )


def _target_lock(target_id: int) -> threading.RLock:
    normalized = int(target_id)
    with _TARGET_LOCKS_GUARD:
        return _TARGET_LOCKS.setdefault(normalized, threading.RLock())


def _find_assignment(session: Session, identity_id: str) -> AccountAssignmentModel | None:
    return session.exec(
        select(AccountAssignmentModel)
        .where(AccountAssignmentModel.identity_id == str(identity_id))
        .where(AccountAssignmentModel.state.in_(["active", "draining", "standby"]))
        .order_by(AccountAssignmentModel.updated_at.desc())
    ).first()


def _find_binding(
    session: Session,
    identity_id: str,
    target_id: int,
) -> AccountTargetBindingModel | None:
    return session.exec(
        select(AccountTargetBindingModel)
        .where(AccountTargetBindingModel.identity_id == str(identity_id))
        .where(AccountTargetBindingModel.target_id == int(target_id))
    ).first()


def plan_migration(
    database_engine,
    *,
    identity_id: str,
    local_account_id: int,
    source_target_id: int,
    destination_target_id: int,
    expected_assignment_version: int,
    expected_credential_revision: str,
    idempotency_key: str | None = None,
    plan: Mapping[str, Any] | None = None,
) -> str:
    """Persist an idempotent migration intent and return its operation ID."""

    if int(source_target_id) == int(destination_target_id):
        raise ValueError("source and destination targets must differ")
    target_engine = database_engine or default_engine
    key = _safe_text(idempotency_key) or str(uuid4())
    with Session(target_engine) as session:
        existing = session.exec(
            select(AccountMigrationModel).where(
                AccountMigrationModel.idempotency_key == key
            )
        ).first()
        if existing is not None:
            return str(existing.id)
        identity = session.get(AccountIdentityModel, str(identity_id))
        account = session.get(AccountModel, int(local_account_id))
        assignment = _find_assignment(session, str(identity_id))
        source_binding = _find_binding(session, identity_id, int(source_target_id))
        if identity is None or account is None:
            raise ValueError("migration account identity does not exist")
        if str(identity.state or "") == "ambiguous":
            raise ValueError("ambiguous account identity cannot be migrated")
        if str(account.identity_id or "") != str(identity_id):
            raise ValueError("local account does not match migration identity")
        if assignment is None:
            raise ValueError("migration source assignment does not exist")
        if int(assignment.target_id) != int(source_target_id):
            raise ValueError("migration source does not match current assignment")
        if int(assignment.assignment_version or 0) != int(expected_assignment_version):
            raise ValueError("migration assignment version is stale")
        if source_binding is None or int(source_binding.remote_account_id or 0) <= 0:
            raise ValueError("migration source binding does not exist")
        expected_revision = _safe_text(expected_credential_revision)
        if expected_revision:
            auth_state = session.exec(
                select(ChatGPTAuthStateModel).where(
                    ChatGPTAuthStateModel.account_id == int(local_account_id)
                )
            ).first()
            current_revision = _safe_text(
                auth_state.credential_revision if auth_state is not None else source_binding.credential_revision
            )
            if current_revision != expected_revision:
                raise ValueError("migration credential revision is stale")
        payload = dict(plan or {})
        payload.setdefault("identity_id", str(identity_id))
        payload.setdefault("local_account_id", int(local_account_id))
        row = AccountMigrationModel(
            id=str(uuid4()),
            identity_id=str(identity_id),
            local_account_id=int(local_account_id),
            source_target_id=int(source_target_id),
            destination_target_id=int(destination_target_id),
            source_remote_id=int(source_binding.remote_account_id or 0)
            if source_binding is not None
            else 0,
            state="planned",
            step="planned",
            expected_assignment_version=int(expected_assignment_version),
            expected_credential_revision=_safe_text(expected_credential_revision),
            idempotency_key=key,
            plan_json=json.dumps(payload, ensure_ascii=False, sort_keys=True),
            error_json="{}",
        )
        session.add(row)
        try:
            session.commit()
        except IntegrityError:
            # Another worker may have won the idempotency race between the
            # initial SELECT and INSERT. Return that durable operation rather
            # than leaking a uniqueness error to the API caller.
            session.rollback()
            existing = session.exec(
                select(AccountMigrationModel).where(
                    AccountMigrationModel.idempotency_key == key
                )
            ).first()
            if existing is not None:
                return str(existing.id)
            raise
        return str(row.id)


def get_migration(database_engine, migration_id: str) -> MigrationResult | None:
    target_engine = database_engine or default_engine
    with Session(target_engine) as session:
        row = session.get(AccountMigrationModel, str(migration_id))
        return _result(row) if row is not None else None


def reassign_account_pool(
    database_engine,
    *,
    identity_id: str,
    local_account_id: int,
    source_target_id: int,
    source_pool_id: str,
    destination_pool_id: str,
    expected_assignment_version: int,
    reason: str = "sustained_low_utilization",
) -> str:
    """Commit a same-target pool move with the same CAS/audit guarantees.

    Shrinking an enterprise pool usually hands the account to the local float
    pool; no remote credential copy is needed when both pools share a target.
    The operation is still serialized with login/delete/migration work and is
    safe to retry with the same assignment version.
    """

    target_engine = database_engine or default_engine
    destination_pool = _safe_text(destination_pool_id).upper()
    source_pool = _safe_text(source_pool_id).upper()
    if not destination_pool or destination_pool == source_pool:
        raise ValueError("pool reassignment requires a different destination pool")
    with chatgpt_account_operation_lock(int(local_account_id), blocking=False) as acquired:
        if not acquired:
            raise MigrationConflict("账号当前正在执行其他凭证操作")
        with Session(target_engine) as session:
            pool = session.get(AccountPoolModel, destination_pool)
            if pool is None or not bool(pool.enabled):
                raise ValueError("destination pool does not exist or is disabled")
            assignment = session.exec(
                select(AccountAssignmentModel).where(
                    AccountAssignmentModel.identity_id == str(identity_id),
                    AccountAssignmentModel.local_account_id == int(local_account_id),
                    AccountAssignmentModel.state.in_(
                        ["active", "draining", "standby"]
                    ),
                )
            ).first()
            if assignment is None:
                raise MigrationConflict("账号当前归属不存在")
            if int(assignment.target_id) != int(source_target_id):
                raise MigrationConflict("账号当前目标已变化")
            if str(assignment.pool_id or "").upper() == destination_pool:
                if int(assignment.assignment_version or 0) > int(
                    expected_assignment_version
                ):
                    return (
                        f"pool-reassign:{identity_id}:"
                        f"{int(assignment.assignment_version)}"
                    )
            if str(assignment.pool_id or "").upper() != source_pool:
                raise MigrationConflict("账号当前号池已变化")
            if int(assignment.assignment_version or 0) != int(
                expected_assignment_version
            ):
                raise MigrationConflict("账号归属版本已变化")
            now = _utcnow()
            result = session.exec(
                update(AccountAssignmentModel)
                .where(AccountAssignmentModel.id == assignment.id)
                .where(
                    AccountAssignmentModel.assignment_version
                    == int(expected_assignment_version)
                )
                .values(
                    pool_id=destination_pool,
                    lease_reason=_safe_text(reason),
                    assignment_version=int(expected_assignment_version) + 1,
                    updated_at=now,
                )
            )
            if int(getattr(result, "rowcount", 0) or 0) != 1:
                session.rollback()
                raise MigrationConflict("账号归属并发更新冲突")
            session.add(
                AccountAssignmentEventModel(
                    identity_id=str(identity_id),
                    local_account_id=int(local_account_id),
                    event_type="pool_reassigned",
                    from_pool_id=source_pool,
                    to_pool_id=destination_pool,
                    from_target_id=int(source_target_id),
                    to_target_id=int(source_target_id),
                    assignment_version=int(expected_assignment_version) + 1,
                    reason=_safe_text(reason) or "pool_reassignment",
                    created_at=now,
                )
            )
            session.commit()
            return f"pool-reassign:{identity_id}:{int(expected_assignment_version) + 1}"


def _load_runtime_context(
    session: Session,
    row: AccountMigrationModel,
) -> tuple[AccountModel, AccountAssignmentModel | None, AccountTargetBindingModel | None]:
    account = session.get(AccountModel, int(row.local_account_id))
    if account is None:
        raise MigrationError("本地账号不存在")
    assignment = _find_assignment(session, row.identity_id)
    source_binding = _find_binding(session, row.identity_id, row.source_target_id)
    return account, assignment, source_binding


def _transition(
    database_engine,
    migration_id: str,
    *,
    expected_step: str,
    step: str,
    state: str | None = None,
    error: str = "",
    source_remote_id: int | None = None,
    destination_remote_id: int | None = None,
    completed: bool = False,
) -> MigrationResult:
    target_engine = database_engine or default_engine
    with Session(target_engine) as session:
        row = session.get(AccountMigrationModel, str(migration_id))
        if row is None:
            raise MigrationError("迁移记录不存在")
        if str(row.step or "planned") != str(expected_step):
            return _result(row)
        row.step = str(step)
        row.state = str(state or step)
        if error:
            row.error_json = json.dumps({"message": str(error)[:240]}, ensure_ascii=False)
        if source_remote_id is not None:
            row.source_remote_id = int(source_remote_id)
        if destination_remote_id is not None:
            row.destination_remote_id = int(destination_remote_id)
        row.updated_at = _utcnow()
        if completed:
            row.completed_at = row.updated_at
        session.add(row)
        session.commit()
        session.refresh(row)
        return _result(row)


def _set_failure(
    database_engine,
    migration_id: str,
    *,
    state: str,
    step: str,
    error: str,
) -> MigrationResult:
    target_engine = database_engine or default_engine
    with Session(target_engine) as session:
        row = session.get(AccountMigrationModel, str(migration_id))
        if row is None:
            raise MigrationError("迁移记录不存在")
        row.state = state
        row.step = step
        row.error_json = json.dumps({"message": str(error)[:240]}, ensure_ascii=False)
        row.updated_at = _utcnow()
        session.add(row)
        session.commit()
        session.refresh(row)
        return _result(row)


def _credential_payload(account: AccountModel) -> dict[str, Any]:
    try:
        extra = account.get_extra()
    except Exception:
        extra = {}
    if not isinstance(extra, dict):
        extra = {}

    def first(*keys: str) -> str:
        for key in keys:
            value = _safe_text(extra.get(key))
            if value:
                return value
        return ""

    payload: dict[str, Any] = {
        "name": _safe_text(account.email),
        "email": _safe_text(account.email),
    }
    values = {
        "refresh_token": first("refresh_token", "refreshToken"),
        "access_token": first("access_token", "accessToken") or _safe_text(account.token),
        "id_token": first("id_token", "idToken"),
        "session_token": first("session_token", "sessionToken"),
        "workspace_id": first("workspace_id", "workspaceId"),
        "account_id": first("account_id", "accountId", "chatgpt_account_id", "chatgptAccountId"),
        "user_id": first("user_id", "userId", "chatgpt_user_id", "chatgptUserId") or _safe_text(account.user_id),
        "client_id": first("client_id", "clientId"),
    }
    if values["workspace_id"]:
        values["account_id"] = values["workspace_id"]
    payload.update({key: value for key, value in values.items() if value})
    return payload


def _remote_id_from_payload(payload: Mapping[str, Any] | None) -> int:
    source = payload if isinstance(payload, Mapping) else {}
    for key in ("remote_id", "id", "account_id"):
        try:
            value = int(source.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value
    accounts = source.get("accounts")
    if isinstance(accounts, list):
        for item in accounts:
            if isinstance(item, Mapping):
                value = _remote_id_from_payload(item)
                if value > 0:
                    return value
    return 0


def _identity_alias_values(database_engine, identity_id: str) -> set[str]:
    with Session(database_engine or default_engine) as session:
        rows = session.exec(
            select(AccountIdentityAliasModel).where(
                AccountIdentityAliasModel.identity_id == str(identity_id),
                AccountIdentityAliasModel.alias_type.in_(
                    ["workspace_id", "chatgpt_account_id"]
                ),
            )
        ).all()
    return {
        _safe_text(row.normalized_value).lower()
        for row in rows
        if _safe_text(row.normalized_value)
    }


def _remote_alias_values(row: Mapping[str, Any]) -> set[str]:
    return {
        value
        for key in ("workspace_id", "chatgpt_account_id", "account_id", "user_id")
        if (value := _safe_text(row.get(key)).lower())
    }


def _find_remote_by_identity(
    client: Any,
    email: str,
    identity_id: str,
    database_engine=None,
) -> int:
    rows = client.list_accounts()
    normalized_email = _safe_text(email).lower()
    candidates = [
        row
        for row in rows
        if isinstance(row, Mapping)
        and _safe_text(row.get("email") or row.get("name")).lower() == normalized_email
    ]
    local_aliases = _identity_alias_values(database_engine, identity_id)
    if len(candidates) == 1:
        remote_aliases = _remote_alias_values(candidates[0])
        if local_aliases and remote_aliases and local_aliases.isdisjoint(remote_aliases):
            raise MigrationError("目标节点账号身份与本地稳定身份不一致")
        return _remote_id_from_payload(candidates[0])
    if len(candidates) > 1 and local_aliases:
        matched = [
            row
            for row in candidates
            if not local_aliases.isdisjoint(_remote_alias_values(row))
        ]
        if len(matched) == 1:
            return _remote_id_from_payload(matched[0])
    if len(candidates) > 1:
        raise MigrationError("目标节点存在多个同邮箱账号，无法唯一确认")
    return 0


def _verify_destination(
    client: Any,
    remote_id: int,
    email: str,
    identity_id: str,
    database_engine=None,
) -> Mapping[str, Any]:
    test_result = client.test_account(int(remote_id))
    if not isinstance(test_result, Mapping) or test_result.get("success") is not True:
        raise MigrationError("目标节点账号测试未通过")
    rows = client.list_accounts()
    row = next(
        (
            item
            for item in rows
            if isinstance(item, Mapping)
            and _remote_id_from_payload(item) == int(remote_id)
        ),
        None,
    )
    if row is None:
        raise MigrationError("目标节点未找到已导入账号")
    remote_email = _safe_text(row.get("email") or row.get("name")).lower()
    if remote_email and remote_email != _safe_text(email).lower():
        raise MigrationError("目标节点账号邮箱与本地身份不一致")
    local_aliases = _identity_alias_values(database_engine, identity_id)
    remote_aliases = _remote_alias_values(row)
    if local_aliases and remote_aliases and local_aliases.isdisjoint(remote_aliases):
        raise MigrationError("目标节点账号身份别名验证失败")
    # A newly imported account may not have its first quota sample yet. The
    # target identity/test proof is sufficient to commit the migration; the
    # quota worker will collect the first snapshot on its next target cycle.
    return row


def _commit_assignment(
    database_engine,
    row: AccountMigrationModel,
    *,
    destination_remote_id: int,
) -> None:
    target_engine = database_engine or default_engine
    with Session(target_engine) as session:
        current = session.get(AccountMigrationModel, str(row.id))
        if current is None:
            raise MigrationConflict("迁移记录不存在")
        assignment = _find_assignment(session, current.identity_id)
        if assignment is None:
            raise MigrationConflict("当前账号归属不存在")
        if int(assignment.assignment_version or 0) != int(
            current.expected_assignment_version
        ):
            raise MigrationConflict("账号归属版本已变化")
        if int(assignment.target_id) != int(current.source_target_id):
            raise MigrationConflict("账号当前目标已变化")
        account = session.get(AccountModel, int(current.local_account_id))
        if account is None:
            raise MigrationConflict("本地账号不存在")
        source_binding = _find_binding(
            session,
            current.identity_id,
            current.source_target_id,
        )
        expected_revision = _safe_text(current.expected_credential_revision)
        if expected_revision:
            auth_state = session.exec(
                select(ChatGPTAuthStateModel).where(
                    ChatGPTAuthStateModel.account_id == int(current.local_account_id)
                )
            ).first()
            current_revision = _safe_text(
                auth_state.credential_revision
                if auth_state is not None
                else source_binding.credential_revision
                if source_binding is not None
                else ""
            )
            if current_revision != expected_revision:
                raise MigrationConflict("账号凭证版本已变化")
        destination_binding = _find_binding(
            session,
            current.identity_id,
            current.destination_target_id,
        )
        now = _utcnow()
        source_pool_id = str(assignment.pool_id or "")
        destination_pool_id = str(
            json.loads(current.plan_json or "{}").get("destination_pool_id")
            or assignment.pool_id
            or "FLOAT_POOL"
        )
        assignment_result = session.exec(
            update(AccountAssignmentModel)
            .where(AccountAssignmentModel.id == assignment.id)
            .where(
                AccountAssignmentModel.assignment_version
                == int(current.expected_assignment_version)
            )
            .where(AccountAssignmentModel.target_id == int(current.source_target_id))
            .where(AccountAssignmentModel.state.in_(["active", "draining", "standby"]))
            .values(
                target_id=int(current.destination_target_id),
                pool_id=destination_pool_id,
                state="active",
                assignment_version=int(current.expected_assignment_version) + 1,
                updated_at=now,
            )
        )
        if int(getattr(assignment_result, "rowcount", 0) or 0) != 1:
            session.rollback()
            raise MigrationConflict("账号归属并发更新冲突")
        session.add(
            AccountAssignmentEventModel(
                identity_id=current.identity_id,
                local_account_id=current.local_account_id,
                event_type="migration_committed",
                from_pool_id=source_pool_id,
                to_pool_id=destination_pool_id,
                from_target_id=int(current.source_target_id),
                to_target_id=int(current.destination_target_id),
                assignment_version=int(current.expected_assignment_version) + 1,
                migration_id=str(current.id),
                reason=str(
                    json.loads(current.plan_json or "{}").get("reason")
                    or "manual_migration"
                ),
                created_at=now,
            )
        )

        if source_binding is not None:
            source_binding.enabled = False
            source_binding.sync_status = "migrated"
            source_binding.updated_at = now
            session.add(source_binding)
        if destination_binding is None:
            destination_binding = AccountTargetBindingModel(
                identity_id=current.identity_id,
                local_account_id=current.local_account_id,
                target_id=current.destination_target_id,
                remote_account_id=int(destination_remote_id),
                remote_email=str(account.email or ""),
                sync_status="verified",
                remote_status="active",
                enabled=False,
                credential_revision=current.expected_credential_revision,
                last_sync_at=now,
                updated_at=now,
            )
        else:
            destination_binding.remote_account_id = int(destination_remote_id)
            destination_binding.remote_email = str(account.email or "")
            destination_binding.sync_status = "verified"
            destination_binding.enabled = False
            destination_binding.credential_revision = current.expected_credential_revision
            destination_binding.last_sync_at = now
            destination_binding.updated_at = now
        session.add(destination_binding)
        session.commit()


def _rollback_local_assignment(database_engine, migration_id: str) -> bool:
    """Move a locally committed assignment back only when its CAS version is ours."""

    target_engine = database_engine or default_engine
    with Session(target_engine) as session:
        migration = session.get(AccountMigrationModel, str(migration_id))
        if migration is None:
            return False
        assignment = _find_assignment(session, migration.identity_id)
        if assignment is None:
            return False
        expected_after_commit = int(migration.expected_assignment_version or 0) + 1
        if not (
            int(assignment.target_id) == int(migration.destination_target_id)
            and int(assignment.assignment_version or 0) == expected_after_commit
        ):
            # A later operator change owns the row; never overwrite it during
            # an automated rollback.
            return False
        plan = {}
        try:
            plan = json.loads(migration.plan_json or "{}")
        except (TypeError, ValueError):
            plan = {}
        now = _utcnow()
        destination_pool_id = str(assignment.pool_id or "")
        source_pool_id = str(plan.get("source_pool_id") or "PUBLIC_POOL")
        assignment.target_id = int(migration.source_target_id)
        assignment.pool_id = source_pool_id
        assignment.state = "active"
        assignment.assignment_version = expected_after_commit + 1
        assignment.updated_at = now
        session.add(assignment)
        session.add(
            AccountAssignmentEventModel(
                identity_id=migration.identity_id,
                local_account_id=migration.local_account_id,
                event_type="migration_rolled_back",
                from_pool_id=destination_pool_id,
                to_pool_id=source_pool_id,
                from_target_id=int(migration.destination_target_id),
                to_target_id=int(migration.source_target_id),
                assignment_version=expected_after_commit + 1,
                migration_id=str(migration.id),
                reason="migration_rollback",
                created_at=now,
            )
        )
        source_binding = _find_binding(
            session,
            migration.identity_id,
            migration.source_target_id,
        )
        destination_binding = _find_binding(
            session,
            migration.identity_id,
            migration.destination_target_id,
        )
        if source_binding is not None:
            source_binding.enabled = True
            source_binding.sync_status = "restored"
            source_binding.updated_at = now
            session.add(source_binding)
        if destination_binding is not None:
            destination_binding.enabled = False
            destination_binding.sync_status = "rolled_back"
            destination_binding.updated_at = now
            session.add(destination_binding)
        session.commit()
        return True


def _restore_source(
    source: Any,
    source_remote_id: int,
    *,
    unlock: bool = True,
) -> None:
    if int(source_remote_id or 0) <= 0:
        return
    try:
        source.set_enabled(int(source_remote_id), True)
    finally:
        if unlock:
            try:
                source.set_locked(int(source_remote_id), False)
            except Exception:
                logger.warning("source unlock deferred after migration rollback")


def _restore_deleted_source(
    database_engine,
    source: Any,
    *,
    migration_id: str,
    account: AccountModel,
    source_remote_id: int,
) -> int:
    """Restore a soft-deleted source or re-import when restore is unavailable."""

    restored_id = int(source_remote_id or 0)
    try:
        source.restore_account(restored_id)
    except Exception:
        payload = _credential_payload(account)
        if payload.get("refresh_token") and payload.get("access_token"):
            imported = source.import_full_json(payload)
        elif payload.get("refresh_token"):
            imported = source.import_refresh_token(payload)
        elif payload.get("access_token"):
            imported = source.import_access_token(payload)
        else:
            raise MigrationError("源账号缺少可恢复凭证")
        restored_id = _remote_id_from_payload(imported) or _find_remote_by_identity(
            source,
            account.email,
            account.identity_id,
            database_engine,
        )
        if restored_id <= 0:
            raise MigrationError("源账号重新导入后无法确认远端身份")
    _restore_source(source, restored_id)
    with Session(database_engine or default_engine) as session:
        migration = session.get(AccountMigrationModel, str(migration_id))
        if migration is not None:
            binding = _find_binding(
                session,
                migration.identity_id,
                migration.source_target_id,
            )
            if binding is not None:
                binding.remote_account_id = restored_id
                binding.enabled = True
                binding.sync_status = "restored"
                binding.updated_at = _utcnow()
                session.add(binding)
                session.commit()
    return restored_id


def _cleanup_destination(destination: Any, destination_remote_id: int) -> bool:
    if int(destination_remote_id or 0) <= 0:
        return True
    try:
        destination.delete_account(int(destination_remote_id))
        return True
    except Exception:
        logger.warning("destination cleanup deferred after migration rollback")
        return False


def run_migration(
    database_engine,
    migration_id: str,
    *,
    clients: Mapping[int, Any] | None = None,
    now: datetime | None = None,
    drain_timeout_seconds: float = 600,
    poll_interval_seconds: float = 2,
    sleep_fn=time.sleep,
    stop_after: str | None = None,
) -> MigrationResult:
    """Run or resume one persisted migration operation.

    A persisted step means "the next remote action to perform".  This is
    deliberate: a crash after the DB write but before the HTTP request causes
    the next worker to repeat an idempotent enable/lock operation rather than
    silently skipping it.
    """

    target_engine = database_engine or default_engine
    with Session(target_engine) as session:
        migration = session.get(AccountMigrationModel, str(migration_id))
        if migration is None:
            raise MigrationError("迁移记录不存在")
        if migration.state in {"committed", "rolled_back", "rollback_required"}:
            return _result(migration)
        source_target_id = int(migration.source_target_id)
        destination_target_id = int(migration.destination_target_id)

    if clients is None:
        from services.codex2api_target_client import get_target_client

        clients = {
            source_target_id: get_target_client(source_target_id, target_engine),
            destination_target_id: get_target_client(destination_target_id, target_engine),
        }
    source = clients[source_target_id]
    destination = clients[destination_target_id]
    source_deleted = False
    local_assignment_committed = False
    source_remote_id = 0
    destination_remote_id = 0
    account: AccountModel | None = None

    with _target_lock(destination_target_id):
        try:
            with chatgpt_account_operation_lock(
                int(migration.local_account_id),
                blocking=False,
            ) as acquired:
                if not acquired:
                    return _set_failure(
                        target_engine,
                        migration_id,
                        state="rollback_required",
                        step="planned",
                        error="账号当前正在执行其他凭证操作",
                    )

                while True:
                    with Session(target_engine) as session:
                        migration = session.get(AccountMigrationModel, str(migration_id))
                        if migration is None:
                            raise MigrationError("迁移记录不存在")
                        if migration.state in {"committed", "rolled_back", "rollback_required"}:
                            return _result(migration)
                        account, assignment, source_binding = _load_runtime_context(
                            session, migration
                        )
                        step = str(migration.step or "planned")
                        source_remote_id = int(
                            migration.source_remote_id
                            or (source_binding.remote_account_id if source_binding else 0)
                            or source_remote_id
                            or 0
                        )
                        destination_remote_id = int(
                            migration.destination_remote_id or destination_remote_id or 0
                        )
                        expected_version = int(migration.expected_assignment_version or 0)
                        local_assignment_committed = bool(
                            assignment is not None
                            and int(assignment.target_id) == destination_target_id
                            and int(assignment.assignment_version or 0) > expected_version
                        )

                    if step == "planned":
                        if assignment is None or int(assignment.assignment_version or 0) != expected_version:
                            return _set_failure(
                                target_engine,
                                migration_id,
                                state="rollback_required",
                                step="planned",
                                error="账号归属版本已变化，未执行远端操作",
                            )
                        expected_revision = _safe_text(
                            migration.expected_credential_revision
                        )
                        if expected_revision:
                            with Session(target_engine) as revision_session:
                                auth_state = revision_session.exec(
                                    select(ChatGPTAuthStateModel).where(
                                        ChatGPTAuthStateModel.account_id
                                        == int(migration.local_account_id)
                                    )
                                ).first()
                            if (
                                _safe_text(
                                    auth_state.credential_revision
                                    if auth_state is not None
                                    else source_binding.credential_revision
                                    if source_binding is not None
                                    else ""
                                )
                                != expected_revision
                            ):
                                return _set_failure(
                                    target_engine,
                                    migration_id,
                                    state="rollback_required",
                                    step="planned",
                                    error="账号凭证版本已变化，未执行远端操作",
                                )
                        if not source_remote_id:
                            source_remote_id = _find_remote_by_identity(
                                source,
                                account.email,
                                migration.identity_id,
                                target_engine,
                            )
                        if not source_remote_id:
                            return _set_failure(
                                target_engine,
                                migration_id,
                                state="rollback_required",
                                step="planned",
                                error="源节点未找到对应账号，未执行远端操作",
                            )
                        _transition(
                            target_engine,
                            migration_id,
                            expected_step="planned",
                            step="locking",
                            source_remote_id=source_remote_id,
                        )
                        if stop_after == "locking":
                            return get_migration(target_engine, migration_id)  # type: ignore[return-value]
                        continue

                    if step == "locking":
                        source.set_locked(source_remote_id, True)
                        source.set_enabled(source_remote_id, False)
                        _transition(
                            target_engine,
                            migration_id,
                            expected_step="locking",
                            step="draining",
                            source_remote_id=source_remote_id,
                        )
                        if stop_after == "draining":
                            return get_migration(target_engine, migration_id)  # type: ignore[return-value]
                        continue

                    if step == "draining":
                        drained = source.wait_for_zero_active_requests(
                            source_remote_id,
                            timeout_seconds=drain_timeout_seconds,
                            poll_interval_seconds=poll_interval_seconds,
                            sleep_fn=sleep_fn,
                        )
                        if not drained:
                            raise MigrationError("源节点活动请求未在时限内排空")
                        _transition(
                            target_engine,
                            migration_id,
                            expected_step="draining",
                            step="uploading",
                            source_remote_id=source_remote_id,
                        )
                        if stop_after == "uploading":
                            return get_migration(target_engine, migration_id)  # type: ignore[return-value]
                        continue

                    if step == "uploading":
                        if not destination_remote_id:
                            # Reconcile an import that may have completed just
                            # before a worker crash before its response was
                            # persisted.  Only a unique email match is safe.
                            existing_remote = _find_remote_by_identity(
                                destination,
                                account.email,
                                migration.identity_id,
                                target_engine,
                            )
                            if existing_remote:
                                destination_remote_id = existing_remote
                            else:
                                payload = _credential_payload(account)
                                if payload.get("refresh_token") and payload.get("access_token"):
                                    imported = destination.import_full_json(payload)
                                elif payload.get("refresh_token"):
                                    imported = destination.import_refresh_token(payload)
                                elif payload.get("access_token"):
                                    imported = destination.import_access_token(payload)
                                else:
                                    raise MigrationError("本地账号缺少可迁移凭证")
                                destination_remote_id = _remote_id_from_payload(imported)
                                if not destination_remote_id:
                                    destination_remote_id = _find_remote_by_identity(
                                        destination,
                                        account.email,
                                        migration.identity_id,
                                        target_engine,
                                    )
                            if not destination_remote_id:
                                raise MigrationError("目标节点导入后未找到对应账号")
                            _transition(
                                target_engine,
                                migration_id,
                                expected_step="uploading",
                                step="uploading",
                                source_remote_id=source_remote_id,
                                destination_remote_id=destination_remote_id,
                            )
                        _transition(
                            target_engine,
                            migration_id,
                            expected_step="uploading",
                            step="target_disabled",
                            source_remote_id=source_remote_id,
                            destination_remote_id=destination_remote_id,
                        )
                        if stop_after == "target_disabled":
                            return get_migration(target_engine, migration_id)  # type: ignore[return-value]
                        continue

                    if step == "target_disabled":
                        destination.set_locked(destination_remote_id, True)
                        destination.set_enabled(destination_remote_id, False)
                        _transition(
                            target_engine,
                            migration_id,
                            expected_step="target_disabled",
                            step="verifying",
                            source_remote_id=source_remote_id,
                            destination_remote_id=destination_remote_id,
                        )
                        if stop_after == "verifying":
                            return get_migration(target_engine, migration_id)  # type: ignore[return-value]
                        continue

                    if step == "verifying":
                        _verify_destination(
                            destination,
                            destination_remote_id,
                            account.email,
                            migration.identity_id,
                            target_engine,
                        )
                        _transition(
                            target_engine,
                            migration_id,
                            expected_step="verifying",
                            step="assignment_committing",
                            source_remote_id=source_remote_id,
                            destination_remote_id=destination_remote_id,
                        )
                        if stop_after == "assignment_committing":
                            return get_migration(target_engine, migration_id)  # type: ignore[return-value]
                        continue

                    if step == "assignment_committing":
                        if not local_assignment_committed:
                            with Session(target_engine) as session:
                                fresh = session.get(AccountMigrationModel, str(migration_id))
                            if fresh is None:
                                raise MigrationConflict("迁移记录不存在")
                            _commit_assignment(
                                target_engine,
                                fresh,
                                destination_remote_id=destination_remote_id,
                            )
                            local_assignment_committed = True
                        _transition(
                            target_engine,
                            migration_id,
                            expected_step="assignment_committing",
                            step="source_cleaning",
                            source_remote_id=source_remote_id,
                            destination_remote_id=destination_remote_id,
                        )
                        if stop_after == "source_cleaning":
                            return get_migration(target_engine, migration_id)  # type: ignore[return-value]
                        continue

                    if step == "source_cleaning":
                        source_rows = source.list_accounts()
                        source_exists = any(
                            isinstance(item, Mapping)
                            and _remote_id_from_payload(item) == source_remote_id
                            for item in source_rows
                        )
                        if source_exists:
                            try:
                                source.delete_account(source_remote_id)
                                source_deleted = True
                            except Exception as exc:
                                if int(getattr(exc, "status_code", 0) or 0) != 404:
                                    # The local assignment is already pointed
                                    # at the destination.  Keep the source
                                    # disabled, activate the verified target,
                                    # and surface cleanup as a resumable state
                                    # instead of silently undoing the switch.
                                    _transition(
                                        target_engine,
                                        migration_id,
                                        expected_step="source_cleaning",
                                        step="source_cleaning",
                                        state="cleanup_pending",
                                        source_remote_id=source_remote_id,
                                        destination_remote_id=destination_remote_id,
                                        error=_safe_error(exc, account),
                                    )
                                    destination.set_enabled(destination_remote_id, True)
                                    return _set_failure(
                                        target_engine,
                                        migration_id,
                                        state="cleanup_pending",
                                        step="source_cleaning",
                                        error=_safe_error(exc, account),
                                    )
                                source_deleted = True
                        else:
                            source_deleted = True
                        _transition(
                            target_engine,
                            migration_id,
                            expected_step="source_cleaning",
                            step="target_enabling",
                            source_remote_id=source_remote_id,
                            destination_remote_id=destination_remote_id,
                        )
                        if stop_after == "target_enabling":
                            return get_migration(target_engine, migration_id)  # type: ignore[return-value]
                        continue

                    if step == "target_enabling":
                        source_rows = source.list_accounts()
                        source_deleted = not any(
                            isinstance(item, Mapping)
                            and _remote_id_from_payload(item) == source_remote_id
                            for item in source_rows
                        )
                        destination.set_enabled(destination_remote_id, True)
                        rows = destination.list_accounts()
                        enabled_row = next(
                            (
                                item
                                for item in rows
                                if isinstance(item, Mapping)
                                and _remote_id_from_payload(item) == destination_remote_id
                            ),
                            None,
                        )
                        if enabled_row is None:
                            raise MigrationError("目标节点账号在启用后未出现")
                        if enabled_row.get("enabled") is False:
                            raise MigrationError("目标节点账号未成功启用")
                        try:
                            destination.set_locked(destination_remote_id, False)
                        except Exception:
                            logger.warning("destination unlock deferred after migration")
                        with Session(target_engine) as binding_session:
                            destination_binding = _find_binding(
                                binding_session,
                                migration.identity_id,
                                destination_target_id,
                            )
                            if destination_binding is not None:
                                binding_now = _utcnow()
                                destination_binding.enabled = True
                                destination_binding.sync_status = "synced"
                                destination_binding.remote_status = str(
                                    enabled_row.get("status") or "active"
                                )
                                destination_binding.last_error = ""
                                destination_binding.last_sync_at = binding_now
                                destination_binding.updated_at = binding_now
                                binding_session.add(destination_binding)
                                binding_session.commit()
                        return _transition(
                            target_engine,
                            migration_id,
                            expected_step="target_enabling",
                            step="committed",
                            state="committed",
                            source_remote_id=source_remote_id,
                            destination_remote_id=destination_remote_id,
                            completed=True,
                        )

                    raise MigrationError(f"未知迁移步骤: {step}")
        except MigrationConflict as exc:
            cleanup_ok = _cleanup_destination(destination, destination_remote_id)
            if source_deleted:
                try:
                    source_remote_id = _restore_deleted_source(
                        target_engine,
                        source,
                        migration_id=migration_id,
                        account=account,
                        source_remote_id=source_remote_id,
                    )
                except Exception:
                    logger.warning("source restore endpoint failed after migration conflict")
            source_ok = True
            try:
                _restore_source(source, source_remote_id)
            except Exception:
                logger.warning("source restore failed after migration conflict")
                source_ok = False
            if local_assignment_committed:
                _rollback_local_assignment(target_engine, migration_id)
            return _set_failure(
                target_engine,
                migration_id,
                state="rollback_required",
                step="assignment_committing",
                error=_safe_error(exc),
            )
        except Exception as exc:
            # Before the local assignment commit, restore the source and clean
            # the temporary destination.  After commit, restore the source via
            # the upstream recycle-bin endpoint if it was already deleted.
            cleanup_ok = _cleanup_destination(destination, destination_remote_id)
            if source_deleted:
                try:
                    source_remote_id = _restore_deleted_source(
                        target_engine,
                        source,
                        migration_id=migration_id,
                        account=account,
                        source_remote_id=source_remote_id,
                    )
                except Exception:
                    logger.warning("source restore endpoint failed after migration error")
            source_ok = True
            try:
                _restore_source(source, source_remote_id)
            except Exception:
                logger.warning("source restore failed after migration error")
                source_ok = False
            if local_assignment_committed:
                _rollback_local_assignment(target_engine, migration_id)
            rollback_complete = cleanup_ok and source_ok
            return _set_failure(
                target_engine,
                migration_id,
                state="rolled_back" if rollback_complete else "rollback_required",
                step="rolled_back" if rollback_complete else "rollback_required",
                error=_safe_error(exc, account),
            )


def resume_pending_migrations(
    database_engine,
    *,
    clients: Mapping[int, Any] | None = None,
    now: datetime | None = None,
    sleep_fn=time.sleep,
) -> list[MigrationResult]:
    target_engine = database_engine or default_engine
    with Session(target_engine) as session:
        rows = session.exec(
            select(AccountMigrationModel).where(
                AccountMigrationModel.state.not_in(["committed", "rolled_back", "rollback_required"])
            ).order_by(AccountMigrationModel.created_at)
        ).all()
        ids = [str(row.id) for row in rows]
    results: list[MigrationResult] = []
    for migration_id in ids:
        result = run_migration(
            target_engine,
            migration_id,
            clients=clients,
            now=now,
            sleep_fn=sleep_fn,
        )
        results.append(result)
    return results


def rollback_migration(
    database_engine,
    migration_id: str,
    *,
    clients: Mapping[int, Any] | None = None,
) -> MigrationResult:
    """Best-effort explicit rollback for a non-final or cleanup-pending run."""

    target_engine = database_engine or default_engine
    with Session(target_engine) as session:
        row = session.get(AccountMigrationModel, str(migration_id))
        if row is None:
            raise MigrationError("迁移记录不存在")
        if clients is None:
            from services.codex2api_target_client import get_target_client

            clients = {
                int(row.source_target_id): get_target_client(int(row.source_target_id), target_engine),
                int(row.destination_target_id): get_target_client(int(row.destination_target_id), target_engine),
            }
        source = clients[int(row.source_target_id)]
        destination = clients[int(row.destination_target_id)]
        account = session.get(AccountModel, int(row.local_account_id))
        cleanup_ok = True
        source_ok = True
        if row.destination_remote_id:
            cleanup_ok = _cleanup_destination(
                destination,
                int(row.destination_remote_id),
            )
        if row.source_remote_id:
            try:
                source.restore_account(int(row.source_remote_id))
            except Exception:
                try:
                    if account is None:
                        raise MigrationError("本地账号不存在，无法恢复源节点")
                    _restore_deleted_source(
                        target_engine,
                        source,
                        migration_id=migration_id,
                        account=account,
                        source_remote_id=int(row.source_remote_id),
                    )
                except Exception:
                    source_ok = False
            else:
                try:
                    _restore_source(source, int(row.source_remote_id))
                except Exception:
                    source_ok = False
        local_ok = _rollback_local_assignment(target_engine, migration_id)
        rollback_complete = cleanup_ok and source_ok and (
            local_ok or row.state not in {"committed", "cleanup_pending"}
        )
        return _set_failure(
            target_engine,
            migration_id,
            state="rolled_back" if rollback_complete else "rollback_required",
            step="rolled_back" if rollback_complete else "rollback_required",
            error=(
                "已执行人工回滚"
                if rollback_complete
                else "人工回滚未完全完成，需要继续处理远端副本"
            ),
        )


__all__ = [
    "MigrationConflict",
    "MigrationError",
    "MigrationResult",
    "get_migration",
    "plan_migration",
    "reassign_account_pool",
    "resume_pending_migrations",
    "rollback_migration",
    "run_migration",
]
