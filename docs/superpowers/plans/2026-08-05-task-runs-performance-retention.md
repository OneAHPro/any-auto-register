# Task Runs Performance and 12-Hour Retention Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the **任务运行** page load from a small non-overlapping summary feed and automatically remove only terminal task runs whose effective completion time is more than 12 hours old.

**Architecture:** Keep the existing full `GET /api/tasks` and per-task log/detail routes intact, and add a SQL-projected `GET /api/tasks/summary` contract for cards. Split persisted task-run retention from the independent 30/90-day task-log policy, derive expiry from immutable `meta.completed_at` with an `updated_at` fallback, and run the failure-isolated cleanup immediately at startup and every 10 minutes.

**Tech Stack:** Python 3, FastAPI, SQLModel/SQLAlchemy, SQLite JSON functions, pytest, React 19, Ant Design, TypeScript, Vitest/Testing Library, systemd.

---

## File map

- `api/tasks.py`: build the lightweight projected task-summary query and expose `/tasks/summary` before the dynamic `/{task_id}` route.
- `tests/test_task_summary.py`: lock the exact summary response, SQL projection, response-size bound, ordering, metadata whitelist, and legacy full-list compatibility.
- `services/task_history_retention.py`: apply 12-hour effective-completion retention to terminal `task_runs` while preserving the existing 30/90-day `task_logs` policy.
- `tests/test_task_history_retention.py`: cover terminal statuses, active preservation, strict boundary, completion fallback, environment parsing, projected selection, bounded deletion, and unchanged task-log retention.
- `deploy/systemd/app.env`: document the production `TASK_RUN_RETENTION_HOURS=12` setting separately from task-log day settings.
- `tests/test_deployment_assets.py`: lock the explicit deployment default.
- `core/scheduler.py`: change the already-isolated cleanup cadence from daily to every 600 seconds while retaining immediate startup cleanup.
- `tests/test_scheduler.py`: lock startup, cadence, retry-after-failure, and scheduler isolation.
- `frontend/src/pages/RunningTasks.tsx`: consume task summaries, guard concurrent loads, render `error_count`, and show `meta.deleted_account_count` independently.
- `frontend/src/pages/RunningTasks.test.tsx`: cover the endpoint, distinct counters, poll guard success/failure recovery, and lazy log drawer.

The companion ChatGPT account-removal implementation owns production and persistence of `meta.deleted_account_count`. This plan owns its summary whitelist, the automatic-card derivation `error_count = max(relogin_failed_count - deleted_account_count, 0)`, and **任务运行** rendering. Missing count fields resolve to zero; `relogin_failed_count` itself remains unchanged because it is the inclusive email-alert metric.

### Task 1: Add the projected task-summary API

**Files:**
- Create: `tests/test_task_summary.py`
- Modify: `api/tasks.py:1-5`
- Modify: `api/tasks.py:481-492`
- Modify: `api/tasks.py:724-745`
- Modify: `api/tasks.py:3943-3953`

- [ ] **Step 1: Write the failing summary contract and projection tests**

Create `tests/test_task_summary.py` with an isolated SQLite engine. The first test inserts a large log and secret-shaped error/meta values, calls the real FastAPI route, asserts the exact card contract, and inspects emitted SQL so the summary query never selects heavy payload columns:

```python
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlmodel import Session, SQLModel
import pytest

import api.tasks as tasks
from core import db
from core.db import TaskRunModel


NOW = datetime(2026, 8, 5, 2, 0, tzinfo=timezone.utc)


def _engine():
    database_engine = db._create_database_engine("sqlite://")
    SQLModel.metadata.create_all(database_engine)
    return database_engine


def _client(database_engine) -> TestClient:
    app = FastAPI()
    app.include_router(tasks.router)
    return TestClient(app)


def test_summary_returns_only_the_card_contract_without_loading_heavy_columns():
    database_engine = _engine()
    statements: list[str] = []
    try:
        with Session(database_engine) as session:
            session.add(
                TaskRunModel(
                    id="task-summary",
                    platform="chatgpt",
                    source="schedule",
                    status="done",
                    total=64,
                    success=40,
                    registered=64,
                    skipped=2,
                    meta_json=json.dumps(
                        {
                            "automation": True,
                            "invalid_rt_count": 20,
                            "relogin_failed_count": 20,
                            "deleted_account_count": 17,
                            "alert_sent": True,
                            "alert_reason": "sent",
                            "private_runtime_value": "META_MUST_NOT_LEAK",
                        }
                    ),
                    logs_json=json.dumps(["SECRET_LOG_LINE" * 16_000]),
                    errors_json=json.dumps(["SECRET_ERROR_A", "SECRET_ERROR_B"]),
                    control_json=json.dumps({"stop_requested": False}),
                    cashier_urls_json=json.dumps(["https://private.example/checkout"]),
                    error="SECRET_TOP_LEVEL_ERROR",
                    created_at=NOW - timedelta(minutes=2),
                    updated_at=NOW,
                )
            )
            session.commit()

        @event.listens_for(database_engine, "before_cursor_execute")
        def capture_statement(_conn, _cursor, statement, _params, _context, _many):
            statements.append(statement)

        with (
            mock.patch.object(tasks, "engine", database_engine),
            mock.patch.object(tasks, "_finalize_orphan_tasks", return_value=set()),
        ):
            response = _client(database_engine).get("/tasks/summary")

        assert response.status_code == 200
        assert response.json() == [
            {
                "id": "task-summary",
                "platform": "chatgpt",
                "source": "schedule",
                "status": "done",
                "total": 64,
                "success": 40,
                "registered": 64,
                "skipped": 2,
                "error_count": 3,
                "created_at": (NOW - timedelta(minutes=2)).timestamp(),
                "updated_at": NOW.timestamp(),
                "meta": {
                    "automation": True,
                    "invalid_rt_count": 20,
                    "relogin_failed_count": 20,
                    "deleted_account_count": 17,
                    "alert_sent": True,
                    "alert_reason": "sent",
                },
            }
        ]
        assert len(response.content) < 2_000
        assert "SECRET_LOG_LINE" not in response.text
        assert "SECRET_ERROR_A" not in response.text
        assert "META_MUST_NOT_LEAK" not in response.text

        summary_select = next(
            statement
            for statement in statements
            if statement.lstrip().upper().startswith("SELECT")
            and "FROM task_runs" in statement
        ).lower()
        assert "task_runs.logs_json" not in summary_select
        assert "task_runs.control_json" not in summary_select
        assert "task_runs.cashier_urls_json" not in summary_select
        assert "task_runs.error," not in summary_select
    finally:
        database_engine.dispose()
```

Add an ordering test. It deliberately gives terminal rows mixed statuses so the expected order proves terminal records are globally newest-first, rather than grouped by terminal status:

```python
def test_summary_orders_active_first_then_terminal_newest_first():
    database_engine = _engine()
    try:
        with Session(database_engine) as session:
            session.add_all(
                [
                    TaskRunModel(
                        id="running-old",
                        platform="chatgpt",
                        status="running",
                        created_at=NOW - timedelta(hours=3),
                        updated_at=NOW,
                    ),
                    TaskRunModel(
                        id="pending-new",
                        platform="chatgpt",
                        status="pending",
                        created_at=NOW - timedelta(minutes=1),
                        updated_at=NOW,
                    ),
                    TaskRunModel(
                        id="failed-newest-terminal",
                        platform="chatgpt",
                        status="failed",
                        created_at=NOW - timedelta(minutes=2),
                        updated_at=NOW,
                    ),
                    TaskRunModel(
                        id="done-older-terminal",
                        platform="chatgpt",
                        status="done",
                        created_at=NOW - timedelta(minutes=5),
                        updated_at=NOW,
                    ),
                ]
            )
            session.commit()

        with (
            mock.patch.object(tasks, "engine", database_engine),
            mock.patch.object(tasks, "_finalize_orphan_tasks", return_value=set()),
        ):
            response = _client(database_engine).get("/tasks/summary")

        assert response.status_code == 200
        assert [item["id"] for item in response.json()] == [
            "running-old",
            "pending-new",
            "failed-newest-terminal",
            "done-older-terminal",
        ]
    finally:
        database_engine.dispose()
```

Add an ordinary-task regression proving only automatic-authentication summaries use the derived count:

```python
def test_summary_ordinary_task_uses_persisted_error_array_length():
    database_engine = _engine()
    try:
        with Session(database_engine) as session:
            session.add(
                TaskRunModel(
                    id="ordinary-task",
                    platform="cursor",
                    source="manual",
                    status="done",
                    errors_json=json.dumps(["a", "b", "c", "d"]),
                    meta_json=json.dumps(
                        {
                            "automation": False,
                            "relogin_failed_count": 100,
                            "deleted_account_count": 99,
                        }
                    ),
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            session.commit()

        with (
            mock.patch.object(tasks, "engine", database_engine),
            mock.patch.object(tasks, "_finalize_orphan_tasks", return_value=set()),
        ):
            response = _client(database_engine).get("/tasks/summary")

        assert response.status_code == 200
        assert response.json()[0]["error_count"] == 4
    finally:
        database_engine.dispose()


@pytest.mark.parametrize(
    ("relogin_failed", "deleted", "expected"),
    [(20, 17, 3), (2, 5, 0), ("invalid", 1, 0)],
)
def test_automatic_summary_error_count_subtracts_deleted_and_clamps_at_zero(
    relogin_failed,
    deleted,
    expected,
):
    assert tasks._task_summary_error_count(
        {
            "automation": True,
            "relogin_failed_count": relogin_failed,
            "deleted_account_count": deleted,
        },
        persisted_error_count=99,
    ) == expected
```

Add a compatibility test proving the original list route still returns its full snapshot and remains separate from the new summary helper:

```python
def test_legacy_full_list_route_remains_unchanged():
    full_snapshot = {
        "id": "legacy-full",
        "status": "done",
        "platform": "chatgpt",
        "source": "manual",
        "meta": {},
        "total": 1,
        "progress": "1/1",
        "logs": ["full detail"],
        "success": 1,
        "registered": 1,
        "skipped": 0,
        "errors": [],
        "control": {},
        "cashier_urls": [],
        "error": "",
        "created_at": NOW.timestamp(),
        "updated_at": NOW.timestamp(),
    }
    with (
        mock.patch.object(tasks, "_finalize_orphan_tasks", return_value=set()),
        mock.patch.object(
            tasks,
            "_list_persisted_tasks",
            return_value=[full_snapshot],
        ),
    ):
        response = _client(None).get("/tasks")

    assert response.status_code == 200
    assert response.json()[0]["logs"] == ["full detail"]
    assert "error_count" not in response.json()[0]
```

- [ ] **Step 2: Run the summary tests and verify RED**

Run:

```bash
PYTHONPATH=. .venv/bin/pytest tests/test_task_summary.py -q
```

Expected: the `/tasks/summary` cases fail because FastAPI currently routes `summary` through `/{task_id}` and no projected summary helper exists; the legacy `/tasks` compatibility case already passes.

- [ ] **Step 3: Implement the metadata whitelist and SQL-projected summary helper**

In `api/tasks.py`, import SQL expression helpers without changing the existing SQLModel imports:

```python
from sqlalchemy import case, func
from sqlmodel import Session, select
```

Add the whitelist beside the persistence constants:

```python
_TASK_SUMMARY_META_KEYS = (
    "automation",
    "invalid_rt_count",
    "relogin_failed_count",
    "deleted_account_count",
    "alert_sent",
    "alert_reason",
)
```

Add these helpers after `_list_persisted_tasks`. `json_valid` prevents malformed legacy `errors_json` from aborting the complete list request, while `json_array_length` computes the count inside SQLite so error strings do not cross into Python:

```python
def _task_summary_meta(raw_meta: str) -> dict:
    parsed = _json_loads(raw_meta, {})
    if not isinstance(parsed, dict):
        return {}
    return {
        key: parsed[key]
        for key in _TASK_SUMMARY_META_KEYS
        if key in parsed
    }


def _task_summary_count(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _task_summary_error_count(meta: dict, persisted_error_count) -> int:
    if not _is_truthy(meta.get("automation")):
        return _task_summary_count(persisted_error_count)
    return max(
        _task_summary_count(meta.get("relogin_failed_count"))
        - _task_summary_count(meta.get("deleted_account_count")),
        0,
    )


def _list_persisted_task_summaries() -> list[dict]:
    error_count = case(
        (
            func.json_valid(TaskRunModel.errors_json) == 1,
            func.json_array_length(TaskRunModel.errors_json),
        ),
        else_=0,
    ).label("error_count")
    status_order = case(
        (TaskRunModel.status == "running", 0),
        (TaskRunModel.status == "pending", 1),
        else_=2,
    )
    statement = (
        select(
            TaskRunModel.id,
            TaskRunModel.platform,
            TaskRunModel.source,
            TaskRunModel.status,
            TaskRunModel.total,
            TaskRunModel.success,
            TaskRunModel.registered,
            TaskRunModel.skipped,
            error_count,
            TaskRunModel.created_at,
            TaskRunModel.updated_at,
            TaskRunModel.meta_json,
        )
        .order_by(status_order, TaskRunModel.created_at.desc())
    )
    with Session(engine) as session:
        rows = session.exec(statement).all()

    summaries: list[dict] = []
    for (
        task_id,
        platform,
        source,
        status,
        total,
        success,
        registered,
        skipped,
        persisted_error_count,
        created_at,
        updated_at,
        meta_json,
    ) in rows:
        meta = _task_summary_meta(meta_json)
        summaries.append({
            "id": str(task_id),
            "platform": str(platform or ""),
            "source": str(source or "manual"),
            "status": str(status or "pending"),
            "total": int(total or 0),
            "success": int(success or 0),
            "registered": int(registered or 0),
            "skipped": int(skipped or 0),
            "error_count": _task_summary_error_count(
                meta,
                persisted_error_count,
            ),
            "created_at": _to_epoch_seconds(created_at),
            "updated_at": _to_epoch_seconds(updated_at),
            "meta": meta,
        })
    return summaries
```

- [ ] **Step 4: Register the static route before the dynamic task route**

Immediately before `@router.get("/{task_id}")`, add:

```python
@router.get("/summary")
def list_task_summaries():
    _finalize_orphan_tasks()
    return _list_persisted_task_summaries()
```

Do not change `list_tasks`, `_list_persisted_tasks`, `get_task`, or `stream_logs`; those remain the compatibility/full-detail paths.

- [ ] **Step 5: Run summary and persistence regressions and verify GREEN**

Run:

```bash
PYTHONPATH=. .venv/bin/pytest \
  tests/test_task_summary.py \
  tests/test_task_snapshot_persistence.py \
  tests/test_task_runtime.py -q
```

Expected: all tests pass. The captured summary SQL omits persisted logs/control/cashier URLs, the response remains below 2 KB despite the large fixture log, and legacy full snapshots still work.

- [ ] **Step 6: Commit the summary API**

```bash
git add api/tasks.py tests/test_task_summary.py
git commit -m "feat: add lightweight task summaries"
```

### Task 2: Separate 12-hour task-run retention from task-log retention

**Files:**
- Modify: `tests/test_task_history_retention.py:1-179`
- Modify: `services/task_history_retention.py:1-109`
- Modify: `tests/test_deployment_assets.py:1-38`
- Modify: `deploy/systemd/app.env:16-19`

- [ ] **Step 1: Replace task-run day-window tests with the failing 12-hour matrix**

Keep the existing in-memory engine helper, import `json`, `pytest`, and SQLAlchemy `event`, then replace the old mixed task-run test with the following effective-completion matrix:

```python
import json

import pytest
from sqlalchemy import event


def _task_run(
    task_id: str,
    status: str,
    *,
    completed_at: datetime | str | None,
    updated_at: datetime,
    logs_json: str = "[]",
) -> TaskRunModel:
    if isinstance(completed_at, datetime):
        completion_value: float | str | None = completed_at.timestamp()
    else:
        completion_value = completed_at
    meta = {} if completion_value is None else {"completed_at": completion_value}
    return TaskRunModel(
        id=task_id,
        platform="chatgpt",
        status=status,
        meta_json=json.dumps(meta),
        logs_json=logs_json,
        created_at=NOW - timedelta(days=100),
        updated_at=updated_at,
    )


def test_task_run_cleanup_uses_effective_completion_and_preserves_active_rows():
    database_engine = _engine()
    try:
        with Session(database_engine) as session:
            session.add_all(
                [
                    _task_run(
                        "done-expired",
                        "done",
                        completed_at=NOW - timedelta(hours=12, seconds=1),
                        updated_at=NOW,
                    ),
                    _task_run(
                        "failed-expired",
                        "failed",
                        completed_at=NOW - timedelta(hours=13),
                        updated_at=NOW,
                    ),
                    _task_run(
                        "stopped-expired",
                        "stopped",
                        completed_at=NOW - timedelta(days=1),
                        updated_at=NOW,
                    ),
                    _task_run(
                        "immutable-completion-wins",
                        "done",
                        completed_at=NOW - timedelta(hours=20),
                        updated_at=NOW - timedelta(minutes=1),
                    ),
                    _task_run(
                        "legacy-updated-fallback",
                        "failed",
                        completed_at=None,
                        updated_at=NOW - timedelta(hours=13),
                    ),
                    _task_run(
                        "malformed-completion-fallback",
                        "stopped",
                        completed_at="not-an-epoch",
                        updated_at=NOW - timedelta(hours=14),
                    ),
                    _task_run(
                        "exact-boundary",
                        "done",
                        completed_at=NOW - timedelta(hours=12),
                        updated_at=NOW,
                    ),
                    _task_run(
                        "recent-terminal",
                        "failed",
                        completed_at=NOW - timedelta(hours=11, minutes=59),
                        updated_at=NOW,
                    ),
                    _task_run(
                        "running-old",
                        "running",
                        completed_at=NOW - timedelta(days=2),
                        updated_at=NOW - timedelta(days=2),
                    ),
                    _task_run(
                        "pending-old",
                        "pending",
                        completed_at=None,
                        updated_at=NOW - timedelta(days=2),
                    ),
                ]
            )
            session.commit()

        result = cleanup_task_history(
            database_engine=database_engine,
            now=NOW,
            task_run_retention_hours=12,
            normal_retention_days=30,
            failure_retention_days=90,
        )

        assert result == {"task_runs": 6, "task_logs": 0}
        with Session(database_engine) as session:
            remaining = set(session.exec(select(TaskRunModel.id)).all())
        assert remaining == {
            "exact-boundary",
            "recent-terminal",
            "running-old",
            "pending-old",
        }
    finally:
        database_engine.dispose()
```

- [ ] **Step 2: Add failing environment, SQL projection, and independent task-log tests**

Add a parameterized environment test. A row completed 10 hours ago expires only with the explicit 6-hour value; zero, negative, blank, and malformed values all fall back to 12 hours:

```python
@pytest.mark.parametrize(
    ("raw_hours", "expected_deleted"),
    [("6", 1), ("0", 0), ("-2", 0), ("", 0), ("invalid", 0)],
)
def test_task_run_retention_environment_and_invalid_fallback(
    monkeypatch,
    raw_hours,
    expected_deleted,
):
    database_engine = _engine()
    try:
        monkeypatch.setenv("TASK_RUN_RETENTION_HOURS", raw_hours)
        with Session(database_engine) as session:
            session.add(
                _task_run(
                    f"env-{raw_hours or 'blank'}",
                    "done",
                    completed_at=NOW - timedelta(hours=10),
                    updated_at=NOW,
                )
            )
            session.commit()

        result = cleanup_task_history(database_engine=database_engine, now=NOW)

        assert result["task_runs"] == expected_deleted
    finally:
        database_engine.dispose()
```

Add a query-shape test with a large persisted log. It must observe a four-column candidate `SELECT` and a set-based `DELETE`, rather than loading `TaskRunModel` objects:

```python
def test_task_run_cleanup_projects_candidates_and_uses_sql_delete():
    database_engine = _engine()
    statements: list[str] = []
    try:
        with Session(database_engine) as session:
            session.add(
                _task_run(
                    "large-expired",
                    "done",
                    completed_at=NOW - timedelta(days=1),
                    updated_at=NOW,
                    logs_json=json.dumps(["x" * 250_000]),
                )
            )
            session.commit()

        @event.listens_for(database_engine, "before_cursor_execute")
        def capture_statement(_conn, _cursor, statement, _params, _context, _many):
            statements.append(statement)

        result = cleanup_task_history(
            database_engine=database_engine,
            now=NOW,
            task_run_retention_hours=12,
        )

        assert result["task_runs"] == 1
        task_run_select = next(
            statement.lower()
            for statement in statements
            if statement.lstrip().upper().startswith("SELECT")
            and "from task_runs" in statement.lower()
        )
        for selected_column in (
            "task_runs.id",
            "task_runs.status",
            "task_runs.meta_json",
            "task_runs.updated_at",
        ):
            assert selected_column in task_run_select
        for heavy_column in (
            "task_runs.logs_json",
            "task_runs.errors_json",
            "task_runs.control_json",
            "task_runs.cashier_urls_json",
        ):
            assert heavy_column not in task_run_select
        assert any(
            statement.lstrip().upper().startswith("DELETE FROM task_runs")
            for statement in statements
        )
    finally:
        database_engine.dispose()
```

Retain the existing 30/90-day task-log behavior in a dedicated test, with no expired task run involved:

```python
def test_task_log_retention_remains_30_days_success_and_90_days_failure():
    database_engine = _engine()
    try:
        with Session(database_engine) as session:
            session.add_all(
                [
                    TaskLog(
                        platform="chatgpt",
                        email="success-old@example.com",
                        status="success",
                        created_at=NOW - timedelta(days=31),
                    ),
                    TaskLog(
                        platform="chatgpt",
                        email="success-recent@example.com",
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

        result = cleanup_task_history(
            database_engine=database_engine,
            now=NOW,
            task_run_retention_hours=12,
            normal_retention_days=30,
            failure_retention_days=90,
        )

        assert result == {"task_runs": 0, "task_logs": 2}
        with Session(database_engine) as session:
            remaining = {
                row.email for row in session.exec(select(TaskLog)).all()
            }
        assert remaining == {
            "success-recent@example.com",
            "failed-recent@example.com",
        }
    finally:
        database_engine.dispose()
```

- [ ] **Step 3: Lock the explicit systemd environment value**

Append this failing test to `tests/test_deployment_assets.py`:

```python
def test_systemd_environment_sets_twelve_hour_task_run_retention():
    environment = (
        PROJECT_ROOT / "deploy" / "systemd" / "app.env"
    ).read_text(encoding="utf-8")

    assert "TASK_RUN_RETENTION_HOURS=12" in environment.splitlines()
    assert "TASK_HISTORY_RETENTION_DAYS=30" in environment.splitlines()
    assert "TASK_HISTORY_FAILURE_RETENTION_DAYS=90" in environment.splitlines()
```

- [ ] **Step 4: Run focused tests and verify RED**

Run:

```bash
PYTHONPATH=. .venv/bin/pytest \
  tests/test_task_history_retention.py \
  tests/test_deployment_assets.py -q
```

Expected: task-run cases fail because `cleanup_task_history` has no `task_run_retention_hours`, still uses `created_at` with 30/90-day windows, selects complete rows, and `deploy/systemd/app.env` has no hour setting.

- [ ] **Step 5: Implement effective-completion parsing and independent cutoffs**

In `services/task_history_retention.py`, add `json` and `math`, and import SQLAlchemy's set-based delete:

```python
from datetime import datetime, timedelta, timezone
import json
import math
import os
from typing import Any

from sqlalchemy import delete
from sqlmodel import Session, select
```

Replace the retention constants and positive-value helper with:

```python
_TERMINAL_TASK_STATUSES = {"done", "failed", "stopped"}
_DEFAULT_TASK_RUN_RETENTION_HOURS = 12
_DEFAULT_NORMAL_RETENTION_DAYS = 30
_DEFAULT_FAILURE_RETENTION_DAYS = 90
_TASK_RUN_DELETE_BATCH_SIZE = 500


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default
```

Keep `_aware_utc` and add the exact completion resolver. It accepts second or millisecond epochs, rejects non-finite/non-positive/out-of-range values, and always falls back to `updated_at`:

```python
def _effective_completion_at(
    meta_json: str,
    updated_at: datetime | None,
) -> datetime:
    fallback = _aware_utc(updated_at)
    try:
        meta = json.loads(meta_json or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback
    if not isinstance(meta, dict):
        return fallback
    try:
        timestamp = float(meta.get("completed_at"))
    except (TypeError, ValueError):
        return fallback
    if not math.isfinite(timestamp) or timestamp <= 0:
        return fallback
    if timestamp > 1_000_000_000_000:
        timestamp /= 1000
    try:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return fallback
```

Change the function signature and cutoff setup to:

```python
def cleanup_task_history(
    *,
    database_engine=engine,
    now: datetime | None = None,
    task_run_retention_hours: int | None = None,
    normal_retention_days: int | None = None,
    failure_retention_days: int | None = None,
) -> dict[str, int]:
    current = _aware_utc(now)
    run_hours = _positive_int(
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
    task_run_cutoff = current - timedelta(hours=run_hours)
    normal_log_cutoff = current - timedelta(days=normal_days)
    failure_log_cutoff = current - timedelta(days=failure_days)
```

- [ ] **Step 6: Replace full-row task-run loading with projected candidates and bounded deletes**

Replace only the `task_runs` block inside the session. Keep the existing task-log row deletion structure, changing its cutoff names to `normal_log_cutoff` and `failure_log_cutoff`:

```python
with Session(database_engine) as session:
    candidates = session.exec(
        select(
            TaskRunModel.id,
            TaskRunModel.status,
            TaskRunModel.meta_json,
            TaskRunModel.updated_at,
        ).where(TaskRunModel.status.in_(_TERMINAL_TASK_STATUSES))
    ).all()
    expired_ids = [
        str(task_id)
        for task_id, _status, meta_json, updated_at in candidates
        if _effective_completion_at(meta_json, updated_at) < task_run_cutoff
    ]
    deleted_task_runs = 0
    for start in range(0, len(expired_ids), _TASK_RUN_DELETE_BATCH_SIZE):
        batch = expired_ids[start : start + _TASK_RUN_DELETE_BATCH_SIZE]
        result = session.exec(
            delete(TaskRunModel).where(
                TaskRunModel.id.in_(batch),
                TaskRunModel.status.in_(_TERMINAL_TASK_STATUSES),
            )
        )
        deleted_task_runs += max(0, int(result.rowcount or 0))

    task_log_rows = session.exec(
        select(TaskLog).where(
            (
                (TaskLog.status == "failed")
                & (TaskLog.created_at < failure_log_cutoff)
            )
            | (
                (TaskLog.status != "failed")
                & (TaskLog.created_at < normal_log_cutoff)
            )
        )
    ).all()
    for row in task_log_rows:
        session.delete(row)
    deleted_task_logs = len(task_log_rows)
    session.commit()
```

The strict `< task_run_cutoff` comparison preserves a row exactly 12 hours old. The second terminal-status predicate in `DELETE` is a concurrency guard; active rows remain protected even if state changes between selection and deletion.

- [ ] **Step 7: Add the production environment default**

In `deploy/systemd/app.env`, place the new key immediately before the existing task-log day values:

```dotenv
TASK_RUN_RETENTION_HOURS=12
TASK_HISTORY_RETENTION_DAYS=30
TASK_HISTORY_FAILURE_RETENTION_DAYS=90
```

- [ ] **Step 8: Run retention, deployment, and database regressions and verify GREEN**

Run:

```bash
PYTHONPATH=. .venv/bin/pytest \
  tests/test_task_history_retention.py \
  tests/test_deployment_assets.py \
  tests/test_task_snapshot_persistence.py -q
```

Expected: all tests pass; active rows and the exact boundary remain, all three terminal statuses expire from effective completion, invalid environment values use 12 hours, and task logs retain their 30/90-day windows.

- [ ] **Step 9: Commit retention as one independently deployable change**

```bash
git add \
  services/task_history_retention.py \
  tests/test_task_history_retention.py \
  deploy/systemd/app.env \
  tests/test_deployment_assets.py
git commit -m "feat: retain terminal tasks for twelve hours"
```

### Task 3: Run cleanup at startup and every 10 minutes

**Files:**
- Modify: `tests/test_scheduler.py:76-147`
- Modify: `core/scheduler.py:22-95`

- [ ] **Step 1: Write the failing 600-second cadence test**

Append this test to `tests/test_scheduler.py`. Trial and CPA maintenance are held outside the test clock so only task cleanup is observed:

```python
def test_task_history_cleanup_runs_every_ten_minutes(monkeypatch):
    scheduler = scheduler_module.Scheduler()
    scheduler._last_trial_check_at = 10_000.0
    scheduler._last_cpa_maintenance_at = 10_000.0
    scheduler._last_task_history_cleanup_at = 100.0
    wall_now = datetime(2026, 8, 5, 2, 0, tzinfo=timezone.utc)
    cleanup = mock.Mock(return_value={"task_runs": 0, "task_logs": 0})

    monkeypatch.setattr(
        scheduler_module,
        "tick_chatgpt_auto_relogin",
        mock.Mock(),
        raising=False,
    )
    monkeypatch.setattr(
        scheduler_module,
        "cleanup_task_history",
        cleanup,
        raising=False,
    )
    monkeypatch.setattr(
        scheduler,
        "_get_cpa_maintenance_interval_seconds",
        lambda: 0,
    )

    assert scheduler._task_history_cleanup_interval_seconds == 600
    scheduler.run_once(wall_now=wall_now, monotonic_now=699.9)
    cleanup.assert_not_called()
    scheduler.run_once(wall_now=wall_now, monotonic_now=700.0)
    cleanup.assert_called_once_with()
    scheduler.run_once(wall_now=wall_now, monotonic_now=1_299.9)
    cleanup.assert_called_once_with()
    scheduler.run_once(wall_now=wall_now, monotonic_now=1_300.0)
    assert cleanup.call_count == 2
    assert scheduler._last_task_history_cleanup_at == 1_300.0
```

In the existing `test_start_is_daemon_nonblocking_and_immediately_ticks_without_trial_or_cpa`, add this assertion before `scheduler.start()`:

```python
assert scheduler._task_history_cleanup_interval_seconds == 600
```

Keep `test_run_once_isolates_task_history_cleanup_failure`: its assertion that `_last_task_history_cleanup_at` stays unchanged locks retry-on-next-loop behavior.

- [ ] **Step 2: Run scheduler tests and verify RED**

Run:

```bash
PYTHONPATH=. .venv/bin/pytest tests/test_scheduler.py -q
```

Expected: the new default-cadence assertions fail with `86400`; existing startup-immediate and failure-isolation tests pass.

- [ ] **Step 3: Change only the cleanup interval default**

In `Scheduler.__init__`, replace the daily value with:

```python
self._task_history_cleanup_interval_seconds = 600
```

Keep the current startup initialization:

```python
self._last_task_history_cleanup_at = (
    now - self._task_history_cleanup_interval_seconds
)
```

Keep the current `try`/`except`/`else` update order in `run_once`; it already isolates exceptions and advances the successful-run timestamp only after cleanup returns.

- [ ] **Step 4: Run scheduler and retention regressions and verify GREEN**

Run:

```bash
PYTHONPATH=. .venv/bin/pytest \
  tests/test_scheduler.py \
  tests/test_task_history_retention.py \
  tests/test_chatgpt_auto_relogin.py -q
```

Expected: all tests pass, including immediate startup cleanup, 600-second cadence, failed-cleanup retry, and automatic re-login isolation.

- [ ] **Step 5: Commit the cadence change**

```bash
git add core/scheduler.py tests/test_scheduler.py
git commit -m "feat: clean task runs every ten minutes"
```

### Task 4: Consume summaries without overlapping polls

**Files:**
- Modify: `frontend/src/pages/RunningTasks.test.tsx:1-75`
- Modify: `frontend/src/pages/RunningTasks.tsx:1-365`

- [ ] **Step 1: Replace the fixture with the summary contract and failing distinct-counter assertions**

In `frontend/src/pages/RunningTasks.test.tsx`, import `act`, `waitFor`, and `userEvent`; make the log-panel mock observable; and add a reusable deferred promise:

```typescript
import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

vi.mock('@/components/TaskLogPanel', () => ({
  TaskLogPanel: ({ taskId }: { taskId: string }) => (
    <div data-testid="task-log-panel">{taskId}</div>
  ),
}))

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

const AUTOMATIC_SUMMARY = {
  id: 'task-auto-history',
  platform: 'chatgpt',
  source: 'schedule',
  status: 'done' as const,
  total: 64,
  success: 44,
  registered: 64,
  skipped: 0,
  error_count: 3,
  created_at: 1_786_000_000,
  updated_at: 1_786_000_060,
  meta: {
    automation: true,
    invalid_rt_count: 20,
    relogin_failed_count: 20,
    deleted_account_count: 17,
    alert_sent: true,
    alert_reason: 'sent',
  },
}
```

Use `AUTOMATIC_SUMMARY` in `beforeEach`, then replace the current display test with:

```typescript
it('loads summaries and displays generic, relogin, and deleted counts distinctly', async () => {
  const user = userEvent.setup()
  render(<RunningTasks />)

  expect(await screen.findByText('自动认证')).toBeTruthy()
  expect(apiFetch).toHaveBeenCalledWith('/tasks/summary')
  expect(screen.getByText('✗ 失败 3')).toBeTruthy()
  expect(screen.getByText('鉴权失效 20')).toBeTruthy()
  expect(screen.getByText('重登失败 20')).toBeTruthy()
  expect(screen.getByText('已删除账号 17')).toBeTruthy()
  expect(screen.getByText('邮件已提醒')).toBeTruthy()
  expect(screen.queryByTestId('task-log-panel')).toBeNull()

  await user.click(screen.getByRole('button', { name: '查看日志' }))
  expect((await screen.findByTestId('task-log-panel')).textContent).toContain(
    'task-auto-history',
  )
})
```

This test depends on the companion account-removal work to persist `deleted_account_count`. The backend summary subtracts that subset from the automatic card's red count, while preserving the inclusive `relogin_failed_count=20` used to trigger the email.

- [ ] **Step 2: Add the failing in-flight guard and recovery test**

Add this test. It manually invokes only the 2.5-second list callback and proves the same guard clears after both fulfillment and rejection:

```typescript
it('skips overlapping polls and retries after success and failure', async () => {
  const first = deferred<(typeof AUTOMATIC_SUMMARY)[]>()
  const second = deferred<(typeof AUTOMATIC_SUMMARY)[]>()
  vi.mocked(apiFetch)
    .mockReset()
    .mockImplementationOnce(() => first.promise)
    .mockImplementationOnce(() => second.promise)
    .mockResolvedValue([])
  const setIntervalSpy = vi.spyOn(window, 'setInterval')

  render(<RunningTasks />)

  await waitFor(() => expect(apiFetch).toHaveBeenCalledTimes(1))
  const poll = setIntervalSpy.mock.calls.find(([, delay]) => delay === 2_500)?.[0]
  expect(poll).toBeTypeOf('function')

  act(() => (poll as () => void)())
  await Promise.resolve()
  expect(apiFetch).toHaveBeenCalledTimes(1)

  await act(async () => {
    first.resolve([AUTOMATIC_SUMMARY])
    await first.promise
  })
  act(() => (poll as () => void)())
  await waitFor(() => expect(apiFetch).toHaveBeenCalledTimes(2))

  await act(async () => {
    second.reject(new Error('summary offline'))
    await second.promise.catch(() => undefined)
  })
  act(() => (poll as () => void)())
  await waitFor(() => expect(apiFetch).toHaveBeenCalledTimes(3))

  setIntervalSpy.mockRestore()
})
```

Update `afterEach` so timer spies cannot leak between tests:

```typescript
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})
```

- [ ] **Step 3: Run the Running Tasks tests and verify RED**

Run from `frontend/`:

```bash
npm test -- src/pages/RunningTasks.test.tsx
```

Expected: failures show that the page still calls `/tasks`, reads `errors.length`, lacks `已删除账号`, and starts another request while the first is unresolved.

- [ ] **Step 4: Change the list type and load function to the summary contract**

In `RunningTasks.tsx`, import `useRef` and replace `TaskSnapshot` with:

```typescript
import { useCallback, useEffect, useRef, useState } from 'react'

interface TaskSummary {
  id: string
  platform: string
  source: string
  status: 'pending' | 'running' | 'done' | 'failed' | 'stopped'
  total: number
  success: number
  registered: number
  skipped: number
  error_count: number
  created_at: number | string | null
  updated_at: number | string | null
  meta?: {
    automation?: boolean
    invalid_rt_count?: number
    relogin_failed_count?: number
    deleted_account_count?: number
    alert_sent?: boolean
    alert_reason?: string
  }
}
```

Update state and `isActive`/`renderTask` parameter types from `TaskSnapshot` to `TaskSummary`. Add the shared ref and replace `load` with:

```typescript
const loadInFlight = useRef(false)

const load = useCallback(async () => {
  if (loadInFlight.current) return
  loadInFlight.current = true
  setLoading(true)
  try {
    const data = (await apiFetch('/tasks/summary')) as TaskSummary[]
    setTasks(Array.isArray(data) ? data : [])
  } catch {
    return
  } finally {
    loadInFlight.current = false
    setLoading(false)
  }
}, [])
```

The backend owns ordering, so remove the old client sort. Invoke the promise explicitly without awaiting it in the effect and timer:

```typescript
useEffect(() => {
  void load()
  const poll = window.setInterval(() => {
    void load()
    setNow(Date.now() / 1000)
  }, 2_500)
  const tick = window.setInterval(() => setNow(Date.now() / 1000), 1_000)
  return () => {
    window.clearInterval(poll)
    window.clearInterval(tick)
  }
}, [load])
```

Change the refresh button callback to `onClick={() => void load()}`.

- [ ] **Step 5: Render the summary error count and independent deleted count**

At the start of `renderTask`, replace `errors.length` and add the deleted counter:

```typescript
const failed = Math.max(0, Number(task.error_count) || 0)
const invalidRtCount = Math.max(0, Number(task.meta?.invalid_rt_count) || 0)
const reloginFailedCount = Math.max(0, Number(task.meta?.relogin_failed_count) || 0)
const deletedAccountCount = Math.max(
  0,
  Number(task.meta?.deleted_account_count) || 0,
)
```

Inside the automatic-authentication tag group, keep the existing authentication and re-login tags and add:

```tsx
<Tag
  color={deletedAccountCount > 0 ? 'warning' : 'default'}
  style={{ margin: 0 }}
>
  已删除账号 {deletedAccountCount}
</Tag>
```

The red text continues to use server-derived `error_count=max(relogin_failed_count-deleted_account_count, 0)` for automatic authentication and persisted error-array length for ordinary tasks. `重登失败` continues to use inclusive `meta.relogin_failed_count` for the email threshold. `已删除账号` is the visible subset produced by the account-removal work.

- [ ] **Step 6: Run frontend focused tests and verify GREEN**

Run from `frontend/`:

```bash
npm test -- \
  src/pages/RunningTasks.test.tsx \
  src/components/TaskLogPanel.test.tsx
npm run lint
npm run build
```

Expected: both test files pass, lint passes, and the production bundle builds. The page asks only for summaries, blocked polls do not overlap, a failed request permits the next poll, and full logs remain lazy until the drawer opens.

- [ ] **Step 7: Commit the summary UI**

```bash
git add \
  frontend/src/pages/RunningTasks.tsx \
  frontend/src/pages/RunningTasks.test.tsx
git commit -m "feat: load task cards from summaries"
```

### Task 5: Cross-layer review and complete local verification

**Files:**
- Verify: `api/tasks.py`
- Verify: `services/task_history_retention.py`
- Verify: `core/scheduler.py`
- Verify: `frontend/src/pages/RunningTasks.tsx`
- Reference: `docs/superpowers/specs/2026-08-05-task-runs-performance-retention-design.md`
- Reference: `docs/superpowers/specs/2026-08-05-chatgpt-account-removal-design.md`

- [ ] **Step 1: Run the focused cross-layer regression set**

From the repository root:

```bash
PYTHONPATH=. .venv/bin/pytest \
  tests/test_task_summary.py \
  tests/test_task_history_retention.py \
  tests/test_scheduler.py \
  tests/test_task_snapshot_persistence.py \
  tests/test_task_runtime.py \
  tests/test_chatgpt_relogin_task.py -q
cd frontend
npm test -- \
  src/pages/RunningTasks.test.tsx \
  src/components/TaskLogPanel.test.tsx
```

Expected: every focused test passes. In particular, the companion account-removal tests produce `deleted_account_count`; the summary derives `失败 3` from `重登失败 20 - 已删除账号 17`; and the UI displays all three values from their distinct contracts.

- [ ] **Step 2: Perform independent specification and code-quality reviews**

Dispatch one specification reviewer and one code-quality reviewer over the complete diff. The specification reviewer must check every chosen-contract bullet: legacy `/tasks`, projected `/tasks/summary`, active-first ordering, exact metadata whitelist, no overlapping poll, 12-hour strict boundary, `completed_at` precedence, active preservation, 10-minute cadence, unchanged task-log policy, and independent deleted-account rendering. The quality reviewer must check malformed JSON handling, SQL parameter bounds, datetime timezone behavior, route order, React rejection handling, timer cleanup, data/credential leakage, and test isolation.

Resolve every Critical or Important finding, rerun the directly affected focused command, and commit review fixes with a message that names the corrected behavior.

- [ ] **Step 3: Run the complete backend and frontend suites**

From the repository root:

```bash
PYTHONPATH=. .venv/bin/pytest -q
cd frontend
npm test
npm run lint
npm run build
```

Expected: the complete backend suite, complete frontend suite, ESLint, TypeScript compilation, and Vite production build all pass.

- [ ] **Step 4: Verify the final diff and commit state**

From the repository root:

```bash
git diff --check
git status --short
git log --oneline -8
```

Expected: `git diff --check` prints nothing; `git status --short` is empty after all reviewed fixes are committed; the recent log includes the summary API, 12-hour retention, 10-minute cleanup, and summary UI commits.

### Task 6: Back up, deploy, purge, and verify production

**Files/locations:**
- Production database: `/www/any-auto-register/shared/data/account_manager.db`
- Production backups: `/www/any-auto-register/shared/backups/`
- Production releases: `/www/any-auto-register/releases/<verified-commit>`
- Production environment: `/www/any-auto-register/shared/app.env`
- Production service: `any-auto-register.service`

- [ ] **Step 1: Push only the fully verified commit**

From the repository root:

```bash
git push origin HEAD:main
git rev-parse HEAD
git ls-remote origin refs/heads/main
```

Expected: the local HEAD and remote `main` object IDs are identical. Record that object ID as `RELEASE_SHA` for the following commands.

- [ ] **Step 2: Confirm the production database and scheduler are idle before backup**

Run the read-only check; it prints counts only:

```bash
ssh -i /Users/xuann/.ssh/id_ed25519_103_144_241_126 \
  -p 55222 root@103.144.241.126 \
  "sqlite3 /www/any-auto-register/shared/data/account_manager.db \
  \"SELECT status, COUNT(*) FROM task_runs GROUP BY status ORDER BY status;\""
```

Expected: `pending` and `running` are absent or have count zero. If either is nonzero, wait for the current task to reach a terminal state and repeat this exact read-only check before continuing.

- [ ] **Step 3: Create and verify a consistent pre-purge SQLite backup**

Run SQLite's backup API on the production host while the service remains available. The script prints only the backup path, byte size, and integrity result:

```bash
ssh -i /Users/xuann/.ssh/id_ed25519_103_144_241_126 \
  -p 55222 root@103.144.241.126 \
  '/www/any-auto-register/venv/bin/python - <<'"'"'PY'"'"'
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

source_path = Path("/www/any-auto-register/shared/data/account_manager.db")
backup_dir = Path("/www/any-auto-register/shared/backups")
backup_dir.mkdir(parents=True, exist_ok=True)
stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
backup_path = backup_dir / f"account_manager.pre-12h-retention.{stamp}.db"

with sqlite3.connect(source_path) as source:
    with sqlite3.connect(backup_path) as destination:
        source.backup(destination)
with sqlite3.connect(backup_path) as backup:
    check = backup.execute("PRAGMA quick_check").fetchone()[0]
if check != "ok":
    raise SystemExit(f"backup quick_check failed: {check}")
print(f"backup={backup_path}")
print(f"bytes={backup_path.stat().st_size}")
print("quick_check=ok")
PY'
```

Expected: the backup is under `/www/any-auto-register/shared/backups`, has a positive byte size, and reports `quick_check=ok`. Retain this file and the prior release for rollback.

- [ ] **Step 4: Build and upload the immutable release**

From the repository root, replace the shell value on the first line with the exact object ID printed in Step 1, then build and upload source plus the already-verified static bundle:

```bash
RELEASE_SHA="$(git rev-parse HEAD)"
PACKAGE_DIR="$(mktemp -d)"
git archive "$RELEASE_SHA" | tar -x -C "$PACKAGE_DIR"
cp -R static "$PACKAGE_DIR/static"
tar -C "$PACKAGE_DIR" -czf "/tmp/any-auto-register-${RELEASE_SHA}.tar.gz" .
ssh -i /Users/xuann/.ssh/id_ed25519_103_144_241_126 \
  -p 55222 root@103.144.241.126 \
  "mkdir -p /www/any-auto-register/releases/${RELEASE_SHA}"
scp -i /Users/xuann/.ssh/id_ed25519_103_144_241_126 \
  -P 55222 "/tmp/any-auto-register-${RELEASE_SHA}.tar.gz" \
  root@103.144.241.126:/tmp/
ssh -i /Users/xuann/.ssh/id_ed25519_103_144_241_126 \
  -p 55222 root@103.144.241.126 \
  "tar -xzf /tmp/any-auto-register-${RELEASE_SHA}.tar.gz \
  -C /www/any-auto-register/releases/${RELEASE_SHA} && \
  /www/any-auto-register/venv/bin/python -m compileall -q \
  /www/any-auto-register/releases/${RELEASE_SHA}/api \
  /www/any-auto-register/releases/${RELEASE_SHA}/core \
  /www/any-auto-register/releases/${RELEASE_SHA}/services"
```

Expected: archive upload, extraction, and Python compilation finish successfully. Do not remove the previous release or backup.

- [ ] **Step 5: Set the shared retention environment without changing automation settings**

Update only the new environment line and then print only the three retention settings:

```bash
ssh -i /Users/xuann/.ssh/id_ed25519_103_144_241_126 \
  -p 55222 root@103.144.241.126 \
  '/www/any-auto-register/venv/bin/python - <<'"'"'PY'"'"'
from pathlib import Path

path = Path("/www/any-auto-register/shared/app.env")
lines = path.read_text(encoding="utf-8").splitlines()
key = "TASK_RUN_RETENTION_HOURS"
replacement = f"{key}=12"
updated: list[str] = []
replaced = False
for line in lines:
    if line.startswith(f"{key}="):
        if not replaced:
            updated.append(replacement)
            replaced = True
        continue
    updated.append(line)
if not replaced:
    updated.append(replacement)
path.write_text("\n".join(updated) + "\n", encoding="utf-8")
for name in (
    "TASK_RUN_RETENTION_HOURS",
    "TASK_HISTORY_RETENTION_DAYS",
    "TASK_HISTORY_FAILURE_RETENTION_DAYS",
):
    print(next(line for line in updated if line.startswith(f"{name}=")))
PY'
```

Expected:

```text
TASK_RUN_RETENTION_HOURS=12
TASK_HISTORY_RETENTION_DAYS=30
TASK_HISTORY_FAILURE_RETENTION_DAYS=90
```

- [ ] **Step 6: Atomically switch only this service and wait for startup cleanup**

From the repository root:

```bash
RELEASE_SHA="$(git rev-parse HEAD)"
ssh -i /Users/xuann/.ssh/id_ed25519_103_144_241_126 \
  -p 55222 root@103.144.241.126 \
  "readlink -f /www/any-auto-register/current && \
  ln -sfn /www/any-auto-register/releases/${RELEASE_SHA} \
  /www/any-auto-register/current.next && \
  mv -Tf /www/any-auto-register/current.next /www/any-auto-register/current && \
  systemctl restart any-auto-register.service && \
  systemctl is-active any-auto-register.service"
```

Expected: the command first prints the previous release path, then `active`. Only `any-auto-register.service` is restarted; Codex2API, Docker, Nginx, and unrelated services are untouched.

- [ ] **Step 7: Verify the purge boundary, active preservation, and both database copies**

Run the deployed completion resolver against projected columns and print counts only:

```bash
ssh -i /Users/xuann/.ssh/id_ed25519_103_144_241_126 \
  -p 55222 root@103.144.241.126 \
  'cd /www/any-auto-register/current && \
  DATABASE_URL=sqlite:////www/any-auto-register/shared/data/account_manager.db \
  /www/any-auto-register/venv/bin/python - <<'"'"'PY'"'"'
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3

from services.task_history_retention import _effective_completion_at

database_path = "/www/any-auto-register/shared/data/account_manager.db"
cutoff = datetime.now(timezone.utc) - timedelta(hours=12)
with sqlite3.connect(database_path) as connection:
    rows = connection.execute(
        "SELECT status, meta_json, updated_at FROM task_runs"
    ).fetchall()
    current_check = connection.execute("PRAGMA quick_check").fetchone()[0]

expired_terminal = 0
active = 0
recent_terminal = 0
for status, meta_json, updated_at in rows:
    updated = datetime.fromisoformat(str(updated_at))
    if status in {"pending", "running"}:
        active += 1
    elif status in {"done", "failed", "stopped"}:
        if _effective_completion_at(meta_json, updated) < cutoff:
            expired_terminal += 1
        else:
            recent_terminal += 1

backup_paths = sorted(
    Path("/www/any-auto-register/shared/backups").glob(
        "account_manager.pre-12h-retention.*.db"
    ),
    key=lambda path: path.stat().st_mtime,
)
if not backup_paths:
    raise SystemExit("pre-purge backup missing")
with sqlite3.connect(backup_paths[-1]) as backup:
    backup_check = backup.execute("PRAGMA quick_check").fetchone()[0]

print(f"task_runs_total={len(rows)}")
print(f"active={active}")
print(f"recent_terminal={recent_terminal}")
print(f"expired_terminal={expired_terminal}")
print(f"current_quick_check={current_check}")
print(f"backup_quick_check={backup_check}")
if expired_terminal != 0 or current_check != "ok" or backup_check != "ok":
    raise SystemExit(1)
PY'
```

Expected: `expired_terminal=0`, both quick checks are `ok`, active is still the pre-deploy count, and recent terminal rows remain. Do not run `VACUUM`.

- [ ] **Step 8: Measure and validate the authenticated summary response without printing its body**

Generate a five-minute server-local bearer token, download the response to a temporary file, print only latency/byte/count assertions, and delete the file:

```bash
ssh -i /Users/xuann/.ssh/id_ed25519_103_144_241_126 \
  -p 55222 root@103.144.241.126 \
  'cd /www/any-auto-register/current && \
  TASK_API_TOKEN="$(DATABASE_URL=sqlite:////www/any-auto-register/shared/data/account_manager.db \
  /www/any-auto-register/venv/bin/python -c \
  '"'"'from api.auth import create_token; print(create_token(300))'"'"')" && \
  TASK_SUMMARY_FILE="$(mktemp /tmp/task-summary.XXXXXX.json)" && \
  curl --fail --silent --show-error \
  --output "$TASK_SUMMARY_FILE" \
  --write-out "http=%{http_code} seconds=%{time_total} bytes=%{size_download}\n" \
  --header "Authorization: Bearer $TASK_API_TOKEN" \
  http://127.0.0.1:18081/api/tasks/summary && \
  /www/any-auto-register/venv/bin/python - "$TASK_SUMMARY_FILE" <<'"'"'PY'"'"'
import json
from pathlib import Path
import sys

path = Path(sys.argv[1])
items = json.loads(path.read_text(encoding="utf-8"))
heavy = {"logs", "errors", "control", "cashier_urls", "error", "progress"}
if not isinstance(items, list):
    raise SystemExit("summary is not a list")
if any(heavy.intersection(item) for item in items):
    raise SystemExit("heavy summary field detected")
for item in items:
    meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
    if meta.get("automation"):
        expected = max(
            int(meta.get("relogin_failed_count") or 0)
            - int(meta.get("deleted_account_count") or 0),
            0,
        )
        if int(item.get("error_count") or 0) != expected:
            raise SystemExit("automatic error_count formula mismatch")
print(f"summary_items={len(items)}")
print(f"summary_bytes={path.stat().st_size}")
PY
  rm -f "$TASK_SUMMARY_FILE" && \
  unset TASK_API_TOKEN TASK_SUMMARY_FILE'
```

Expected: HTTP 200, a small response relative to the former approximately 13.6 MB full-list payload, no heavy fields, and no response body or token printed to the terminal.

- [ ] **Step 9: Verify UI assets, scheduler settings, and clean service health**

Run count-only and non-secret checks:

```bash
ssh -i /Users/xuann/.ssh/id_ed25519_103_144_241_126 \
  -p 55222 root@103.144.241.126 \
  'systemctl is-active any-auto-register.service && \
  grep -RIl "/tasks/summary" /www/any-auto-register/current/static/assets | wc -l && \
  grep -RIl "已删除账号" /www/any-auto-register/current/static/assets | wc -l && \
  sqlite3 /www/any-auto-register/shared/data/account_manager.db \
  "SELECT key, value FROM configs WHERE key IN 
  ('"'"'chatgpt_auto_relogin_enabled'"'"',
   '"'"'chatgpt_auto_relogin_interval_minutes'"'"',
   '"'"'chatgpt_auto_relogin_concurrency'"'"',
   '"'"'chatgpt_auto_relogin_alert_threshold'"'"') ORDER BY key;" && \
  journalctl -u any-auto-register.service --since "10 minutes ago" --no-pager \
  | grep -Eic "Traceback|Unhandled|任务历史清理错误"'
```

Expected: service is `active`; each bundle search count is at least 1; automatic re-login remains enabled with interval 10, concurrency 4, and threshold 20; the final error-pattern count is `0`. Keep the prior release directory and verified backup. Do not delete or compact production data beyond the scheduler's exact expired terminal IDs.
