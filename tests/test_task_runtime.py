import threading
import unittest
from unittest import mock

import api.tasks as tasks_module
from core import task_runtime
from core.task_runtime import (
    AttemptOutcome,
    AttemptResult,
    RegisterTaskControl,
    RegisterTaskStore,
    SkipCurrentAttemptRequested,
    StopTaskRequested,
)


class AttemptResultTests(unittest.TestCase):
    def test_removed_factory_uses_distinct_removed_outcome(self):
        result = AttemptResult.removed("账号已删除")

        self.assertEqual(result.outcome, AttemptOutcome.REMOVED)
        self.assertEqual(result.message, "账号已删除")

    def test_auto_upload_integrations_returns_joinable_worker(self):
        account = type("Account", (), {"email": "demo@example.com"})()
        with (
            mock.patch(
                "services.external_sync.sync_account",
                return_value=[{"name": "Codex2API", "ok": True, "msg": "ok"}],
            ),
            mock.patch("api.tasks._log") as log,
        ):
            worker = tasks_module._auto_upload_integrations("task-sync", account)
            self.assertIsInstance(worker, threading.Thread)
            worker.join(timeout=2)

        self.assertFalse(worker.is_alive())
        log.assert_called_once_with("task-sync", "  [Codex2API] [OK] ok")


class RegisterTaskControlTests(unittest.TestCase):
    def test_task_attempt_context_is_scoped_and_restored(self):
        outer_control = RegisterTaskControl()
        inner_control = RegisterTaskControl()
        outer_attempt = outer_control.start_attempt()
        inner_attempt = inner_control.start_attempt()

        self.assertIsNone(task_runtime.current_task_attempt_context())
        with task_runtime.bind_task_attempt_context(
            outer_control,
            outer_attempt,
        ):
            outer = task_runtime.current_task_attempt_context()
            self.assertIs(outer.control, outer_control)
            self.assertEqual(outer.attempt_id, outer_attempt)
            with task_runtime.bind_task_attempt_context(
                inner_control,
                inner_attempt,
            ):
                inner = task_runtime.current_task_attempt_context()
                self.assertIs(inner.control, inner_control)
                self.assertEqual(inner.attempt_id, inner_attempt)
            self.assertIs(
                task_runtime.current_task_attempt_context().control,
                outer_control,
            )
        self.assertIsNone(task_runtime.current_task_attempt_context())

        outer_control.finish_attempt(outer_attempt)
        inner_control.finish_attempt(inner_attempt)

    def test_stop_interrupts_registered_attempt_resource(self):
        control = RegisterTaskControl()
        attempt_id = control.start_attempt()
        interrupted: list[int] = []

        unregister = control.register_attempt_interrupt(
            attempt_id,
            lambda: interrupted.append(attempt_id),
        )

        self.assertTrue(control.request_stop_once())
        self.assertEqual(interrupted, [attempt_id])
        self.assertFalse(control.request_stop_once())
        self.assertEqual(interrupted, [attempt_id])

        unregister()
        control.finish_attempt(attempt_id)

    def test_skip_interrupts_only_live_attempt_resources(self):
        control = RegisterTaskControl()
        live_attempt = control.start_attempt()
        finished_attempt = control.start_attempt()
        interrupted: list[int] = []

        control.register_attempt_interrupt(
            live_attempt,
            lambda: interrupted.append(live_attempt),
        )
        control.register_attempt_interrupt(
            finished_attempt,
            lambda: interrupted.append(finished_attempt),
        )
        control.finish_attempt(finished_attempt)

        control.request_skip_current()

        self.assertEqual(interrupted, [live_attempt])
        with self.assertRaises(SkipCurrentAttemptRequested):
            control.checkpoint(attempt_id=live_attempt)
        control.finish_attempt(live_attempt)

    def test_paused_active_slot_allows_next_attempt_then_reacquires(self):
        control = RegisterTaskControl()
        control.configure_active_slots(1)
        first_attempt = control.start_attempt()
        second_started = threading.Event()
        allow_second_finish = threading.Event()
        second_finished = threading.Event()

        def run_second_attempt() -> None:
            second_attempt = control.start_attempt()
            second_started.set()
            allow_second_finish.wait(timeout=1)
            control.finish_attempt(second_attempt)
            second_finished.set()

        worker = threading.Thread(target=run_second_attempt)
        worker.start()
        self.assertFalse(second_started.wait(timeout=0.05))

        with control.pause_active_slot(first_attempt):
            self.assertTrue(second_started.wait(timeout=1))
            allow_second_finish.set()
            self.assertTrue(second_finished.wait(timeout=1))

        control.finish_attempt(first_attempt)
        worker.join(timeout=1)
        self.assertFalse(worker.is_alive())

    def test_stop_interrupts_attempt_waiting_for_active_slot(self):
        control = RegisterTaskControl()
        control.configure_active_slots(1)
        first_attempt = control.start_attempt()
        interrupted = threading.Event()

        def wait_for_slot() -> None:
            try:
                control.start_attempt()
            except StopTaskRequested:
                interrupted.set()

        worker = threading.Thread(target=wait_for_slot)
        worker.start()
        self.assertFalse(interrupted.wait(timeout=0.05))

        control.request_stop()

        self.assertTrue(interrupted.wait(timeout=1))
        control.finish_attempt(first_attempt)
        worker.join(timeout=1)
        self.assertFalse(worker.is_alive())

    def test_skip_request_is_consumed_only_once(self):
        control = RegisterTaskControl()

        control.request_skip_current()

        with self.assertRaises(SkipCurrentAttemptRequested):
            control.checkpoint()

        control.checkpoint()

    def test_stop_request_is_sticky(self):
        control = RegisterTaskControl()

        control.request_stop()

        with self.assertRaises(StopTaskRequested):
            control.checkpoint()
        with self.assertRaises(StopTaskRequested):
            control.checkpoint()

    def test_only_one_concurrent_stop_request_is_reported_as_new(self):
        control = RegisterTaskControl()
        barrier = threading.Barrier(8)
        results: list[bool] = []
        results_lock = threading.Lock()

        def request_stop() -> None:
            barrier.wait()
            is_new = control.request_stop_once()
            with results_lock:
                results.append(is_new)

        workers = [threading.Thread(target=request_stop) for _ in range(8)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=1)

        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(results.count(True), 1)
        self.assertEqual(results.count(False), 7)

    def test_skip_current_targets_only_active_attempts_in_multithread_mode(self):
        control = RegisterTaskControl()
        attempt_a = control.start_attempt()
        attempt_b = control.start_attempt()

        control.request_skip_current()

        with self.assertRaises(SkipCurrentAttemptRequested):
            control.checkpoint(attempt_id=attempt_a)
        with self.assertRaises(SkipCurrentAttemptRequested):
            control.checkpoint(attempt_id=attempt_b)

        control.finish_attempt(attempt_a)
        control.finish_attempt(attempt_b)

        attempt_c = control.start_attempt()
        control.checkpoint(attempt_id=attempt_c)
        control.finish_attempt(attempt_c)


class RegisterTaskStoreTests(unittest.TestCase):
    def test_cleanup_preserves_terminal_record_until_runner_releases_it(self):
        store = RegisterTaskStore(
            max_finished_tasks=1,
            cleanup_threshold=1,
        )
        protected_id = "task-runtime-finalizing"
        newer_id = "task-runtime-newer"
        store.create(
            protected_id,
            platform="chatgpt",
            total=1,
            source="schedule",
        )
        store.finish(
            protected_id,
            status="done",
            success=1,
            registered=1,
            skipped=0,
            errors=[],
        )
        store.protect_from_cleanup(protected_id)
        store.create(
            newer_id,
            platform="chatgpt",
            total=1,
            source="manual",
        )
        store.finish(
            newer_id,
            status="done",
            success=1,
            registered=1,
            skipped=0,
            errors=[],
        )

        store.cleanup()

        self.assertTrue(store.exists(protected_id))
        self.assertFalse(store.exists(newer_id))

        store.release_cleanup_protection(protected_id)
        latest_id = "task-runtime-latest"
        store.create(
            latest_id,
            platform="chatgpt",
            total=1,
            source="manual",
        )
        store.finish(
            latest_id,
            status="done",
            success=1,
            registered=1,
            skipped=0,
            errors=[],
        )
        store.cleanup()
        self.assertFalse(store.exists(protected_id))
        self.assertTrue(store.exists(latest_id))

    def test_request_stop_if_active_checks_state_and_sets_flag_atomically(self):
        store = RegisterTaskStore()
        task_id = "task-runtime-atomic-stop"
        store.create(
            task_id,
            platform="chatgpt",
            total=1,
            source="schedule",
        )

        state, first_request, control = store.request_stop_if_active(task_id)

        self.assertEqual(state, "active")
        self.assertTrue(first_request)
        self.assertTrue(control["stop_requested"])

        state, first_request, _ = store.request_stop_if_active(task_id)
        self.assertEqual(state, "active")
        self.assertFalse(first_request)

        store.finish(
            task_id,
            status="stopped",
            success=0,
            registered=0,
            skipped=0,
            errors=[],
        )
        state, first_request, _ = store.request_stop_if_active(task_id)
        self.assertEqual(state, "terminal")
        self.assertFalse(first_request)

        state, first_request, control = store.request_stop_if_active("missing")
        self.assertEqual(state, "missing")
        self.assertFalse(first_request)
        self.assertEqual(control, {})

    def test_update_meta_merges_cycle_results_without_dropping_task_identity(self):
        store = RegisterTaskStore()
        task_id = "task-runtime-meta"
        store.create(
            task_id,
            platform="chatgpt",
            total=3,
            source="schedule",
            meta={"automation": True, "concurrency": 2},
        )

        store.update_meta(
            task_id,
            invalid_rt_count=2,
            relogin_failed_count=1,
            alert_sent=False,
        )

        self.assertEqual(
            store.snapshot(task_id)["meta"],
            {
                "automation": True,
                "concurrency": 2,
                "invalid_rt_count": 2,
                "relogin_failed_count": 1,
                "alert_sent": False,
            },
        )

    def test_terminal_completion_time_is_immutable_after_post_processing(self):
        store = RegisterTaskStore()
        task_id = "task-runtime-completed-at"
        store.create(
            task_id,
            platform="chatgpt",
            total=1,
            source="schedule",
            meta={"automation": True},
        )

        with mock.patch("core.task_runtime.time.time", side_effect=[100.0, 200.0, 300.0]):
            store.finish(
                task_id,
                status="done",
                success=1,
                registered=1,
                skipped=0,
                errors=[],
            )
            completed_at = store.snapshot(task_id)["meta"]["completed_at"]
            store.append_log(task_id, "alert sent")
            store.update_meta(task_id, alert_sent=True)

        snapshot = store.snapshot(task_id)
        self.assertEqual(completed_at, 100.0)
        self.assertEqual(snapshot["meta"]["completed_at"], completed_at)
        self.assertEqual(snapshot["updated_at"], 300.0)

    def test_snapshot_contains_control_and_skip_fields(self):
        store = RegisterTaskStore()
        task_id = "task-runtime-snapshot"

        store.create(
            task_id,
            platform="chatgpt",
            total=2,
            source="manual",
            meta={"scope": "unit"},
        )
        store.request_skip_current(task_id)
        store.finish(
            task_id,
            status="done",
            success=1,
            skipped=1,
            errors=["error-a"],
        )

        snapshot = store.snapshot(task_id)

        self.assertEqual(snapshot["success"], 1)
        self.assertEqual(snapshot["skipped"], 1)
        self.assertEqual(snapshot["errors"], ["error-a"])
        self.assertEqual(
            snapshot["control"]["pending_skip_requests"],
            1,
        )

    def test_optional_snapshots_return_none_after_cleanup_or_missing_task(self):
        store = RegisterTaskStore()

        self.assertIsNone(store.snapshot_if_present("missing"))
        self.assertIsNone(store.log_snapshot_if_present("missing"))

    def test_log_snapshot_is_copied_atomically(self):
        store = RegisterTaskStore()
        task_id = "task-runtime-log-snapshot"
        store.create(
            task_id,
            platform="chatgpt",
            total=1,
            source="manual",
        )
        store.append_log(task_id, "first")
        store.mark_running(task_id)

        logs, status, snapshot = store.log_snapshot_if_present(task_id)
        logs.append("mutated")

        self.assertEqual(status, "running")
        self.assertEqual(snapshot["logs"], ["first"])
        self.assertEqual(store.snapshot(task_id)["logs"], ["first"])


if __name__ == "__main__":
    unittest.main()
