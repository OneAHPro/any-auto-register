from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

from core.db import TaskLog, TaskRunModel
from services import task_history_retention as retention


NOW = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
_MISSING = object()


def _engine():
    database_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(database_engine)
    return database_engine


def _add_task(
    session: Session,
    task_id: str,
    *,
    status: str = "done",
    updated_at: datetime,
    completed_at=_MISSING,
    meta_json: str | None = None,
    **fields,
) -> None:
    if meta_json is None:
        if completed_at is _MISSING:
            meta = {}
        else:
            completion_value = (
                completed_at.timestamp()
                if isinstance(completed_at, datetime)
                else completed_at
            )
            meta = {"completed_at": completion_value}
        meta_json = json.dumps(meta)
    session.add(
        TaskRunModel(
            id=task_id,
            platform="chatgpt",
            status=status,
            created_at=NOW - timedelta(days=365),
            updated_at=updated_at,
            meta_json=meta_json,
            **fields,
        )
    )


def _task_ids(database_engine) -> set[str]:
    with Session(database_engine) as session:
        return set(session.exec(select(TaskRunModel.id)).all())


def test_cleanup_uses_one_twelve_hour_window_for_all_terminal_statuses_and_keeps_active():
    database_engine = _engine()
    try:
        with Session(database_engine) as session:
            for status in ("done", "failed", "stopped"):
                _add_task(
                    session,
                    f"{status}-expired",
                    status=status,
                    updated_at=NOW - timedelta(minutes=1),
                    completed_at=NOW - timedelta(hours=12, seconds=1),
                )
            for status in ("pending", "running"):
                _add_task(
                    session,
                    f"{status}-old",
                    status=status,
                    updated_at=NOW - timedelta(days=90),
                    completed_at=NOW - timedelta(days=90),
                )
            session.commit()

        assert retention.cleanup_task_history(
            database_engine=database_engine,
            now=NOW,
        ) == {"task_runs": 3, "task_logs": 0}
        assert _task_ids(database_engine) == {"pending-old", "running-old"}
    finally:
        database_engine.dispose()


def test_cleanup_prefers_completed_at_over_updated_at_in_both_directions():
    database_engine = _engine()
    try:
        with Session(database_engine) as session:
            _add_task(
                session,
                "old-completion-new-update",
                updated_at=NOW - timedelta(minutes=1),
                completed_at=NOW - timedelta(hours=13),
            )
            _add_task(
                session,
                "new-completion-old-update",
                updated_at=NOW - timedelta(days=3),
                completed_at=NOW - timedelta(hours=1),
            )
            session.commit()

        assert retention.cleanup_task_history(
            database_engine=database_engine,
            now=NOW,
        ) == {"task_runs": 1, "task_logs": 0}
        assert _task_ids(database_engine) == {"new-completion-old-update"}
    finally:
        database_engine.dispose()


@pytest.mark.parametrize(
    "meta_json",
    [
        "{}",
        '{"completed_at": null}',
        '{"completed_at": ""}',
        '{"completed_at": "not-an-epoch"}',
        '{"completed_at": NaN}',
        '{"completed_at": Infinity}',
        '{"completed_at": 1e1000}',
        '{"completed_at": 1000000000000000000000000000000}',
        '{"completed_at": true}',
        "[]",
        '"not-an-object"',
        "{malformed",
    ],
)
def test_effective_completion_at_safely_falls_back_to_updated_at(meta_json):
    helper = getattr(retention, "_effective_completion_at", None)
    assert helper is not None
    updated_at = NOW - timedelta(hours=3)

    assert helper(meta_json, updated_at) == updated_at


def test_effective_completion_at_accepts_numeric_epoch_strings_in_valid_objects():
    helper = getattr(retention, "_effective_completion_at", None)
    assert helper is not None
    completed_at = NOW - timedelta(hours=4)

    assert helper(
        json.dumps({"completed_at": str(completed_at.timestamp())}),
        NOW,
    ) == completed_at


def test_cleanup_uses_updated_at_for_missing_or_invalid_completed_at():
    database_engine = _engine()
    try:
        with Session(database_engine) as session:
            _add_task(
                session,
                "missing-meta-expired",
                updated_at=NOW - timedelta(hours=13),
            )
            _add_task(
                session,
                "invalid-meta-recent",
                updated_at=NOW - timedelta(hours=1),
                meta_json='{"completed_at": "broken"}',
            )
            session.commit()

        assert retention.cleanup_task_history(
            database_engine=database_engine,
            now=NOW,
        ) == {"task_runs": 1, "task_logs": 0}
        assert _task_ids(database_engine) == {"invalid-meta-recent"}
    finally:
        database_engine.dispose()


def test_cleanup_keeps_rows_exactly_at_twelve_hour_boundary():
    database_engine = _engine()
    try:
        with Session(database_engine) as session:
            _add_task(
                session,
                "boundary",
                updated_at=NOW - timedelta(days=5),
                completed_at=NOW - timedelta(hours=12),
            )
            _add_task(
                session,
                "just-expired",
                updated_at=NOW,
                completed_at=NOW - timedelta(hours=12, microseconds=1),
            )
            session.commit()

        assert retention.cleanup_task_history(
            database_engine=database_engine,
            now=NOW,
        ) == {"task_runs": 1, "task_logs": 0}
        assert _task_ids(database_engine) == {"boundary"}
    finally:
        database_engine.dispose()


@pytest.mark.parametrize(
    ("environment_value", "expected_remaining"),
    [
        (None, {"ten-hours"}),
        ("6", set()),
        ("0", {"ten-hours"}),
        ("-4", {"ten-hours"}),
        ("garbage", {"ten-hours"}),
    ],
)
def test_task_run_retention_hours_environment_validation(
    monkeypatch,
    environment_value,
    expected_remaining,
):
    database_engine = _engine()
    try:
        if environment_value is None:
            monkeypatch.delenv("TASK_RUN_RETENTION_HOURS", raising=False)
        else:
            monkeypatch.setenv("TASK_RUN_RETENTION_HOURS", environment_value)
        with Session(database_engine) as session:
            _add_task(
                session,
                "ten-hours",
                updated_at=NOW - timedelta(hours=10),
            )
            _add_task(
                session,
                "thirteen-hours",
                updated_at=NOW - timedelta(hours=13),
            )
            session.commit()

        retention.cleanup_task_history(
            database_engine=database_engine,
            now=NOW,
        )
        assert _task_ids(database_engine) == expected_remaining
    finally:
        database_engine.dispose()


def test_task_run_candidate_query_does_not_select_heavy_columns():
    database_engine = _engine()
    statements: list[str] = []

    def capture_statement(_conn, _cursor, statement, _params, _context, _many):
        statements.append(statement.lower())

    try:
        with Session(database_engine) as session:
            _add_task(
                session,
                "heavy-expired",
                updated_at=NOW - timedelta(days=2),
                logs_json=json.dumps(["LOG_SECRET" * 10_000]),
                errors_json=json.dumps(["ERROR_SECRET" * 10_000]),
                control_json=json.dumps({"secret": "CONTROL_SECRET"}),
                cashier_urls_json=json.dumps(["CASHIER_SECRET"]),
                error="TOP_LEVEL_SECRET",
            )
            session.commit()

        event.listen(database_engine, "before_cursor_execute", capture_statement)
        try:
            retention.cleanup_task_history(
                database_engine=database_engine,
                now=NOW,
            )
        finally:
            event.remove(
                database_engine,
                "before_cursor_execute",
                capture_statement,
            )

        task_run_selects = [
            statement
            for statement in statements
            if statement.lstrip().startswith("select")
            and "from task_runs" in statement
        ]
        assert len(task_run_selects) == 1
        task_run_select = task_run_selects[0]
        assert "task_runs.id" in task_run_select
        assert "task_runs.meta_json" in task_run_select
        assert "task_runs.updated_at" in task_run_select
        for forbidden in (
            "task_runs.logs_json",
            "task_runs.errors_json",
            "task_runs.control_json",
            "task_runs.cashier_urls_json",
            "task_runs.error",
            "task_runs.created_at",
        ):
            assert forbidden not in task_run_select
    finally:
        database_engine.dispose()


def test_cleanup_deletes_more_than_one_parameter_batch():
    database_engine = _engine()
    delete_calls: list[tuple[str, object]] = []

    def capture_statement(_conn, _cursor, statement, params, _context, _many):
        if statement.lstrip().lower().startswith("delete from task_runs"):
            delete_calls.append((statement.lower(), params))

    try:
        with Session(database_engine) as session:
            for index in range(1_205):
                _add_task(
                    session,
                    f"expired-{index}",
                    updated_at=NOW - timedelta(days=2),
                )
            session.commit()

        event.listen(database_engine, "before_cursor_execute", capture_statement)
        try:
            assert retention.cleanup_task_history(
                database_engine=database_engine,
                now=NOW,
            ) == {"task_runs": 1_205, "task_logs": 0}
        finally:
            event.remove(
                database_engine,
                "before_cursor_execute",
                capture_statement,
            )

        assert _task_ids(database_engine) == set()
        assert len(delete_calls) >= 3
        for statement, params in delete_calls:
            assert " where task_runs.id in " in statement
            assert len(params) <= 500
    finally:
        database_engine.dispose()


def test_task_logs_keep_independent_thirty_and_ninety_day_policy(monkeypatch):
    database_engine = _engine()
    try:
        monkeypatch.setenv("TASK_HISTORY_RETENTION_DAYS", "30")
        monkeypatch.setenv("TASK_HISTORY_FAILURE_RETENTION_DAYS", "90")
        monkeypatch.setenv("TASK_RUN_RETENTION_HOURS", "1")
        with Session(database_engine) as session:
            session.add_all(
                [
                    TaskLog(
                        platform="chatgpt",
                        email="success-old@example.com",
                        status="success",
                        created_at=NOW - timedelta(days=30, microseconds=1),
                    ),
                    TaskLog(
                        platform="chatgpt",
                        email="success-boundary@example.com",
                        status="success",
                        created_at=NOW - timedelta(days=30),
                    ),
                    TaskLog(
                        platform="chatgpt",
                        email="failed-old@example.com",
                        status="failed",
                        created_at=NOW - timedelta(days=90, microseconds=1),
                    ),
                    TaskLog(
                        platform="chatgpt",
                        email="failed-boundary@example.com",
                        status="failed",
                        created_at=NOW - timedelta(days=90),
                    ),
                ]
            )
            session.commit()

        assert retention.cleanup_task_history(
            database_engine=database_engine,
            now=NOW,
        ) == {"task_runs": 0, "task_logs": 2}
        with Session(database_engine) as session:
            remaining = {
                row.email for row in session.exec(select(TaskLog)).all()
            }
        assert remaining == {
            "success-boundary@example.com",
            "failed-boundary@example.com",
        }
    finally:
        database_engine.dispose()
