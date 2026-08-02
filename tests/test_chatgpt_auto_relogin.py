from __future__ import annotations

import importlib
import threading
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient


PUBLIC_KEYS = {
    "chatgpt_auto_relogin_enabled",
    "chatgpt_auto_relogin_interval_minutes",
    "chatgpt_auto_relogin_concurrency",
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
    assert response["chatgpt_auto_relogin_enabled"] == "0"
    assert response["chatgpt_auto_relogin_interval_minutes"] == "30"
    assert response["chatgpt_auto_relogin_concurrency"] == "10"
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
        ("chatgpt_auto_relogin_interval_minutes", 19),
        ("chatgpt_auto_relogin_interval_minutes", 1441),
        ("chatgpt_auto_relogin_interval_minutes", "not-an-integer"),
        ("chatgpt_auto_relogin_concurrency", 0),
        ("chatgpt_auto_relogin_concurrency", 11),
        ("chatgpt_auto_relogin_concurrency", "not-an-integer"),
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
    assert defaults.interval_minutes == 30
    assert defaults.concurrency == 10
    assert bounded.enabled is True
    assert bounded.interval_minutes == 20
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
        "interval_minutes": 30,
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
        "interval_minutes": 30,
        "concurrency": 10,
    }


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
        now=t0 + timedelta(minutes=29, seconds=59),
        list_eligible=lambda: [7],
        try_enqueue=lambda *args: enqueues.append(args) or _accepted(),
        observe=lambda _: None,
    )

    assert enabled["next_run_at"] == "2026-08-02T12:30:00Z"
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


def test_terminal_active_schedules_from_last_start_without_stacking_runtime():
    service = _service_module()
    started_at = datetime(2026, 8, 2, 11, 30, tzinfo=timezone.utc)
    completed_at = datetime(2026, 8, 2, 11, 40, tzinfo=timezone.utc)
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
            "updated_at": completed_at,
            "live": False,
            "orphaned": False,
        },
    )
    next_tick = service.tick_chatgpt_auto_relogin(
        store=store,
        now=datetime(2026, 8, 2, 12, 5, 1, tzinfo=timezone.utc),
        list_eligible=lambda: [4, 5],
        try_enqueue=lambda *args: enqueues.append(args) or _accepted("task-next"),
        observe=lambda _: None,
    )

    assert reconciled["active_task_id"] is None
    assert reconciled["next_run_at"] == "2026-08-02T12:00:00Z"
    assert enqueues == [([4, 5], 10)]
    assert next_tick["active_task_id"] == "task-next"


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
