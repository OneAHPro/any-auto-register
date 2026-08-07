from __future__ import annotations

import importlib
import threading
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient


PUBLIC_KEYS = {
    "codex2api_delete_on_account_remove_enabled",
    "chatgpt_auto_relogin_enabled",
    "chatgpt_auto_relogin_interval_minutes",
    "chatgpt_auto_relogin_concurrency",
    "chatgpt_auto_relogin_alert_threshold",
    "chatgpt_auto_relogin_quota_alert_threshold_usd",
    "smtp_host",
    "smtp_port",
    "smtp_username",
    "smtp_password",
    "smtp_sender_email",
    "smtp_recipient_email",
    "smtp_use_ssl",
    "smtp_force_auth_login",
}


class FakeConfigStore:
    def __init__(self, values: dict[str, object] | None = None):
        self.values = dict(values or {})
        self.writes: list[dict[str, object]] = []

    def get(self, key: str, default: str = ""):
        return self.values.get(key, default)

    def get_all(self) -> dict[str, object]:
        return dict(self.values)

    def set_many(self, data: dict[str, object]) -> None:
        payload = dict(data)
        self.writes.append(payload)
        self.values.update(payload)


class MutatingConfigStore(FakeConfigStore):
    """Returns a new point-in-time view each time the store is queried."""

    def __init__(self, snapshots: list[dict[str, object]]):
        super().__init__()
        self.snapshots = [dict(snapshot) for snapshot in snapshots]
        self.get_all_calls = 0

    def get_all(self) -> dict[str, object]:
        index = min(self.get_all_calls, len(self.snapshots) - 1)
        self.get_all_calls += 1
        return dict(self.snapshots[index])

    def get(self, key: str, default: str = ""):
        return self.get_all().get(key, default)


def _service_module():
    return importlib.import_module("services.chatgpt_auto_relogin")


def _automations_module():
    return importlib.import_module("api.automations")


def test_public_config_defaults_are_exposed_without_database_writes(monkeypatch):
    from api import config as config_api

    store = FakeConfigStore()
    monkeypatch.setattr(config_api, "config_store", store)

    response = config_api.get_config()

    assert PUBLIC_KEYS.issubset(config_api.CONFIG_KEYS)
    assert response["codex2api_delete_on_account_remove_enabled"] == "0"
    assert response["chatgpt_auto_relogin_enabled"] == "0"
    assert response["chatgpt_auto_relogin_interval_minutes"] == "2"
    assert response["chatgpt_auto_relogin_concurrency"] == "10"
    assert response["chatgpt_auto_relogin_alert_threshold"] == "20"
    assert response["chatgpt_auto_relogin_quota_alert_threshold_usd"] == "0.00"
    assert response["smtp_port"] == "587"
    assert response["smtp_use_ssl"] == "1"
    assert store.writes == []


@pytest.mark.parametrize(
    ("submitted", "expected"),
    [
        (True, "1"),
        (" YES ", "1"),
        ("On", "1"),
        (False, "0"),
        ("unexpected", "0"),
    ],
)
def test_codex2api_account_removal_link_normalizes_independently(
    monkeypatch,
    submitted,
    expected,
):
    from api import config as config_api

    store = FakeConfigStore({"codex2api_enabled": "0"})
    monkeypatch.setattr(config_api, "config_store", store)

    result = config_api.update_config(
        config_api.ConfigUpdate(
            data={"codex2api_delete_on_account_remove_enabled": submitted}
        )
    )

    assert result == {
        "ok": True,
        "updated": ["codex2api_delete_on_account_remove_enabled"],
    }
    assert store.writes == [
        {"codex2api_delete_on_account_remove_enabled": expected}
    ]
    assert store.values["codex2api_enabled"] == "0"


def test_public_config_preserves_saved_relogin_alert_threshold_without_writes(monkeypatch):
    from api import config as config_api

    store = FakeConfigStore({"chatgpt_auto_relogin_alert_threshold": "5"})
    monkeypatch.setattr(config_api, "config_store", store)

    response = config_api.get_config()

    assert response["chatgpt_auto_relogin_alert_threshold"] == "5"
    assert store.writes == []


def test_public_config_never_returns_or_clears_saved_smtp_password(monkeypatch):
    from api import config as config_api

    store = FakeConfigStore({"smtp_password": "stored-smtp-credential"})
    monkeypatch.setattr(config_api, "config_store", store)

    response = config_api.get_config()
    result = config_api.update_config(
        config_api.ConfigUpdate(
            data={
                "smtp_password": "",
                "chatgpt_auto_relogin_alert_threshold": 5,
            }
        )
    )

    assert response["smtp_password"] == ""
    assert store.values["smtp_password"] == "stored-smtp-credential"
    assert store.writes == [{"chatgpt_auto_relogin_alert_threshold": "5"}]
    assert result["updated"] == ["chatgpt_auto_relogin_alert_threshold"]


def test_smtp_test_uses_unsaved_form_values_without_persisting_them(monkeypatch):
    from api import config as config_api
    from services import chatgpt_auto_relogin_alerts as alerts

    store = FakeConfigStore(
        {
            "smtp_host": "smtp.saved.example",
            "smtp_port": "587",
            "smtp_username": "saved@example.com",
            "smtp_password": "stored-smtp-credential",
            "smtp_sender_email": "saved@example.com",
            "smtp_recipient_email": "old@example.com",
            "smtp_use_ssl": "1",
            "smtp_force_auth_login": "0",
        }
    )
    observed = {}

    def send_test(*, config):
        observed.update(config)
        return {"sent": True, "reason": "sent", "recipient_count": 1}

    monkeypatch.setattr(config_api, "config_store", store)
    monkeypatch.setattr(alerts, "send_smtp_test_email", send_test)

    result = config_api.test_smtp_config(
        config_api.SMTPTestRequest(
            data={
                "smtp_host": "smtp.form.example",
                "smtp_port": 465,
                "smtp_password": "",
                "smtp_recipient_email": "new@example.com",
                "smtp_use_ssl": True,
                "smtp_force_auth_login": False,
                "chatgpt_auto_relogin_enabled": True,
            }
        )
    )

    assert result == {
        "ok": True,
        "message": "测试邮件已发送",
        "recipient_count": 1,
    }
    assert observed["smtp_host"] == "smtp.form.example"
    assert observed["smtp_port"] == "465"
    assert observed["smtp_password"] == "stored-smtp-credential"
    assert observed["smtp_recipient_email"] == "new@example.com"
    assert observed["smtp_use_ssl"] == "1"
    assert observed["smtp_force_auth_login"] == "0"
    assert "chatgpt_auto_relogin_enabled" not in observed
    assert store.writes == []


@pytest.mark.parametrize(
    ("submitted", "expected"),
    [
        (True, "1"),
        (False, "0"),
        ("yes", "1"),
        ("off", "0"),
    ],
)
def test_public_config_put_normalizes_enabled_to_zero_or_one(
    monkeypatch,
    submitted,
    expected,
):
    from api import config as config_api

    store = FakeConfigStore()
    reconciles = []
    monkeypatch.setattr(config_api, "config_store", store)
    monkeypatch.setattr(
        _service_module(),
        "tick_chatgpt_auto_relogin",
        lambda **kwargs: reconciles.append(kwargs) or {},
    )

    result = config_api.update_config(
        config_api.ConfigUpdate(
            data={
                "chatgpt_auto_relogin_enabled": submitted,
                "chatgpt_auto_relogin_interval_minutes": 30,
                "chatgpt_auto_relogin_concurrency": 10,
            }
        )
    )

    assert result["ok"] is True
    assert store.values == {
        "chatgpt_auto_relogin_enabled": expected,
        "chatgpt_auto_relogin_interval_minutes": "30",
        "chatgpt_auto_relogin_concurrency": "10",
    }
    assert reconciles == [{"store": store}]


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("chatgpt_auto_relogin_interval_minutes", 1),
        ("chatgpt_auto_relogin_interval_minutes", 1441),
        ("chatgpt_auto_relogin_interval_minutes", "not-an-integer"),
        ("chatgpt_auto_relogin_concurrency", 0),
        ("chatgpt_auto_relogin_concurrency", 11),
        ("chatgpt_auto_relogin_concurrency", "not-an-integer"),
        ("chatgpt_auto_relogin_alert_threshold", 0),
        ("chatgpt_auto_relogin_alert_threshold", 10001),
        ("chatgpt_auto_relogin_alert_threshold", "not-an-integer"),
        ("smtp_port", 0),
        ("smtp_port", 65536),
        ("smtp_port", "not-an-integer"),
    ],
)
def test_public_config_put_rejects_invalid_auto_relogin_numbers(
    monkeypatch,
    key,
    value,
):
    from api import config as config_api

    store = FakeConfigStore()
    monkeypatch.setattr(config_api, "config_store", store)

    with pytest.raises(HTTPException) as error:
        config_api.update_config(config_api.ConfigUpdate(data={key: value}))

    assert error.value.status_code == 400
    assert store.writes == []


def test_public_config_put_reports_relogin_alert_threshold_bounds(monkeypatch):
    from api import config as config_api

    store = FakeConfigStore()
    monkeypatch.setattr(config_api, "config_store", store)

    with pytest.raises(HTTPException) as error:
        config_api.update_config(
            config_api.ConfigUpdate(data={"chatgpt_auto_relogin_alert_threshold": 0})
        )

    assert error.value.status_code == 400
    assert error.value.detail == "重登失败告警阈值必须在 1 到 10000 之间"
    assert store.writes == []


@pytest.mark.parametrize(
    ("submitted", "expected"),
    [
        (0, "0.00"),
        (1200, "1200.00"),
        (1200.5, "1200.50"),
        ("1200.55", "1200.55"),
    ],
)
def test_public_config_put_normalizes_quota_alert_threshold(
    monkeypatch,
    submitted,
    expected,
):
    from api import config as config_api

    store = FakeConfigStore()
    monkeypatch.setattr(config_api, "config_store", store)

    result = config_api.update_config(
        config_api.ConfigUpdate(
            data={
                "chatgpt_auto_relogin_quota_alert_threshold_usd": submitted
            }
        )
    )

    assert result == {
        "ok": True,
        "updated": ["chatgpt_auto_relogin_quota_alert_threshold_usd"],
    }
    assert store.writes == [
        {"chatgpt_auto_relogin_quota_alert_threshold_usd": expected}
    ]


@pytest.mark.parametrize(
    "submitted",
    [-0.01, 10000000.01, 12.345, "NaN", "Infinity", "not-a-number"],
)
def test_public_config_put_rejects_invalid_quota_alert_threshold(
    monkeypatch,
    submitted,
):
    from api import config as config_api

    store = FakeConfigStore()
    monkeypatch.setattr(config_api, "config_store", store)

    with pytest.raises(HTTPException) as error:
        config_api.update_config(
            config_api.ConfigUpdate(
                data={
                    "chatgpt_auto_relogin_quota_alert_threshold_usd": submitted
                }
            )
        )

    assert error.value.status_code == 400
    assert store.writes == []


def test_service_normalizes_defaults_and_bounds_from_an_isolated_store():
    service = _service_module()

    defaults = service.get_chatgpt_auto_relogin_settings(FakeConfigStore())
    bounded = service.get_chatgpt_auto_relogin_settings(
        FakeConfigStore(
            {
                "chatgpt_auto_relogin_enabled": "yes",
                "chatgpt_auto_relogin_interval_minutes": "1",
                "chatgpt_auto_relogin_concurrency": "99",
            }
        )
    )
    invalid = service.get_chatgpt_auto_relogin_settings(
        FakeConfigStore(
            {
                "chatgpt_auto_relogin_enabled": "unexpected",
                "chatgpt_auto_relogin_interval_minutes": "unexpected",
                "chatgpt_auto_relogin_concurrency": "unexpected",
            }
        )
    )

    assert defaults.enabled is False
    assert defaults.interval_minutes == 2
    assert defaults.concurrency == 10
    assert bounded.enabled is True
    assert bounded.interval_minutes == 2
    assert bounded.concurrency == 10
    assert invalid == defaults


def test_settings_and_status_each_use_one_coherent_store_snapshot():
    service = _service_module()
    first_snapshot = {
        "chatgpt_auto_relogin_enabled": "1",
        "chatgpt_auto_relogin_interval_minutes": "30",
        "chatgpt_auto_relogin_concurrency": "2",
        "chatgpt_auto_relogin_status_state": "running",
        "chatgpt_auto_relogin_status_reason": "scheduled",
        "chatgpt_auto_relogin_status_eligible_accounts": "4",
        "chatgpt_auto_relogin_status_active_task_id": "task-first",
        "chatgpt_auto_relogin_status_last_task_id": "task-first",
        "chatgpt_auto_relogin_status_last_started_at": "2026-08-02T12:00:00Z",
        "chatgpt_auto_relogin_status_next_run_at": "2026-08-02T12:30:00Z",
    }
    later_snapshot = {
        "chatgpt_auto_relogin_enabled": "0",
        "chatgpt_auto_relogin_interval_minutes": "1440",
        "chatgpt_auto_relogin_concurrency": "10",
        "chatgpt_auto_relogin_status_state": "disabled",
        "chatgpt_auto_relogin_status_reason": "disabled_by_config",
        "chatgpt_auto_relogin_status_eligible_accounts": "99",
        "chatgpt_auto_relogin_status_active_task_id": "task-later",
        "chatgpt_auto_relogin_status_last_task_id": "task-later",
        "chatgpt_auto_relogin_status_last_started_at": "2026-08-03T12:00:00Z",
        "chatgpt_auto_relogin_status_next_run_at": "2026-08-04T12:00:00Z",
    }
    settings_store = MutatingConfigStore([first_snapshot, later_snapshot])
    status_store = MutatingConfigStore([first_snapshot, later_snapshot])

    settings = service.get_chatgpt_auto_relogin_settings(settings_store)
    status = service.get_chatgpt_auto_relogin_status(status_store)

    assert settings == service.ChatGPTAutoReloginSettings(
        enabled=True,
        interval_minutes=30,
        concurrency=2,
    )
    assert settings_store.get_all_calls == 1
    assert status == {
        "enabled": True,
        "state": "running",
        "reason": "scheduled",
        "eligible_accounts": 4,
        "active_task_id": "task-first",
        "last_task_id": "task-first",
        "last_started_at": "2026-08-02T12:00:00Z",
        "next_run_at": "2026-08-02T12:30:00Z",
        "interval_minutes": 30,
        "concurrency": 2,
    }
    assert status_store.get_all_calls == 1


def test_internal_status_can_receive_scheduler_state_without_becoming_public_config():
    service = _service_module()
    from api.config import CONFIG_KEYS

    store = FakeConfigStore({"chatgpt_auto_relogin_enabled": "1"})
    service.update_chatgpt_auto_relogin_status(
        store=store,
        state="running",
        reason="scheduled",
        eligible_accounts=12,
        active_task_id="task-active",
        last_task_id="task-active",
        last_started_at="2026-08-02T12:00:00Z",
        next_run_at="2026-08-02T12:30:00Z",
    )

    status = service.get_chatgpt_auto_relogin_status(store)

    assert status == {
        "enabled": True,
        "state": "running",
        "reason": "scheduled",
        "eligible_accounts": 12,
        "active_task_id": "task-active",
        "last_task_id": "task-active",
        "last_started_at": "2026-08-02T12:00:00Z",
        "next_run_at": "2026-08-02T12:30:00Z",
        "interval_minutes": 2,
        "concurrency": 10,
    }
    assert set(service.INTERNAL_STATUS_CONFIG_KEYS).isdisjoint(CONFIG_KEYS)


def test_status_endpoint_has_a_coherent_disabled_response(monkeypatch):
    service = _service_module()
    automations = _automations_module()
    store = FakeConfigStore()
    monkeypatch.setattr(service, "_get_config_store", lambda: store)

    app = FastAPI()
    app.include_router(automations.router, prefix="/api")
    response = TestClient(app).get("/api/automations/chatgpt-relogin")

    assert response.status_code == 200
    assert response.json() == {
        "enabled": False,
        "state": "disabled",
        "reason": "disabled_by_config",
        "eligible_accounts": 0,
        "active_task_id": None,
        "last_task_id": None,
        "last_started_at": None,
        "next_run_at": None,
        "interval_minutes": 2,
        "concurrency": 10,
    }


def test_run_now_endpoint_returns_the_started_automation_status(monkeypatch):
    automations = _automations_module()
    result = {
        "accepted": True,
        "task_id": "task-now",
        "reason": "enqueued",
        "status": {
            "enabled": True,
            "state": "running",
            "reason": "task_running",
            "active_task_id": "task-now",
        },
    }
    monkeypatch.setattr(
        automations,
        "trigger_chatgpt_auto_relogin_now",
        lambda: result,
        raising=False,
    )
    app = FastAPI()
    app.include_router(automations.router, prefix="/api")

    response = TestClient(app).post(
        "/api/automations/chatgpt-relogin/run-now"
    )

    assert response.status_code == 200
    assert response.json() == result


@pytest.mark.parametrize(
    ("reason", "status_code", "detail"),
    [
        ("disabled_by_config", 409, "自动重登已关闭，请先在设置中开启"),
        ("no_eligible_accounts", 409, "当前没有可执行自动认证的账号"),
        ("foreground_busy", 409, "当前有手工 ChatGPT 任务正在等待或运行"),
        ("task_busy", 409, "当前已有 ChatGPT 自动化任务正在运行"),
        ("enqueue_failed", 503, "自动化任务启动失败，请稍后重试"),
    ],
)
def test_run_now_endpoint_maps_rejections_to_bounded_errors(
    monkeypatch,
    reason,
    status_code,
    detail,
):
    automations = _automations_module()
    monkeypatch.setattr(
        automations,
        "trigger_chatgpt_auto_relogin_now",
        lambda: {
            "accepted": False,
            "task_id": None,
            "reason": reason,
            "status": {"enabled": True, "state": "idle"},
        },
        raising=False,
    )
    app = FastAPI()
    app.include_router(automations.router, prefix="/api")

    response = TestClient(app).post(
        "/api/automations/chatgpt-relogin/run-now"
    )

    assert response.status_code == status_code
    assert response.json() == {"detail": detail}


def test_main_includes_automations_router_under_api():
    main = importlib.import_module("main")
    automations = _automations_module()

    route = next(
        route
        for route in automations.router.routes
        if route.path == "/automations/chatgpt-relogin"
    )

    assert route.methods == {"GET"}
    assert route.endpoint is automations.get_chatgpt_relogin_automation_status
    assert route.name == "get_chatgpt_relogin_automation_status"
    assert "get" in main.app.openapi()["paths"][
        "/api/automations/chatgpt-relogin"
    ]


def test_main_includes_run_now_automation_route_under_api():
    main = importlib.import_module("main")
    automations = _automations_module()

    route = next(
        route
        for route in automations.router.routes
        if route.path == "/automations/chatgpt-relogin/run-now"
    )

    assert route.methods == {"POST"}
    assert route.endpoint is automations.run_chatgpt_relogin_now
    assert "post" in main.app.openapi()["paths"][
        "/api/automations/chatgpt-relogin/run-now"
    ]


def test_eligibility_reconcile_pauses_without_creating_an_empty_task():
    service = _service_module()
    store = FakeConfigStore(
        {
            "chatgpt_auto_relogin_enabled": "1",
            "chatgpt_auto_relogin_interval_minutes": "30",
            "chatgpt_auto_relogin_status_state": "idle",
            "chatgpt_auto_relogin_status_reason": "scheduled",
            "chatgpt_auto_relogin_status_eligible_accounts": "2",
            "chatgpt_auto_relogin_status_active_task_id": "task-active",
            "chatgpt_auto_relogin_status_next_run_at": "2026-08-02T12:30:00Z",
        }
    )

    status = service.reconcile_chatgpt_auto_relogin_eligibility(
        store=store,
        eligible_account_ids=[],
        now=datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc),
    )

    assert status["state"] == "paused_no_accounts"
    assert status["reason"] == "no_eligible_accounts"
    assert status["eligible_accounts"] == 0
    assert status["active_task_id"] is None
    assert status["next_run_at"] is None


def test_eligibility_reconcile_resumes_after_accounts_are_added():
    service = _service_module()
    store = FakeConfigStore(
        {
            "chatgpt_auto_relogin_enabled": "1",
            "chatgpt_auto_relogin_interval_minutes": "30",
            "chatgpt_auto_relogin_status_state": "paused_no_accounts",
            "chatgpt_auto_relogin_status_reason": "no_eligible_accounts",
            "chatgpt_auto_relogin_status_eligible_accounts": "0",
            "chatgpt_auto_relogin_status_active_task_id": "",
            "chatgpt_auto_relogin_status_next_run_at": "",
        }
    )

    status = service.reconcile_chatgpt_auto_relogin_eligibility(
        store=store,
        eligible_account_ids=[41],
        now=datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc),
    )

    assert status["state"] == "idle"
    assert status["reason"] == "scheduled"
    assert status["eligible_accounts"] == 1
    assert status["active_task_id"] is None
    assert status["next_run_at"] == "2026-08-02T12:30:00Z"


def test_eligibility_reconcile_defaults_to_refresh_token_maintenance_accounts(
    monkeypatch,
):
    service = _service_module()
    store = FakeConfigStore(
        {
            "chatgpt_auto_relogin_enabled": "1",
            "chatgpt_auto_relogin_interval_minutes": "10",
            "chatgpt_auto_relogin_status_state": "paused_no_accounts",
        }
    )
    monkeypatch.setattr(
        "services.chatgpt_relogin.list_auto_maintenance_account_ids",
        lambda: [71, 72],
    )
    monkeypatch.setattr(
        "services.chatgpt_relogin.list_relogin_eligible_account_ids",
        lambda: pytest.fail("reconcile used full-login eligibility"),
    )

    status = service.reconcile_chatgpt_auto_relogin_eligibility(
        store=store,
        now=datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc),
    )

    assert status["eligible_accounts"] == 2
    assert status["next_run_at"] == "2026-08-02T12:10:00Z"


def test_repeated_eligibility_reconcile_does_not_move_scheduled_deadline():
    service = _service_module()
    store = FakeConfigStore(
        {
            "chatgpt_auto_relogin_enabled": "1",
            "chatgpt_auto_relogin_interval_minutes": "30",
            "chatgpt_auto_relogin_status_state": "paused_no_accounts",
            "chatgpt_auto_relogin_status_reason": "no_eligible_accounts",
        }
    )
    first = service.reconcile_chatgpt_auto_relogin_eligibility(
        store=store,
        eligible_account_ids=[41],
        now=datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc),
    )

    second = service.reconcile_chatgpt_auto_relogin_eligibility(
        store=store,
        eligible_account_ids=[41],
        now=datetime(2026, 8, 2, 12, 1, tzinfo=timezone.utc),
    )

    assert first["next_run_at"] == "2026-08-02T12:30:00Z"
    assert second["next_run_at"] == first["next_run_at"]


def test_zero_eligible_accounts_do_not_overwrite_an_active_running_task():
    service = _service_module()
    store = FakeConfigStore(
        {
            "chatgpt_auto_relogin_enabled": "1",
            "chatgpt_auto_relogin_status_state": "running",
            "chatgpt_auto_relogin_status_reason": "task_running",
            "chatgpt_auto_relogin_status_eligible_accounts": "2",
            "chatgpt_auto_relogin_status_active_task_id": "task-running",
            "chatgpt_auto_relogin_status_next_run_at": "2026-08-02T12:30:00Z",
        }
    )

    status = service.reconcile_chatgpt_auto_relogin_eligibility(
        store=store,
        eligible_account_ids=[],
        now=datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc),
    )

    assert status["state"] == "running"
    assert status["reason"] == "task_running"
    assert status["eligible_accounts"] == 0
    assert status["active_task_id"] == "task-running"
    assert status["next_run_at"] == "2026-08-02T12:30:00Z"


def test_reconcile_cannot_overwrite_a_concurrent_running_transition():
    service = _service_module()
    store = FakeConfigStore(
        {
            "chatgpt_auto_relogin_enabled": "1",
            "chatgpt_auto_relogin_status_state": "idle",
            "chatgpt_auto_relogin_status_reason": "scheduled",
            "chatgpt_auto_relogin_status_eligible_accounts": "1",
        }
    )
    eligibility_started = threading.Event()
    release_eligibility = threading.Event()
    writer_finished = threading.Event()

    class BlockingEmptyIds:
        def __iter__(self):
            eligibility_started.set()
            release_eligibility.wait(timeout=2)
            return iter(())

    reconcile_thread = threading.Thread(
        target=lambda: service.reconcile_chatgpt_auto_relogin_eligibility(
            store=store,
            eligible_account_ids=BlockingEmptyIds(),
            now=datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc),
        )
    )
    reconcile_thread.start()
    assert eligibility_started.wait(timeout=1)

    def mark_running():
        service.update_chatgpt_auto_relogin_status(
            store=store,
            state="running",
            reason="task_running",
            active_task_id="task-running",
        )
        writer_finished.set()

    writer_thread = threading.Thread(target=mark_running)
    writer_thread.start()
    writer_finished.wait(timeout=0.1)
    release_eligibility.set()
    reconcile_thread.join(timeout=2)
    writer_thread.join(timeout=2)

    status = service.get_chatgpt_auto_relogin_status(store)
    assert status["state"] == "running"
    assert status["reason"] == "task_running"
    assert status["active_task_id"] == "task-running"


def _enabled_store(**overrides) -> FakeConfigStore:
    values = {
        "chatgpt_auto_relogin_enabled": "1",
        "chatgpt_auto_relogin_interval_minutes": "30",
        "chatgpt_auto_relogin_concurrency": "10",
    }
    values.update(overrides)
    return FakeConfigStore(values)


def _accepted(task_id: str = "task-scheduled") -> dict[str, object]:
    return {"accepted": True, "task_id": task_id, "reason": "enqueued"}


def _busy(reason: str = "task_busy") -> dict[str, object]:
    return {"accepted": False, "task_id": None, "reason": reason}


def test_run_now_enqueues_immediately_and_tracks_the_automation_task():
    service = _service_module()
    store = _enabled_store(
        chatgpt_auto_relogin_status_state="idle",
        chatgpt_auto_relogin_status_reason="scheduled",
        chatgpt_auto_relogin_status_next_run_at="2026-08-02T12:30:00Z",
    )
    now = datetime(2026, 8, 2, 12, 5, tzinfo=timezone.utc)
    enqueues = []

    result = service.trigger_chatgpt_auto_relogin_now(
        store=store,
        now=now,
        list_eligible=lambda: [3, 1, 2, 1],
        try_enqueue=lambda ids, concurrency: (
            enqueues.append((list(ids), concurrency)) or _accepted("task-now")
        ),
    )

    assert enqueues == [([1, 2, 3], 10)]
    assert result["accepted"] is True
    assert result["task_id"] == "task-now"
    assert result["reason"] == "enqueued"
    assert result["status"]["state"] == "running"
    assert result["status"]["reason"] == "task_running"
    assert result["status"]["eligible_accounts"] == 3
    assert result["status"]["active_task_id"] == "task-now"
    assert result["status"]["last_task_id"] == "task-now"
    assert result["status"]["last_started_at"] == "2026-08-02T12:05:00Z"
    assert result["status"]["next_run_at"] is None


def test_run_now_rejects_disabled_before_querying_accounts():
    service = _service_module()
    store = FakeConfigStore({"chatgpt_auto_relogin_enabled": "0"})

    result = service.trigger_chatgpt_auto_relogin_now(
        store=store,
        list_eligible=lambda: pytest.fail("disabled trigger queried accounts"),
        try_enqueue=lambda *_: pytest.fail("disabled trigger enqueued"),
    )

    assert result["accepted"] is False
    assert result["reason"] == "disabled_by_config"
    assert result["task_id"] is None
    assert result["status"]["state"] == "disabled"


def test_run_now_pauses_when_no_accounts_are_eligible():
    service = _service_module()
    store = _enabled_store(
        chatgpt_auto_relogin_status_state="idle",
        chatgpt_auto_relogin_status_next_run_at="2026-08-02T12:30:00Z",
    )

    result = service.trigger_chatgpt_auto_relogin_now(
        store=store,
        list_eligible=lambda: [],
        try_enqueue=lambda *_: pytest.fail("empty trigger enqueued"),
    )

    assert result["accepted"] is False
    assert result["reason"] == "no_eligible_accounts"
    assert result["status"]["state"] == "paused_no_accounts"
    assert result["status"]["active_task_id"] is None
    assert result["status"]["next_run_at"] is None


def test_run_now_rejects_a_persisted_active_task_without_enqueuing():
    service = _service_module()
    store = _enabled_store(
        chatgpt_auto_relogin_status_state="running",
        chatgpt_auto_relogin_status_reason="task_running",
        chatgpt_auto_relogin_status_active_task_id="task-active",
    )

    result = service.trigger_chatgpt_auto_relogin_now(
        store=store,
        list_eligible=lambda: [1],
        try_enqueue=lambda *_: pytest.fail("overlapping trigger enqueued"),
    )

    assert result["accepted"] is False
    assert result["reason"] == "task_busy"
    assert result["status"]["active_task_id"] == "task-active"


@pytest.mark.parametrize("reason", ["foreground_busy", "task_busy"])
def test_run_now_busy_decision_preserves_the_existing_deadline(reason):
    service = _service_module()
    deadline = "2026-08-02T12:30:00Z"
    store = _enabled_store(
        chatgpt_auto_relogin_status_state="idle",
        chatgpt_auto_relogin_status_reason="scheduled",
        chatgpt_auto_relogin_status_next_run_at=deadline,
    )

    result = service.trigger_chatgpt_auto_relogin_now(
        store=store,
        list_eligible=lambda: [1, 2],
        try_enqueue=lambda *_: _busy(reason),
    )

    assert result["accepted"] is False
    assert result["reason"] == reason
    assert result["status"]["state"] == "idle"
    assert result["status"]["next_run_at"] == deadline


def test_run_now_enqueue_exception_is_redacted_and_preserves_the_deadline():
    service = _service_module()
    deadline = "2026-08-02T12:30:00Z"
    store = _enabled_store(
        chatgpt_auto_relogin_status_state="idle",
        chatgpt_auto_relogin_status_reason="scheduled",
        chatgpt_auto_relogin_status_next_run_at=deadline,
    )

    def fail_enqueue(*_):
        raise RuntimeError("secret queue detail")

    result = service.trigger_chatgpt_auto_relogin_now(
        store=store,
        list_eligible=lambda: [1],
        try_enqueue=fail_enqueue,
    )

    assert result["accepted"] is False
    assert result["reason"] == "enqueue_failed"
    assert result["status"]["next_run_at"] == deadline
    assert "secret" not in str(result)


def test_concurrent_run_now_calls_enqueue_only_one_task():
    service = _service_module()
    store = _enabled_store(
        chatgpt_auto_relogin_status_state="idle",
        chatgpt_auto_relogin_status_next_run_at="2026-08-02T12:30:00Z",
    )
    enqueue_started = threading.Event()
    release_enqueue = threading.Event()
    results = []
    enqueues = []

    def enqueue(account_ids, concurrency):
        enqueues.append((list(account_ids), concurrency))
        enqueue_started.set()
        release_enqueue.wait(timeout=2)
        return _accepted("task-only")

    def run_now():
        results.append(
            service.trigger_chatgpt_auto_relogin_now(
                store=store,
                list_eligible=lambda: [1],
                try_enqueue=enqueue,
            )
        )

    first = threading.Thread(target=run_now)
    second = threading.Thread(target=run_now)
    first.start()
    assert enqueue_started.wait(timeout=2)
    second.start()
    release_enqueue.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert enqueues == [([1], 10)]
    assert sorted(result["accepted"] for result in results) == [False, True]
    assert {result["reason"] for result in results} == {"enqueued", "task_busy"}


def test_run_now_completion_restarts_the_interval_from_completed_at():
    service = _service_module()
    t0 = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
    store = _enabled_store(chatgpt_auto_relogin_interval_minutes="30")
    started = service.trigger_chatgpt_auto_relogin_now(
        store=store,
        now=t0,
        list_eligible=lambda: [1],
        try_enqueue=lambda *_: _accepted("task-now"),
    )
    assert started["accepted"] is True

    status = service.tick_chatgpt_auto_relogin(
        store=store,
        now=t0 + timedelta(minutes=6),
        list_eligible=lambda: [1],
        try_enqueue=lambda *_: pytest.fail("completed task enqueued early"),
        observe=lambda _: {
            "status": "done",
            "completed_at": t0 + timedelta(minutes=5),
            "updated_at": t0 + timedelta(minutes=6),
            "live": False,
            "orphaned": False,
        },
    )

    assert status["state"] == "idle"
    assert status["active_task_id"] is None
    assert status["next_run_at"] == "2026-08-02T12:35:00Z"


def test_tick_waits_a_full_interval_then_enqueues_all_accounts_at_concurrency_ten():
    service = _service_module()
    store = _enabled_store()
    t0 = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
    enqueues: list[tuple[list[int], int]] = []

    def enqueue(account_ids, concurrency):
        enqueues.append((list(account_ids), concurrency))
        return _accepted()

    at_t0 = service.tick_chatgpt_auto_relogin(
        store=store,
        now=t0,
        list_eligible=lambda: [3, 1, 2, 1],
        try_enqueue=enqueue,
        observe=lambda task_id: None,
    )
    before_due = service.tick_chatgpt_auto_relogin(
        store=store,
        now=t0 + timedelta(minutes=29, seconds=59),
        list_eligible=lambda: [3, 1, 2, 1],
        try_enqueue=enqueue,
        observe=lambda task_id: None,
    )
    at_due = service.tick_chatgpt_auto_relogin(
        store=store,
        now=t0 + timedelta(minutes=30),
        list_eligible=lambda: [3, 1, 2, 1],
        try_enqueue=enqueue,
        observe=lambda task_id: None,
    )

    assert at_t0["state"] == "idle"
    assert at_t0["next_run_at"] == "2026-08-02T12:30:00Z"
    assert before_due["next_run_at"] == at_t0["next_run_at"]
    assert enqueues == [([1, 2, 3], 10)]
    assert at_due["state"] == "running"
    assert at_due["reason"] == "task_running"
    assert at_due["active_task_id"] == "task-scheduled"
    assert at_due["last_task_id"] == "task-scheduled"
    assert at_due["last_started_at"] == "2026-08-02T12:30:00Z"
    assert at_due["next_run_at"] is None


def test_disabled_tick_stops_active_task_before_clearing_visible_state(monkeypatch):
    service = _service_module()
    store = FakeConfigStore(
        {
            "chatgpt_auto_relogin_enabled": "0",
            "chatgpt_auto_relogin_status_state": "running",
            "chatgpt_auto_relogin_status_reason": "task_running",
            "chatgpt_auto_relogin_status_active_task_id": "task-active",
            "chatgpt_auto_relogin_status_last_task_id": "task-old",
            "chatgpt_auto_relogin_status_last_started_at": "2026-08-01T10:00:00Z",
            "chatgpt_auto_relogin_status_next_run_at": "2026-08-02T12:30:00Z",
        }
    )
    stop_requests = []
    observations = iter(
        [
            {
                "status": "running",
                "live": True,
                "orphaned": False,
            },
            {
                "status": "stopped",
                "live": False,
                "orphaned": False,
            },
        ]
    )
    monkeypatch.setattr(
        "api.tasks.observe_chatgpt_task",
        lambda task_id: next(observations),
    )
    monkeypatch.setattr(
        "api.tasks.stop_task",
        lambda task_id: stop_requests.append(task_id) or {"ok": True},
    )

    stopping = service.tick_chatgpt_auto_relogin(
        store=store,
        now=datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc),
        list_eligible=lambda: pytest.fail("disabled tick queried accounts"),
        try_enqueue=lambda *_: pytest.fail("disabled tick enqueued"),
    )
    stopped = service.tick_chatgpt_auto_relogin(
        store=store,
        now=datetime(2026, 8, 2, 12, 0, 1, tzinfo=timezone.utc),
        list_eligible=lambda: pytest.fail("disabled tick queried accounts"),
        try_enqueue=lambda *_: pytest.fail("disabled tick enqueued"),
    )

    assert stopping["enabled"] is False
    assert stopping["state"] == "stopping"
    assert stopping["reason"] == "disabled_stopping"
    assert stopping["active_task_id"] == "task-active"
    assert stopping["next_run_at"] is None
    assert stop_requests == ["task-active"]
    assert stopped["state"] == "disabled"
    assert stopped["reason"] == "disabled_by_config"
    assert stopped["active_task_id"] is None
    assert stopped["last_task_id"] == "task-old"
    assert stopped["last_started_at"] == "2026-08-01T10:00:00Z"


def test_enabling_after_disabled_waits_a_complete_interval():
    service = _service_module()
    store = FakeConfigStore({"chatgpt_auto_relogin_enabled": "0"})
    t0 = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
    enqueues = []

    service.tick_chatgpt_auto_relogin(store=store, now=t0)
    store.values["chatgpt_auto_relogin_enabled"] = "1"
    enabled = service.tick_chatgpt_auto_relogin(
        store=store,
        now=t0,
        list_eligible=lambda: [7],
        try_enqueue=lambda *args: enqueues.append(args) or _accepted(),
        observe=lambda _: None,
    )
    service.tick_chatgpt_auto_relogin(
        store=store,
        now=t0 + timedelta(minutes=1, seconds=59),
        list_eligible=lambda: [7],
        try_enqueue=lambda *args: enqueues.append(args) or _accepted(),
        observe=lambda _: None,
    )

    assert enabled["next_run_at"] == "2026-08-02T12:02:00Z"
    assert enqueues == []


def test_tick_pauses_with_no_accounts_and_resumes_from_discovery_time():
    service = _service_module()
    store = _enabled_store()
    t0 = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)

    paused = service.tick_chatgpt_auto_relogin(
        store=store,
        now=t0,
        list_eligible=lambda: [],
        try_enqueue=lambda *_: pytest.fail("empty task enqueued"),
        observe=lambda _: None,
    )
    resumed = service.tick_chatgpt_auto_relogin(
        store=store,
        now=t0 + timedelta(minutes=5),
        list_eligible=lambda: [8],
        try_enqueue=lambda *_: pytest.fail("resume should only schedule"),
        observe=lambda _: None,
    )

    assert paused["state"] == "paused_no_accounts"
    assert paused["reason"] == "no_eligible_accounts"
    assert paused["active_task_id"] is None
    assert paused["next_run_at"] is None
    assert resumed["state"] == "idle"
    assert resumed["next_run_at"] == "2026-08-02T12:35:00Z"


def test_due_busy_ticks_preserve_overdue_deadline_then_enqueue_only_once():
    service = _service_module()
    deadline = "2026-08-02T12:30:00Z"
    store = _enabled_store(
        chatgpt_auto_relogin_status_state="idle",
        chatgpt_auto_relogin_status_reason="scheduled",
        chatgpt_auto_relogin_status_eligible_accounts="2",
        chatgpt_auto_relogin_status_next_run_at=deadline,
    )
    decisions = iter([_busy("foreground_busy"), _busy("task_busy"), _accepted()])
    enqueues = []

    def enqueue(account_ids, concurrency):
        enqueues.append((list(account_ids), concurrency))
        return next(decisions)

    first_busy = service.tick_chatgpt_auto_relogin(
        store=store,
        now=datetime(2026, 8, 2, 12, 31, tzinfo=timezone.utc),
        list_eligible=lambda: [1, 2],
        try_enqueue=enqueue,
        observe=lambda _: None,
    )
    second_busy = service.tick_chatgpt_auto_relogin(
        store=store,
        now=datetime(2026, 8, 2, 12, 32, tzinfo=timezone.utc),
        list_eligible=lambda: [1, 2],
        try_enqueue=enqueue,
        observe=lambda _: None,
    )
    started = service.tick_chatgpt_auto_relogin(
        store=store,
        now=datetime(2026, 8, 2, 12, 33, tzinfo=timezone.utc),
        list_eligible=lambda: [1, 2],
        try_enqueue=enqueue,
        observe=lambda _: None,
    )

    assert first_busy["reason"] == "foreground_busy"
    assert second_busy["reason"] == "task_busy"
    assert first_busy["next_run_at"] == deadline
    assert second_busy["next_run_at"] == deadline
    assert len(enqueues) == 3
    assert started["active_task_id"] == "task-scheduled"
    assert started["next_run_at"] is None


def test_live_active_task_never_overlaps_even_when_no_accounts_remain():
    service = _service_module()
    store = _enabled_store(
        chatgpt_auto_relogin_status_state="running",
        chatgpt_auto_relogin_status_reason="task_running",
        chatgpt_auto_relogin_status_active_task_id="task-live",
    )
    updated = datetime(2026, 8, 2, 12, 10, tzinfo=timezone.utc)

    status = service.tick_chatgpt_auto_relogin(
        store=store,
        now=datetime(2026, 8, 2, 13, 0, tzinfo=timezone.utc),
        list_eligible=lambda: [],
        try_enqueue=lambda *_: pytest.fail("overlapping task enqueued"),
        observe=lambda _: {
            "status": "running",
            "updated_at": updated,
            "live": True,
            "orphaned": False,
        },
    )

    assert status["state"] == "running"
    assert status["reason"] == "task_running"
    assert status["eligible_accounts"] == 0
    assert status["active_task_id"] == "task-live"
    assert status["next_run_at"] is None


def test_terminal_active_schedules_full_interval_from_completion():
    service = _service_module()
    completed_at = datetime(2026, 8, 2, 11, 40, tzinfo=timezone.utc)
    mutable_updated_at = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
    store = _enabled_store(
        chatgpt_auto_relogin_status_state="running",
        chatgpt_auto_relogin_status_active_task_id="task-complete",
        chatgpt_auto_relogin_status_last_started_at="2026-08-02T11:30:00Z",
        chatgpt_auto_relogin_status_next_run_at="",
    )
    enqueues = []

    reconciled = service.tick_chatgpt_auto_relogin(
        store=store,
        now=datetime(2026, 8, 2, 12, 5, tzinfo=timezone.utc),
        list_eligible=lambda: [4, 5],
        try_enqueue=lambda *args: enqueues.append(args) or _accepted("task-next"),
        observe=lambda _: {
            "status": "done",
            "completed_at": completed_at,
            "updated_at": mutable_updated_at,
            "live": False,
            "orphaned": False,
        },
    )
    before_due = service.tick_chatgpt_auto_relogin(
        store=store,
        now=datetime(2026, 8, 2, 12, 9, 59, tzinfo=timezone.utc),
        list_eligible=lambda: [4, 5],
        try_enqueue=lambda *args: enqueues.append(args) or _accepted("task-next"),
        observe=lambda _: None,
    )
    at_due = service.tick_chatgpt_auto_relogin(
        store=store,
        now=datetime(2026, 8, 2, 12, 10, tzinfo=timezone.utc),
        list_eligible=lambda: [4, 5],
        try_enqueue=lambda *args: enqueues.append(args) or _accepted("task-next"),
        observe=lambda _: None,
    )

    assert reconciled["active_task_id"] is None
    assert reconciled["next_run_at"] == "2026-08-02T12:10:00Z"
    assert before_due["next_run_at"] == reconciled["next_run_at"]
    assert enqueues == [([4, 5], 10)]
    assert at_due["active_task_id"] == "task-next"


def test_overdue_terminal_task_enqueues_without_waiting_for_another_tick():
    service = _service_module()
    store = _enabled_store(
        chatgpt_auto_relogin_status_state="running",
        chatgpt_auto_relogin_status_active_task_id="task-overdue",
        chatgpt_auto_relogin_status_next_run_at="",
    )
    enqueues = []

    status = service.tick_chatgpt_auto_relogin(
        store=store,
        now=datetime(2026, 8, 2, 12, 5, tzinfo=timezone.utc),
        list_eligible=lambda: [4, 5],
        try_enqueue=lambda *args: enqueues.append(args) or _accepted("task-next"),
        observe=lambda _: {
            "status": "done",
            "completed_at": datetime(2026, 8, 2, 11, 0, tzinfo=timezone.utc),
            "updated_at": datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc),
            "live": False,
            "orphaned": False,
        },
    )

    assert enqueues == [([4, 5], 10)]
    assert status["state"] == "running"
    assert status["active_task_id"] == "task-next"
    assert status["next_run_at"] is None


@pytest.mark.parametrize(
    ("previous_interval", "new_interval", "old_deadline", "expected_deadline"),
    [
        (1440, 20, "2026-08-03T12:00:00Z", "2026-08-02T12:20:00Z"),
        (20, 1440, "2026-08-02T12:20:00Z", "2026-08-03T12:00:00Z"),
    ],
)
def test_interval_change_replans_idle_deadline_from_detection_time(
    previous_interval,
    new_interval,
    old_deadline,
    expected_deadline,
):
    service = _service_module()
    store = _enabled_store(
        chatgpt_auto_relogin_interval_minutes=str(new_interval),
        chatgpt_auto_relogin_status_state="idle",
        chatgpt_auto_relogin_status_reason="scheduled",
        chatgpt_auto_relogin_status_eligible_accounts="1",
        chatgpt_auto_relogin_status_next_run_at=old_deadline,
        chatgpt_auto_relogin_status_scheduled_interval_minutes=str(
            previous_interval
        ),
    )

    status = service.tick_chatgpt_auto_relogin(
        store=store,
        now=datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc),
        list_eligible=lambda: [1],
        try_enqueue=lambda *_: pytest.fail("interval change enqueued immediately"),
        observe=lambda _: None,
    )

    assert status["state"] == "idle"
    assert status["reason"] == "scheduled"
    assert status["next_run_at"] == expected_deadline


def test_future_deadline_survives_a_new_service_tick_without_drift():
    service = _service_module()
    store = _enabled_store()
    t0 = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)

    first = service.tick_chatgpt_auto_relogin(
        store=store,
        now=t0,
        list_eligible=lambda: [1],
        try_enqueue=lambda *_: pytest.fail("not due"),
        observe=lambda _: None,
    )
    second = service.tick_chatgpt_auto_relogin(
        store=store,
        now=t0 + timedelta(minutes=5),
        list_eligible=lambda: [1],
        try_enqueue=lambda *_: pytest.fail("not due"),
        observe=lambda _: None,
    )

    assert first["next_run_at"] == "2026-08-02T12:30:00Z"
    assert second["next_run_at"] == first["next_run_at"]


@pytest.mark.parametrize("observation", [None, "orphan"])
def test_missing_or_orphaned_active_restarts_interval_from_detection_time(observation):
    service = _service_module()
    t0 = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
    store = _enabled_store(
        chatgpt_auto_relogin_status_state="running",
        chatgpt_auto_relogin_status_active_task_id="task-lost",
    )

    observed = (
        None
        if observation is None
        else {
            "status": "stopped",
            "updated_at": t0,
            "live": False,
            "orphaned": True,
        }
    )
    status = service.tick_chatgpt_auto_relogin(
        store=store,
        now=t0 + timedelta(minutes=7),
        list_eligible=lambda: [1],
        try_enqueue=lambda *_: pytest.fail("recovery tick enqueued"),
        observe=lambda _: observed,
    )

    assert status["state"] == "idle"
    assert status["active_task_id"] is None
    assert status["next_run_at"] == "2026-08-02T12:37:00Z"


def test_enqueue_exception_keeps_overdue_deadline_and_no_active_task():
    service = _service_module()
    deadline = "2026-08-02T12:30:00Z"
    store = _enabled_store(
        chatgpt_auto_relogin_status_state="idle",
        chatgpt_auto_relogin_status_next_run_at=deadline,
    )

    def fail_enqueue(*_):
        raise RuntimeError("queue unavailable")

    status = service.tick_chatgpt_auto_relogin(
        store=store,
        now=datetime(2026, 8, 2, 12, 31, tzinfo=timezone.utc),
        list_eligible=lambda: [1],
        try_enqueue=fail_enqueue,
        observe=lambda _: None,
    )

    assert status["state"] == "idle"
    assert status["reason"] == "enqueue_failed"
    assert status["active_task_id"] is None
    assert status["next_run_at"] == deadline


def test_concurrent_due_ticks_enqueue_one_task_and_use_atomic_store_transitions():
    service = _service_module()
    store = _enabled_store(
        chatgpt_auto_relogin_status_state="idle",
        chatgpt_auto_relogin_status_next_run_at="2026-08-02T12:30:00Z",
    )
    now = datetime(2026, 8, 2, 12, 30, tzinfo=timezone.utc)
    enqueue_started = threading.Event()
    release_enqueue = threading.Event()
    enqueues = []
    errors: list[BaseException] = []

    def enqueue(account_ids, concurrency):
        enqueues.append((list(account_ids), concurrency))
        enqueue_started.set()
        release_enqueue.wait(timeout=2)
        return _accepted("task-only")

    def observe(task_id):
        assert task_id == "task-only"
        return {
            "status": "pending",
            "updated_at": now,
            "live": True,
            "orphaned": False,
        }

    def run_tick():
        try:
            service.tick_chatgpt_auto_relogin(
                store=store,
                now=now,
                list_eligible=lambda: [2, 1],
                try_enqueue=enqueue,
                observe=observe,
            )
        except BaseException as exc:
            errors.append(exc)

    first = threading.Thread(target=run_tick)
    second = threading.Thread(target=run_tick)
    first.start()
    assert enqueue_started.wait(timeout=1)
    second.start()
    release_enqueue.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert errors == []
    assert enqueues == [([1, 2], 10)]
    assert store.values["chatgpt_auto_relogin_status_active_task_id"] == "task-only"
    assert len(store.writes) == 2


def test_tick_reads_one_snapshot_and_writes_one_multi_key_transition():
    service = _service_module()
    snapshot = {
        "chatgpt_auto_relogin_enabled": "1",
        "chatgpt_auto_relogin_interval_minutes": "30",
        "chatgpt_auto_relogin_concurrency": "10",
    }
    store = MutatingConfigStore(
        [snapshot, {**snapshot, "chatgpt_auto_relogin_enabled": "0"}]
    )

    status = service.tick_chatgpt_auto_relogin(
        store=store,
        now=datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc),
        list_eligible=lambda: [9],
        try_enqueue=lambda *_: pytest.fail("not due"),
        observe=lambda _: None,
    )

    assert status["enabled"] is True
    assert store.get_all_calls == 1
    assert len(store.writes) == 1
