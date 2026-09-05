"""Non-blocking runtime coordinator for account-control plane jobs."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from sqlmodel import Session, select

from core.db import Codex2APITargetModel, engine


_EXECUTOR = ThreadPoolExecutor(max_workers=3, thread_name_prefix="account-control")
_JOBS_LOCK = threading.Lock()
_RUNNING_JOBS: set[str] = set()


def _enabled() -> bool:
    try:
        from core.config_store import config_store

        value = str(config_store.get("codex2api_scheduler_enabled", "0") or "")
    except Exception:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _submit_once(
    name: str,
    job: Callable[[], object],
    *,
    require_scheduler_enabled: bool = True,
) -> bool:
    if require_scheduler_enabled and not _enabled():
        return False
    with _JOBS_LOCK:
        if name in _RUNNING_JOBS:
            return False
        _RUNNING_JOBS.add(name)

    def run() -> None:
        try:
            job()
        finally:
            with _JOBS_LOCK:
                _RUNNING_JOBS.discard(name)

    _EXECUTOR.submit(run)
    return True


def _enabled_target_ids() -> list[int]:
    with Session(engine) as session:
        rows = session.exec(
            select(Codex2APITargetModel)
            .where(Codex2APITargetModel.enabled == True)  # noqa: E712
            .order_by(Codex2APITargetModel.id)
        ).all()
    return [int(row.id) for row in rows if row.id is not None]


def run_all_target_health() -> list[object]:
    from services.control_plane_workers import collect_target_health

    results = []
    for target_id in _enabled_target_ids():
        try:
            results.append(collect_target_health(engine, target_id=target_id))
        except Exception:
            # The worker persists a degraded state when the client fails; a
            # setup/schema failure must not terminate the scheduler thread.
            continue
    return results


def run_all_target_quota() -> list[object]:
    from services.codex2api_target_client import get_target_client
    from services.control_plane_workers import collect_customer_usage, collect_target_quota
    from services.codex_inventory import materialize_inventory, sync_inventory

    results = []
    for target_id in _enabled_target_ids():
        try:
            client = get_target_client(target_id, engine)
            sync_inventory(engine, target_id=target_id, clients={target_id: client}, refresh=True)
            materialize_inventory(engine)
            results.append(
                collect_target_quota(
                    engine,
                    target_id=target_id,
                    client=client,
                )
            )
            collect_customer_usage(
                engine,
                target_id=target_id,
                client=client,
            )
        except Exception:
            continue
    return results


def run_pool_planning() -> list[object]:
    from services.pool_scheduler import generate_scheduled_plans

    return generate_scheduled_plans(engine)


def run_pending_migrations() -> list[object]:
    """Resume durable migration Sagas after a process restart."""

    from services.account_migration import resume_pending_migrations

    return resume_pending_migrations(engine)


def wake_target_health() -> bool:
    return _submit_once("target_health", run_all_target_health)


def wake_target_quota() -> bool:
    return _submit_once("target_quota", run_all_target_quota)


def wake_pool_planning() -> bool:
    return _submit_once("pool_planning", run_pool_planning)


def wake_pending_migrations() -> bool:
    # Recovery is independent from automatic capacity planning. A manually
    # confirmed migration must finish even while the scheduler is disabled.
    return _submit_once(
        "pending_migrations",
        run_pending_migrations,
        require_scheduler_enabled=False,
    )


__all__ = [
    "run_all_target_health",
    "run_all_target_quota",
    "run_pending_migrations",
    "run_pool_planning",
    "wake_pending_migrations",
    "wake_pool_planning",
    "wake_target_health",
    "wake_target_quota",
]
