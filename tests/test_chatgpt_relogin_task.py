import threading
import unittest
import uuid
from datetime import datetime, timezone
from unittest import mock

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlmodel import Session, SQLModel

import api.tasks as tasks_module
from api.tasks import (
    ChatGPTReloginTaskRequest,
    RegisterTaskRequest,
    _create_chatgpt_relogin_task_record,
    _run_chatgpt_relogin_task,
    _task_store,
)
from core.chatgpt_task_gate import ChatGPTTaskGate
from core import db
from core.db import TaskRunModel


class ChatGPTReloginTaskTests(unittest.TestCase):
    def setUp(self):
        self.initial_task_ids = {
            snapshot["id"] for snapshot in _task_store.list_snapshots()
        }
        persistence = mock.patch("api.tasks._persist_task_snapshot")
        persistence.start()
        self.addCleanup(persistence.stop)
        save_log = mock.patch("api.tasks._save_task_log")
        save_log.start()
        self.addCleanup(save_log.stop)
        alert_sender = mock.patch(
            "services.chatgpt_auto_relogin_alerts.send_auto_relogin_alert",
            return_value={
                "sent": False,
                "reason": "below_threshold",
                "threshold": 5,
            },
        )
        self.alert_sender = alert_sender.start()
        self.addCleanup(alert_sender.stop)

    def tearDown(self):
        with _task_store._lock:
            new_task_ids = set(_task_store._records) - self.initial_task_ids
            for task_id in new_task_ids:
                _task_store._records.pop(task_id, None)

    def assert_enqueue_failure_is_terminal(
        self,
        task_id: str,
        source: str,
        message: str,
    ) -> None:
        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(snapshot["status"], "failed")
        self.assertTrue(snapshot["control"]["stop_requested"])
        self.assertIn(message, snapshot["error"])
        self.assertFalse(_task_store.has_active(source=source))

    def test_teardown_restores_the_original_task_record_collection(self):
        task_id = f"task-relogin-{uuid.uuid4().hex}"
        _create_chatgpt_relogin_task_record(task_id, [17])

        self.tearDown()

        remaining_ids = {
            snapshot["id"] for snapshot in _task_store.list_snapshots()
        }
        self.assertEqual(remaining_ids, self.initial_task_ids)

    def test_request_validates_relogin_concurrency_bounds(self):
        self.assertEqual(
            ChatGPTReloginTaskRequest(account_ids=[17]).concurrency,
            1,
        )
        request = ChatGPTReloginTaskRequest(account_ids=[17, 18], concurrency=2)
        self.assertEqual(request.concurrency, 2)
        for invalid in (0, 11):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                ChatGPTReloginTaskRequest(account_ids=[17], concurrency=invalid)

    def test_request_rejects_coercible_non_integer_ids_and_concurrency(self):
        invalid_payloads = (
            {"account_ids": [True], "concurrency": 1},
            {"account_ids": [17.0], "concurrency": 1},
            {"account_ids": ["17"], "concurrency": 1},
            {"account_ids": [17], "concurrency": True},
            {"account_ids": [17], "concurrency": 1.0},
            {"account_ids": [17], "concurrency": "1"},
        )

        for payload in invalid_payloads:
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                ChatGPTReloginTaskRequest(**payload)

    def test_route_rejects_coercible_non_integer_values_before_enqueue(self):
        app = FastAPI()
        app.include_router(tasks_module.router)
        client = TestClient(app)
        invalid_payloads = (
            {"account_ids": [True], "concurrency": 1},
            {"account_ids": [17.0], "concurrency": 1},
            {"account_ids": ["17"], "concurrency": 1},
            {"account_ids": [17], "concurrency": True},
            {"account_ids": [17], "concurrency": 1.0},
            {"account_ids": [17], "concurrency": "1"},
        )

        for payload in invalid_payloads:
            with self.subTest(payload=payload), mock.patch(
                "api.tasks.enqueue_chatgpt_relogin_task",
                return_value="task-should-not-exist",
            ) as enqueue, mock.patch.object(
                _task_store,
                "snapshot",
                return_value={"total": 1, "meta": {"concurrency": 1}},
            ):
                response = client.post("/tasks/chatgpt-relogin", json=payload)

                self.assertEqual(response.status_code, 422)
                enqueue.assert_not_called()

    def test_task_record_caps_concurrency_to_selected_account_count(self):
        task_id = f"task-relogin-{uuid.uuid4().hex}"
        _create_chatgpt_relogin_task_record(task_id, [17, 18], concurrency=10)
        self.assertEqual(_task_store.snapshot(task_id)["meta"]["concurrency"], 2)

    def test_enqueue_automatic_task_accepts_all_accounts_and_queues_runner(self):
        account_ids = [*range(1, 102), 3, 3]
        background_tasks = BackgroundTasks()

        task_id = tasks_module.enqueue_chatgpt_relogin_task(
            account_ids,
            concurrency=10,
            source="schedule",
            automation=True,
            background_tasks=background_tasks,
        )

        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(snapshot["source"], "schedule")
        self.assertEqual(snapshot["total"], 101)
        self.assertEqual(snapshot["meta"]["mode"], "relogin")
        self.assertTrue(snapshot["meta"]["automation"])
        self.assertEqual(snapshot["meta"]["account_ids"], list(range(1, 102)))
        self.assertEqual(snapshot["meta"]["concurrency"], 10)
        self.assertEqual(len(background_tasks.tasks), 1)
        queued = background_tasks.tasks[0]
        self.assertIs(queued.func, _run_chatgpt_relogin_task)
        self.assertEqual(
            queued.args,
            (task_id, list(range(1, 102)), 10),
        )

    def test_manual_route_reuses_enqueue_and_preserves_response_shape(self):
        request = ChatGPTReloginTaskRequest(
            account_ids=[17, 17, 18],
            concurrency=10,
        )
        background_tasks = BackgroundTasks()
        task_id = f"task-relogin-{uuid.uuid4().hex}"

        with mock.patch(
            "api.tasks.enqueue_chatgpt_relogin_task",
            return_value=task_id,
        ) as enqueue, mock.patch.object(
            _task_store,
            "snapshot",
            return_value={"total": 2, "meta": {"concurrency": 2}},
        ):
            response = tasks_module.create_chatgpt_relogin_task(
                request,
                background_tasks,
            )

        self.assertEqual(
            response,
            {"task_id": task_id, "count": 2, "concurrency": 2},
        )
        enqueue.assert_called_once_with(
            [17, 17, 18],
            10,
            background_tasks=background_tasks,
        )

    def test_enqueue_rejects_values_that_are_not_positive_integer_ids(self):
        invalid_values = ([], [0], [-1], [1.5], [True], ["2"])

        for account_ids in invalid_values:
            with self.subTest(account_ids=account_ids), self.assertRaises(
                HTTPException
            ):
                tasks_module.enqueue_chatgpt_relogin_task(
                    account_ids,
                    concurrency=10,
                    source="schedule",
                    automation=True,
                    background_tasks=BackgroundTasks(),
                )

    def test_enqueue_rejects_invalid_concurrency_without_clamping_or_coercion(self):
        invalid_values = (0, -1, 11, True, 1.0, "1")

        for concurrency in invalid_values:
            with self.subTest(concurrency=concurrency), self.assertRaises(
                HTTPException
            ):
                tasks_module.enqueue_chatgpt_relogin_task(
                    [17],
                    concurrency=concurrency,
                    source="schedule",
                    automation=True,
                    background_tasks=BackgroundTasks(),
                )

    def test_enqueue_without_fastapi_background_tasks_starts_daemon_thread(self):
        with mock.patch("api.tasks.threading.Thread") as thread_class:
            task_id = tasks_module.enqueue_chatgpt_relogin_task(
                [17, 17, 18],
                concurrency=10,
                source="schedule",
                automation=True,
            )

        thread_class.assert_called_once_with(
            target=_run_chatgpt_relogin_task,
            args=(task_id, [17, 18], 2),
            daemon=True,
        )
        thread_class.return_value.start.assert_called_once_with()
        thread_class.return_value.join.assert_not_called()

    def test_manual_enqueue_retains_one_hundred_account_limit(self):
        background_tasks = BackgroundTasks()

        with self.assertRaises(HTTPException) as error:
            tasks_module.enqueue_chatgpt_relogin_task(
                list(range(1, 102)),
                concurrency=10,
                background_tasks=background_tasks,
            )

        self.assertEqual(error.exception.status_code, 400)
        self.assertEqual(background_tasks.tasks, [])

    def test_enqueue_persistence_failure_terminalizes_created_record(self):
        suffix = uuid.uuid4().hex
        task_id = f"task_relogin_{suffix}"
        source = f"failure_persist_{suffix}"
        failure = RuntimeError("initial snapshot failed")

        with mock.patch(
            "api.tasks.uuid.uuid4",
            return_value=mock.Mock(hex=suffix),
        ), mock.patch(
            "api.tasks._persist_task_snapshot",
            side_effect=failure,
        ) as persist:
            with self.assertRaisesRegex(RuntimeError, "initial snapshot failed"):
                tasks_module.enqueue_chatgpt_relogin_task(
                    [17],
                    concurrency=1,
                    source=source,
                    automation=True,
                    background_tasks=BackgroundTasks(),
                )

        self.assertGreaterEqual(persist.call_count, 2)
        self.assert_enqueue_failure_is_terminal(task_id, source, str(failure))

    def test_enqueue_thread_construction_and_start_failures_are_terminal(self):
        failures = ("construct", "start")

        for failure_stage in failures:
            suffix = uuid.uuid4().hex
            task_id = f"task_relogin_{suffix}"
            source = f"failure_thread_{failure_stage}_{suffix}"
            failure = RuntimeError(f"thread {failure_stage} failed")
            thread = mock.Mock()
            if failure_stage == "start":
                thread.start.side_effect = failure
                thread_patch = mock.patch(
                    "api.tasks.threading.Thread",
                    return_value=thread,
                )
            else:
                thread_patch = mock.patch(
                    "api.tasks.threading.Thread",
                    side_effect=failure,
                )

            with self.subTest(failure_stage=failure_stage), mock.patch(
                "api.tasks.uuid.uuid4",
                return_value=mock.Mock(hex=suffix),
            ), thread_patch:
                with self.assertRaisesRegex(
                    RuntimeError,
                    f"thread {failure_stage} failed",
                ):
                    tasks_module.enqueue_chatgpt_relogin_task(
                        [17],
                        concurrency=1,
                        source=source,
                        automation=True,
                    )

                self.assert_enqueue_failure_is_terminal(
                    task_id,
                    source,
                    str(failure),
                )

    def test_enqueue_background_task_failure_terminalizes_created_record(self):
        suffix = uuid.uuid4().hex
        task_id = f"task_relogin_{suffix}"
        source = f"failure_background_{suffix}"
        failure = RuntimeError("background add_task failed")
        background_tasks = mock.Mock()
        background_tasks.add_task.side_effect = failure

        with mock.patch(
            "api.tasks.uuid.uuid4",
            return_value=mock.Mock(hex=suffix),
        ):
            with self.assertRaisesRegex(RuntimeError, "background add_task failed"):
                tasks_module.enqueue_chatgpt_relogin_task(
                    [17],
                    concurrency=1,
                    source=source,
                    automation=True,
                    background_tasks=background_tasks,
                )

        self.assert_enqueue_failure_is_terminal(task_id, source, str(failure))

    def test_task_runs_selected_accounts_at_requested_concurrency(self):
        task_id = f"task-relogin-{uuid.uuid4().hex}"
        _create_chatgpt_relogin_task_record(task_id, [17, 18], concurrency=2)
        state_lock = threading.Lock()
        both_started = threading.Event()
        active = 0
        max_active = 0

        def concurrent_relogin(account_id, **kwargs):
            nonlocal active, max_active
            with state_lock:
                active += 1
                max_active = max(max_active, active)
                if active == 2:
                    both_started.set()
            both_started.wait(timeout=1)
            with state_lock:
                active -= 1
            return {
                "ok": True,
                "relogin_ok": True,
                "stage": "completed",
                "account_id": account_id,
                "email": f"account-{account_id}@example.com",
                "message": "重登并同步成功",
            }

        with mock.patch(
            "services.chatgpt_relogin.relogin_chatgpt_account",
            side_effect=concurrent_relogin,
        ):
            _run_chatgpt_relogin_task(task_id, [17, 18], concurrency=2)

        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(max_active, 2)
        self.assertEqual(snapshot["success"], 2)
        self.assertEqual(snapshot["registered"], 2)
        self.assertEqual(snapshot["meta"]["concurrency"], 2)

    def test_automatic_task_uses_refresh_first_account_action(self):
        task_id = f"task-relogin-{uuid.uuid4().hex}"
        _create_chatgpt_relogin_task_record(
            task_id,
            [17],
            source="schedule",
            automation=True,
        )
        result = {
            "ok": True,
            "relogin_ok": False,
            "refresh_ok": True,
            "mode": "refresh_token",
            "stage": "completed",
            "account_id": 17,
            "email": "refresh@example.com",
            "message": "RT 刷新并同步 Codex2API 成功",
        }

        with mock.patch(
            "services.chatgpt_relogin.refresh_or_relogin_chatgpt_account",
            return_value=result,
        ) as refresh_first, mock.patch(
            "services.chatgpt_relogin.relogin_chatgpt_account"
        ) as full_login:
            _run_chatgpt_relogin_task(task_id, [17])

        refresh_first.assert_called_once()
        full_login.assert_not_called()
        self.assertEqual(_task_store.snapshot(task_id)["success"], 1)

    def test_manual_task_keeps_forced_full_login_action(self):
        task_id = f"task-relogin-{uuid.uuid4().hex}"
        _create_chatgpt_relogin_task_record(task_id, [18])
        result = {
            "ok": True,
            "relogin_ok": True,
            "stage": "completed",
            "account_id": 18,
            "email": "manual@example.com",
            "message": "重登并同步 Codex2API 成功",
        }

        with mock.patch(
            "services.chatgpt_relogin.refresh_or_relogin_chatgpt_account"
        ) as refresh_first, mock.patch(
            "services.chatgpt_relogin.relogin_chatgpt_account",
            return_value=result,
        ) as full_login:
            _run_chatgpt_relogin_task(task_id, [18])

        full_login.assert_called_once()
        refresh_first.assert_not_called()
        self.alert_sender.assert_not_called()
        self.assertEqual(_task_store.snapshot(task_id)["success"], 1)

    def test_automatic_task_records_cycle_counts_and_sends_one_summary_alert(self):
        task_id = f"task-relogin-{uuid.uuid4().hex}"
        account_ids = [201, 202, 203, 204]
        _create_chatgpt_relogin_task_record(
            task_id,
            account_ids,
            source="schedule",
            automation=True,
        )
        results = [
            {
                "ok": False,
                "relogin_ok": False,
                "refresh_state": "invalid",
                "mode": "full_login",
                "stage": "relogin",
                "account_id": 201,
                "email": "failed@example.com",
                "message": "验证码登录失败",
            },
            {
                "ok": True,
                "relogin_ok": True,
                "refresh_state": "invalid",
                "mode": "full_login",
                "stage": "completed",
                "account_id": 202,
                "email": "recovered@example.com",
                "message": "完整登录并同步成功",
            },
            {
                "ok": False,
                "relogin_ok": True,
                "refresh_state": "invalid",
                "mode": "full_login",
                "stage": "codex2api_sync",
                "account_id": 203,
                "email": "sync-failed@example.com",
                "message": "登录成功但同步失败",
            },
            {
                "ok": False,
                "relogin_ok": False,
                "refresh_state": "transient_error",
                "mode": "refresh_token",
                "stage": "refresh_deferred",
                "account_id": 204,
                "email": "retry@example.com",
                "message": "下轮重试",
            },
        ]
        self.alert_sender.return_value = {
            "sent": True,
            "reason": "sent",
            "threshold": 2,
        }

        with mock.patch(
            "services.chatgpt_relogin.refresh_or_relogin_chatgpt_account",
            side_effect=results,
        ):
            _run_chatgpt_relogin_task(task_id, account_ids, concurrency=1)

        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(snapshot["status"], "done")
        self.assertEqual(snapshot["meta"]["invalid_rt_count"], 3)
        self.assertEqual(snapshot["meta"]["relogin_failed_count"], 1)
        self.assertTrue(snapshot["meta"]["alert_sent"])
        self.assertEqual(snapshot["meta"]["alert_reason"], "sent")
        self.assertEqual(snapshot["meta"]["alert_threshold"], 2)
        self.alert_sender.assert_called_once_with(
            task_id=task_id,
            total_accounts=4,
            invalid_rt_count=3,
            relogin_failed_count=1,
        )

    def test_automatic_alert_exception_does_not_change_task_outcome(self):
        task_id = f"task-relogin-{uuid.uuid4().hex}"
        _create_chatgpt_relogin_task_record(
            task_id,
            [205],
            source="schedule",
            automation=True,
        )
        self.alert_sender.side_effect = RuntimeError("smtp secret detail")

        with mock.patch(
            "services.chatgpt_relogin.refresh_or_relogin_chatgpt_account",
            return_value={
                "ok": True,
                "relogin_ok": False,
                "refresh_state": "valid",
                "mode": "refresh_token",
                "stage": "completed",
                "account_id": 205,
                "email": "valid@example.com",
                "message": "RT 刷新成功",
            },
        ):
            _run_chatgpt_relogin_task(task_id, [205])

        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(snapshot["status"], "done")
        self.assertEqual(snapshot["success"], 1)
        self.assertFalse(snapshot["meta"]["alert_sent"])
        self.assertEqual(snapshot["meta"]["alert_reason"], "send_failed")
        self.assertNotIn("smtp secret detail", "\n".join(snapshot["logs"]))

    def test_task_reports_each_account_and_finishes_only_after_sync_results(self):
        task_id = f"task-relogin-{uuid.uuid4().hex}"
        _create_chatgpt_relogin_task_record(task_id, [17, 18])
        results = [
            {
                "ok": True,
                "relogin_ok": True,
                "stage": "completed",
                "account_id": 17,
                "email": "ok@example.com",
                "message": "重登并同步成功",
            },
            {
                "ok": False,
                "relogin_ok": True,
                "stage": "codex2api_sync",
                "account_id": 18,
                "email": "sync-failed@example.com",
                "message": "重登成功，但 Codex2API 覆盖更新失败",
            },
        ]

        with mock.patch(
            "services.chatgpt_relogin.relogin_chatgpt_account",
            side_effect=results,
        ) as relogin:
            _run_chatgpt_relogin_task(task_id, [17, 18])

        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(snapshot["status"], "done")
        self.assertEqual(snapshot["meta"]["mode"], "relogin")
        self.assertEqual(snapshot["success"], 1)
        self.assertEqual(snapshot["registered"], 2)
        self.assertEqual(snapshot["progress"], "2/2")
        self.assertEqual(len(snapshot["errors"]), 1)
        self.assertIn("sync-failed@example.com", snapshot["errors"][0])
        self.assertTrue(any("重登并同步成功" in line for line in snapshot["logs"]))
        self.assertTrue(any("覆盖更新失败" in line for line in snapshot["logs"]))
        self.assertEqual(relogin.call_args_list[0].args[0], 17)
        self.assertEqual(relogin.call_args_list[1].args[0], 18)

    def test_task_logs_a_real_login_failure_as_failure(self):
        task_id = f"task-relogin-{uuid.uuid4().hex}"
        _create_chatgpt_relogin_task_record(task_id, [19])
        with mock.patch(
            "services.chatgpt_relogin.relogin_chatgpt_account",
            return_value={
                "ok": False,
                "relogin_ok": False,
                "stage": "relogin",
                "account_id": 19,
                "email": "failed@example.com",
                "message": "邮箱验证码校验失败",
            },
        ):
            _run_chatgpt_relogin_task(task_id, [19])

        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(snapshot["success"], 0)
        self.assertEqual(snapshot["registered"], 1)
        self.assertIn("邮箱验证码校验失败", snapshot["errors"][0])
        self.assertTrue(any("重登失败" in line for line in snapshot["logs"]))

    def test_task_reports_deactivated_account_as_removed(self):
        task_id = f"task-relogin-{uuid.uuid4().hex}"
        _create_chatgpt_relogin_task_record(task_id, [24])
        with mock.patch(
            "services.chatgpt_relogin.relogin_chatgpt_account",
            return_value={
                "ok": False,
                "relogin_ok": False,
                "account_removed": True,
                "stage": "account_removed",
                "account_id": 24,
                "email": "removed@example.com",
                "message": "账号已被删除或停用，本地记录已自动删除",
            },
        ), mock.patch("api.tasks._save_task_log") as save_task_log:
            _run_chatgpt_relogin_task(task_id, [24])

        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(snapshot["success"], 0)
        self.assertEqual(snapshot["registered"], 1)
        self.assertIn("本地记录已自动删除", snapshot["errors"][0])
        self.assertTrue(
            any(
                "[REMOVE]" in line and "本地记录已移除" in line
                for line in snapshot["logs"]
            )
        )
        self.assertTrue(
            save_task_log.call_args.kwargs["detail"]["account_removed"]
        )

    def test_stop_requested_after_completed_account_keeps_its_success_result(self):
        task_id = f"task-relogin-{uuid.uuid4().hex}"
        _create_chatgpt_relogin_task_record(task_id, [20])

        def complete_then_stop(account_id, **kwargs):
            kwargs["task_control"].request_stop()
            return {
                "ok": True,
                "relogin_ok": True,
                "stage": "completed",
                "account_id": account_id,
                "email": "completed@example.com",
                "message": "重登并同步成功",
            }

        with mock.patch(
            "services.chatgpt_relogin.relogin_chatgpt_account",
            side_effect=complete_then_stop,
        ):
            _run_chatgpt_relogin_task(task_id, [20])

        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(snapshot["status"], "stopped")
        self.assertEqual(snapshot["success"], 1)
        self.assertEqual(snapshot["registered"], 1)
        self.assertEqual(snapshot["progress"], "1/1")

    def test_stop_after_start_log_does_not_begin_relogin_side_effects(self):
        task_id = f"task-relogin-{uuid.uuid4().hex}"
        _create_chatgpt_relogin_task_record(task_id, [25])
        real_log = tasks_module._log

        def log_then_stop(current_task_id, message):
            real_log(current_task_id, message)
            if "开始重登第" in message:
                _task_store.control_for(current_task_id).request_stop()

        with mock.patch("api.tasks._log", side_effect=log_then_stop), mock.patch(
            "services.chatgpt_relogin.relogin_chatgpt_account"
        ) as relogin:
            _run_chatgpt_relogin_task(task_id, [25])

        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(snapshot["status"], "stopped")
        self.assertEqual(snapshot["success"], 0)
        self.assertEqual(snapshot["registered"], 0)
        relogin.assert_not_called()

    def test_snapshot_persistence_failure_does_not_abort_business_task(self):
        task_id = f"task-relogin-{uuid.uuid4().hex}"
        _create_chatgpt_relogin_task_record(task_id, [26])

        with mock.patch(
            "api.tasks._persist_task_snapshot",
            side_effect=RuntimeError("database is locked"),
        ), mock.patch(
            "services.chatgpt_relogin.relogin_chatgpt_account",
            return_value={
                "ok": True,
                "relogin_ok": True,
                "stage": "completed",
                "account_id": 26,
                "email": "account-26@example.com",
                "message": "重登并同步成功",
            },
        ) as relogin:
            _run_chatgpt_relogin_task(task_id, [26])

        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(snapshot["status"], "done")
        self.assertEqual(snapshot["success"], 1)
        self.assertEqual(snapshot["registered"], 1)
        self.assertEqual(snapshot.get("error", ""), "")
        relogin.assert_called_once()

    def test_aggregation_persistence_failure_keeps_all_business_results(self):
        task_id = f"task-relogin-{uuid.uuid4().hex}"
        _create_chatgpt_relogin_task_record(task_id, [27, 28, 29])
        relogin_calls = []

        def successful_relogin(account_id, **kwargs):
            del kwargs
            relogin_calls.append(account_id)
            return {
                "ok": True,
                "relogin_ok": True,
                "stage": "completed",
                "account_id": account_id,
                "email": f"account-{account_id}@example.com",
                "message": "重登并同步成功",
            }

        def fail_first_aggregate_snapshot(current_task_id):
            snapshot = _task_store.snapshot(current_task_id)
            if snapshot["progress"] == "1/3":
                raise RuntimeError("database is locked during aggregate")

        with mock.patch(
            "api.tasks._persist_task_snapshot",
            side_effect=fail_first_aggregate_snapshot,
        ), mock.patch(
            "services.chatgpt_relogin.relogin_chatgpt_account",
            side_effect=successful_relogin,
        ):
            _run_chatgpt_relogin_task(task_id, [27, 28, 29], concurrency=1)

        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(relogin_calls, [27, 28, 29])
        self.assertEqual(snapshot["status"], "done")
        self.assertEqual(snapshot["success"], 3)
        self.assertEqual(snapshot["registered"], 3)
        self.assertEqual(snapshot.get("error", ""), "")

    def test_initial_submit_failure_requests_stop_before_executor_exit(self):
        from concurrent.futures import Future

        task_id = f"task-relogin-{uuid.uuid4().hex}"
        _create_chatgpt_relogin_task_record(task_id, [30, 31])
        observed = {}

        class SubmitFailingExecutor:
            def __init__(self, max_workers):
                self.max_workers = max_workers
                self.submit_calls = 0
                self.first_future = Future()

            def __enter__(self):
                return self

            def submit(self, fn, *args):
                del fn, args
                self.submit_calls += 1
                if self.submit_calls == 2:
                    raise RuntimeError("executor submit failed")
                return self.first_future

            def __exit__(self, exc_type, exc, traceback):
                del exc_type, exc, traceback
                observed["stop_on_exit"] = _task_store.control_for(
                    task_id
                ).is_stop_requested()
                self.first_future.cancel()
                return False

        with mock.patch(
            "concurrent.futures.ThreadPoolExecutor",
            SubmitFailingExecutor,
        ):
            _run_chatgpt_relogin_task(task_id, [30, 31], concurrency=2)

        snapshot = _task_store.snapshot(task_id)
        self.assertTrue(observed["stop_on_exit"])
        self.assertEqual(snapshot["status"], "failed")
        self.assertIn("executor submit failed", snapshot["error"])

    def test_outer_failure_handler_preserves_existing_terminal_state(self):
        task_id = f"task-relogin-{uuid.uuid4().hex}"
        _create_chatgpt_relogin_task_record(task_id, [32])

        def finish_then_raise(*args, **kwargs):
            del args, kwargs
            _task_store.finish(
                task_id,
                status="done",
                success=1,
                registered=1,
                skipped=0,
                errors=[],
            )
            raise RuntimeError("late persistence failure")

        with mock.patch(
            "api.tasks._run_chatgpt_relogin_task_inner",
            side_effect=finish_then_raise,
        ):
            _run_chatgpt_relogin_task(task_id, [32])

        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(snapshot["status"], "done")
        self.assertEqual(snapshot["success"], 1)
        self.assertEqual(snapshot["registered"], 1)
        self.assertEqual(snapshot.get("error", ""), "")

    def test_task_redacts_otp_and_authorization_code_from_persisted_logs(self):
        task_id = f"task-relogin-{uuid.uuid4().hex}"
        _create_chatgpt_relogin_task_record(task_id, [21])

        def relogin_with_sensitive_logs(account_id, **kwargs):
            kwargs["log_fn"]("成功获取验证码: 123456")
            kwargs["log_fn"](
                "获取到 authorization code: auth-code-secret-prefix..."
            )
            kwargs["log_fn"]("进入 token_exchange: code=exchange-secret...")
            return {
                "ok": True,
                "relogin_ok": True,
                "stage": "completed",
                "account_id": account_id,
                "email": "redacted@example.com",
                "message": "重登并同步成功",
            }

        with mock.patch(
            "services.chatgpt_relogin.relogin_chatgpt_account",
            side_effect=relogin_with_sensitive_logs,
        ):
            _run_chatgpt_relogin_task(task_id, [21])

        logs = "\n".join(_task_store.snapshot(task_id)["logs"])
        self.assertNotIn("123456", logs)
        self.assertNotIn("auth-code-secret-prefix", logs)
        self.assertNotIn("exchange-secret", logs)
        self.assertIn("已隐藏", logs)

    def test_task_redacts_sensitive_failure_result_from_all_persisted_outputs(self):
        task_id = f"task-relogin-{uuid.uuid4().hex}"
        _create_chatgpt_relogin_task_record(task_id, [22])
        sensitive_message = (
            "验证码: 654321 authorization code: result-auth-secret "
            "access_token=result-at-secret"
        )

        with mock.patch(
            "services.chatgpt_relogin.relogin_chatgpt_account",
            return_value={
                "ok": False,
                "relogin_ok": False,
                "stage": "relogin",
                "account_id": 22,
                "email": "failed-redaction@example.com",
                "message": sensitive_message,
            },
        ), mock.patch("api.tasks._save_task_log") as save_task_log:
            _run_chatgpt_relogin_task(task_id, [22])

        snapshot = _task_store.snapshot(task_id)
        persisted_text = "\n".join(snapshot["logs"] + snapshot["errors"])
        task_log_error = save_task_log.call_args.kwargs["error"]
        for secret in ("654321", "result-auth-secret", "result-at-secret"):
            self.assertNotIn(secret, persisted_text)
            self.assertNotIn(secret, task_log_error)
        self.assertIn("已隐藏", persisted_text)
        self.assertIn("已隐藏", task_log_error)

    def test_task_redacts_sensitive_relogin_exception_from_all_persisted_outputs(self):
        task_id = f"task-relogin-{uuid.uuid4().hex}"
        _create_chatgpt_relogin_task_record(task_id, [23])
        sensitive_error = (
            "OTP 987654 code=exception-code-secret "
            "refresh_token=exception-rt-secret"
        )

        with mock.patch(
            "services.chatgpt_relogin.relogin_chatgpt_account",
            side_effect=RuntimeError(sensitive_error),
        ), mock.patch("api.tasks._save_task_log") as save_task_log:
            _run_chatgpt_relogin_task(task_id, [23])

        snapshot = _task_store.snapshot(task_id)
        persisted_text = "\n".join(snapshot["logs"] + snapshot["errors"])
        task_log_error = save_task_log.call_args.kwargs["error"]
        for secret in ("987654", "exception-code-secret", "exception-rt-secret"):
            self.assertNotIn(secret, persisted_text)
            self.assertNotIn(secret, task_log_error)
        self.assertIn("已隐藏", persisted_text)
        self.assertIn("已隐藏", task_log_error)

    def test_automatic_runner_rejected_by_foreground_is_terminal_without_accounts(self):
        task_id = f"task-relogin-{uuid.uuid4().hex}"
        _create_chatgpt_relogin_task_record(
            task_id,
            [41, 42],
            concurrency=2,
            automation=True,
        )
        gate = ChatGPTTaskGate()
        foreground_lease = gate.enter_foreground()
        self.assertIsNotNone(foreground_lease)

        try:
            with mock.patch(
                "api.tasks.chatgpt_task_gate",
                gate,
            ), mock.patch(
                "api.tasks._run_chatgpt_relogin_task_inner"
            ) as inner, mock.patch(
                "api.tasks._persist_task_snapshot_best_effort",
                return_value=True,
            ) as persist:
                _run_chatgpt_relogin_task(task_id, [41, 42], concurrency=2)
        finally:
            gate.leave_foreground(foreground_lease)

        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(snapshot["status"], "stopped")
        self.assertEqual(snapshot["registered"], 0)
        self.assertTrue(snapshot["control"]["stop_requested"])
        self.assertTrue(any("自动重登" in line for line in snapshot["logs"]))
        inner.assert_not_called()
        persist.assert_called_with(task_id)

    def test_foreground_relogin_preempts_auto_without_dispatching_new_account(self):
        auto_task_id = f"task-relogin-auto-{uuid.uuid4().hex}"
        manual_task_id = f"task-relogin-manual-{uuid.uuid4().hex}"
        _create_chatgpt_relogin_task_record(
            auto_task_id,
            [51, 52, 53],
            concurrency=2,
            source="schedule",
            automation=True,
        )
        _create_chatgpt_relogin_task_record(manual_task_id, [99])
        gate = ChatGPTTaskGate()
        both_auto_started = threading.Event()
        release_auto = threading.Event()
        stop_requested = threading.Event()
        manual_started = threading.Event()
        calls: list[int] = []
        thread_errors: list[BaseException] = []
        calls_lock = threading.Lock()

        def relogin(account_id, **kwargs):
            del kwargs
            with calls_lock:
                calls.append(account_id)
                auto_started_count = len(
                    [value for value in calls if value in {51, 52}]
                )
                if auto_started_count == 2:
                    both_auto_started.set()
            if account_id in {51, 52}:
                self.assertTrue(release_auto.wait(timeout=2))
            elif account_id == 99:
                manual_started.set()
            return {
                "ok": True,
                "relogin_ok": True,
                "stage": "completed",
                "account_id": account_id,
                "email": f"account-{account_id}@example.com",
                "message": "重登并同步成功",
            }

        auto_control = _task_store.control_for(auto_task_id)
        real_request_stop = auto_control.request_stop

        def request_stop() -> None:
            real_request_stop()
            stop_requested.set()

        def run(task_id, account_ids, concurrency):
            try:
                _run_chatgpt_relogin_task(
                    task_id,
                    account_ids,
                    concurrency=concurrency,
                )
            except BaseException as exc:
                thread_errors.append(exc)

        auto_thread = threading.Thread(
            target=run,
            args=(auto_task_id, [51, 52, 53], 2),
        )
        manual_thread = threading.Thread(
            target=run,
            args=(manual_task_id, [99], 1),
        )

        with mock.patch(
            "api.tasks.chatgpt_task_gate",
            gate,
        ), mock.patch(
            "services.chatgpt_relogin.relogin_chatgpt_account",
            side_effect=relogin,
        ), mock.patch(
            "services.chatgpt_relogin.refresh_or_relogin_chatgpt_account",
            side_effect=relogin,
        ), mock.patch.object(
            auto_control,
            "request_stop",
            side_effect=request_stop,
        ) as stop_spy:
            auto_thread.start()
            self.assertTrue(both_auto_started.wait(timeout=1))
            manual_thread.start()
            try:
                self.assertTrue(stop_requested.wait(timeout=1))
                self.assertFalse(manual_started.is_set())
            finally:
                release_auto.set()
                auto_thread.join(timeout=2)
                manual_thread.join(timeout=2)

        self.assertFalse(auto_thread.is_alive())
        self.assertFalse(manual_thread.is_alive())
        self.assertEqual(thread_errors, [])
        self.assertNotIn(53, calls)
        self.assertIn(99, calls)
        self.assertEqual(stop_spy.call_count, 1)
        self.assertEqual(_task_store.snapshot(auto_task_id)["status"], "stopped")
        self.assertEqual(_task_store.snapshot(auto_task_id)["success"], 2)
        self.assertEqual(_task_store.snapshot(manual_task_id)["status"], "done")
        manual_logs = "\n".join(_task_store.snapshot(manual_task_id)["logs"])
        self.assertIn("等待自动重登释放", manual_logs)
        self.assertIn("手工任务优先", manual_logs)
        self.assertEqual(
            dict(gate.snapshot()),
            {
                "automation_active": False,
                "automation_stop_requested": False,
                "foreground_active": 0,
                "foreground_waiters": 0,
            },
        )

    def test_stopped_manual_relogin_cancels_wait_without_running_accounts(self):
        task_id = f"task-relogin-{uuid.uuid4().hex}"
        _create_chatgpt_relogin_task_record(task_id, [61])
        gate = ChatGPTTaskGate()
        waiting_logged = threading.Event()
        thread_errors: list[BaseException] = []
        automation_lease = gate.try_enter_automation(lambda: None)
        self.assertIsNotNone(automation_lease)
        real_log = tasks_module._log

        def observe_log(current_task_id, message):
            real_log(current_task_id, message)
            if "等待自动重登释放" in message:
                waiting_logged.set()

        def run():
            try:
                _run_chatgpt_relogin_task(task_id, [61])
            except BaseException as exc:
                thread_errors.append(exc)

        worker = threading.Thread(target=run)
        try:
            with mock.patch(
                "api.tasks.chatgpt_task_gate",
                gate,
            ), mock.patch(
                "api.tasks._run_chatgpt_relogin_task_inner"
            ) as inner, mock.patch(
                "api.tasks._log",
                side_effect=observe_log,
            ):
                worker.start()
                self.assertTrue(waiting_logged.wait(timeout=1))
                _task_store.control_for(task_id).request_stop()
                worker.join(timeout=1)

            self.assertFalse(worker.is_alive())
            self.assertEqual(thread_errors, [])
            self.assertEqual(_task_store.snapshot(task_id)["status"], "stopped")
            self.assertEqual(gate.snapshot()["foreground_waiters"], 0)
            inner.assert_not_called()
        finally:
            gate.leave_automation(automation_lease)

    def test_scheduled_enqueue_returns_foreground_busy_without_creating_task(self):
        with mock.patch.object(
            tasks_module,
            "_finalize_orphan_tasks",
        ) as finalize, mock.patch.object(
            _task_store,
            "has_active",
            return_value=True,
        ), mock.patch.object(
            tasks_module.chatgpt_task_gate,
            "snapshot",
            return_value={
                "automation_active": False,
                "foreground_active": 1,
                "foreground_waiters": 0,
            },
        ), mock.patch.object(
            tasks_module,
            "enqueue_chatgpt_relogin_task",
        ) as enqueue:
            decision = tasks_module.try_enqueue_scheduled_chatgpt_relogin(
                [17, 18],
                10,
            )

        self.assertEqual(
            decision,
            {"accepted": False, "task_id": None, "reason": "foreground_busy"},
        )
        finalize.assert_called_once_with()
        enqueue.assert_not_called()

    def test_two_scheduled_enqueue_calls_atomically_create_only_one_task(self):
        active = False
        active_lock = threading.Lock()
        enqueue_started = threading.Event()
        release_enqueue = threading.Event()
        decisions = []
        errors = []

        def has_active(**kwargs):
            self.assertEqual(kwargs, {"platform": "chatgpt"})
            with active_lock:
                return active

        def enqueue(account_ids, concurrency, **kwargs):
            nonlocal active
            self.assertEqual(list(account_ids), [17, 18])
            self.assertEqual(concurrency, 10)
            self.assertEqual(
                kwargs,
                {
                    "source": "schedule",
                    "automation": True,
                    "background_tasks": None,
                },
            )
            with active_lock:
                active = True
            enqueue_started.set()
            release_enqueue.wait(timeout=2)
            return "task-only"

        def run():
            try:
                decisions.append(
                    tasks_module.try_enqueue_scheduled_chatgpt_relogin(
                        [17, 18],
                        10,
                    )
                )
            except BaseException as exc:
                errors.append(exc)

        with mock.patch.object(
            tasks_module,
            "_finalize_orphan_tasks",
        ), mock.patch.object(
            _task_store,
            "has_active",
            side_effect=has_active,
        ), mock.patch.object(
            tasks_module.chatgpt_task_gate,
            "snapshot",
            return_value={
                "automation_active": False,
                "foreground_active": 0,
                "foreground_waiters": 0,
            },
        ), mock.patch.object(
            tasks_module,
            "enqueue_chatgpt_relogin_task",
            side_effect=enqueue,
        ) as enqueue_mock:
            first = threading.Thread(target=run)
            second = threading.Thread(target=run)
            first.start()
            self.assertTrue(enqueue_started.wait(timeout=1))
            second.start()
            release_enqueue.set()
            first.join(timeout=2)
            second.join(timeout=2)

        self.assertEqual(errors, [])
        self.assertEqual(enqueue_mock.call_count, 1)
        self.assertCountEqual(
            decisions,
            [
                {"accepted": True, "task_id": "task-only", "reason": "enqueued"},
                {"accepted": False, "task_id": None, "reason": "task_busy"},
            ],
        )

    def test_manual_register_and_scheduled_relogin_share_atomic_enqueue_lock(self):
        manual_preparing = threading.Event()
        release_manual = threading.Event()
        scheduled_attempted_lock = threading.Event()
        scheduled_acquired_lock = threading.Event()
        active = False
        results = {}
        errors = []

        class ObservedRLock:
            def __init__(self):
                self._lock = threading.RLock()

            def __enter__(self):
                scheduled = threading.current_thread().name == "scheduled"
                if scheduled:
                    scheduled_attempted_lock.set()
                self._lock.acquire()
                if scheduled:
                    scheduled_acquired_lock.set()
                return self

            def __exit__(self, exc_type, exc, traceback):
                del exc_type, exc, traceback
                self._lock.release()

        def prepare(request):
            manual_preparing.set()
            self.assertTrue(release_manual.wait(timeout=2))
            return request

        def create_manual(*args, **kwargs):
            nonlocal active
            del args, kwargs
            active = True
            return "task-manual"

        def has_active(**kwargs):
            self.assertEqual(kwargs, {"platform": "chatgpt"})
            return active

        def run_manual():
            try:
                results["manual"] = tasks_module.enqueue_register_task(
                    RegisterTaskRequest(platform="chatgpt"),
                    background_tasks=BackgroundTasks(),
                )
            except BaseException as exc:
                errors.append(exc)

        def run_scheduled():
            try:
                results["scheduled"] = (
                    tasks_module.try_enqueue_scheduled_chatgpt_relogin(
                        [17],
                        10,
                    )
                )
            except BaseException as exc:
                errors.append(exc)

        with mock.patch.object(
            tasks_module,
            "_chatgpt_task_enqueue_lock",
            ObservedRLock(),
        ), mock.patch.object(
            tasks_module,
            "_prepare_register_request",
            side_effect=prepare,
        ), mock.patch.object(
            tasks_module,
            "_enqueue_prepared_register_task",
            side_effect=create_manual,
        ), mock.patch.object(
            tasks_module,
            "_finalize_orphan_tasks",
            return_value=set(),
        ), mock.patch.object(
            _task_store,
            "has_active",
            side_effect=has_active,
        ), mock.patch.object(
            tasks_module.chatgpt_task_gate,
            "snapshot",
            return_value={
                "automation_active": False,
                "foreground_active": 0,
                "foreground_waiters": 0,
            },
        ), mock.patch.object(
            tasks_module,
            "enqueue_chatgpt_relogin_task",
            return_value="task-overlap",
        ) as scheduled_enqueue:
            manual = threading.Thread(target=run_manual, name="manual")
            scheduled = threading.Thread(target=run_scheduled, name="scheduled")
            manual.start()
            self.assertTrue(manual_preparing.wait(timeout=1))
            scheduled.start()
            self.assertTrue(scheduled_attempted_lock.wait(timeout=1))
            self.assertFalse(scheduled_acquired_lock.is_set())
            release_manual.set()
            manual.join(timeout=2)
            scheduled.join(timeout=2)

        self.assertFalse(manual.is_alive())
        self.assertFalse(scheduled.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results["manual"], "task-manual")
        self.assertEqual(
            results["scheduled"],
            {"accepted": False, "task_id": None, "reason": "task_busy"},
        )
        scheduled_enqueue.assert_not_called()

    def test_observe_chatgpt_task_prefers_live_memory_and_returns_aware_utc(self):
        updated_at = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc).timestamp()
        memory = {
            "id": "task-live",
            "platform": "chatgpt",
            "status": "running",
            "updated_at": updated_at,
        }

        with mock.patch.object(
            _task_store,
            "snapshot_if_present",
            return_value=memory,
        ), mock.patch.object(
            tasks_module,
            "_finalize_orphan_tasks",
        ) as finalize, mock.patch.object(
            tasks_module,
            "_get_persisted_task",
        ) as persisted:
            observation = tasks_module.observe_chatgpt_task("task-live")

        self.assertEqual(observation["status"], "running")
        self.assertEqual(
            observation["updated_at"],
            datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc),
        )
        self.assertIsNotNone(observation["updated_at"].tzinfo)
        self.assertTrue(observation["live"])
        self.assertFalse(observation["orphaned"])
        finalize.assert_not_called()
        persisted.assert_not_called()

    def test_observe_chatgpt_task_finalizes_persisted_orphan_and_marks_it(self):
        test_engine = db._create_database_engine("sqlite://")
        SQLModel.metadata.create_all(test_engine)
        old_updated_at = datetime(2026, 8, 2, 11, 0, tzinfo=timezone.utc)
        try:
            with Session(test_engine) as session:
                session.add(
                    TaskRunModel(
                        id="task-orphan",
                        platform="chatgpt",
                        source="schedule",
                        status="pending",
                        updated_at=old_updated_at,
                    )
                )
                session.commit()

            with mock.patch.object(
                tasks_module,
                "engine",
                test_engine,
            ), mock.patch.object(
                _task_store,
                "snapshot_if_present",
                return_value=None,
            ), mock.patch.object(
                _task_store,
                "exists",
                return_value=False,
            ):
                observation = tasks_module.observe_chatgpt_task("task-orphan")

            self.assertEqual(observation["status"], "stopped")
            self.assertFalse(observation["live"])
            self.assertTrue(observation["orphaned"])
            self.assertEqual(observation["updated_at"].tzinfo, timezone.utc)
            with Session(test_engine) as session:
                self.assertEqual(
                    session.get(TaskRunModel, "task-orphan").status,
                    "stopped",
                )
        finally:
            test_engine.dispose()

    def test_observe_chatgpt_task_returns_none_for_missing_id(self):
        with mock.patch.object(
            _task_store,
            "snapshot_if_present",
            return_value=None,
        ), mock.patch.object(
            tasks_module,
            "_finalize_orphan_tasks",
        ), mock.patch.object(
            tasks_module,
            "_get_persisted_task",
            return_value=None,
        ):
            self.assertIsNone(tasks_module.observe_chatgpt_task("task-missing"))

    def test_observe_persisted_terminal_task_preserves_exact_utc_timestamp(self):
        test_engine = db._create_database_engine("sqlite://")
        SQLModel.metadata.create_all(test_engine)
        updated_at = datetime(2026, 8, 2, 11, 0, tzinfo=timezone.utc)
        try:
            with Session(test_engine) as session:
                session.add(
                    TaskRunModel(
                        id="task-terminal-time",
                        platform="chatgpt",
                        source="schedule",
                        status="done",
                        updated_at=updated_at,
                    )
                )
                session.commit()

            with mock.patch.object(
                tasks_module,
                "engine",
                test_engine,
            ), mock.patch.object(
                _task_store,
                "snapshot_if_present",
                return_value=None,
            ):
                observation = tasks_module.observe_chatgpt_task(
                    "task-terminal-time"
                )

            self.assertEqual(observation["updated_at"], updated_at)
            self.assertFalse(observation["live"])
            self.assertFalse(observation["orphaned"])
        finally:
            test_engine.dispose()


if __name__ == "__main__":
    unittest.main()
