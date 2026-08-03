from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlmodel import Session, SQLModel, create_engine, select

from core.db import TaskLog, TaskRunModel
from services.task_history_retention import cleanup_task_history


NOW = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)


def _engine():
    database_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(database_engine)
    return database_engine


def _add_rows(database_engine):
    with Session(database_engine) as session:
        session.add_all(
            [
                TaskRunModel(
                    id="done-old",
                    platform="chatgpt",
                    status="done",
                    created_at=NOW - timedelta(days=31),
                    updated_at=NOW - timedelta(days=31),
                ),
                TaskRunModel(
                    id="done-recent",
                    platform="chatgpt",
                    status="done",
                    created_at=NOW - timedelta(days=29),
                    updated_at=NOW - timedelta(days=29),
                ),
                TaskRunModel(
                    id="failed-old",
                    platform="chatgpt",
                    status="failed",
                    created_at=NOW - timedelta(days=91),
                    updated_at=NOW - timedelta(days=91),
                ),
                TaskRunModel(
                    id="failed-recent",
                    platform="chatgpt",
                    status="failed",
                    created_at=NOW - timedelta(days=89),
                    updated_at=NOW - timedelta(days=89),
                ),
                TaskRunModel(
                    id="pending-old",
                    platform="chatgpt",
                    status="pending",
                    created_at=NOW - timedelta(days=365),
                    updated_at=NOW - timedelta(days=365),
                ),
                TaskLog(
                    platform="chatgpt",
                    email="done-old@example.com",
                    status="success",
                    created_at=NOW - timedelta(days=31),
                ),
                TaskLog(
                    platform="chatgpt",
                    email="done-recent@example.com",
                    status="success",
                    created_at=NOW - timedelta(days=29),
                ),
                TaskLog(
                    platform="chatgpt",
                    email="failed-old@example.com",
                    status="failed",
                    created_at=NOW - timedelta(days=91),
                ),
                TaskLog(
                    platform="chatgpt",
                    email="failed-recent@example.com",
                    status="failed",
                    created_at=NOW - timedelta(days=89),
                ),
            ]
        )
        session.commit()


def test_cleanup_removes_expired_terminal_rows_but_preserves_active_and_recent_rows():
    database_engine = _engine()
    try:
        _add_rows(database_engine)

        result = cleanup_task_history(
            database_engine=database_engine,
            now=NOW,
            normal_retention_days=30,
            failure_retention_days=90,
        )

        assert result == {"task_runs": 2, "task_logs": 2}
        with Session(database_engine) as session:
            task_ids = set(
                session.exec(select(TaskRunModel.id)).all()
            )
            task_log_emails = {
                row.email for row in session.exec(select(TaskLog)).all()
            }
        assert task_ids == {"done-recent", "failed-recent", "pending-old"}
        assert task_log_emails == {
            "done-recent@example.com",
            "failed-recent@example.com",
        }
    finally:
        database_engine.dispose()


def test_cleanup_keeps_rows_exactly_at_retention_boundary():
    database_engine = _engine()
    try:
        with Session(database_engine) as session:
            session.add(
                TaskRunModel(
                    id="boundary",
                    platform="chatgpt",
                    status="done",
                    created_at=NOW - timedelta(days=30),
                    updated_at=NOW - timedelta(days=30),
                )
            )
            session.commit()

        assert cleanup_task_history(
            database_engine=database_engine,
            now=NOW,
            normal_retention_days=30,
            failure_retention_days=90,
        ) == {"task_runs": 0, "task_logs": 0}
    finally:
        database_engine.dispose()


def test_cleanup_uses_environment_retention_defaults(monkeypatch):
    database_engine = _engine()
    try:
        monkeypatch.setenv("TASK_HISTORY_RETENTION_DAYS", "1")
        monkeypatch.setenv("TASK_HISTORY_FAILURE_RETENTION_DAYS", "2")
        with Session(database_engine) as session:
            session.add_all(
                [
                    TaskRunModel(
                        id="done-env-old",
                        platform="chatgpt",
                        status="done",
                        created_at=NOW - timedelta(days=2),
                        updated_at=NOW - timedelta(days=2),
                    ),
                    TaskRunModel(
                        id="failed-env-old",
                        platform="chatgpt",
                        status="failed",
                        created_at=NOW - timedelta(days=3),
                        updated_at=NOW - timedelta(days=3),
                    ),
                ]
            )
            session.commit()

        assert cleanup_task_history(
            database_engine=database_engine,
            now=NOW,
        ) == {"task_runs": 2, "task_logs": 0}
    finally:
        database_engine.dispose()
