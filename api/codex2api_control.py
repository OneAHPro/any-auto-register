"""Account-control plane API for multiple Codex2API targets."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, update
from sqlmodel import Session, select

from core.config_store import ConfigItem
from core.db import (
    AccountAssignmentEventModel,
    AccountAssignmentModel,
    AccountIdentityModel,
    AccountMigrationModel,
    AccountPoolModel,
    AccountQuotaSnapshotModel,
    AccountTargetBindingModel,
    AccountModel,
    ChatGPTAuthStateModel,
    Codex2APITargetModel,
    CustomerModel,
    PoolTargetPolicyModel,
    SchedulerRunModel,
    get_session,
)
from services.account_migration import (
    MigrationError,
    plan_migration,
    reassign_account_pool,
    rollback_migration,
    run_migration,
)
from services.codex2api_target_client import Codex2APITargetError, get_target_client
from services.control_plane_workers import collect_target_health, collect_target_quota
from services.pool_scheduler import (
    PlanConfirmationRequired,
    PlanError,
    PlanExpired,
    PoolAction,
    apply_confirmed_plan,
    confirm_plan,
    generate_scheduled_plans,
    load_plan,
)
from services.quota_ledger import history, latest_snapshot
from services.secret_store import SecretStoreError, seal_secret


router = APIRouter(tags=["codex2api-control"])

_POOL_ID_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
_TARGET_TYPES = {"public", "enterprise", "float", "standby"}
_POOL_TYPES = {"public", "enterprise", "float", "standby"}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _operation_id() -> str:
    return str(uuid4())


def _normalize_url(value: str) -> str:
    text = str(value or "").strip()
    try:
        parsed = urlsplit(text)
    except ValueError:
        raise HTTPException(status_code=422, detail="Codex2API 地址格式无效") from None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise HTTPException(
            status_code=422,
            detail="Codex2API 地址必须是无凭证、查询参数和片段的 HTTP(S) 地址",
        )
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme, host, path, "", ""))


def _safe_json(value: str, default: Any) -> Any:
    try:
        parsed = json.loads(value or "")
    except (TypeError, ValueError):
        return default
    return parsed


_PRIVATE_PLAN_KEYS = {
    "credential_revision",
    "expected_credential_revision",
    "admin_key",
    "password",
    "token",
    "access_token",
    "refresh_token",
    "cookie",
}


def _public_plan_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _public_plan_value(child)
            for key, child in value.items()
            if str(key).lower() not in _PRIVATE_PLAN_KEYS
        }
    if isinstance(value, list):
        return [_public_plan_value(child) for child in value]
    return value


def _positive_int(value: Any) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


def _target_payload(target: Codex2APITargetModel, binding_count: int = 0) -> dict[str, Any]:
    return {
        "id": int(target.id or 0),
        "name": target.name,
        "target_type": target.target_type,
        "server_label": target.server_label,
        "base_url": target.base_url,
        "admin_key": "********" if target.admin_key_ref else "",
        "default_pool_id": target.default_pool_id,
        "enabled": bool(target.enabled),
        "health_status": target.health_status,
        "health_success_count": int(target.health_success_count or 0),
        "health_failure_count": int(target.health_failure_count or 0),
        "capabilities": _safe_json(target.capability_json, {}),
        "last_health_at": target.last_health_at.isoformat()
        if target.last_health_at
        else None,
        "last_sync_at": target.last_sync_at.isoformat()
        if target.last_sync_at
        else None,
        "last_error": target.last_error,
        "account_count": int(binding_count),
    }


def _quota_payload(snapshot) -> dict[str, Any]:
    return {
        "window": snapshot.window,
        "continuous_billed_usd": float(snapshot.continuous_billed_usd),
        "billed_usd": float(snapshot.billed_usd)
        if snapshot.billed_usd is not None
        else None,
        "remaining_usd": float(snapshot.remaining_usd)
        if snapshot.remaining_usd is not None
        else None,
        "continuous_remaining_usd": float(snapshot.continuous_remaining_usd)
        if snapshot.continuous_remaining_usd is not None
        else None,
        "remaining_scope": snapshot.remaining_scope,
        "usage_percent": float(snapshot.usage_percent)
        if snapshot.usage_percent is not None
        else None,
        "reset_at": snapshot.reset_at.isoformat() if snapshot.reset_at else None,
        "captured_at": snapshot.captured_at.isoformat(),
        "source_updated_at": snapshot.source_updated_at.isoformat()
        if snapshot.source_updated_at
        else None,
        "continuity_state": snapshot.continuity_state,
        "fresh": snapshot.fresh,
        "scheduler_eligible": snapshot.scheduler_eligible,
    }


class TargetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    target_type: str = "public"
    server_label: str = Field(default="", max_length=120)
    base_url: str
    admin_key: str = Field(min_length=1, max_length=4096)
    default_pool_id: str = Field(default="PUBLIC_POOL", max_length=64)
    enabled: bool = True


class TargetUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    target_type: str | None = None
    server_label: str | None = Field(default=None, max_length=120)
    base_url: str | None = None
    admin_key: str | None = Field(default=None, max_length=4096)
    default_pool_id: str | None = Field(default=None, max_length=64)
    enabled: bool | None = None


class PoolCreate(BaseModel):
    id: str
    name: str = Field(min_length=1, max_length=80)
    pool_type: str = "enterprise"
    customer_id: str = Field(default="", max_length=80)
    customer_name: str = Field(default="", max_length=120)
    target_id: int
    remote_api_key_ids: list[int] = Field(default_factory=list)
    bandwidth_mbps: int = Field(default=0, ge=0, le=100000)
    min_accounts: int = Field(default=0, ge=0, le=10000)
    max_accounts: int = Field(default=0, ge=0, le=10000)
    safe_concurrency_per_account: int = Field(default=1, ge=1, le=1000)
    min_lease_hours: int = Field(default=6, ge=1, le=720)


class SchedulerPlanRequest(BaseModel):
    pool_id: str = Field(default="", max_length=64)


class SchedulerApplyRequest(BaseModel):
    run_id: str
    confirm: bool = False


class AssignmentRequest(BaseModel):
    target_id: int
    pool_id: str
    reason: str = Field(default="manual_assignment", max_length=200)


@router.get("/codex2api/targets")
def list_targets(session: Session = Depends(get_session)):
    targets = session.exec(
        select(Codex2APITargetModel).order_by(Codex2APITargetModel.id)
    ).all()
    counts = {
        int(target_id): int(count)
        for target_id, count in session.exec(
            select(AccountTargetBindingModel.target_id, func.count(AccountTargetBindingModel.id))
            .where(AccountTargetBindingModel.enabled == True)  # noqa: E712
            .group_by(AccountTargetBindingModel.target_id)
        ).all()
    }
    return {
        "targets": [
            _target_payload(target, counts.get(int(target.id or 0), 0))
            for target in targets
        ]
    }


@router.post("/codex2api/targets", status_code=status.HTTP_201_CREATED)
def create_target(body: TargetCreate, session: Session = Depends(get_session)):
    target_type = str(body.target_type or "").strip().lower()
    if target_type not in _TARGET_TYPES:
        raise HTTPException(status_code=422, detail="目标类型无效")
    name = body.name.strip()
    if session.exec(
        select(Codex2APITargetModel).where(Codex2APITargetModel.name == name)
    ).first() is not None:
        raise HTTPException(status_code=409, detail="目标名称已存在")
    max_id = session.exec(select(func.max(Codex2APITargetModel.id))).one() or 0
    target_id = int(max_id) + 1
    ref = f"codex2api_target_{target_id}_admin_key"
    try:
        sealed = seal_secret(body.admin_key)
    except SecretStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    ConfigItem.__table__.create(bind=session.get_bind(), checkfirst=True)
    row = Codex2APITargetModel(
        id=target_id,
        name=name,
        target_type=target_type,
        server_label=body.server_label.strip(),
        base_url=_normalize_url(body.base_url),
        admin_key_ref=ref,
        default_pool_id=body.default_pool_id.strip() or "PUBLIC_POOL",
        enabled=bool(body.enabled),
        health_status="unknown",
    )
    session.add(row)
    session.add(ConfigItem(key=ref, value=sealed))
    session.commit()
    session.refresh(row)
    return {
        "operation_id": _operation_id(),
        "status": "committed",
        "target": _target_payload(row),
    }


@router.patch("/codex2api/targets/{target_id}")
def update_target(
    target_id: int,
    body: TargetUpdate,
    session: Session = Depends(get_session),
):
    row = session.get(Codex2APITargetModel, int(target_id))
    if row is None:
        raise HTTPException(status_code=404, detail="目标不存在")
    if body.name is not None:
        name = body.name.strip()
        conflict = session.exec(
            select(Codex2APITargetModel).where(
                Codex2APITargetModel.name == name,
                Codex2APITargetModel.id != int(target_id),
            )
        ).first()
        if conflict is not None:
            raise HTTPException(status_code=409, detail="目标名称已存在")
        row.name = name
    if body.target_type is not None:
        target_type = body.target_type.strip().lower()
        if target_type not in _TARGET_TYPES:
            raise HTTPException(status_code=422, detail="目标类型无效")
        row.target_type = target_type
    if body.server_label is not None:
        row.server_label = body.server_label.strip()
    if body.base_url is not None:
        row.base_url = _normalize_url(body.base_url)
    if body.default_pool_id is not None:
        row.default_pool_id = body.default_pool_id.strip() or "PUBLIC_POOL"
    if body.enabled is not None:
        row.enabled = bool(body.enabled)
    if body.admin_key is not None and body.admin_key.strip():
        try:
            sealed = seal_secret(body.admin_key)
        except SecretStoreError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from None
        secret = session.get(ConfigItem, row.admin_key_ref)
        if secret is None:
            secret = ConfigItem(key=row.admin_key_ref, value=sealed)
        else:
            secret.value = sealed
        session.add(secret)
    row.updated_at = _utcnow()
    session.add(row)
    session.commit()
    session.refresh(row)
    return {
        "operation_id": _operation_id(),
        "status": "committed",
        "target": _target_payload(row),
    }


@router.post("/codex2api/targets/{target_id}/health")
def check_target_health(target_id: int, session: Session = Depends(get_session)):
    if session.get(Codex2APITargetModel, int(target_id)) is None:
        raise HTTPException(status_code=404, detail="目标不存在")
    try:
        client = get_target_client(int(target_id), session.get_bind())
        result = collect_target_health(
            session.get_bind(),
            target_id=int(target_id),
            client=client,
        )
    except Codex2APITargetError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None
    except SecretStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    return {
        "operation_id": _operation_id(),
        "status": "committed",
        "target_id": result.target_id,
        "health_status": result.health_status,
        "health_success_count": result.health_success_count,
        "health_failure_count": result.health_failure_count,
        "capabilities": result.capabilities,
        "last_error": result.last_error,
    }


@router.get("/codex2api/pools")
def list_pools(session: Session = Depends(get_session)):
    pools = session.exec(select(AccountPoolModel).order_by(AccountPoolModel.id)).all()
    policies = {
        str(policy.pool_id): policy
        for policy in session.exec(
            select(PoolTargetPolicyModel).order_by(
                PoolTargetPolicyModel.priority,
                PoolTargetPolicyModel.id,
            )
        ).all()
    }
    return {
        "pools": [
            {
                **pool.model_dump(),
                "target_id": int(policies[str(pool.id)].target_id)
                if str(pool.id) in policies
                else None,
                "remote_api_key_ids": _safe_json(
                    policies[str(pool.id)].remote_api_key_ids_json,
                    [],
                )
                if str(pool.id) in policies
                else [],
            }
            for pool in pools
        ]
    }


@router.post("/codex2api/pools", status_code=status.HTTP_201_CREATED)
def create_pool(body: PoolCreate, session: Session = Depends(get_session)):
    pool_id = body.id.strip().upper()
    if not _POOL_ID_RE.fullmatch(pool_id):
        raise HTTPException(status_code=422, detail="号池 ID 格式无效")
    pool_type = body.pool_type.strip().lower()
    if pool_type not in _POOL_TYPES:
        raise HTTPException(status_code=422, detail="号池类型无效")
    if body.max_accounts and body.max_accounts < body.min_accounts:
        raise HTTPException(status_code=422, detail="最大账号数不能小于最小账号数")
    if session.get(AccountPoolModel, pool_id) is not None:
        raise HTTPException(status_code=409, detail="号池已存在")
    target = session.get(Codex2APITargetModel, int(body.target_id))
    if target is None:
        raise HTTPException(status_code=404, detail="目标不存在")
    customer_id = body.customer_id.strip()
    if customer_id and session.get(CustomerModel, customer_id) is None:
        session.add(
            CustomerModel(
                id=customer_id,
                name=body.customer_name.strip() or customer_id,
            )
        )
    pool = AccountPoolModel(
        id=pool_id,
        name=body.name.strip(),
        pool_type=pool_type,
        customer_id=customer_id,
        min_accounts=int(body.min_accounts),
        max_accounts=int(body.max_accounts),
        safe_concurrency_per_account=int(body.safe_concurrency_per_account),
        min_lease_hours=int(body.min_lease_hours),
        enabled=True,
    )
    policy = PoolTargetPolicyModel(
        pool_id=pool_id,
        target_id=int(body.target_id),
        priority=1,
        remote_api_key_ids_json=json.dumps(
            sorted({int(value) for value in body.remote_api_key_ids if int(value) > 0})
        ),
        bandwidth_mbps=int(body.bandwidth_mbps),
        min_accounts=int(body.min_accounts),
        max_accounts=int(body.max_accounts),
        enabled=True,
    )
    session.add(pool)
    session.add(policy)
    session.commit()
    session.refresh(pool)
    return {
        "operation_id": _operation_id(),
        "status": "committed",
        "pool": {
            **pool.model_dump(),
            "target_id": int(policy.target_id),
            "remote_api_key_ids": _safe_json(policy.remote_api_key_ids_json, []),
            "bandwidth_mbps": policy.bandwidth_mbps,
        }
    }


@router.get("/accounts/{account_id}/quota")
def account_quota(account_id: int, session: Session = Depends(get_session)):
    account = session.get(AccountModel, int(account_id))
    if account is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    identity_id = str(account.identity_id or "")
    if not identity_id:
        raise HTTPException(status_code=409, detail="账号身份尚未完成归档")
    assignment = session.exec(
        select(AccountAssignmentModel).where(
            AccountAssignmentModel.identity_id == identity_id,
            AccountAssignmentModel.state.in_(["active", "draining", "standby"]),
        )
    ).first()
    windows = {}
    for window in ("5h", "7d", "monthly"):
        snapshot = latest_snapshot(
            session.get_bind(),
            identity_id=identity_id,
            window=window,
            target_id=int(assignment.target_id) if assignment else None,
        )
        if snapshot is not None:
            windows[window] = _quota_payload(snapshot)
    return {
        "account_id": int(account_id),
        "identity_id": identity_id,
        "target_id": int(assignment.target_id) if assignment else None,
        "pool_id": assignment.pool_id if assignment else None,
        "windows": windows,
    }


@router.get("/accounts/{account_id}/quota/history")
def account_quota_history(
    account_id: int,
    window: str = "7d",
    limit: int = 200,
    session: Session = Depends(get_session),
):
    account = session.get(AccountModel, int(account_id))
    if account is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    identity_id = str(account.identity_id or "")
    rows = history(
        session.get_bind(),
        identity_id=identity_id,
        window=window,
        limit=limit,
    )
    return {"items": [_quota_payload(row) for row in rows]}


@router.post("/accounts/{account_id}/quota/refresh")
def refresh_account_quota(account_id: int, session: Session = Depends(get_session)):
    account = session.get(AccountModel, int(account_id))
    if account is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    assignment = session.exec(
        select(AccountAssignmentModel).where(
            AccountAssignmentModel.identity_id == account.identity_id,
            AccountAssignmentModel.state == "active",
        )
    ).first()
    if assignment is None:
        raise HTTPException(status_code=409, detail="账号尚未绑定目标")
    try:
        client = get_target_client(int(assignment.target_id), session.get_bind())
        result = collect_target_quota(
            session.get_bind(),
            target_id=int(assignment.target_id),
            client=client,
        )
    except Codex2APITargetError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None
    except SecretStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    return {
        "operation_id": _operation_id(),
        "status": "committed",
        "target_id": result.target_id,
        "collected_accounts": result.collected_accounts,
    }


def _latest_run(session: Session) -> SchedulerRunModel | None:
    return session.exec(
        select(SchedulerRunModel).order_by(SchedulerRunModel.created_at.desc())
    ).first()


def _run_payload(run: SchedulerRunModel, session: Session | None = None) -> dict[str, Any]:
    plan = _public_plan_value(_safe_json(run.plan_json, {}))
    if session is not None and isinstance(plan, dict):
        actions = plan.get("actions")
        if isinstance(actions, list):
            account_ids = {
                _positive_int(action.get("local_account_id"))
                for action in actions
                if isinstance(action, dict)
                and _positive_int(action.get("local_account_id")) > 0
            }
            emails = {
                int(account.id or 0): account.email
                for account in session.exec(
                    select(AccountModel).where(AccountModel.id.in_(account_ids))
                ).all()
            } if account_ids else {}
            plan["actions"] = [
                {
                    **action,
                    "email": emails.get(_positive_int(action.get("local_account_id")), ""),
                }
                if isinstance(action, dict)
                else action
                for action in actions
            ]
    return {
        "id": run.id,
        "mode": run.mode,
        "status": run.status,
        "trigger": run.trigger,
        "plan": plan,
        "executed": _safe_json(run.executed_json, []),
        "errors": _safe_json(run.error_json, {}),
        "created_at": run.created_at.isoformat(),
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
    }


@router.get("/scheduler/plan")
def get_scheduler_plan(session: Session = Depends(get_session)):
    run = _latest_run(session)
    return {"run": _run_payload(run, session) if run is not None else None}


@router.post("/scheduler/plan")
def create_scheduler_plan(
    body: SchedulerPlanRequest,
    session: Session = Depends(get_session),
):
    pool_id = body.pool_id.strip().upper()
    if pool_id and not _POOL_ID_RE.fullmatch(pool_id):
        raise HTTPException(status_code=422, detail="号池 ID 格式无效")
    runs = generate_scheduled_plans(session.get_bind(), pool_id=pool_id)
    rows = [
        row
        for run in runs
        if (row := session.get(SchedulerRunModel, run.id)) is not None
    ]
    return {
        "operation_id": runs[-1].id if runs else _operation_id(),
        "status": "awaiting_confirmation",
        "runs": [_run_payload(row, session) for row in rows],
    }


def _validate_action_targets(session: Session, action: PoolAction) -> None:
    same_target_pool_move = action.source_target_id == action.destination_target_id
    if int(action.local_account_id or 0) <= 0 and not same_target_pool_move:
        raise PlanError("远端托管账号跨目标调整需要原始 JSON")
    if same_target_pool_move:
        destination_pool_id = str(action.destination_pool_id or "").strip().upper()
        if not destination_pool_id or destination_pool_id == str(action.source_pool_id or "").strip().upper():
            raise PlanError("同目标调整没有不同的目标号池")
        destination_pool = session.get(AccountPoolModel, destination_pool_id)
        if destination_pool is None or not destination_pool.enabled:
            raise PlanError("同目标调整的目标号池不可用")
    for target_id in (action.source_target_id, action.destination_target_id):
        target = session.get(Codex2APITargetModel, int(target_id))
        if target is None or not target.enabled or target.health_status != "healthy":
            raise PlanError("scheduler target is unavailable")
        capabilities = _safe_json(target.capability_json, {})
        if (
            target_id == action.destination_target_id
            and not same_target_pool_move
            and capabilities.get("migratable") is not True
        ):
            raise PlanError("destination target is not migration-ready")
    assignment = session.exec(
        select(AccountAssignmentModel).where(
            AccountAssignmentModel.identity_id == action.identity_id,
            AccountAssignmentModel.local_account_id == action.local_account_id,
            AccountAssignmentModel.state.in_(["active", "draining", "standby"]),
        )
    ).first()
    if (
        assignment is None
        or int(assignment.target_id) != int(action.source_target_id)
        or int(assignment.assignment_version or 0) != int(action.assignment_version)
    ):
        raise PlanError("scheduler assignment snapshot is stale")
    quota = latest_snapshot(
        session.get_bind(),
        identity_id=action.identity_id,
        window="7d",
        target_id=int(action.source_target_id),
    )
    if quota is None or not quota.scheduler_eligible:
        raise PlanError("scheduler quota snapshot is stale")


@router.post("/scheduler/apply", status_code=status.HTTP_202_ACCEPTED)
def apply_scheduler_plan(
    body: SchedulerApplyRequest,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
):
    if not body.confirm:
        raise HTTPException(status_code=409, detail="请确认后执行")
    run = load_plan(session.get_bind(), body.run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="调度计划不存在")
    stored_run = session.get(SchedulerRunModel, body.run_id)
    stored_plan = _safe_json(stored_run.plan_json, {}) if stored_run is not None else {}
    if (
        stored_plan.get("executable") is False
        or stored_plan.get("blockers")
    ):
        raise HTTPException(status_code=409, detail="调度计划当前不可执行")
    if run.status == "awaiting_confirmation":
        run = confirm_plan(session.get_bind(), body.run_id)

    def queue(action: PoolAction) -> str:
        _validate_action_targets(session, action)
        source_pool_id = str(action.source_pool_id or "").strip().upper()
        destination_pool_id = str(action.destination_pool_id or "").strip().upper()
        if action.source_target_id == action.destination_target_id:
            return reassign_account_pool(
                session.get_bind(),
                identity_id=action.identity_id,
                local_account_id=action.local_account_id,
                source_target_id=action.source_target_id,
                source_pool_id=source_pool_id,
                destination_pool_id=destination_pool_id,
                expected_assignment_version=action.assignment_version,
                reason=action.reason,
            )
        return plan_migration(
            session.get_bind(),
            identity_id=action.identity_id,
            local_account_id=action.local_account_id,
            source_target_id=action.source_target_id,
            destination_target_id=action.destination_target_id,
            expected_assignment_version=action.assignment_version,
            expected_credential_revision=action.credential_revision,
            idempotency_key=f"scheduler:{body.run_id}:{action.identity_id}:{action.action}",
            plan={
                "source_pool_id": source_pool_id,
                "destination_pool_id": destination_pool_id or run.pool_id,
                "reason": action.reason,
            },
        )

    try:
        applied = apply_confirmed_plan(
            session.get_bind(),
            body.run_id,
            migration_runner=queue,
        )
    except PlanConfirmationRequired as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except PlanExpired as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except (PlanError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    background_tasks.add_task(_run_pending_migrations, session.get_bind())
    return {"run_id": applied.id, "status": applied.status}


def _run_pending_migrations(database_engine) -> None:
    with Session(database_engine) as session:
        ids = [
            str(row.id)
            for row in session.exec(
                select(AccountMigrationModel).where(
                    AccountMigrationModel.state.not_in(
                        ["committed", "rolled_back", "rollback_required"]
                    )
                )
            ).all()
        ]
    for migration_id in ids:
        try:
            run_migration(database_engine, migration_id)
        except Exception:
            continue


@router.get("/scheduler/runs")
def list_scheduler_runs(limit: int = 50, session: Session = Depends(get_session)):
    rows = session.exec(
        select(SchedulerRunModel)
        .order_by(SchedulerRunModel.created_at.desc())
        .limit(max(1, min(int(limit), 200)))
    ).all()
    return {"runs": [_run_payload(row, session) for row in rows]}


@router.post("/accounts/{account_id}/assignment", status_code=status.HTTP_202_ACCEPTED)
def assign_account(
    account_id: int,
    body: AssignmentRequest,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
):
    account = session.get(AccountModel, int(account_id))
    if account is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    identity = session.get(AccountIdentityModel, str(account.identity_id or ""))
    if identity is None or identity.state != "active":
        raise HTTPException(status_code=409, detail="账号身份不可迁移")
    assignment = session.exec(
        select(AccountAssignmentModel).where(
            AccountAssignmentModel.identity_id == identity.id,
            AccountAssignmentModel.state == "active",
        )
    ).first()
    if assignment is None:
        raise HTTPException(status_code=409, detail="账号当前归属不存在")
    target = session.get(Codex2APITargetModel, int(body.target_id))
    if target is None or not target.enabled:
        raise HTTPException(status_code=409, detail="目标不可用")
    capabilities = _safe_json(target.capability_json, {})
    if target.health_status != "healthy" or capabilities.get("migratable") is not True:
        raise HTTPException(status_code=409, detail="目标尚未达到迁移条件")
    pool_id = body.pool_id.strip().upper()
    if session.get(AccountPoolModel, pool_id) is None:
        # Manual migrations may create the first enterprise pool together with
        # the target; retain a strict, visible default rather than accepting a
        # misspelled opaque pool id.
        raise HTTPException(status_code=404, detail="号池不存在")
    if int(assignment.target_id) == int(body.target_id):
        previous_pool = assignment.pool_id
        previous_version = int(assignment.assignment_version or 0)
        result = session.exec(
            update(AccountAssignmentModel)
            .where(AccountAssignmentModel.id == assignment.id)
            .where(AccountAssignmentModel.assignment_version == previous_version)
            .values(
                pool_id=pool_id,
                lease_reason=body.reason,
                assignment_version=previous_version + 1,
                updated_at=_utcnow(),
            )
        )
        if int(getattr(result, "rowcount", 0) or 0) != 1:
            session.rollback()
            raise HTTPException(status_code=409, detail="账号归属已变化")
        session.add(
            AccountAssignmentEventModel(
                identity_id=identity.id,
                local_account_id=int(account.id or 0),
                event_type="pool_reassigned",
                from_pool_id=previous_pool,
                to_pool_id=pool_id,
                from_target_id=int(assignment.target_id),
                to_target_id=int(assignment.target_id),
                assignment_version=previous_version + 1,
                reason=body.reason,
            )
        )
        session.commit()
        return {
            "operation_id": f"assignment:{int(account.id)}:{previous_version + 1}",
            "status": "committed",
        }
    source_binding = session.exec(
        select(AccountTargetBindingModel).where(
            AccountTargetBindingModel.identity_id == identity.id,
            AccountTargetBindingModel.target_id == assignment.target_id,
        )
    ).first()
    auth_state = session.exec(
        select(ChatGPTAuthStateModel).where(
            ChatGPTAuthStateModel.account_id == int(account.id or 0)
        )
    ).first()
    credential_revision = str(
        auth_state.credential_revision
        if auth_state is not None
        else source_binding.credential_revision
        if source_binding is not None
        else ""
    )
    try:
        operation_id = plan_migration(
            session.get_bind(),
            identity_id=identity.id,
            local_account_id=int(account.id or 0),
            source_target_id=int(assignment.target_id),
            destination_target_id=int(body.target_id),
            expected_assignment_version=int(assignment.assignment_version or 0),
            expected_credential_revision=credential_revision,
            idempotency_key=(
                f"manual:{identity.id}:{assignment.assignment_version}:"
                f"{body.target_id}:{pool_id}"
            ),
            plan={
                "source_pool_id": assignment.pool_id,
                "destination_pool_id": pool_id,
                "reason": body.reason,
            },
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    background_tasks.add_task(run_migration, session.get_bind(), operation_id)
    return {"operation_id": operation_id, "status": "planned"}


@router.get("/accounts/{account_id}/migrations")
def account_migrations(account_id: int, session: Session = Depends(get_session)):
    rows = session.exec(
        select(AccountMigrationModel)
        .where(AccountMigrationModel.local_account_id == int(account_id))
        .order_by(AccountMigrationModel.created_at.desc())
    ).all()
    return {
        "migrations": [
            {
                "id": row.id,
                "state": row.state,
                "step": row.step,
                "source_target_id": row.source_target_id,
                "destination_target_id": row.destination_target_id,
                "source_remote_id": row.source_remote_id,
                "destination_remote_id": row.destination_remote_id,
                "error": _safe_json(row.error_json, {}),
                "created_at": row.created_at.isoformat(),
                "updated_at": row.updated_at.isoformat(),
                "completed_at": row.completed_at.isoformat()
                if row.completed_at
                else None,
            }
            for row in rows
        ]
    }


@router.post("/migrations/{migration_id}/rollback")
def rollback_account_migration(
    migration_id: str,
    session: Session = Depends(get_session),
):
    try:
        result = rollback_migration(session.get_bind(), migration_id)
    except MigrationError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    return {
        "operation_id": migration_id,
        "status": result.state,
        **result.__dict__,
    }


__all__ = ["router"]
