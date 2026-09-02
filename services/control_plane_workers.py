"""Background target-health and quota collection jobs."""

from __future__ import annotations

import json
import time
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
    Codex2APITargetModel,
    CustomerUsageSampleModel,
    PoolTargetPolicyModel,
    engine as default_engine,
)
from services.quota_ledger import merge_remote_rows


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
    try:
        value = int(row.get("id") or row.get("remote_id") or 0)
    except (TypeError, ValueError):
        return 0
    return value if value > 0 else 0


def _binding_data(database_engine, target_id: int) -> list[dict[str, Any]]:
    with Session(database_engine) as session:
        bindings = session.exec(
            select(AccountTargetBindingModel).where(
                AccountTargetBindingModel.target_id == int(target_id)
            )
        ).all()
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
) -> int:
    """Bootstrap local binding/assignment rows from a target account list."""

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
            email = str(row.get("email") or row.get("name") or "").strip().lower()
            if email:
                remote_by_email.setdefault(email, []).append(row)
        created = 0
        for email, local_matches in local_by_email.items():
            remote_matches = remote_by_email.get(email, [])
            if len(local_matches) != 1 or len(remote_matches) != 1:
                continue
            account = local_matches[0]
            remote = remote_matches[0]
            remote_id = _remote_id(remote)
            if remote_id <= 0:
                continue
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
                    remote_status=str(remote.get("status") or ""),
                    enabled=bool(remote.get("enabled", True)),
                    last_sync_at=now,
                    created_at=now,
                    updated_at=now,
                )
                session.add(binding)
                created += 1
            assignment = session.exec(
                select(AccountAssignmentModel).where(
                    AccountAssignmentModel.identity_id == account.identity_id,
                    AccountAssignmentModel.state.in_(["active", "draining", "standby"]),
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
    sleep_fn=time.sleep,
) -> TargetQuotaResult:
    """Run one target-level probe and persist every bound account snapshot."""

    target_engine = database_engine or default_engine
    captured_at = _aware(now)
    with Session(target_engine) as session:
        target = session.get(Codex2APITargetModel, int(target_id))
        if target is None:
            raise ValueError("Codex2API target does not exist")
        if not target.enabled:
            raise ValueError("Codex2API target is disabled")

    resolved_client = _client_for(int(target_id), target_engine, client)
    resolved_client.trigger_usage_probe()
    for attempt in range(max(min(int(probe_poll_attempts), 300), 1)):
        runtime = resolved_client.runtime_status()
        probes = runtime.get("probes") if isinstance(runtime, Mapping) else {}
        running = bool(
            isinstance(probes, Mapping)
            and probes.get("usage_probe_running")
        )
        if not running:
            break
        if attempt + 1 < probe_poll_attempts:
            sleep_fn(max(float(probe_poll_interval_seconds), 0))
    else:
        raise RuntimeError("Codex2API usage probe did not finish")

    rows = [
        dict(row)
        for row in resolved_client.list_accounts()
        if isinstance(row, Mapping)
    ]
    reconcile_target_bindings(
        target_engine,
        target_id=int(target_id),
        rows=rows,
        now=captured_at,
    )
    binding_data = _binding_data(target_engine, int(target_id))
    by_id = {_remote_id(row): row for row in rows if _remote_id(row)}
    by_email: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        email = str(row.get("email") or row.get("name") or "").strip().lower()
        if email:
            by_email.setdefault(email, []).append(row)

    collected = 0
    missing = 0
    ambiguous = 0
    for item in binding_data:
        row = by_id.get(int(item["remote_account_id"]))
        if row is None:
            candidates = by_email.get(str(item["remote_email"]).lower(), [])
            if len(candidates) == 1:
                row = candidates[0]
            elif len(candidates) > 1:
                ambiguous += 1
                status = "ambiguous"
            else:
                missing += 1
                status = "remote_missing"
            if row is None:
                with Session(target_engine) as session:
                    binding = session.get(AccountTargetBindingModel, int(item["id"]))
                    if binding is not None:
                        binding.sync_status = status
                        binding.last_error = (
                            "目标节点存在多个同邮箱账号"
                            if status == "ambiguous"
                            else "目标节点未找到账号"
                        )
                        binding.updated_at = captured_at
                        session.add(binding)
                        session.commit()
                continue

        remote_id = _remote_id(row)
        merge_remote_rows(
            target_engine,
            identity_id=str(item["identity_id"]),
            local_account_id=int(item["local_account_id"]),
            target_id=int(target_id),
            remote_id=remote_id,
            email=str(item["remote_email"]),
            rows=[row],
            captured_at=captured_at,
        )
        with Session(target_engine) as session:
            binding = session.get(AccountTargetBindingModel, int(item["id"]))
            if binding is not None:
                binding.remote_account_id = remote_id
                binding.remote_email = str(
                    row.get("email") or row.get("name") or binding.remote_email
                ).strip().lower()
                binding.remote_status = str(row.get("status") or "")
                binding.enabled = bool(row.get("enabled", True))
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
        ambiguous_accounts=ambiguous,
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
