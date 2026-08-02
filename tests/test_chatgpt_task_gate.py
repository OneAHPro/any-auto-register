import queue
import threading

import pytest

from core.chatgpt_task_gate import ChatGPTTaskGate, chatgpt_task_gate


def _join(thread: threading.Thread) -> None:
    thread.join(timeout=1)
    assert not thread.is_alive(), "worker thread did not finish"


def test_global_gate_is_available() -> None:
    assert isinstance(chatgpt_task_gate, ChatGPTTaskGate)


def test_automation_enters_only_when_gate_is_idle() -> None:
    gate = ChatGPTTaskGate()

    automation_lease = gate.try_enter_automation()
    assert automation_lease is not None
    assert gate.try_enter_automation() is None
    gate.leave_automation(automation_lease)

    foreground_lease = gate.enter_foreground()
    assert foreground_lease is not None
    assert gate.try_enter_automation() is None
    gate.leave_foreground(foreground_lease)

    automation_lease = gate.try_enter_automation()
    assert automation_lease is not None
    gate.leave_automation(automation_lease)


def test_waiting_foreground_can_cancel_without_waiting_for_automation_leave() -> None:
    gate = ChatGPTTaskGate()
    waiting = threading.Event()
    cancelled = threading.Event()
    finished = threading.Event()
    entered: list[object | None] = []
    thread_errors: queue.Queue[BaseException] = queue.Queue()

    automation_lease = gate.try_enter_automation()
    assert automation_lease is not None

    def run_foreground() -> None:
        try:
            entered.append(
                gate.enter_foreground(
                    on_wait=waiting.set,
                    cancelled=cancelled.is_set,
                )
            )
        except BaseException as exc:
            thread_errors.put(exc)
        finally:
            finished.set()

    worker = threading.Thread(target=run_foreground)
    worker.start()
    assert waiting.wait(timeout=1)
    assert gate.snapshot()["foreground_waiters"] == 1

    cancelled.set()
    assert finished.wait(timeout=1)
    _join(worker)

    assert thread_errors.empty()
    assert entered == [None]
    assert gate.snapshot()["foreground_waiters"] == 0
    assert gate.snapshot()["foreground_active"] == 0
    assert gate.snapshot()["automation_active"] is True
    gate.leave_automation(automation_lease)


def test_first_foreground_waiter_requests_automation_stop_once_and_waits() -> None:
    gate = ChatGPTTaskGate()
    stop_requested = threading.Event()
    first_waiting = threading.Event()
    second_waiting = threading.Event()
    release_foreground = threading.Event()
    first_entered = threading.Event()
    second_entered = threading.Event()
    stop_calls = 0
    thread_errors: queue.Queue[BaseException] = queue.Queue()

    def request_stop() -> None:
        nonlocal stop_calls
        stop_calls += 1
        stop_requested.set()

    automation_lease = gate.try_enter_automation(request_stop)
    assert automation_lease is not None

    def run_foreground(
        waiting: threading.Event,
        entered: threading.Event,
    ) -> None:
        try:
            lease = gate.enter_foreground(on_wait=waiting.set)
            assert lease is not None
            entered.set()
            assert release_foreground.wait(timeout=1)
            gate.leave_foreground(lease)
        except BaseException as exc:
            thread_errors.put(exc)

    first = threading.Thread(
        target=run_foreground,
        args=(first_waiting, first_entered),
    )
    second = threading.Thread(
        target=run_foreground,
        args=(second_waiting, second_entered),
    )
    first.start()
    assert first_waiting.wait(timeout=1)
    assert stop_requested.wait(timeout=1)
    second.start()
    assert second_waiting.wait(timeout=1)

    snapshot = gate.snapshot()
    assert snapshot["automation_active"] is True
    assert snapshot["foreground_active"] == 0
    assert snapshot["foreground_waiters"] == 2
    assert stop_calls == 1
    assert not first_entered.is_set()
    assert not second_entered.is_set()
    assert gate.try_enter_automation() is None

    gate.leave_automation(automation_lease)
    assert first_entered.wait(timeout=1)
    assert second_entered.wait(timeout=1)
    assert gate.snapshot()["foreground_active"] == 2

    release_foreground.set()
    _join(first)
    _join(second)
    assert thread_errors.empty()
    assert gate.snapshot()["foreground_active"] == 0


def test_multiple_foreground_tasks_can_be_active_together() -> None:
    gate = ChatGPTTaskGate()
    first_entered = threading.Event()
    second_entered = threading.Event()
    release = threading.Event()
    thread_errors: queue.Queue[BaseException] = queue.Queue()

    def run_foreground(entered: threading.Event) -> None:
        try:
            lease = gate.enter_foreground()
            assert lease is not None
            entered.set()
            assert release.wait(timeout=1)
            gate.leave_foreground(lease)
        except BaseException as exc:
            thread_errors.put(exc)

    first = threading.Thread(target=run_foreground, args=(first_entered,))
    second = threading.Thread(target=run_foreground, args=(second_entered,))
    first.start()
    second.start()

    assert first_entered.wait(timeout=1)
    assert second_entered.wait(timeout=1)
    assert gate.snapshot()["foreground_active"] == 2
    assert gate.try_enter_automation() is None

    release.set()
    _join(first)
    _join(second)
    assert thread_errors.empty()


def test_callback_and_wait_log_exceptions_do_not_break_foreground_entry() -> None:
    gate = ChatGPTTaskGate()
    wait_logged = threading.Event()
    stop_requested = threading.Event()
    foreground_entered = threading.Event()
    thread_errors: queue.Queue[BaseException] = queue.Queue()

    def broken_wait_log() -> None:
        wait_logged.set()
        raise KeyboardInterrupt("log unavailable")

    def broken_stop_callback() -> None:
        stop_requested.set()
        raise SystemExit("stop unavailable")

    automation_lease = gate.try_enter_automation(broken_stop_callback)
    assert automation_lease is not None

    def run_foreground() -> None:
        try:
            lease = gate.enter_foreground(on_wait=broken_wait_log)
            assert lease is not None
            foreground_entered.set()
            gate.leave_foreground(lease)
        except BaseException as exc:
            thread_errors.put(exc)

    worker = threading.Thread(target=run_foreground)
    worker.start()
    assert wait_logged.wait(timeout=1)
    assert stop_requested.wait(timeout=1)
    assert not foreground_entered.is_set()

    gate.leave_automation(automation_lease)
    assert foreground_entered.wait(timeout=1)
    _join(worker)
    assert thread_errors.empty()
    assert gate.snapshot()["foreground_active"] == 0


def test_stale_or_duplicate_lease_cannot_release_a_later_owner() -> None:
    gate = ChatGPTTaskGate()

    old_automation = gate.try_enter_automation()
    assert old_automation is not None
    gate.leave_automation(old_automation)
    current_automation = gate.try_enter_automation()
    assert current_automation is not None

    with pytest.raises(RuntimeError):
        gate.leave_automation(old_automation)
    assert gate.snapshot()["automation_active"] is True
    assert gate.try_enter_automation() is None
    gate.leave_automation(current_automation)

    old_foreground = gate.enter_foreground()
    assert old_foreground is not None
    gate.leave_foreground(old_foreground)
    current_foreground = gate.enter_foreground()
    assert current_foreground is not None

    with pytest.raises(RuntimeError):
        gate.leave_foreground(old_foreground)
    assert gate.snapshot()["foreground_active"] == 1
    assert gate.try_enter_automation() is None
    gate.leave_foreground(current_foreground)

    with pytest.raises(RuntimeError):
        gate.leave_foreground(current_foreground)


def test_stop_wins_when_it_races_with_automation_leave_before_promotion() -> None:
    gate = ChatGPTTaskGate()
    waiting = threading.Event()
    cancel = threading.Event()
    first_check_captured = threading.Event()
    release_first_check = threading.Event()
    first_check_returning = threading.Event()
    result: list[object | None] = []
    thread_errors: queue.Queue[BaseException] = queue.Queue()
    checks = 0

    automation_lease = gate.try_enter_automation(lambda: None)
    assert automation_lease is not None

    def cancelled() -> bool:
        nonlocal checks
        checks += 1
        if checks == 1:
            captured = cancel.is_set()
            first_check_captured.set()
            assert release_first_check.wait(timeout=1)
            first_check_returning.set()
            return captured
        return cancel.is_set()

    def run_foreground() -> None:
        try:
            result.append(
                gate.enter_foreground(
                    on_wait=waiting.set,
                    cancelled=cancelled,
                )
            )
        except BaseException as exc:
            thread_errors.put(exc)

    worker = threading.Thread(target=run_foreground)
    worker.start()
    assert waiting.wait(timeout=1)
    assert first_check_captured.wait(timeout=1)

    with gate._condition:
        release_first_check.set()
        assert first_check_returning.wait(timeout=1)
        cancel.set()
        gate.leave_automation(automation_lease)

    _join(worker)
    assert thread_errors.empty()
    assert result == [None]
    assert gate.snapshot()["foreground_active"] == 0
    assert gate.snapshot()["foreground_waiters"] == 0


def test_slow_initial_cancel_check_does_not_block_snapshot_or_existing_leave() -> None:
    gate = ChatGPTTaskGate()
    existing_lease = gate.enter_foreground()
    assert existing_lease is not None
    cancel_check_started = threading.Event()
    release_cancel_check = threading.Event()
    leave_finished = threading.Event()
    snapshot_finished = threading.Event()
    worker_finished = threading.Event()
    cancel = threading.Event()
    result: list[object | None] = []
    thread_errors: queue.Queue[BaseException] = queue.Queue()

    def cancelled() -> bool:
        cancel_check_started.set()
        assert release_cancel_check.wait(timeout=1)
        return cancel.is_set()

    def enter_foreground() -> None:
        try:
            result.append(gate.enter_foreground(cancelled=cancelled))
        except BaseException as exc:
            thread_errors.put(exc)
        finally:
            worker_finished.set()

    def leave_existing() -> None:
        try:
            gate.leave_foreground(existing_lease)
        except BaseException as exc:
            thread_errors.put(exc)
        finally:
            leave_finished.set()

    def take_snapshot() -> None:
        try:
            gate.snapshot()
        except BaseException as exc:
            thread_errors.put(exc)
        finally:
            snapshot_finished.set()

    worker = threading.Thread(target=enter_foreground)
    leaver = threading.Thread(target=leave_existing)
    observer = threading.Thread(target=take_snapshot)
    worker.start()
    assert cancel_check_started.wait(timeout=1)
    leaver.start()
    observer.start()
    try:
        assert leave_finished.wait(timeout=0.5)
        assert snapshot_finished.wait(timeout=0.5)
        cancel.set()
    finally:
        release_cancel_check.set()
        _join(worker)
        _join(leaver)
        _join(observer)

    assert worker_finished.is_set()
    assert thread_errors.empty()
    assert result == [None]
    assert gate.snapshot()["foreground_active"] == 0


def test_slow_final_cancel_check_is_lock_free_and_rolls_back_provisional_lease() -> None:
    gate = ChatGPTTaskGate()
    waiting = threading.Event()
    final_check_started = threading.Event()
    release_final_check = threading.Event()
    snapshot_finished = threading.Event()
    cancel = threading.Event()
    result: list[object | None] = []
    thread_errors: queue.Queue[BaseException] = queue.Queue()
    checks = 0

    automation_lease = gate.try_enter_automation(lambda: None)
    assert automation_lease is not None

    def cancelled() -> bool:
        nonlocal checks
        checks += 1
        if checks == 3:
            final_check_started.set()
            assert release_final_check.wait(timeout=1)
            return cancel.is_set()
        return False

    def enter_foreground() -> None:
        try:
            result.append(
                gate.enter_foreground(
                    on_wait=waiting.set,
                    cancelled=cancelled,
                )
            )
        except BaseException as exc:
            thread_errors.put(exc)

    def take_snapshot() -> None:
        try:
            gate.snapshot()
        except BaseException as exc:
            thread_errors.put(exc)
        finally:
            snapshot_finished.set()

    worker = threading.Thread(target=enter_foreground)
    observer = threading.Thread(target=take_snapshot)
    worker.start()
    assert waiting.wait(timeout=1)
    gate.leave_automation(automation_lease)
    assert final_check_started.wait(timeout=1)
    observer.start()
    try:
        assert snapshot_finished.wait(timeout=0.5)
        cancel.set()
    finally:
        release_final_check.set()
        _join(worker)
        _join(observer)

    assert thread_errors.empty()
    assert result == [None]
    assert gate.snapshot()["foreground_active"] == 0
    assert gate.snapshot()["foreground_waiters"] == 0


def test_snapshot_is_read_only() -> None:
    snapshot = ChatGPTTaskGate().snapshot()

    with pytest.raises(TypeError):
        snapshot["foreground_active"] = 99
