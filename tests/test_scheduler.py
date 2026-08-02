from __future__ import annotations

import threading
from datetime import datetime, timezone
from unittest import mock

from core import scheduler as scheduler_module


def test_run_once_calls_auto_tick_exactly_once_and_isolates_its_failure(monkeypatch):
    scheduler = scheduler_module.Scheduler()
    scheduler._last_trial_check_at = 0.0
    scheduler._last_cpa_maintenance_at = 0.0
    scheduler._trial_check_interval_seconds = 10
    wall_now = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
    auto_tick = mock.Mock(side_effect=RuntimeError("auto tick failed"))
    trial = mock.Mock()
    cpa = mock.Mock()

    monkeypatch.setattr(
        scheduler_module,
        "tick_chatgpt_auto_relogin",
        auto_tick,
        raising=False,
    )
    monkeypatch.setattr(scheduler, "check_trial_expiry", trial)
    monkeypatch.setattr(scheduler, "check_cpa_credentials", cpa)
    monkeypatch.setattr(
        scheduler,
        "_get_cpa_maintenance_interval_seconds",
        lambda: 10,
    )

    scheduler.run_once(wall_now, 10.0)

    auto_tick.assert_called_once_with(now=wall_now)
    trial.assert_called_once_with()
    cpa.assert_called_once_with()
    assert scheduler._last_trial_check_at == 10.0
    assert scheduler._last_cpa_maintenance_at == 10.0


def test_run_once_keeps_trial_and_cpa_failures_independent(monkeypatch):
    scheduler = scheduler_module.Scheduler()
    scheduler._last_trial_check_at = 0.0
    scheduler._last_cpa_maintenance_at = 0.0
    scheduler._trial_check_interval_seconds = 10
    wall_now = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
    auto_tick = mock.Mock()
    trial = mock.Mock(side_effect=RuntimeError("trial failed"))
    cpa = mock.Mock()

    monkeypatch.setattr(
        scheduler_module,
        "tick_chatgpt_auto_relogin",
        auto_tick,
        raising=False,
    )
    monkeypatch.setattr(scheduler, "check_trial_expiry", trial)
    monkeypatch.setattr(scheduler, "check_cpa_credentials", cpa)
    monkeypatch.setattr(
        scheduler,
        "_get_cpa_maintenance_interval_seconds",
        lambda: 10,
    )

    scheduler.run_once(wall_now=wall_now, monotonic_now=10.0)

    auto_tick.assert_called_once_with(now=wall_now)
    trial.assert_called_once_with()
    cpa.assert_called_once_with()
    assert scheduler._last_trial_check_at == 0.0
    assert scheduler._last_cpa_maintenance_at == 10.0


def test_start_is_daemon_nonblocking_and_immediately_ticks_without_trial_or_cpa(
    monkeypatch,
):
    scheduler = scheduler_module.Scheduler()
    ticked = threading.Event()
    auto_tick = mock.Mock(side_effect=lambda **_: ticked.set())
    trial = mock.Mock()
    cpa = mock.Mock()

    monkeypatch.setattr(
        scheduler_module,
        "tick_chatgpt_auto_relogin",
        auto_tick,
        raising=False,
    )
    monkeypatch.setattr(scheduler, "check_trial_expiry", trial)
    monkeypatch.setattr(scheduler, "check_cpa_credentials", cpa)
    monkeypatch.setattr(
        scheduler,
        "_get_cpa_maintenance_interval_seconds",
        lambda: 3600,
    )

    scheduler.start()
    try:
        assert ticked.wait(timeout=1)
        assert scheduler._thread is not None
        assert scheduler._thread.daemon is True
        auto_tick.assert_called_once()
        called_now = auto_tick.call_args.kwargs["now"]
        assert called_now.tzinfo is not None
        assert called_now.utcoffset() == timezone.utc.utcoffset(called_now)
        trial.assert_not_called()
        cpa.assert_not_called()
    finally:
        scheduler.stop()
        scheduler._thread.join(timeout=1)


def test_stop_wakes_the_scheduler_event_wait_immediately(monkeypatch):
    scheduler = scheduler_module.Scheduler()
    ticked = threading.Event()

    monkeypatch.setattr(
        scheduler_module,
        "tick_chatgpt_auto_relogin",
        lambda **_: ticked.set(),
        raising=False,
    )
    monkeypatch.setattr(
        scheduler,
        "_get_cpa_maintenance_interval_seconds",
        lambda: 3600,
    )

    scheduler.start()
    assert ticked.wait(timeout=1)
    scheduler.stop()
    scheduler._thread.join(timeout=1)

    assert not scheduler._thread.is_alive()


def test_rapid_restart_does_not_revive_the_previous_scheduler_thread(monkeypatch):
    scheduler = scheduler_module.Scheduler()
    first_tick_started = threading.Event()
    release_first_tick = threading.Event()
    second_tick_started = threading.Event()
    call_lock = threading.Lock()
    call_count = 0

    def controlled_run_once(*args, **kwargs):
        nonlocal call_count
        del args, kwargs
        with call_lock:
            call_count += 1
            current_call = call_count
        if current_call == 1:
            first_tick_started.set()
            assert release_first_tick.wait(timeout=2)
        else:
            second_tick_started.set()

    monkeypatch.setattr(scheduler, "run_once", controlled_run_once)

    scheduler.start()
    first_thread = scheduler._thread
    assert first_thread is not None
    assert first_tick_started.wait(timeout=1)

    scheduler.stop()
    scheduler.start()
    second_thread = scheduler._thread
    assert second_thread is not None
    assert second_thread is not first_thread
    assert second_tick_started.wait(timeout=1)
    release_first_tick.set()

    try:
        first_thread.join(timeout=1)
        assert not first_thread.is_alive()
    finally:
        scheduler.stop()
        first_thread.join(timeout=1)
        second_thread.join(timeout=1)
