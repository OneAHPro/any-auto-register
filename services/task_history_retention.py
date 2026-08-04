"""Bounded retention for persisted task history."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import math
import os
from typing import Any

from sqlalchemy import delete
from sqlmodel import Session, select

from core.db import TaskLog, TaskRunModel, engine


_TERMINAL_TASK_STATUSES = {"done", "failed", "stopped"}
_DEFAULT_TASK_RUN_RETENTION_HOURS = 12
_DEFAULT_NORMAL_RETENTION_DAYS = 30
_DEFAULT_FAILURE_RETENTION_DAYS = 90
_DELETE_BATCH_SIZE = 500


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _aware_utc(value: datetime | None) -> datetime:
    resolved = value or datetime.now(timezone.utc)
    if resolved.tzinfo is None:
        return resolved.replace(tzinfo=timezone.utc)
    return resolved.astimezone(timezone.utc)


def _effective_completion_at(
    meta_json: Any,
    updated_at: datetime | None,
) -> datetime:
    """Resolve a task's immutable completion time, falling back safely."""

    fallback = _aware_utc(updated_at)
    try:
        meta = meta_json if isinstance(meta_json, dict) else json.loads(meta_json)
    except (TypeError, ValueError):
        return fallback
    if not isinstance(meta, dict):
        return fallback

    raw_completed_at = meta.get("completed_at")
    if isinstance(raw_completed_at, bool):
        return fallback
    try:
        timestamp = float(raw_completed_at)
    except (TypeError, ValueError):
        return fallback
    if not math.isfinite(timestamp):
        return fallback
    try:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return fallback


def _delete_ids(session: Session, model, ids: list[Any]) -> None:
    for start in range(0, len(ids), _DELETE_BATCH_SIZE):
        batch = ids[start : start + _DELETE_BATCH_SIZE]
        session.exec(delete(model).where(model.id.in_(batch)))


def cleanup_task_history(
    *,
    database_engine=engine,
    now: datetime | None = None,
    task_run_retention_hours: int | None = None,
    normal_retention_days: int | None = None,
    failure_retention_days: int | None = None,
) -> dict[str, int]:
    """Delete expired terminal task rows and task-history rows.

    Terminal task runs have their own short, status-independent retention
    window. TaskLog keeps the longer normal/failure troubleshooting windows.
    Active task runs are deliberately excluded from deletion.
    """

    current = _aware_utc(now)
    task_run_hours = _positive_int(
        task_run_retention_hours
        if task_run_retention_hours is not None
        else os.getenv("TASK_RUN_RETENTION_HOURS"),
        _DEFAULT_TASK_RUN_RETENTION_HOURS,
    )
    normal_days = _positive_int(
        normal_retention_days
        if normal_retention_days is not None
        else os.getenv("TASK_HISTORY_RETENTION_DAYS"),
        _DEFAULT_NORMAL_RETENTION_DAYS,
    )
    failure_days = _positive_int(
        failure_retention_days
        if failure_retention_days is not None
        else os.getenv("TASK_HISTORY_FAILURE_RETENTION_DAYS"),
        _DEFAULT_FAILURE_RETENTION_DAYS,
    )
    task_run_cutoff = current - timedelta(hours=task_run_hours)
    normal_cutoff = current - timedelta(days=normal_days)
    failure_cutoff = current - timedelta(days=failure_days)

    deleted_task_runs = 0
    deleted_task_logs = 0
    with Session(database_engine) as session:
        task_candidates = session.exec(
            select(
                TaskRunModel.id,
                TaskRunModel.meta_json,
                TaskRunModel.updated_at,
            ).where(
                TaskRunModel.status.in_(_TERMINAL_TASK_STATUSES)
            )
        ).all()
        expired_task_ids = [
            task_id
            for task_id, meta_json, updated_at in task_candidates
            if _effective_completion_at(meta_json, updated_at) < task_run_cutoff
        ]
        _delete_ids(session, TaskRunModel, expired_task_ids)
        deleted_task_runs = len(expired_task_ids)

        expired_task_log_ids = session.exec(
            select(TaskLog.id).where(
                (
                    (TaskLog.status == "failed")
                    & (TaskLog.created_at < failure_cutoff)
                )
                | (
                    (TaskLog.status != "failed")
                    & (TaskLog.created_at < normal_cutoff)
                )
            )
        ).all()
        persisted_task_log_ids = [
            task_log_id
            for task_log_id in expired_task_log_ids
            if task_log_id is not None
        ]
        _delete_ids(session, TaskLog, persisted_task_log_ids)
        deleted_task_logs = len(persisted_task_log_ids)
        session.commit()

    return {
        "task_runs": deleted_task_runs,
        "task_logs": deleted_task_logs,
    }


__all__ = ["cleanup_task_history"]
