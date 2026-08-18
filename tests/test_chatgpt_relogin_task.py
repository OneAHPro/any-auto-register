import os
import threading
import time
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
        quota_alert_sender = mock.patch(
            "services.chatgpt_auto_relogin_alerts.send_quota_threshold_alert",
            return_value={
                "sent": False,
                "reason": "quota_alert_disabled",
                "threshold_usd": "0.00",
                "estimated_remaining_usd": "0.00",
            },
        )
        self.quota_alert_sender = quota_alert_sender.start()
        self.addCleanup(quota_alert_sender.stop)
        bark_alert_sender = mock.patch(
            "services.chatgpt_bark_alerts.send_bark_relogin_alert",
            return_value={
                "sent": False,
                "reason": "bark_disabled",
                "threshold": 5,
            },
        )
        self.bark_alert_sender = bark_alert_sender.start()
        self.addCleanup(bark_alert_sender.stop)
        bark_quota_alert_sender = mock.patch(
            "services.chatgpt_bark_alerts.send_bark_quota_threshold_alert",
            return_value={
                "sent": False,
                "reason": "bark_disabled",
                "threshold_usd": "0.00",
                "estimated_remaining_usd": "0.00",
            },
        )
        self.bark_quota_alert_sender = bark_quota_alert_sender.start()
        self.addCleanup(bark_quota_alert_sender.stop)
        final_quota_reader = mock.patch(
            "services.chatgpt_codex2api_health."
            "fetch_codex2api_quota_accounts",
            return_value=[],
        )
        self.final_quota_reader = final_quota_reader.start()
        self.addCleanup(final_quota_reader.stop)

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

    def test_request_supports_all_eligible_mfa_rotation_mode(self):
        request = ChatGPTReloginTaskRequest(
            all_eligible=True,
            rotate_mfa=True,
            concurrency=3,
        )

        self.assertEqual(request.account_ids, [])
        self.assertTrue(request.all_eligible)
        self.assertTrue(request.rotate_mfa)

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
        self.assertEqual(snapshot["meta"]["mode"], "remote_auth_monitor")
        self.assertTrue(snapshot["meta"]["automation"])
        self.assertEqual(snapshot["meta"]["account_ids"], list(range(1, 102)))
        self.assertEqual(snapshot["meta"]["concurrency"], 10)
        self.assertEqual(snapshot["meta"]["deleted_account_count"], 0)
        self.assertEqual(len(background_tasks.tasks), 1)
        queued = background_tasks.tasks[0]
        self.assertIs(queued.func, _run_chatgpt_relogin_task)
        self.assertEqual(
            queued.args,
            (task_id, list(range(1, 102)), 10),
        )

    def test_automatic_task_freezes_linked_credential_delete_setting_for_cycle(self):
        task_id = f"task-relogin-{uuid.uuid4().hex}"
        with mock.patch(
            "core.config_store.config_store.get",
            return_value="1",
        ):
            _create_chatgpt_relogin_task_record(
                task_id,
                [211],
                source="schedule",
                automation=True,
            )

        health = {
            211: {
                "account_id": 211,
                "email": "frozen@example.com",
                "state": "auth_failed",
                "remote_status": "unauthorized",
            }
        }
        with mock.patch(
            "core.config_store.config_store.get",
            return_value="0",
        ), mock.patch(
            "services.chatgpt_codex2api_health.inspect_codex2api_account_health",
            return_value=health,
        ), mock.patch(
            "services.chatgpt_codex2api_health.confirm_codex2api_auth_failure",
            side_effect=lambda value: value,
        ), mock.patch(
            "services.chatgpt_relogin.relogin_chatgpt_account",
            return_value={
                "ok": True,
                "relogin_ok": True,
                "stage": "completed",
                "account_id": 211,
                "email": "frozen@example.com",
                "message": "完整登录并同步成功",
            },
        ) as relogin:
            _run_chatgpt_relogin_task(task_id, [211])

        self.assertTrue(
            _task_store.snapshot(task_id)["meta"][
                "codex2api_delete_on_account_remove_enabled"
            ]
        )
        self.assertIs(
            relogin.call_args.kwargs[
                "codex2api_delete_on_account_remove_enabled"
            ],
            True,
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

    def test_all_eligible_mfa_route_resolves_ids_and_marks_rotation(self):
        request = ChatGPTReloginTaskRequest(
            all_eligible=True,
            rotate_mfa=True,
            concurrency=4,
        )
        background_tasks = BackgroundTasks()
        task_id = f"task-mfa-{uuid.uuid4().hex}"

        with mock.patch(
            "services.chatgpt_relogin.list_relogin_eligible_account_ids",
            return_value=[17, 18, 19],
        ), mock.patch(
            "api.tasks.enqueue_chatgpt_relogin_task",
            return_value=task_id,
        ) as enqueue, mock.patch.object(
            _task_store,
            "snapshot",
            return_value={"total": 3, "meta": {"concurrency": 3}},
        ):
            response = tasks_module.create_chatgpt_relogin_task(
                request,
                background_tasks,
            )

        self.assertEqual(
            response,
            {"task_id": task_id, "count": 3, "concurrency": 3},
        )
        enqueue.assert_called_once_with(
            [17, 18, 19],
            4,
            rotate_mfa=True,
            background_tasks=background_tasks,
        )

    def test_all_eligible_mfa_route_rejects_empty_eligible_set(self):
        request = ChatGPTReloginTaskRequest(all_eligible=True, rotate_mfa=True)
        with mock.patch(
            "services.chatgpt_relogin.list_relogin_eligible_account_ids",
            return_value=[],
        ), mock.patch("api.tasks.enqueue_chatgpt_relogin_task") as enqueue:
            with self.assertRaises(HTTPException) as error:
                tasks_module.create_chatgpt_relogin_task(
                    request,
                    BackgroundTasks(),
                )

        self.assertEqual(error.exception.status_code, 400)
        enqueue.assert_not_called()

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

    def test_mfa_rotation_task_counts_rotation_when_codex2api_sync_fails(self):
        task_id = f"task-mfa-{uuid.uuid4().hex}"
        _create_chatgpt_relogin_task_record(
            task_id,
            [17],
            concurrency=1,
            rotate_mfa=True,
        )

        with mock.patch(
            "services.chatgpt_relogin.relogin_chatgpt_account",
            return_value={
                "ok": False,
                "relogin_ok": True,
                "mfa_rotated": True,
                "stage": "codex2api_sync",
                "account_id": 17,
                "email": "rotated@example.com",
                "message": "重登成功，但 Codex2API 覆盖更新失败: 模型不支持",
            },
        ):
            _run_chatgpt_relogin_task(task_id, [17], concurrency=1)

        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(snapshot["success"], 1)
        self.assertEqual(len(snapshot.get("errors") or []), 0)
        self.assertTrue(any("MFA 重设成功" in line for line in snapshot["logs"]))
        self.assertTrue(any("Codex2API" in line for line in snapshot["logs"]))

    def test_automatic_task_skips_remote_healthy_account_without_refreshing_rt(self):
        task_id = f"task-relogin-{uuid.uuid4().hex}"
        _create_chatgpt_relogin_task_record(
            task_id,
            [17],
            source="schedule",
            automation=True,
        )

        with mock.patch(
            "services.chatgpt_codex2api_health."
            "inspect_codex2api_account_health",
            return_value={
                17: {
                    "account_id": 17,
                    "email": "healthy@example.com",
                    "state": "healthy",
                    "remote_id": 101,
                    "remote_status": "active",
                    "message": "Codex2API 鉴权状态正常（active）",
                }
            },
        ) as inspect_remote, mock.patch(
            "services.chatgpt_relogin.refresh_or_relogin_chatgpt_account",
        ) as refresh_first, mock.patch(
            "services.chatgpt_relogin.relogin_chatgpt_account"
        ) as full_login, mock.patch(
            "api.tasks._save_task_log"
        ) as save_task_log:
            _run_chatgpt_relogin_task(task_id, [17])

        inspect_remote.assert_called_once_with([17], quota_accounts=[])
        refresh_first.assert_not_called()
        full_login.assert_not_called()
        save_task_log.assert_not_called()
        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(snapshot["success"], 1)
        self.assertTrue(
            any("无需重登" in line for line in snapshot["logs"])
        )

    def test_automatic_task_records_all_probe_only_results_before_login(self):
        task_id = f"task-relogin-{uuid.uuid4().hex}"
        _create_chatgpt_relogin_task_record(
            task_id,
            [501, 502],
            source="schedule",
            automation=True,
            concurrency=1,
        )
        health = {
            501: {
                "account_id": 501,
                "email": "needs-login@example.com",
                "state": "remote_missing",
                "message": "Codex2API 未找到同邮箱账号",
            },
            502: {
                "account_id": 502,
                "email": "healthy@example.com",
                "state": "healthy",
                "remote_status": "active",
                "message": "Codex2API 鉴权状态正常（active）",
            },
        }
        observed = {}

        def full_login(account_id, **kwargs):
            del kwargs
            snapshot = _task_store.snapshot(task_id)
            observed["healthy_recorded_before_login"] = any(
                "[OK] 远端认证正常: healthy@example.com" in line
                for line in snapshot["logs"]
            )
            return {
                "ok": True,
                "relogin_ok": True,
                "stage": "completed",
                "account_id": account_id,
                "email": "needs-login@example.com",
                "message": "完整登录并同步成功",
            }

        with mock.patch(
            "services.chatgpt_codex2api_health.inspect_codex2api_account_health",
            return_value=health,
        ), mock.patch(
            "services.chatgpt_relogin.relogin_chatgpt_account",
            side_effect=full_login,
        ):
            _run_chatgpt_relogin_task(
                task_id,
                [501, 502],
                concurrency=1,
            )

        self.assertTrue(observed["healthy_recorded_before_login"])
        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(snapshot["success"], 2)
        self.assertTrue(
            any("全量探针结果已处理完成" in line for line in snapshot["logs"])
        )

    def test_automatic_task_full_logins_when_remote_credential_is_missing(self):
        task_id = f"task-relogin-{uuid.uuid4().hex}"
        _create_chatgpt_relogin_task_record(
            task_id,
            [117],
            source="schedule",
            automation=True,
        )
        health = {
            117: {
                "account_id": 117,
                "email": "remote-missing@example.com",
                "state": "remote_missing",
                "message": "Codex2API 未找到同邮箱账号，将执行一次完整登录确认",
            }
        }

        with mock.patch(
            "services.chatgpt_codex2api_health.inspect_codex2api_account_health",
            return_value=health,
        ), mock.patch(
            "services.chatgpt_codex2api_health.confirm_codex2api_auth_failure",
        ) as confirm_remote, mock.patch(
            "services.chatgpt_relogin.relogin_chatgpt_account",
            return_value={
                "ok": True,
                "relogin_ok": True,
                "stage": "completed",
                "account_id": 117,
                "email": "remote-missing@example.com",
                "message": "完整登录并同步成功",
            },
        ) as full_login, mock.patch(
            "api.tasks._save_task_log"
        ) as save_task_log:
            _run_chatgpt_relogin_task(task_id, [117])

        confirm_remote.assert_not_called()
        full_login.assert_called_once()
        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(snapshot["success"], 1)
        self.assertEqual(snapshot["meta"]["invalid_rt_count"], 0)
        self.assertEqual(snapshot["meta"]["relogin_failed_count"], 0)
        self.assertEqual(
            save_task_log.call_args.kwargs["detail"]["mode"],
            "full_login",
        )

    def test_remote_missing_login_confirmation_removes_deactivated_account(self):
        task_id = f"task-relogin-{uuid.uuid4().hex}"
        _create_chatgpt_relogin_task_record(
            task_id,
            [118],
            source="schedule",
            automation=True,
        )
        health = {
            118: {
                "account_id": 118,
                "email": "removed@example.com",
                "state": "remote_missing",
            }
        }

        with mock.patch(
            "services.chatgpt_codex2api_health.inspect_codex2api_account_health",
            return_value=health,
        ), mock.patch(
            "services.chatgpt_relogin.relogin_chatgpt_account",
            return_value={
                "ok": False,
                "relogin_ok": False,
                "account_removed": True,
                "stage": "account_removed",
                "account_id": 118,
                "email": "removed@example.com",
                "message": "账号已被删除或停用，本地记录已自动删除",
            },
        ) as full_login, mock.patch(
            "api.tasks._save_task_log"
        ) as save_task_log:
            _run_chatgpt_relogin_task(task_id, [118])

        full_login.assert_called_once()
        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(snapshot["success"], 0)
        self.assertEqual(snapshot["errors"], [])
        self.assertEqual(snapshot["meta"]["invalid_rt_count"], 0)
        self.assertEqual(snapshot["meta"]["relogin_failed_count"], 1)
        self.assertEqual(snapshot["meta"]["deleted_account_count"], 1)
        self.assertEqual(save_task_log.call_args.args[2], "removed")

    def test_remote_missing_transient_login_failure_keeps_local_account(self):
        task_id = f"task-relogin-{uuid.uuid4().hex}"
        _create_chatgpt_relogin_task_record(
            task_id,
            [119],
            source="schedule",
            automation=True,
        )
        health = {
            119: {
                "account_id": 119,
                "email": "retry@example.com",
                "state": "remote_missing",
            }
        }

        with mock.patch(
            "services.chatgpt_codex2api_health.inspect_codex2api_account_health",
            return_value=health,
        ), mock.patch(
            "services.chatgpt_relogin.relogin_chatgpt_account",
            return_value={
                "ok": False,
                "relogin_ok": False,
                "account_removed": False,
                "stage": "relogin",
                "account_id": 119,
                "email": "retry@example.com",
                "message": "邮箱验证码暂时未收到",
            },
        ) as full_login:
            _run_chatgpt_relogin_task(task_id, [119])

        full_login.assert_called_once()
        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(len(snapshot["errors"]), 1)
        self.assertEqual(snapshot["meta"]["invalid_rt_count"], 0)
        self.assertEqual(snapshot["meta"]["relogin_failed_count"], 1)
        self.assertEqual(snapshot["meta"]["deleted_account_count"], 0)
        self.assertFalse(
            full_login.call_args.kwargs.get(
                "remove_on_mailbox_otp_timeout",
                True,
            )
        )

    def test_timeout_removed_account_uses_generic_removal_log(self):
        task_id = f"task-relogin-{uuid.uuid4().hex}"
        _create_chatgpt_relogin_task_record(
            task_id,
            [121],
            source="schedule",
            automation=True,
        )
        health = {
            121: {
                "account_id": 121,
                "email": "timed-out@example.com",
                "state": "remote_missing",
            }
        }

        with mock.patch(
            "services.chatgpt_codex2api_health.inspect_codex2api_account_health",
            return_value=health,
        ), mock.patch(
            "services.chatgpt_relogin.relogin_chatgpt_account",
            return_value={
                "ok": False,
                "relogin_ok": False,
                "account_removed": True,
                "removal_reason": "mailbox_otp_timeout",
                "stage": "account_removed",
                "account_id": 121,
                "email": "timed-out@example.com",
                "message": "邮箱 OTP 等待满 180 秒仍未收到，账号已自动移除",
            },
        ), mock.patch("api.tasks._save_task_log") as save_task_log:
            _run_chatgpt_relogin_task(task_id, [121])

        snapshot = _task_store.snapshot(task_id)
        self.assertTrue(
            any(
                "[REMOVE] 本地记录已移除" in line
                and "邮箱 OTP 等待满 180 秒" in line
                for line in snapshot["logs"]
            )
        )
        self.assertEqual(snapshot["meta"]["deleted_account_count"], 1)
        self.assertEqual(save_task_log.call_args.args[2], "removed")

    def test_local_missing_record_does_not_attempt_a_remote_missing_login(self):
        task_id = f"task-relogin-{uuid.uuid4().hex}"
        _create_chatgpt_relogin_task_record(
            task_id,
            [120],
            source="schedule",
            automation=True,
        )

        with mock.patch(
            "services.chatgpt_codex2api_health.inspect_codex2api_account_health",
            return_value={
                120: {
                    "account_id": 120,
                    "email": "",
                    "state": "missing",
                    "message": "本地 ChatGPT 账号记录已不存在",
                }
            },
        ), mock.patch(
            "services.chatgpt_relogin.relogin_chatgpt_account",
        ) as full_login:
            _run_chatgpt_relogin_task(task_id, [120])

        full_login.assert_not_called()
        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(len(snapshot["errors"]), 1)
        self.assertEqual(snapshot["meta"]["relogin_failed_count"], 0)

    def test_automatic_task_stopped_during_probe_dispatches_no_accounts(self):
        task_id = f"task-relogin-{uuid.uuid4().hex}"
        _create_chatgpt_relogin_task_record(
            task_id,
            [171],
            source="schedule",
            automation=True,
        )

        def stop_during_probe(account_ids, **_kwargs):
            _task_store.control_for(task_id).request_stop()
            return {
                account_ids[0]: {
                    "account_id": account_ids[0],
                    "email": "stopped@example.com",
                    "state": "healthy",
                    "message": "Codex2API 鉴权状态正常",
                }
            }

        with mock.patch(
            "services.chatgpt_codex2api_health."
            "inspect_codex2api_account_health",
            side_effect=stop_during_probe,
        ), mock.patch(
            "concurrent.futures.ThreadPoolExecutor",
        ) as executor, mock.patch(
            "services.chatgpt_relogin.relogin_chatgpt_account",
        ) as full_login:
            _run_chatgpt_relogin_task(task_id, [171])

        executor.assert_not_called()
        full_login.assert_not_called()
        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(snapshot["status"], "stopped")
        self.assertEqual(snapshot["registered"], 0)
        self.final_quota_reader.assert_not_called()
        self.quota_alert_sender.assert_not_called()

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
        self.quota_alert_sender.assert_not_called()
        self.final_quota_reader.assert_not_called()
        self.assertEqual(_task_store.snapshot(task_id)["success"], 1)

    def test_automatic_task_lets_codex2api_self_refresh_before_full_login(self):
        task_id = f"task-relogin-{uuid.uuid4().hex}"
        _create_chatgpt_relogin_task_record(
            task_id,
            [19],
            source="schedule",
            automation=True,
        )
        initial = {
            "account_id": 19,
            "email": "recovered@example.com",
            "state": "auth_failed",
            "remote_id": 1019,
            "remote_status": "unauthorized",
            "remote_updated_at": "2026-08-03T08:00:00+08:00",
            "message": "Codex2API 已明确标记账号鉴权失效",
        }
        recovered = {
            **initial,
            "state": "healthy",
            "remote_status": "active",
            "resolution": "remote_refresh_recovered",
            "message": "Codex2API 已使用自身 RT 恢复鉴权，无需本地重登",
        }

        with mock.patch(
            "services.chatgpt_codex2api_health."
            "inspect_codex2api_account_health",
            return_value={19: initial},
        ), mock.patch(
            "services.chatgpt_codex2api_health."
            "confirm_codex2api_auth_failure",
            return_value=recovered,
        ) as confirm_remote, mock.patch(
            "services.chatgpt_relogin.relogin_chatgpt_account"
        ) as full_login:
            _run_chatgpt_relogin_task(task_id, [19])

        confirm_remote.assert_called_once_with(initial)
        full_login.assert_not_called()
        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(snapshot["success"], 1)
        self.assertEqual(snapshot["meta"]["invalid_rt_count"], 0)
        self.assertTrue(any("无需本地重登" in line for line in snapshot["logs"]))

    def test_automatic_task_records_cycle_counts_and_sends_one_summary_alert(self):
        task_id = f"task-relogin-{uuid.uuid4().hex}"
        account_ids = [201, 202, 203, 204]
        _create_chatgpt_relogin_task_record(
            task_id,
            account_ids,
            source="schedule",
            automation=True,
        )
        relogin_results = [
            {
                "ok": False,
                "relogin_ok": False,
                "stage": "relogin",
                "account_id": 201,
                "email": "failed@example.com",
                "message": "验证码登录失败",
            },
            {
                "ok": True,
                "relogin_ok": True,
                "stage": "completed",
                "account_id": 202,
                "email": "recovered@example.com",
                "message": "完整登录并同步成功",
            },
            {
                "ok": False,
                "relogin_ok": True,
                "stage": "codex2api_sync",
                "account_id": 203,
                "email": "sync-failed@example.com",
                "message": "登录成功但同步失败",
            },
        ]
        health_snapshot = {
            account_id: {
                "account_id": account_id,
                "email": f"account-{account_id}@example.com",
                "state": "auth_failed",
                "remote_id": account_id + 1000,
                "remote_status": "unauthorized",
                "message": "Codex2API 已明确标记账号鉴权失效",
            }
            for account_id in account_ids[:3]
        }
        health_snapshot[204] = {
            "account_id": 204,
            "email": "retry@example.com",
            "state": "deferred",
            "remote_id": 1204,
            "remote_status": "error",
            "message": "Codex2API 账号状态为临时错误，等待下一轮复查",
        }
        self.alert_sender.return_value = {
            "sent": True,
            "reason": "sent",
            "threshold": 2,
        }

        confirmed_failures = {
            account_id: {
                **health_snapshot[account_id],
                "resolution": "remote_refresh_confirmed_failure",
                "message": "Codex2API 自刷新后仍鉴权失败",
            }
            for account_id in account_ids[:3]
        }

        with mock.patch(
            "services.chatgpt_codex2api_health."
            "inspect_codex2api_account_health",
            return_value=health_snapshot,
        ), mock.patch(
            "services.chatgpt_codex2api_health."
            "confirm_codex2api_auth_failure",
            side_effect=lambda health: confirmed_failures[health["account_id"]],
        ) as confirm_remote, mock.patch(
            "services.chatgpt_relogin.relogin_chatgpt_account",
            side_effect=relogin_results,
        ) as full_login, mock.patch(
            "services.chatgpt_relogin.refresh_or_relogin_chatgpt_account"
        ) as refresh_first:
            _run_chatgpt_relogin_task(task_id, account_ids, concurrency=1)

        self.assertEqual(full_login.call_count, 3)
        self.assertEqual(confirm_remote.call_count, 3)
        refresh_first.assert_not_called()
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
            successful_accounts=1,
            invalid_rt_count=3,
            relogin_failed_count=1,
            deleted_account_count=0,
            quota_eligible_failure_count=0,
            quota_exhausted_failure_count=0,
            quota_report=mock.ANY,
        )
        self.bark_alert_sender.assert_called_once_with(
            task_id=task_id,
            quota_report=mock.ANY,
            quota_eligible_failure_count=0,
            quota_exhausted_failure_count=0,
            relogin_failed_count=1,
            deleted_account_count=0,
        )
        self.assertTrue(
            any(
                "[ALERT] 本轮重登失败告警邮件已发送" in line
                for line in snapshot["logs"]
            )
        )

    def test_automatic_task_uses_one_fresh_final_quota_report_for_both_alerts(self):
        task_id = f"task-relogin-{uuid.uuid4().hex}"
        _create_chatgpt_relogin_task_record(
            task_id,
            [241],
            source="schedule",
            automation=True,
        )
        events: list[str] = []
        final_rows = [
            {
                "email": "a@example.com",
                "remote_status": "active",
                "usage_percent_7d": 53,
                "billed_7d": 68.26,
            },
            {
                "email": "b@example.com",
                "remote_status": "rate_limited",
                "usage_percent_7d": 68,
                "billed_7d": 81.42,
            },
        ]

        def relogin(*_args, **_kwargs):
            events.append("relogin")
            return {
                "ok": True,
                "relogin_ok": True,
                "stage": "completed",
                "account_id": 241,
                "email": "a@example.com",
                "message": "完整登录并同步成功",
            }

        def fetch_final_rows():
            self.assertEqual(events, ["relogin"])
            events.append("final_quota")
            return final_rows

        self.final_quota_reader.side_effect = fetch_final_rows
        self.alert_sender.return_value = {
            "sent": True,
            "reason": "sent",
            "threshold": 5,
        }
        self.quota_alert_sender.return_value = {
            "sent": True,
            "reason": "sent",
            "threshold_usd": "120.00",
            "estimated_remaining_usd": "98.85",
        }
        self.bark_alert_sender.return_value = {
            "sent": True,
            "reason": "sent",
            "threshold": 5,
        }
        self.bark_quota_alert_sender.return_value = {
            "sent": True,
            "reason": "sent",
            "threshold_usd": "120.00",
            "estimated_remaining_usd": "98.85",
        }

        with mock.patch(
            "services.chatgpt_codex2api_health."
            "inspect_codex2api_account_health",
            return_value={
                241: {
                    "account_id": 241,
                    "email": "a@example.com",
                    "state": "remote_missing",
                    "message": "Codex2API 未找到同邮箱账号",
                }
            },
        ), mock.patch(
            "services.chatgpt_relogin.relogin_chatgpt_account",
            side_effect=relogin,
        ):
            _run_chatgpt_relogin_task(task_id, [241])

        self.assertEqual(events, ["relogin", "final_quota"])
        self.final_quota_reader.assert_called_once_with()
        relogin_report = self.alert_sender.call_args.kwargs["quota_report"]
        quota_report = self.quota_alert_sender.call_args.kwargs["quota_report"]
        self.assertIs(relogin_report, quota_report)
        bark_relogin_report = self.bark_alert_sender.call_args.kwargs[
            "quota_report"
        ]
        bark_quota_report = self.bark_quota_alert_sender.call_args.kwargs[
            "quota_report"
        ]
        self.assertIs(relogin_report, bark_relogin_report)
        self.assertIs(relogin_report, bark_quota_report)
        self.assertEqual(quota_report.remote_account_count, 2)
        self.assertEqual(quota_report.account_count, 2)
        self.assertEqual(str(quota_report.estimated_remaining_usd), "98.85")

        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(snapshot["status"], "done")
        meta = snapshot["meta"]
        self.assertEqual(meta["codex2api_account_count"], 2)
        self.assertEqual(meta["available_quota_account_count"], 2)
        self.assertEqual(meta["estimated_remaining_usd"], "98.85")
        self.assertTrue(meta["quota_data_available"])
        self.assertTrue(meta["quota_alert_sent"])
        self.assertEqual(meta["quota_alert_reason"], "sent")
        self.assertEqual(meta["quota_alert_threshold_usd"], "120.00")
        self.assertTrue(meta["bark_alert_sent"])
        self.assertEqual(meta["bark_alert_reason"], "sent")
        self.assertTrue(meta["bark_quota_alert_sent"])
        self.assertEqual(meta["bark_quota_alert_reason"], "sent")
        logs = "\n".join(snapshot["logs"])
        self.assertIn("本轮重登失败告警邮件已发送", logs)
        self.assertIn("本轮剩余额度不足告警邮件已发送", logs)
        self.assertIn("本轮重登失败 Bark 强提醒已发送", logs)
        self.assertIn("本轮剩余额度不足 Bark 强提醒已发送", logs)

    def test_final_quota_query_failure_uses_initial_report_for_relogin_alert(self):
        task_id = f"task-relogin-{uuid.uuid4().hex}"
        _create_chatgpt_relogin_task_record(
            task_id,
            [242],
            source="schedule",
            automation=True,
        )
        initial_rows = [
            {
                "email": "initial@example.com",
                "remote_status": "active",
                "usage_percent_7d": 53,
                "billed_7d": 68.26,
            }
        ]

        def inspect(_account_ids, *, quota_accounts=None, **_kwargs):
            quota_accounts.extend(initial_rows)
            return {
                242: {
                    "account_id": 242,
                    "email": "initial@example.com",
                    "state": "healthy",
                    "remote_status": "active",
                    "message": "Codex2API 鉴权状态正常",
                }
            }

        self.final_quota_reader.side_effect = RuntimeError("secret detail")
        with mock.patch(
            "services.chatgpt_codex2api_health."
            "inspect_codex2api_account_health",
            side_effect=inspect,
        ):
            _run_chatgpt_relogin_task(task_id, [242])

        report = self.alert_sender.call_args.kwargs["quota_report"]
        self.assertEqual(report.remote_account_count, 1)
        self.assertEqual(report.account_count, 1)
        self.assertEqual(str(report.estimated_remaining_usd), "60.53")
        bark_report = self.bark_alert_sender.call_args.kwargs["quota_report"]
        self.assertIs(report, bark_report)
        self.quota_alert_sender.assert_not_called()
        self.bark_quota_alert_sender.assert_not_called()
        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(snapshot["status"], "done")
        self.assertEqual(snapshot["meta"]["quota_alert_reason"], "quota_query_failed")
        self.assertEqual(
            snapshot["meta"]["quota_query_error_type"],
            "RuntimeError",
        )
        self.assertFalse(snapshot["meta"]["quota_data_available"])
        self.assertNotIn("secret detail", "\n".join(snapshot["logs"]))

    def test_quota_alert_exception_does_not_change_terminal_task_outcome(self):
        task_id = f"task-relogin-{uuid.uuid4().hex}"
        _create_chatgpt_relogin_task_record(
            task_id,
            [243],
            source="schedule",
            automation=True,
        )
        self.final_quota_reader.return_value = [
            {
                "email": "healthy@example.com",
                "remote_status": "active",
                "usage_percent_7d": 50,
                "billed_7d": 50,
            }
        ]
        self.quota_alert_sender.side_effect = RuntimeError("smtp secret detail")
        self.bark_quota_alert_sender.side_effect = RuntimeError(
            "bark secret detail"
        )

        with mock.patch(
            "services.chatgpt_codex2api_health."
            "inspect_codex2api_account_health",
            return_value={
                243: {
                    "account_id": 243,
                    "email": "healthy@example.com",
                    "state": "healthy",
                    "remote_status": "active",
                    "message": "Codex2API 鉴权状态正常",
                }
            },
        ):
            _run_chatgpt_relogin_task(task_id, [243])

        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(snapshot["status"], "done")
        self.assertEqual(snapshot["success"], 1)
        self.assertFalse(snapshot["meta"]["quota_alert_sent"])
        self.assertEqual(snapshot["meta"]["quota_alert_reason"], "send_failed")
        self.assertEqual(
            snapshot["meta"]["quota_alert_error_type"],
            "RuntimeError",
        )
        self.assertNotIn("smtp secret detail", "\n".join(snapshot["logs"]))
        self.assertFalse(snapshot["meta"]["bark_quota_alert_sent"])
        self.assertEqual(
            snapshot["meta"]["bark_quota_alert_reason"],
            "send_failed",
        )
        self.assertEqual(
            snapshot["meta"]["bark_quota_alert_error_type"],
            "RuntimeError",
        )
        self.assertNotIn("bark secret detail", "\n".join(snapshot["logs"]))

    def test_automatic_task_counts_quota_available_and_exhausted_failures(self):
        task_id = f"task-relogin-{uuid.uuid4().hex}"
        account_ids = list(range(261, 267))
        _create_chatgpt_relogin_task_record(
            task_id,
            account_ids,
            source="schedule",
            automation=True,
        )
        health = {
            account_id: {
                "account_id": account_id,
                "email": f"account-{account_id}@example.com",
                "state": "auth_failed",
                "remote_id": account_id + 1000,
                "remote_status": "unauthorized",
                "usage_percent_7d": 100 if index < 3 else 50,
                "billed_7d": 50.0,
            }
            for index, account_id in enumerate(account_ids)
        }

        def inspect_health(_account_ids, *, quota_accounts=None, **_kwargs):
            if quota_accounts is not None:
                quota_accounts.extend(health.values())
            return health

        self.alert_sender.return_value = {
            "sent": False,
            "reason": "below_threshold",
            "threshold": 5,
        }
        with mock.patch(
            "services.chatgpt_codex2api_health.inspect_codex2api_account_health",
            side_effect=inspect_health,
        ), mock.patch(
            "services.chatgpt_codex2api_health.confirm_codex2api_auth_failure",
            side_effect=lambda value: value,
        ), mock.patch(
            "services.chatgpt_relogin.relogin_chatgpt_account",
            side_effect=[
                {
                    "ok": False,
                    "relogin_ok": False,
                    "stage": "relogin",
                    "account_id": account_id,
                    "email": f"account-{account_id}@example.com",
                    "message": "验证码登录失败",
                }
                for account_id in account_ids
            ],
        ):
            _run_chatgpt_relogin_task(task_id, account_ids, concurrency=1)

        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(snapshot["meta"]["relogin_failed_count"], 6)
        self.assertEqual(snapshot["meta"]["quota_eligible_failure_count"], 3)
        self.assertEqual(snapshot["meta"]["quota_exhausted_failure_count"], 3)
        self.alert_sender.assert_called_once_with(
            task_id=task_id,
            total_accounts=6,
            successful_accounts=0,
            invalid_rt_count=6,
            relogin_failed_count=6,
            deleted_account_count=0,
            quota_eligible_failure_count=3,
            quota_exhausted_failure_count=3,
            quota_report=mock.ANY,
        )

    def test_automatic_task_keeps_all_exhausted_failures_below_alert_threshold(self):
        task_id = f"task-relogin-{uuid.uuid4().hex}"
        account_ids = list(range(271, 277))
        _create_chatgpt_relogin_task_record(
            task_id,
            account_ids,
            source="schedule",
            automation=True,
        )
        health = {
            account_id: {
                "account_id": account_id,
                "email": f"account-{account_id}@example.com",
                "state": "auth_failed",
                "remote_status": "unauthorized",
                "usage_percent_7d": 100,
                "billed_7d": 50.0,
            }
            for account_id in account_ids
        }

        def inspect_health(_account_ids, *, quota_accounts=None, **_kwargs):
            if quota_accounts is not None:
                quota_accounts.extend(health.values())
            return health

        with mock.patch(
            "services.chatgpt_codex2api_health.inspect_codex2api_account_health",
            side_effect=inspect_health,
        ), mock.patch(
            "services.chatgpt_codex2api_health.confirm_codex2api_auth_failure",
            side_effect=lambda value: value,
        ), mock.patch(
            "services.chatgpt_relogin.relogin_chatgpt_account",
            side_effect=[
                {
                    "ok": False,
                    "relogin_ok": False,
                    "stage": "relogin",
                    "account_id": account_id,
                    "email": f"account-{account_id}@example.com",
                    "message": "验证码登录失败",
                }
                for account_id in account_ids
            ],
        ):
            _run_chatgpt_relogin_task(task_id, account_ids, concurrency=1)

        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(snapshot["meta"]["relogin_failed_count"], 6)
        self.assertEqual(snapshot["meta"]["quota_eligible_failure_count"], 0)
        self.assertEqual(snapshot["meta"]["quota_exhausted_failure_count"], 6)
        self.assertFalse(snapshot["meta"]["alert_sent"])
        self.assertTrue(
            any(
                "仍有额度的重登失败数未达到配置阈值" in line
                for line in snapshot["logs"]
            )
        )

    def test_automatic_task_counts_removed_accounts_inside_relogin_failures(self):
        task_id = f"task-relogin-{uuid.uuid4().hex}"
        account_ids = list(range(301, 321))
        with mock.patch(
            "core.config_store.config_store.get",
            return_value="1",
        ):
            _create_chatgpt_relogin_task_record(
                task_id,
                account_ids,
                source="schedule",
                automation=True,
            )
        health = {
            account_id: {
                "account_id": account_id,
                "email": f"account-{account_id}@example.com",
                "state": "auth_failed",
                "remote_status": "unauthorized",
            }
            for account_id in account_ids
        }
        ordinary_failures = [
            {
                "ok": False,
                "relogin_ok": False,
                "stage": "relogin",
                "account_id": account_id,
                "email": f"failed-{account_id}@example.com",
                "message": "验证码登录失败",
            }
            for account_id in account_ids[:3]
        ]
        removed = [
            {
                "ok": False,
                "relogin_ok": False,
                "account_removed": True,
                "stage": "account_removed",
                "account_id": account_id,
                "email": f"removed-{account_id}@example.com",
                "message": "账号已被删除或停用，本地记录已自动删除",
            }
            for account_id in account_ids[3:]
        ]
        self.alert_sender.return_value = {
            "sent": True,
            "reason": "sent",
            "threshold": 20,
        }

        with mock.patch(
            "services.chatgpt_codex2api_health.inspect_codex2api_account_health",
            return_value=health,
        ), mock.patch(
            "services.chatgpt_codex2api_health.confirm_codex2api_auth_failure",
            side_effect=lambda value: value,
        ), mock.patch(
            "services.chatgpt_relogin.relogin_chatgpt_account",
            side_effect=[*ordinary_failures, *removed],
        ) as relogin, mock.patch("api.tasks._save_task_log") as save_task_log:
            _run_chatgpt_relogin_task(task_id, account_ids, concurrency=1)

        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(snapshot["registered"], 20)
        self.assertEqual(len(snapshot["errors"]), 3)
        self.assertEqual(snapshot["meta"]["relogin_failed_count"], 20)
        self.assertEqual(snapshot["meta"]["deleted_account_count"], 17)
        self.assertTrue(
            all(
                call.kwargs[
                    "codex2api_delete_on_account_remove_enabled"
                ] is True
                for call in relogin.call_args_list
            )
        )
        removed_logs = [
            call for call in save_task_log.call_args_list
            if len(call.args) >= 3 and call.args[2] == "removed"
        ]
        self.assertEqual(len(removed_logs), 17)
        self.alert_sender.assert_called_once_with(
            task_id=task_id,
            total_accounts=20,
            successful_accounts=0,
            invalid_rt_count=20,
            relogin_failed_count=20,
            deleted_account_count=17,
            quota_eligible_failure_count=0,
            quota_exhausted_failure_count=0,
            quota_report=mock.ANY,
        )

    def test_cleanup_failure_counts_as_relogin_failure_but_not_deleted(self):
        task_id = f"task-relogin-{uuid.uuid4().hex}"
        _create_chatgpt_relogin_task_record(
            task_id,
            [321],
            source="schedule",
            automation=True,
        )
        health = {
            321: {
                "account_id": 321,
                "email": "cleanup-failed@example.com",
                "state": "auth_failed",
                "remote_status": "unauthorized",
            }
        }
        with mock.patch(
            "services.chatgpt_codex2api_health.inspect_codex2api_account_health",
            return_value=health,
        ), mock.patch(
            "services.chatgpt_codex2api_health.confirm_codex2api_auth_failure",
            side_effect=lambda value: value,
        ), mock.patch(
            "services.chatgpt_relogin.relogin_chatgpt_account",
            return_value={
                "ok": False,
                "relogin_ok": False,
                "account_removed": False,
                "stage": "account_remove_failed",
                "account_id": 321,
                "email": "cleanup-failed@example.com",
                "message": "远端认证删除失败",
            },
        ):
            _run_chatgpt_relogin_task(task_id, [321])

        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(snapshot["meta"]["relogin_failed_count"], 1)
        self.assertEqual(snapshot["meta"]["deleted_account_count"], 0)
        self.assertEqual(len(snapshot["errors"]), 1)

    def test_automatic_alert_exception_does_not_change_task_outcome(self):
        task_id = f"task-relogin-{uuid.uuid4().hex}"
        _create_chatgpt_relogin_task_record(
            task_id,
            [205],
            source="schedule",
            automation=True,
        )
        self.alert_sender.side_effect = RuntimeError("smtp secret detail")
        self.bark_alert_sender.return_value = {
            "sent": True,
            "reason": "sent",
            "threshold": 5,
        }

        with mock.patch(
            "services.chatgpt_codex2api_health."
            "inspect_codex2api_account_health",
            return_value={
                205: {
                    "account_id": 205,
                    "email": "valid@example.com",
                    "state": "healthy",
                    "remote_id": 1205,
                    "remote_status": "active",
                    "message": "Codex2API 鉴权状态正常（active）",
                }
            },
        ), mock.patch(
            "services.chatgpt_relogin.relogin_chatgpt_account"
        ) as full_login, mock.patch(
            "services.chatgpt_relogin.refresh_or_relogin_chatgpt_account"
        ) as refresh_first:
            _run_chatgpt_relogin_task(task_id, [205])

        full_login.assert_not_called()
        refresh_first.assert_not_called()
        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(snapshot["status"], "done")
        self.assertEqual(snapshot["success"], 1)
        self.assertFalse(snapshot["meta"]["alert_sent"])
        self.assertEqual(snapshot["meta"]["alert_reason"], "send_failed")
        self.assertTrue(snapshot["meta"]["bark_alert_sent"])
        self.assertEqual(snapshot["meta"]["bark_alert_reason"], "sent")
        self.bark_alert_sender.assert_called_once()
        self.assertNotIn("smtp secret detail", "\n".join(snapshot["logs"]))

    def test_terminal_snapshot_is_persisted_before_automatic_alert(self):
        task_id = f"task-relogin-{uuid.uuid4().hex}"
        _create_chatgpt_relogin_task_record(
            task_id,
            [206],
            source="schedule",
            automation=True,
        )
        events: list[str] = []

        def record_persistence(current_task_id):
            status = _task_store.snapshot(current_task_id)["status"]
            if status in {"done", "failed", "stopped"}:
                events.append("terminal_persisted")

        def record_alert(**_kwargs):
            events.append("alert_started")
            return {
                "sent": False,
                "reason": "below_threshold",
                "threshold": 5,
            }

        self.alert_sender.side_effect = record_alert
        with mock.patch(
            "services.chatgpt_codex2api_health."
            "inspect_codex2api_account_health",
            return_value={
                206: {
                    "account_id": 206,
                    "email": "valid@example.com",
                    "state": "healthy",
                    "remote_status": "active",
                    "message": "Codex2API 鉴权状态正常（active）",
                }
            },
        ), mock.patch(
            "api.tasks._persist_task_snapshot",
            side_effect=record_persistence,
        ):
            _run_chatgpt_relogin_task(task_id, [206])

        self.assertIn("terminal_persisted", events)
        self.assertLess(
            events.index("terminal_persisted"),
            events.index("alert_started"),
        )
        snapshot = _task_store.snapshot(task_id)
        self.assertTrue(
            any(
                "邮件告警未触发：本轮仍有额度的重登失败数未达到配置阈值" in line
                for line in snapshot["logs"]
            )
        )

    def test_stop_accepted_at_completion_boundary_suppresses_alert(self):
        task_id = f"task-relogin-{uuid.uuid4().hex}"
        _create_chatgpt_relogin_task_record(
            task_id,
            [207],
            source="schedule",
            automation=True,
        )

        real_finish = _task_store.finish

        def stop_before_finishing(*args, **kwargs):
            _task_store.control_for(task_id).request_stop_once()
            return real_finish(*args, **kwargs)

        with mock.patch(
            "services.chatgpt_codex2api_health."
            "inspect_codex2api_account_health",
            return_value={
                207: {
                    "account_id": 207,
                    "email": "valid@example.com",
                    "state": "healthy",
                    "remote_status": "active",
                    "message": "Codex2API 鉴权状态正常（active）",
                }
            },
        ), mock.patch.object(
            _task_store,
            "finish",
            side_effect=stop_before_finishing,
        ):
            _run_chatgpt_relogin_task(task_id, [207])

        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(snapshot["status"], "stopped")
        self.assertFalse(snapshot["meta"]["alert_sent"])
        self.assertEqual(snapshot["meta"]["alert_reason"], "task_stopped")
        self.assertFalse(snapshot["meta"]["bark_alert_sent"])
        self.assertEqual(snapshot["meta"]["bark_alert_reason"], "task_stopped")
        self.assertFalse(snapshot["meta"]["bark_quota_alert_sent"])
        self.assertEqual(
            snapshot["meta"]["bark_quota_alert_reason"],
            "task_stopped",
        )
        self.assertFalse(
            any("[ALERT]" in line for line in snapshot["logs"])
        )
        self.alert_sender.assert_not_called()
        self.bark_alert_sender.assert_not_called()
        self.bark_quota_alert_sender.assert_not_called()

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
        self.assertEqual(snapshot["errors"], [])
        self.assertTrue(
            any(
                "[REMOVE]" in line and "本地记录已移除" in line
                for line in snapshot["logs"]
            )
        )
        self.assertTrue(
            save_task_log.call_args.kwargs["detail"]["account_removed"]
        )
        self.assertEqual(save_task_log.call_args.args[2], "removed")

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

    def test_explicit_stop_escalates_a_stuck_automatic_task(self):
        task_id = f"task-relogin-{uuid.uuid4().hex}"
        _create_chatgpt_relogin_task_record(
            task_id,
            [201],
            source="schedule",
            automation=True,
        )
        _task_store.mark_running(task_id)
        forced_exit = threading.Event()
        exit_codes: list[int] = []

        def record_exit(code: int) -> None:
            exit_codes.append(code)
            forced_exit.set()

        with mock.patch.object(
            tasks_module,
            "_finalize_orphan_tasks",
        ) as finalize_orphans, mock.patch.dict(
            os.environ,
            {"CHATGPT_AUTOMATION_FORCE_STOP_SECONDS": "0.05"},
        ), mock.patch(
            "os._exit",
            side_effect=record_exit,
        ), mock.patch.object(
            tasks_module,
            "_append_task_log_best_effort",
        ) as append_watchdog_log, mock.patch.object(
            tasks_module,
            "_persist_task_snapshot_best_effort",
        ) as persist_watchdog_snapshot:
            tasks_module.stop_task(task_id)
            persist_calls_before_watchdog = (
                persist_watchdog_snapshot.call_count
            )
            self.assertTrue(forced_exit.wait(timeout=0.5))

        self.assertEqual(exit_codes, [75])
        finalize_orphans.assert_not_called()
        append_watchdog_log.assert_not_called()
        # The stop endpoint persists; the watchdog itself must not touch
        # SQLite because a wedged DB lock may be the reason it is firing.
        self.assertEqual(
            persist_watchdog_snapshot.call_count,
            persist_calls_before_watchdog,
        )
        snapshot = _task_store.snapshot(task_id)
        self.assertTrue(snapshot["control"]["stop_requested"])

    def test_force_stop_grace_rejects_non_finite_environment_values(self):
        for raw_value in ("nan", "inf", "-inf"):
            with self.subTest(raw_value=raw_value), mock.patch.dict(
                os.environ,
                {"CHATGPT_AUTOMATION_FORCE_STOP_SECONDS": raw_value},
            ):
                self.assertEqual(
                    tasks_module._automation_force_stop_seconds(),
                    30.0,
                )

    def test_stop_watchdog_keeps_running_until_automation_runner_finishes(self):
        task_id = f"task-relogin-{uuid.uuid4().hex}"
        _create_chatgpt_relogin_task_record(
            task_id,
            [202],
            source="schedule",
            automation=True,
        )
        _task_store.mark_running(task_id)
        tasks_module._mark_automation_runner_started(task_id)
        forced_exit = threading.Event()

        with mock.patch.dict(
            os.environ,
            {"CHATGPT_AUTOMATION_FORCE_STOP_SECONDS": "0.05"},
        ), mock.patch(
            "os._exit",
            side_effect=lambda code: forced_exit.set(),
        ) as exit_mock:
            self.assertTrue(
                tasks_module._arm_automation_stop_watchdog(task_id)
            )
            _task_store.finish(
                task_id,
                status="stopped",
                success=0,
                registered=0,
                skipped=0,
                errors=[],
            )
            self.assertTrue(forced_exit.wait(timeout=0.5))

        exit_mock.assert_called_once_with(75)
        tasks_module._mark_automation_runner_finished(task_id)

    def test_stop_watchdog_stands_down_after_automation_runner_finishes(self):
        task_id = f"task-relogin-{uuid.uuid4().hex}"
        _create_chatgpt_relogin_task_record(
            task_id,
            [205],
            source="schedule",
            automation=True,
        )
        _task_store.mark_running(task_id)
        tasks_module._mark_automation_runner_started(task_id)

        with mock.patch.dict(
            os.environ,
            {"CHATGPT_AUTOMATION_FORCE_STOP_SECONDS": "0.05"},
        ), mock.patch("os._exit") as forced_exit:
            self.assertTrue(
                tasks_module._arm_automation_stop_watchdog(task_id)
            )
            _task_store.finish(
                task_id,
                status="stopped",
                success=0,
                registered=0,
                skipped=0,
                errors=[],
            )
            tasks_module._mark_automation_runner_finished(task_id)
            time.sleep(0.1)

        forced_exit.assert_not_called()

    def test_watchdog_start_failure_immediately_recycles_the_process(self):
        task_id = f"task-relogin-{uuid.uuid4().hex}"
        _create_chatgpt_relogin_task_record(
            task_id,
            [206],
            source="schedule",
            automation=True,
        )
        thread = mock.Mock()
        thread.start.side_effect = RuntimeError("thread start failed")

        with mock.patch(
            "api.tasks.threading.Thread",
            return_value=thread,
        ), mock.patch("os._exit") as forced_exit:
            armed = tasks_module._arm_automation_stop_watchdog(task_id)

        self.assertFalse(armed)
        forced_exit.assert_called_once_with(75)
        self.assertNotIn(
            task_id,
            tasks_module._automation_stop_watchdog_tasks,
        )

    def test_terminal_snapshot_can_still_stop_an_active_automation_runner(self):
        task_id = f"task-relogin-{uuid.uuid4().hex}"
        _create_chatgpt_relogin_task_record(
            task_id,
            [207],
            source="schedule",
            automation=True,
        )
        _task_store.mark_running(task_id)
        tasks_module._mark_automation_runner_started(task_id)
        _task_store.finish(
            task_id,
            status="done",
            success=1,
            registered=1,
            skipped=0,
            errors=[],
        )

        try:
            with mock.patch.object(
                tasks_module,
                "_arm_automation_stop_watchdog",
                return_value=True,
            ) as arm_watchdog, mock.patch.object(
                tasks_module,
                "_persist_task_snapshot_best_effort",
            ):
                response = tasks_module.stop_task(task_id)

            self.assertTrue(response["ok"])
            arm_watchdog.assert_called_once_with(task_id)
        finally:
            tasks_module._mark_automation_runner_finished(task_id)

    def test_coordinator_observes_stop_while_account_step_is_blocked(self):
        task_id = f"task-relogin-{uuid.uuid4().hex}"
        _create_chatgpt_relogin_task_record(task_id, [203])
        started = threading.Event()
        release = threading.Event()

        def blocked_relogin(account_id, **kwargs):
            started.set()
            release.wait(timeout=1)
            kwargs["task_control"].checkpoint(
                attempt_id=kwargs["attempt_id"]
            )

        worker = threading.Thread(
            target=_run_chatgpt_relogin_task,
            args=(task_id, [203]),
        )
        with mock.patch(
            "services.chatgpt_relogin.relogin_chatgpt_account",
            side_effect=blocked_relogin,
        ):
            worker.start()
            self.assertTrue(started.wait(timeout=0.5))
            _task_store.control_for(task_id).request_stop()

            deadline = time.monotonic() + 0.5
            while time.monotonic() < deadline:
                snapshot = _task_store.snapshot(task_id)
                if any(
                    "停止请求已生效" in line
                    for line in snapshot["logs"]
                ):
                    break
                time.sleep(0.01)
            else:
                self.fail("coordinator did not observe the stop request")

            release.set()
            worker.join(timeout=1)

        self.assertFalse(worker.is_alive())
        self.assertEqual(_task_store.snapshot(task_id)["status"], "stopped")

    def test_repeated_stop_request_logs_only_once(self):
        task_id = f"task-relogin-{uuid.uuid4().hex}"
        _create_chatgpt_relogin_task_record(task_id, [204])
        _task_store.mark_running(task_id)

        with mock.patch.object(
            tasks_module,
            "_finalize_orphan_tasks",
        ), mock.patch.object(
            tasks_module,
            "_arm_automation_stop_watchdog",
        ):
            tasks_module.stop_task(task_id)
            tasks_module.stop_task(task_id)

        stop_logs = [
            line
            for line in _task_store.snapshot(task_id)["logs"]
            if "收到手动停止任务请求" in line
        ]
        self.assertEqual(len(stop_logs), 1)

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
        # The executor may over-provision workers so OTP waits do not occupy
        # the requested active-login slots; completion order is therefore not
        # part of the task contract.
        self.assertCountEqual(relogin_calls, [27, 28, 29])
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
        real_request_stop_once = auto_control.request_stop_once

        def request_stop_once() -> bool:
            first_request = real_request_stop_once()
            stop_requested.set()
            return first_request

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
        remote_failures = {
            account_id: {
                "account_id": account_id,
                "email": f"account-{account_id}@example.com",
                "state": "auth_failed",
                "remote_id": 1000 + account_id,
                "remote_status": "unauthorized",
                "message": "Codex2API 已明确标记账号鉴权失效",
            }
            for account_id in (51, 52, 53)
        }

        with mock.patch(
            "api.tasks.chatgpt_task_gate",
            gate,
        ), mock.patch(
            "services.chatgpt_codex2api_health."
            "inspect_codex2api_account_health",
            return_value=remote_failures,
        ), mock.patch(
            "services.chatgpt_codex2api_health."
            "confirm_codex2api_auth_failure",
            side_effect=lambda health: {
                **health,
                "resolution": "remote_refresh_confirmed_failure",
            },
        ), mock.patch(
            "services.chatgpt_relogin.relogin_chatgpt_account",
            side_effect=relogin,
        ), mock.patch(
            "services.chatgpt_relogin.refresh_or_relogin_chatgpt_account",
            side_effect=relogin,
        ), mock.patch.object(
            auto_control,
            "request_stop_once",
            side_effect=request_stop_once,
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

    def test_observe_terminal_task_uses_immutable_completion_time(self):
        completed_at = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
        updated_at = datetime(2026, 8, 2, 12, 5, tzinfo=timezone.utc)
        memory = {
            "id": "task-terminal",
            "platform": "chatgpt",
            "status": "done",
            "meta": {"completed_at": completed_at.timestamp()},
            "updated_at": updated_at.timestamp(),
        }

        with mock.patch.object(
            _task_store,
            "snapshot_if_present",
            return_value=memory,
        ):
            observation = tasks_module.observe_chatgpt_task("task-terminal")

        self.assertEqual(observation["completed_at"], completed_at)
        self.assertEqual(observation["updated_at"], updated_at)
        self.assertFalse(observation["live"])

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
