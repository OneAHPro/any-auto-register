"""Pure account-pool capacity planning plus durable manual confirmation."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Callable, Iterable, Mapping
from uuid import uuid4

from sqlmodel import Session, select

from core.db import (
    AccountAssignmentModel,
    AccountIdentityModel,
    AccountPoolModel,
    AccountQuotaSnapshotModel,
    AccountTargetBindingModel,
    ChatGPTAuthStateModel,
    Codex2APITargetModel,
    CustomerUsageSampleModel,
    PoolTargetPolicyModel,
    SchedulerActionModel,
    SchedulerRunModel,
    engine as default_engine,
)


CENT = Decimal("0.01")
DEFAULT_SAFE_7D_QUOTA = Decimal("1800.00")
DEFAULT_PLAN_TTL = timedelta(minutes=15)


class PlanError(RuntimeError):
    pass


class PlanConfirmationRequired(PlanError):
    pass


class PlanExpired(PlanError):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime:
    result = value or _utcnow()
    if result.tzinfo is None:
        return result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _decimal(value: Any, default: Decimal = Decimal("0")) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return default
    return parsed if parsed.is_finite() else default


def _money(value: Any) -> Decimal:
    return _decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def _ceil_ratio(numerator: Decimal, denominator: Decimal) -> int:
    if numerator <= 0 or denominator <= 0:
        return 0
    return int(math.ceil(float(numerator / denominator)))


@dataclass(frozen=True)
class AccountCandidate:
    identity_id: str
    local_account_id: int = 0
    health: str = "healthy"
    remaining_usd: Decimal = Decimal("0")
    already_on_target: bool = False
    reset_at: datetime | None = None
    stability_score: Decimal = Decimal("0")
    pool_type: str = "float"
    source_target_id: int = 0
    destination_target_id: int = 0
    assignment_version: int = 0
    credential_revision: str = ""
    active_requests: int = 0
    lease_elapsed: bool = True


@dataclass(frozen=True)
class PoolInput:
    pool_id: str
    forecast_7d_usd: Decimal = Decimal("0")
    safe_7d_quota: Decimal = DEFAULT_SAFE_7D_QUOTA
    historical_7d_outputs: tuple[Decimal, ...] = ()
    peak_concurrency: int = 0
    safe_concurrency_per_account: int = 1
    pool_min_accounts: int = 0
    pool_max_accounts: int = 0
    current_accounts: int = 0
    utilization: Decimal = Decimal("0")
    low_utilization_cycles: int = 0
    min_lease_elapsed: bool = True
    quota_fresh: bool = True
    target_healthy: bool = True
    confirmation_required: bool = True
    candidates: tuple[AccountCandidate, ...] = ()


@dataclass(frozen=True)
class PoolAction:
    identity_id: str
    local_account_id: int
    action: str
    source_target_id: int
    destination_target_id: int
    assignment_version: int
    credential_revision: str
    reason: str


@dataclass(frozen=True)
class PoolPlan:
    pool_id: str
    current_count: int
    desired_count: int
    scale_up_count: int
    scale_down_count: int
    executable: bool
    requires_confirmation: bool
    blockers: tuple[str, ...] = ()
    actions: tuple[PoolAction, ...] = ()


@dataclass(frozen=True)
class CostEstimate:
    revenue_cny: Decimal
    account_cost_cny: Decimal
    bandwidth_cost_cny: Decimal
    operations_cost_cny: Decimal
    margin_cny: Decimal


@dataclass(frozen=True)
class PersistedPlan:
    id: str
    status: str
    mode: str
    pool_id: str
    created_at: datetime
    actions: tuple[PoolAction, ...] = ()


def safe_quota(observations: Iterable[Decimal]) -> Decimal:
    values = sorted(_money(value) for value in observations if _decimal(value) > 0)
    if len(values) < 20:
        return DEFAULT_SAFE_7D_QUOTA
    index = int(math.floor((len(values) - 1) * 0.25))
    return values[index]


def rank_candidates(candidates: Iterable[AccountCandidate]) -> list[AccountCandidate]:
    health_rank = {
        "healthy": 0,
        "available": 0,
        "near_limit": 1,
        "cooldown": 2,
        "stale": 3,
        "unknown": 4,
        "error": 5,
    }

    def key(candidate: AccountCandidate):
        reset = _aware(candidate.reset_at).timestamp() if candidate.reset_at else float("inf")
        return (
            health_rank.get(str(candidate.health).lower(), 4),
            0 if candidate.already_on_target else 1,
            -float(_decimal(candidate.remaining_usd)),
            reset,
            -float(_decimal(candidate.stability_score)),
            1 if str(candidate.pool_type).lower() == "float" else 0,
            str(candidate.identity_id),
        )

    return sorted(candidates, key=key)


def estimate_costs(
    *,
    customer_usage_usd: Decimal,
    customer_price_cny_per_usd: Decimal,
    account_count: int,
    account_monthly_rent_cny: Decimal,
    occupancy_ratio: Decimal,
    bandwidth_mbps: int,
    bandwidth_price_per_mbps_cny: Decimal,
    operations_cost_cny: Decimal,
) -> CostEstimate:
    revenue = _money(_decimal(customer_usage_usd) * _decimal(customer_price_cny_per_usd))
    account_cost = _money(
        Decimal(max(int(account_count), 0))
        * _decimal(account_monthly_rent_cny)
        * max(min(_decimal(occupancy_ratio), Decimal("1")), Decimal("0"))
    )
    bandwidth_cost = _money(
        Decimal(max(int(bandwidth_mbps), 0))
        * _decimal(bandwidth_price_per_mbps_cny)
    )
    operations_cost = _money(operations_cost_cny)
    return CostEstimate(
        revenue_cny=revenue,
        account_cost_cny=account_cost,
        bandwidth_cost_cny=bandwidth_cost,
        operations_cost_cny=operations_cost,
        margin_cny=_money(revenue - account_cost - bandwidth_cost - operations_cost),
    )


def plan_pool(pool: PoolInput) -> PoolPlan:
    blockers: list[str] = []
    if not pool.quota_fresh:
        blockers.append("quota_stale")
    if not pool.target_healthy:
        blockers.append("target_unhealthy")
    if blockers:
        return PoolPlan(
            pool_id=pool.pool_id,
            current_count=max(int(pool.current_accounts), 0),
            desired_count=max(int(pool.current_accounts), 0),
            scale_up_count=0,
            scale_down_count=0,
            executable=False,
            requires_confirmation=False,
            blockers=tuple(blockers),
        )

    quota = (
        safe_quota(pool.historical_7d_outputs)
        if pool.historical_7d_outputs
        else _money(pool.safe_7d_quota or DEFAULT_SAFE_7D_QUOTA)
    )
    required_by_quota = _ceil_ratio(_decimal(pool.forecast_7d_usd), quota)
    required_by_concurrency = _ceil_ratio(
        Decimal(max(int(pool.peak_concurrency), 0)),
        Decimal(max(int(pool.safe_concurrency_per_account), 1)),
    )
    desired = max(required_by_quota, required_by_concurrency, int(pool.pool_min_accounts))
    if int(pool.pool_max_accounts) > 0:
        desired = min(desired, int(pool.pool_max_accounts))
    current = max(int(pool.current_accounts), 0)
    scale_up = max(desired - current, 0)
    scale_down = 0
    if current > desired:
        if (
            _decimal(pool.utilization) < Decimal("0.60")
            and int(pool.low_utilization_cycles) >= 2
            and bool(pool.min_lease_elapsed)
        ):
            scale_down = current - desired

    ranked = rank_candidates(pool.candidates)
    actions: list[PoolAction] = []
    for candidate in ranked[:scale_up]:
        if (
            str(candidate.health).lower() not in {"healthy", "available"}
            or int(candidate.active_requests) > 0
            or not candidate.lease_elapsed
        ):
            continue
        actions.append(
            PoolAction(
                identity_id=candidate.identity_id,
                local_account_id=int(candidate.local_account_id),
                action="scale_up",
                source_target_id=int(candidate.source_target_id),
                destination_target_id=int(candidate.destination_target_id),
                assignment_version=int(candidate.assignment_version),
                credential_revision=str(candidate.credential_revision or ""),
                reason="forecast_capacity_required",
            )
        )
    if scale_down:
        removable = [
            candidate
            for candidate in reversed(ranked)
            if candidate.lease_elapsed and int(candidate.active_requests) == 0
        ]
        for candidate in removable[:scale_down]:
            actions.append(
                PoolAction(
                    identity_id=candidate.identity_id,
                    local_account_id=int(candidate.local_account_id),
                    action="scale_down",
                    source_target_id=int(candidate.source_target_id),
                    destination_target_id=int(candidate.destination_target_id),
                    assignment_version=int(candidate.assignment_version),
                    credential_revision=str(candidate.credential_revision or ""),
                    reason="sustained_low_utilization",
                )
            )
    return PoolPlan(
        pool_id=pool.pool_id,
        current_count=current,
        desired_count=desired,
        scale_up_count=scale_up,
        scale_down_count=scale_down,
        executable=True,
        requires_confirmation=bool(pool.confirmation_required and (scale_up or scale_down)),
        actions=tuple(actions),
    )


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return _aware(value).isoformat()
    if isinstance(value, Mapping):
        return {str(key): _jsonable(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(child) for child in value]
    return value


def _action_from_dict(value: Mapping[str, Any]) -> PoolAction:
    return PoolAction(
        identity_id=str(value.get("identity_id") or ""),
        local_account_id=int(value.get("local_account_id") or 0),
        action=str(value.get("action") or ""),
        source_target_id=int(value.get("source_target_id") or 0),
        destination_target_id=int(value.get("destination_target_id") or 0),
        assignment_version=int(value.get("assignment_version") or 0),
        credential_revision=str(value.get("credential_revision") or ""),
        reason=str(value.get("reason") or ""),
    )


def _persisted(row: SchedulerRunModel) -> PersistedPlan:
    try:
        payload = json.loads(row.plan_json or "{}")
    except (TypeError, ValueError):
        payload = {}
    actions = tuple(
        _action_from_dict(item)
        for item in payload.get("actions", [])
        if isinstance(item, Mapping)
    )
    return PersistedPlan(
        id=str(row.id),
        status=str(row.status),
        mode=str(row.mode),
        pool_id=str(payload.get("pool_id") or ""),
        created_at=_aware(row.created_at),
        actions=actions,
    )


def create_dry_run(
    database_engine,
    pool_input: PoolInput,
    *,
    trigger: str = "manual",
    now: datetime | None = None,
) -> PersistedPlan:
    target_engine = database_engine or default_engine
    created_at = _aware(now)
    plan = plan_pool(pool_input)
    run_id = str(uuid4())
    plan_payload = {
        **_jsonable(asdict(plan)),
        "input": _jsonable(asdict(pool_input)),
        "created_at": created_at.isoformat(),
    }
    with Session(target_engine) as session:
        run = SchedulerRunModel(
            id=run_id,
            mode="dry_run",
            status="awaiting_confirmation",
            trigger=str(trigger or "manual"),
            plan_json=json.dumps(plan_payload, ensure_ascii=False, sort_keys=True),
            created_at=created_at,
        )
        session.add(run)
        for action in plan.actions:
            session.add(
                SchedulerActionModel(
                    run_id=run_id,
                    identity_id=action.identity_id,
                    action=action.action,
                    source_target_id=action.source_target_id,
                    destination_target_id=action.destination_target_id,
                    reason=action.reason,
                    status="planned",
                    detail_json=json.dumps(
                        _jsonable(asdict(action)),
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    created_at=created_at,
                    updated_at=created_at,
                )
            )
        session.commit()
        session.refresh(run)
        return _persisted(run)


def load_plan(database_engine, run_id: str) -> PersistedPlan | None:
    target_engine = database_engine or default_engine
    with Session(target_engine) as session:
        row = session.get(SchedulerRunModel, str(run_id))
        return _persisted(row) if row is not None else None


def confirm_plan(
    database_engine,
    run_id: str,
    *,
    now: datetime | None = None,
) -> PersistedPlan:
    target_engine = database_engine or default_engine
    confirmed_at = _aware(now)
    with Session(target_engine) as session:
        row = session.get(SchedulerRunModel, str(run_id))
        if row is None:
            raise PlanError("scheduler plan does not exist")
        try:
            payload = json.loads(row.plan_json or "{}")
        except (TypeError, ValueError):
            raise PlanError("scheduler plan payload is invalid") from None
        payload["confirmed_at"] = confirmed_at.isoformat()
        row.plan_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        row.status = "confirmed"
        session.add(row)
        session.commit()
        session.refresh(row)
        return _persisted(row)


def apply_confirmed_plan(
    database_engine,
    run_id: str,
    *,
    migration_runner: Callable[[PoolAction], str],
    now: datetime | None = None,
    ttl: timedelta = DEFAULT_PLAN_TTL,
) -> PersistedPlan:
    target_engine = database_engine or default_engine
    applied_at = _aware(now)
    with Session(target_engine) as session:
        row = session.get(SchedulerRunModel, str(run_id))
        if row is None:
            raise PlanError("scheduler plan does not exist")
        if row.status != "confirmed":
            raise PlanConfirmationRequired("scheduler plan requires confirmation")
        try:
            payload = json.loads(row.plan_json or "{}")
            confirmed_at = datetime.fromisoformat(str(payload["confirmed_at"]))
        except (KeyError, TypeError, ValueError):
            raise PlanConfirmationRequired("scheduler plan confirmation is missing") from None
        if applied_at - _aware(confirmed_at) > ttl:
            row.status = "expired"
            session.add(row)
            session.commit()
            raise PlanExpired("scheduler plan expired before apply")
        actions = tuple(
            _action_from_dict(item)
            for item in payload.get("actions", [])
            if isinstance(item, Mapping)
        )
        executed: list[dict[str, str]] = []
        action_rows = session.exec(
            select(SchedulerActionModel)
            .where(SchedulerActionModel.run_id == str(run_id))
            .order_by(SchedulerActionModel.id)
        ).all()
        for action, action_row in zip(actions, action_rows):
            migration_id = str(migration_runner(action))
            action_row.status = "queued"
            action_row.detail_json = json.dumps(
                {**_jsonable(asdict(action)), "migration_id": migration_id},
                ensure_ascii=False,
                sort_keys=True,
            )
            action_row.updated_at = applied_at
            session.add(action_row)
            executed.append(
                {"identity_id": action.identity_id, "migration_id": migration_id}
            )
        row.mode = "apply"
        row.status = "queued"
        row.executed_json = json.dumps(executed, ensure_ascii=False, sort_keys=True)
        session.add(row)
        session.commit()
        session.refresh(row)
        return _persisted(row)


def ensure_default_pools(database_engine=None) -> list[AccountPoolModel]:
    """Create the three global pools once without altering configured pools."""

    target_engine = database_engine or default_engine
    defaults = (
        ("PUBLIC_POOL", "公共池", "public"),
        ("FLOAT_POOL", "浮动池", "float"),
        ("STANDBY_POOL", "备用池", "standby"),
    )
    with Session(target_engine) as session:
        rows: list[AccountPoolModel] = []
        for pool_id, name, pool_type in defaults:
            row = session.get(AccountPoolModel, pool_id)
            if row is None:
                row = AccountPoolModel(
                    id=pool_id,
                    name=name,
                    pool_type=pool_type,
                    min_accounts=0,
                    safe_concurrency_per_account=1,
                    min_lease_hours=6,
                    enabled=True,
                )
                session.add(row)
            rows.append(row)
        session.commit()
        for row in rows:
            session.refresh(row)
        return rows


def _latest_quota_by_identity(
    session: Session,
    identity_ids: set[str],
) -> dict[str, Any]:
    if not identity_ids:
        return {}
    rows = session.exec(
        select(AccountQuotaSnapshotModel)
        .where(AccountQuotaSnapshotModel.identity_id.in_(identity_ids))
        .where(AccountQuotaSnapshotModel.window == "7d")
        .order_by(AccountQuotaSnapshotModel.captured_at.desc())
    ).all()
    result: dict[str, Any] = {}
    for row in rows:
        result.setdefault(str(row.identity_id), row)
    return result


def _build_pool_candidates(
    session: Session,
    *,
    destination_target_id: int,
    destination_pool_id: str,
    now: datetime,
) -> tuple[AccountCandidate, ...]:
    assignments = session.exec(
        select(AccountAssignmentModel)
        .where(AccountAssignmentModel.state == "active")
        .where(AccountAssignmentModel.pool_id != destination_pool_id)
        .order_by(AccountAssignmentModel.updated_at)
    ).all()
    identity_ids = {str(row.identity_id) for row in assignments}
    identities = {
        str(row.id): row
        for row in session.exec(
            select(AccountIdentityModel).where(AccountIdentityModel.id.in_(identity_ids))
        ).all()
    } if identity_ids else {}
    quotas = _latest_quota_by_identity(session, identity_ids)
    auth_states = {
        int(row.account_id): row
        for row in session.exec(
            select(ChatGPTAuthStateModel).where(
                ChatGPTAuthStateModel.account_id.in_(
                    [int(item.local_account_id) for item in assignments]
                )
            )
        ).all()
    } if assignments else {}
    bindings = {
        (str(row.identity_id), int(row.target_id)): row
        for row in session.exec(
            select(AccountTargetBindingModel).where(
                AccountTargetBindingModel.identity_id.in_(identity_ids)
            )
        ).all()
    } if identity_ids else {}
    from services.quota_ledger import evaluate_snapshot

    candidates: list[AccountCandidate] = []
    for assignment in assignments:
        identity_id = str(assignment.identity_id)
        identity = identities.get(identity_id)
        quota_row = quotas.get(identity_id)
        quota = evaluate_snapshot(quota_row) if quota_row is not None else None
        binding = bindings.get((identity_id, int(assignment.target_id)))
        health = "healthy"
        if identity is None or str(identity.state) != "active":
            health = "ambiguous"
        elif binding is None or str(binding.remote_status or "").lower() not in {
            "active",
            "ready",
            "rate_limited",
        }:
            health = "error"
        elif quota is None or not quota.scheduler_eligible:
            health = "stale"
        auth = auth_states.get(int(assignment.local_account_id))
        lease_elapsed = bool(
            assignment.lease_started_at is None
            or now - _aware(assignment.lease_started_at)
            >= timedelta(hours=6)
        )
        candidates.append(
            AccountCandidate(
                identity_id=identity_id,
                local_account_id=int(assignment.local_account_id),
                health=health,
                remaining_usd=(
                    quota.remaining_usd
                    if quota is not None and quota.remaining_usd is not None
                    else Decimal("0")
                ),
                already_on_target=int(assignment.target_id) == int(destination_target_id),
                reset_at=quota.reset_at if quota is not None else None,
                stability_score=Decimal("1") if quota is not None else Decimal("0"),
                pool_type=str(assignment.pool_id),
                source_target_id=int(assignment.target_id),
                destination_target_id=int(destination_target_id),
                assignment_version=int(assignment.assignment_version or 0),
                credential_revision=str(
                    auth.credential_revision if auth is not None else ""
                ),
                lease_elapsed=lease_elapsed,
            )
        )
    return tuple(candidates)


def generate_scheduled_plans(
    database_engine=None,
    *,
    now: datetime | None = None,
) -> list[PersistedPlan]:
    """Build data-backed dry-run plans for every enabled logical pool."""

    target_engine = database_engine or default_engine
    generated_at = _aware(now)
    ensure_default_pools(target_engine)
    with Session(target_engine) as session:
        pools = session.exec(
            select(AccountPoolModel)
            .where(AccountPoolModel.enabled == True)  # noqa: E712
            .order_by(AccountPoolModel.id)
        ).all()
        assignments = session.exec(
            select(AccountAssignmentModel).where(
                AccountAssignmentModel.state == "active"
            )
        ).all()
        policies = session.exec(
            select(PoolTargetPolicyModel)
            .where(PoolTargetPolicyModel.enabled == True)  # noqa: E712
            .order_by(PoolTargetPolicyModel.priority, PoolTargetPolicyModel.id)
        ).all()
        targets = {
            int(row.id): row
            for row in session.exec(select(Codex2APITargetModel)).all()
            if row.id is not None
        }
        usage_rows = session.exec(
            select(CustomerUsageSampleModel).where(
                CustomerUsageSampleModel.bucket_start >= generated_at - timedelta(days=7)
            )
        ).all()
        previous_runs = session.exec(
            select(SchedulerRunModel)
            .where(SchedulerRunModel.trigger == "automatic")
            .order_by(SchedulerRunModel.created_at.desc())
            .limit(100)
        ).all()
        plans: list[tuple[PoolInput, str]] = []
        for pool in pools:
            current = sum(1 for row in assignments if row.pool_id == pool.id)
            pool_usage = [row for row in usage_rows if row.pool_id == pool.id]
            forecast = Decimal(sum(int(row.billed_cents or 0) for row in pool_usage)) / 100
            peak = max((int(row.peak_concurrency or 0) for row in pool_usage), default=0)
            policy = next((row for row in policies if row.pool_id == pool.id), None)
            destination_target_id = int(policy.target_id) if policy is not None else 0
            target = targets.get(destination_target_id)
            target_healthy = bool(
                target is not None
                and target.enabled
                and target.health_status == "healthy"
            )
            candidates = _build_pool_candidates(
                session,
                destination_target_id=destination_target_id,
                destination_pool_id=str(pool.id),
                now=generated_at,
            ) if destination_target_id else ()
            capacity = Decimal(max(current, 1)) * DEFAULT_SAFE_7D_QUOTA
            utilization = min(forecast / capacity, Decimal("1")) if capacity > 0 else Decimal("0")
            low_cycles = 0
            for previous in previous_runs:
                try:
                    previous_payload = json.loads(previous.plan_json or "{}")
                    if str(previous_payload.get("pool_id") or "") != str(pool.id):
                        continue
                    previous_utilization = _decimal(
                        (previous_payload.get("input") or {}).get("utilization")
                    )
                except (AttributeError, TypeError, ValueError):
                    continue
                if previous_utilization < Decimal("0.60"):
                    low_cycles += 1
                    if low_cycles >= 2:
                        break
                else:
                    break
            pool_input = PoolInput(
                pool_id=str(pool.id),
                forecast_7d_usd=forecast,
                safe_7d_quota=DEFAULT_SAFE_7D_QUOTA,
                peak_concurrency=peak,
                safe_concurrency_per_account=max(
                    int(pool.safe_concurrency_per_account or 1), 1
                ),
                pool_min_accounts=max(int(pool.min_accounts or 0), 0),
                pool_max_accounts=max(int(pool.max_accounts or 0), 0),
                current_accounts=current,
                utilization=utilization,
                low_utilization_cycles=low_cycles,
                min_lease_elapsed=True,
                quota_fresh=all(
                    candidate.health != "stale" for candidate in candidates
                ),
                target_healthy=(
                    target_healthy
                    if destination_target_id
                    else str(pool.pool_type) in {"public", "float", "standby"}
                ),
                confirmation_required=True,
                candidates=candidates,
            )
            plans.append((pool_input, str(pool.pool_type)))

    return [
        create_dry_run(
            target_engine,
            pool_input,
            trigger="automatic",
            now=generated_at,
        )
        for pool_input, _pool_type in plans
    ]


__all__ = [
    "AccountCandidate",
    "CostEstimate",
    "PersistedPlan",
    "PlanConfirmationRequired",
    "PlanError",
    "PlanExpired",
    "PoolAction",
    "PoolInput",
    "PoolPlan",
    "apply_confirmed_plan",
    "confirm_plan",
    "create_dry_run",
    "estimate_costs",
    "ensure_default_pools",
    "generate_scheduled_plans",
    "load_plan",
    "plan_pool",
    "rank_candidates",
    "safe_quota",
]
