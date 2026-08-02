import unittest

from core.task_runtime import (
    RegisterTaskControl,
    RegisterTaskStore,
    SkipCurrentAttemptRequested,
    StopTaskRequested,
)


class RegisterTaskControlTests(unittest.TestCase):
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
