"""Bounded retention for persisted task history."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from typing import Any

from sqlmodel import Session, select

from core.db import TaskLog, TaskRunModel, engine


_TERMINAL_TASK_STATUSES = {"done", "failed", "stopped"}
_DEFAULT_NORMAL_RETENTION_DAYS = 30
_DEFAULT_FAILURE_RETENTION_DAYS = 90


def _positive_days(value: Any, default: int) -> int:
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


def cleanup_task_history(
    *,
    database_engine=engine,
    now: datetime | None = None,
    normal_retention_days: int | None = None,
    failure_retention_days: int | None = None,
) -> dict[str, int]:
    """Delete expired terminal task rows and task-history rows.

    Active task runs are deliberately excluded. Failed rows receive a longer
    retention window so troubleshooting details remain available after a
    normal successful history cycle has been trimmed.
    """

    current = _aware_utc(now)
    normal_days = _positive_days(
        normal_retention_days
        if normal_retention_days is not None
        else os.getenv("TASK_HISTORY_RETENTION_DAYS"),
        _DEFAULT_NORMAL_RETENTION_DAYS,
    )
    failure_days = _positive_days(
        failure_retention_days
        if failure_retention_days is not None
        else os.getenv("TASK_HISTORY_FAILURE_RETENTION_DAYS"),
        _DEFAULT_FAILURE_RETENTION_DAYS,
    )
    normal_cutoff = current - timedelta(days=normal_days)
    failure_cutoff = current - timedelta(days=failure_days)

    deleted_task_runs = 0
    deleted_task_logs = 0
    with Session(database_engine) as session:
        task_rows = session.exec(
            select(TaskRunModel).where(
                TaskRunModel.status.in_(_TERMINAL_TASK_STATUSES),
                (
                    (
                        TaskRunModel.status == "failed"
                    )
                    & (TaskRunModel.created_at < failure_cutoff)
                )
                | (
                    TaskRunModel.status.in_({"done", "stopped"})
                    & (TaskRunModel.created_at < normal_cutoff)
                ),
            )
        ).all()
        for row in task_rows:
            session.delete(row)
        deleted_task_runs = len(task_rows)

        task_log_rows = session.exec(
            select(TaskLog).where(
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
        for row in task_log_rows:
            session.delete(row)
        deleted_task_logs = len(task_log_rows)
        session.commit()

    return {
        "task_runs": deleted_task_runs,
        "task_logs": deleted_task_logs,
    }


__all__ = ["cleanup_task_history"]
