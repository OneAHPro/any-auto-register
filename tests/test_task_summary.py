import json
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlmodel import Session, SQLModel

from api import tasks
from core import db
from core.db import TaskRunModel


@pytest.fixture
def task_api(tmp_path):
    test_engine = db._create_database_engine(
        f"sqlite:///{tmp_path / 'task-summary.db'}"
    )
    SQLModel.metadata.create_all(test_engine)
    app = FastAPI()
    app.include_router(tasks.router)

    with (
        mock.patch.object(tasks, "engine", test_engine),
        mock.patch.object(
            tasks._task_store,
            "exists",
            return_value=True,
        ) as live_task_exists,
        TestClient(app) as client,
    ):
        yield client, test_engine, live_task_exists

    test_engine.dispose()


def _insert_task(
    engine,
    task_id: str,
    *,
    status: str = "done",
    source: str = "manual",
    created_at: datetime | None = None,
    meta_json: str = "{}",
    errors_json: str = "[]",
    logs_json: str = "[]",
) -> None:
    created = created_at or datetime.now(timezone.utc)
    with Session(engine) as session:
        session.add(
            TaskRunModel(
                id=task_id,
                platform="chatgpt",
                source=source,
                status=status,
                total=11,
                progress="11/11",
                success=7,
                registered=10,
                skipped=1,
                error="TOP_LEVEL_SECRET",
                meta_json=meta_json,
                logs_json=logs_json,
                errors_json=errors_json,
                cashier_urls_json='["https://secret.example/cashier"]',
                control_json='{"private_control": "secret"}',
                created_at=created,
                updated_at=created + timedelta(seconds=45),
            )
        )
        session.commit()


def test_summary_contract_is_small_and_does_not_select_large_payload_columns(task_api):
    client, engine, live_task_exists = task_api
    secret = "ERROR_BODY_MUST_NOT_LEAVE_SQLITE"
    active_secret = "ACTIVE_ERROR_BODY_MUST_NOT_LEAVE_SQLITE"
    created_at = datetime(2026, 8, 5, 1, 2, 3, tzinfo=timezone.utc)
    _insert_task(
        engine,
        "task-contract",
        created_at=created_at,
        meta_json=json.dumps(
            {
                "automation": False,
                "invalid_rt_count": 3,
                "alert_reason": "below_threshold",
                "private_token": "META_SECRET",
            }
        ),
        errors_json=json.dumps([secret, "second error"]),
        logs_json=json.dumps(["L" * 250_000]),
    )
    _insert_task(
        engine,
        "task-active-live",
        status="running",
        created_at=created_at + timedelta(minutes=1),
        errors_json=json.dumps([active_secret] * 2_000),
        logs_json=json.dumps(["ACTIVE_LOG_SECRET" + ("X" * 250_000)]),
    )

    statements: list[str] = []

    def capture_statement(_conn, _cursor, statement, _params, _context, _many):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture_statement)
    try:
        response = client.get("/tasks/summary")
    finally:
        event.remove(engine, "before_cursor_execute", capture_statement)

    assert response.status_code == 200
    assert len(response.content) < 1_500
    assert secret not in response.text
    assert active_secret not in response.text
    assert "ACTIVE_LOG_SECRET" not in response.text
    assert "META_SECRET" not in response.text
    assert "TOP_LEVEL_SECRET" not in response.text
    assert response.json() == [
        {
            "id": "task-active-live",
            "platform": "chatgpt",
            "source": "manual",
            "status": "running",
            "total": 11,
            "success": 7,
            "registered": 10,
            "skipped": 1,
            "error_count": 2_000,
            "created_at": (created_at + timedelta(minutes=1)).timestamp(),
            "updated_at": (
                created_at + timedelta(minutes=1, seconds=45)
            ).timestamp(),
            "meta": {},
        },
        {
            "id": "task-contract",
            "platform": "chatgpt",
            "source": "manual",
            "status": "done",
            "total": 11,
            "success": 7,
            "registered": 10,
            "skipped": 1,
            "error_count": 2,
            "created_at": created_at.timestamp(),
            "updated_at": (created_at + timedelta(seconds=45)).timestamp(),
            "meta": {
                "automation": False,
                "invalid_rt_count": 3,
                "alert_reason": "below_threshold",
            },
        }
    ]
    select_statements = [
        statement.lower()
        for statement in statements
        if statement.lstrip().lower().startswith("select")
    ]
    select_sql = "\n".join(select_statements)
    assert len(select_statements) == 2
    assert "logs_json" not in select_sql
    assert "control_json" not in select_sql
    assert "cashier_urls_json" not in select_sql
    assert "task_runs.error," not in select_sql
    assert "json_array_length" in select_sql
    assert " as errors_json" not in select_sql
    orphan_scan_sql = next(
        statement
        for statement in select_statements
        if "where task_runs.status in" in statement
    )
    assert "select task_runs.id" in orphan_scan_sql
    assert "json_array_length" not in orphan_scan_sql
    live_task_exists.assert_called_once_with("task-active-live")


def test_summary_uses_automatic_and_regular_error_count_rules(task_api):
    client, engine, _live_task_exists = task_api
    now = datetime.now(timezone.utc)
    _insert_task(
        engine,
        "task-automatic",
        source="schedule",
        created_at=now,
        meta_json=json.dumps(
            {
                "automation": True,
                "invalid_rt_count": 9,
                "relogin_failed_count": 9,
                "deleted_account_count": 4,
                "alert_sent": True,
                "alert_reason": "sent",
                "bark_alert_sent": False,
                "bark_alert_reason": "send_failed",
                "bark_alert_error_type": "TimeoutError",
                "bark_alert_threshold": 20,
                "bark_quota_alert_sent": True,
                "bark_quota_alert_reason": "sent",
                "bark_quota_alert_error_type": "",
                "bark_quota_alert_threshold_usd": "120.00",
                "private": "hidden",
            }
        ),
        errors_json=json.dumps(["ignored"] * 30),
    )
    _insert_task(
        engine,
        "task-regular",
        created_at=now - timedelta(seconds=1),
        errors_json=json.dumps(["first", "second", "third"]),
    )
    _insert_task(
        engine,
        "task-automatic-floor",
        source="schedule",
        created_at=now - timedelta(seconds=2),
        meta_json=json.dumps(
            {
                "automation": True,
                "relogin_failed_count": 2,
                "deleted_account_count": 8,
            }
        ),
        errors_json=json.dumps(["ignored"] * 10),
    )

    response = client.get("/tasks/summary")

    assert response.status_code == 200
    by_id = {item["id"]: item for item in response.json()}
    assert by_id["task-automatic"]["error_count"] == 5
    assert by_id["task-automatic"]["meta"] == {
        "automation": True,
        "invalid_rt_count": 9,
        "relogin_failed_count": 9,
        "deleted_account_count": 4,
        "alert_sent": True,
        "alert_reason": "sent",
        "bark_alert_sent": False,
        "bark_alert_reason": "send_failed",
        "bark_alert_error_type": "TimeoutError",
        "bark_alert_threshold": 20,
        "bark_quota_alert_sent": True,
        "bark_quota_alert_reason": "sent",
        "bark_quota_alert_error_type": "",
        "bark_quota_alert_threshold_usd": "120.00",
    }
    assert by_id["task-regular"]["error_count"] == 3
    assert by_id["task-automatic-floor"]["error_count"] == 0


@pytest.mark.parametrize(
    ("errors_json", "meta_json"),
    [
        ("not-json", "not-json"),
        ('{"not": "an array"}', "{}"),
        ("null", '{"automation": true}'),
        (
            "[]",
            json.dumps(
                {
                    "automation": True,
                    "relogin_failed_count": "invalid",
                    "deleted_account_count": -4,
                }
            ),
        ),
    ],
)
def test_summary_safely_normalizes_invalid_or_missing_json(
    task_api,
    errors_json,
    meta_json,
):
    client, engine, _live_task_exists = task_api
    _insert_task(
        engine,
        "task-invalid",
        meta_json=meta_json,
        errors_json=errors_json,
    )

    response = client.get("/tasks/summary")

    assert response.status_code == 200
    [item] = response.json()
    assert item["error_count"] == 0
    assert set(item["meta"]).issubset(
        {
            "automation",
            "invalid_rt_count",
            "relogin_failed_count",
            "deleted_account_count",
            "alert_sent",
            "alert_reason",
        }
    )


def test_summary_orders_active_statuses_then_all_terminal_tasks_by_recency(task_api):
    client, engine, _live_task_exists = task_api
    base = datetime(2026, 8, 5, 1, 0, tzinfo=timezone.utc)
    fixtures = [
        ("terminal-old-done", "done", 1),
        ("pending-old", "pending", 2),
        ("running-old", "running", 3),
        ("terminal-new-stopped", "stopped", 9),
        ("pending-new", "pending", 7),
        ("terminal-middle-failed", "failed", 5),
        ("running-new", "running", 8),
    ]
    for task_id, status, offset in fixtures:
        _insert_task(
            engine,
            task_id,
            status=status,
            created_at=base + timedelta(minutes=offset),
        )

    response = client.get("/tasks/summary")

    assert response.status_code == 200
    assert [item["id"] for item in response.json()] == [
        "running-new",
        "running-old",
        "pending-new",
        "pending-old",
        "terminal-new-stopped",
        "terminal-middle-failed",
        "terminal-old-done",
    ]


def test_static_summary_route_does_not_break_full_legacy_tasks_response(task_api):
    client, engine, _live_task_exists = task_api
    _insert_task(
        engine,
        "task-legacy",
        errors_json='["legacy error"]',
        logs_json='["legacy log"]',
    )

    summary_response = client.get("/tasks/summary")
    legacy_response = client.get("/tasks")
    detail_response = client.get("/tasks/task-legacy")

    assert summary_response.status_code == 200
    assert summary_response.json()[0]["id"] == "task-legacy"
    assert legacy_response.status_code == 200
    assert legacy_response.json()[0]["id"] == "task-legacy"
    assert legacy_response.json()[0]["errors"] == ["legacy error"]
    assert legacy_response.json()[0]["logs"] == ["legacy log"]
    assert detail_response.status_code == 200
    assert detail_response.json()["id"] == "task-legacy"
    assert detail_response.json()["errors"] == ["legacy error"]
    assert detail_response.json()["logs"] == ["legacy log"]
