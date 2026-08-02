import threading
import unittest
import uuid
from unittest import mock
from unittest.mock import patch

from api.tasks import RegisterTaskRequest, _create_task_record, _run_register, _task_store
from core.base_mailbox import BaseMailbox, MailboxAccount
from core.base_platform import Account, BasePlatform
from core.chatgpt_task_gate import ChatGPTTaskGate


class _FakeMailbox(BaseMailbox):
    def get_email(self) -> MailboxAccount:
        return MailboxAccount(email="demo@example.com")

    def get_current_ids(self, account: MailboxAccount) -> set:
        return set()

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        **kwargs,
    ) -> str:
        def poll_once():
            return None

        return self._run_polling_wait(
            timeout=timeout,
            poll_interval=0.01,
            poll_once=poll_once,
        )


class _FakePlatform(BasePlatform):
    name = "fake"
    display_name = "Fake"

    def __init__(self, config=None, mailbox=None):
        super().__init__(config)
        self.mailbox = mailbox

    def register(self, email: str, password: str = None) -> Account:
        account = self.mailbox.get_email()
        self.mailbox.wait_for_code(account, timeout=1)
        return Account(
            platform="fake",
            email=account.email,
            password=password or "pw",
        )

    def check_valid(self, account: Account) -> bool:
        return True


class _FakeChatGPTWorkspacePlatform(BasePlatform):
    name = "chatgpt"
    display_name = "ChatGPT"

    _counter = 0

    def __init__(self, config=None, mailbox=None):
        super().__init__(config)
        self.mailbox = mailbox

    @classmethod
    def reset_counter(cls):
        cls._counter = 0

    def register(self, email: str, password: str = None) -> Account:
        type(self)._counter += 1
        index = type(self)._counter
        return Account(
            platform="chatgpt",
            email=f"user{index}@example.com",
            password=password or "pw",
            extra={"workspace_id": f"ws-{index}"},
        )

    def check_valid(self, account: Account) -> bool:
        return True


class RegisterTaskControlFlowTests(unittest.TestCase):
    def setUp(self):
        self.initial_task_ids = {
            snapshot["id"] for snapshot in _task_store.list_snapshots()
        }
        persistence = patch("api.tasks._persist_task_snapshot")
        persistence.start()
        self.addCleanup(persistence.stop)
        config = patch("core.config_store.config_store.get_all", return_value={})
        config.start()
        self.addCleanup(config.stop)
        for target in (
            "core.proxy_pool.proxy_pool.report_success",
            "core.proxy_pool.proxy_pool.report_fail",
            "api.tasks._auto_upload_integrations",
        ):
            proxy_report = patch(target)
            proxy_report.start()
            self.addCleanup(proxy_report.stop)

    def tearDown(self):
        with _task_store._lock:
            new_task_ids = set(_task_store._records) - self.initial_task_ids
            for task_id in new_task_ids:
                _task_store._records.pop(task_id, None)

    def _build_request(self, **overrides):
        payload = {
            "platform": "fake",
            "count": 1,
            "concurrency": 1,
            "proxy": "http://proxy.local:8080",
            "extra": {"mail_provider": "fake"},
        }
        payload.update(overrides)
        return RegisterTaskRequest(**payload)

    def _run_with_control(self, task_id: str, *, stop: bool = False, skip: bool = False):
        req = self._build_request()
        _create_task_record(task_id, req, "manual", None)
        if stop:
            _task_store.request_stop(task_id)
        if skip:
            _task_store.request_skip_current(task_id)

        with (
            patch("core.registry.get", return_value=_FakePlatform),
            patch("core.base_mailbox.create_mailbox", return_value=_FakeMailbox()),
            patch("core.db.save_account", side_effect=lambda account: account),
            patch("api.tasks._save_task_log"),
        ):
            _run_register(task_id, req)

        return _task_store.snapshot(task_id)

    def test_skip_current_marks_attempt_as_skipped(self):
        snapshot = self._run_with_control("task-control-skip", skip=True)

        self.assertEqual(snapshot["status"], "done")
        self.assertEqual(snapshot["success"], 0)
        self.assertEqual(snapshot["skipped"], 1)
        self.assertEqual(snapshot["errors"], [])

    def test_stop_marks_task_as_stopped(self):
        snapshot = self._run_with_control("task-control-stop", stop=True)

        self.assertEqual(snapshot["status"], "stopped")
        self.assertEqual(snapshot["success"], 0)
        self.assertEqual(snapshot["skipped"], 0)
        self.assertEqual(snapshot["errors"], [])

    def test_chatgpt_updates_task_counters_after_each_success(self):
        task_id = "task-chatgpt-workspace-progress"
        req = self._build_request(platform="chatgpt", count=2, concurrency=1)
        _create_task_record(task_id, req, "manual", None)
        _FakeChatGPTWorkspacePlatform.reset_counter()

        with (
            patch("core.registry.get", return_value=_FakeChatGPTWorkspacePlatform),
            patch("core.base_mailbox.create_mailbox", return_value=_FakeMailbox()),
            patch("core.db.save_account", side_effect=lambda account: account),
            patch("api.tasks._save_task_log"),
        ):
            _run_register(task_id, req)

        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(snapshot["success"], 2)
        self.assertEqual(snapshot["registered"], 2)
        self.assertEqual(snapshot["total"], 2)

    def test_chatgpt_login_uses_each_imported_mail_provider_in_plan(self):
        task_id = "task-chatgpt-combined-mail-imports"
        req = self._build_request(
            platform="chatgpt",
            count=2,
            concurrency=1,
            extra={
                "mail_provider": "microsoft",
                "chatgpt_existing_account_login_only": True,
                "chatgpt_existing_account_mail_provider_plan": [
                    "microsoft",
                    "applemail",
                ],
            },
        )
        _create_task_record(task_id, req, "manual", None)
        created_providers = []

        def create_mailbox_for_provider(provider, **_kwargs):
            created_providers.append(provider)
            return _FakeMailbox()

        with (
            patch("core.registry.get", return_value=_FakeChatGPTWorkspacePlatform),
            patch(
                "core.base_mailbox.create_mailbox",
                side_effect=create_mailbox_for_provider,
            ),
            patch("core.db.save_account", side_effect=lambda account: account),
            patch("api.tasks._save_task_log"),
        ):
            _run_register(task_id, req)

        self.assertEqual(created_providers, ["microsoft", "applemail"])

    def test_chatgpt_register_waits_for_auto_and_logs_foreground_priority(self):
        task_id = f"task-chatgpt-wait-{uuid.uuid4().hex}"
        req = self._build_request(platform="chatgpt")
        _create_task_record(task_id, req, "manual", None)
        gate = ChatGPTTaskGate()
        auto_stop_requested = threading.Event()
        inner_started = threading.Event()
        thread_errors: list[BaseException] = []
        automation_lease = gate.try_enter_automation(auto_stop_requested.set)
        self.assertIsNotNone(automation_lease)

        def run_inner(current_task_id, current_req):
            del current_req
            inner_started.set()
            _task_store.finish(
                current_task_id,
                status="done",
                success=1,
                registered=1,
                skipped=0,
                errors=[],
            )

        def run():
            try:
                _run_register(task_id, req)
            except BaseException as exc:
                thread_errors.append(exc)

        worker = threading.Thread(target=run)
        with mock.patch(
            "api.tasks.chatgpt_task_gate",
            gate,
        ), mock.patch(
            "api.tasks._run_register_inner",
            side_effect=run_inner,
        ):
            worker.start()
            self.assertTrue(auto_stop_requested.wait(timeout=1))
            self.assertFalse(inner_started.is_set())
            logs = "\n".join(_task_store.snapshot(task_id)["logs"])
            self.assertIn("等待自动重登释放", logs)
            self.assertIn("手工任务优先", logs)
            gate.leave_automation(automation_lease)
            worker.join(timeout=1)

        self.assertFalse(worker.is_alive())
        self.assertEqual(thread_errors, [])
        self.assertTrue(inner_started.is_set())
        self.assertEqual(_task_store.snapshot(task_id)["status"], "done")
        self.assertEqual(gate.snapshot()["foreground_active"], 0)

    def test_stopped_chatgpt_register_cancels_gate_wait_without_running_inner(self):
        task_id = f"task-chatgpt-cancel-wait-{uuid.uuid4().hex}"
        req = self._build_request(
            platform="chatgpt",
            extra={
                "mail_provider": "fake",
                "chatgpt_existing_account_use_sms_pool": True,
            },
        )
        _create_task_record(task_id, req, "manual", None)
        gate = ChatGPTTaskGate()
        waiting_logged = threading.Event()
        thread_errors: list[BaseException] = []
        automation_lease = gate.try_enter_automation(lambda: None)
        self.assertIsNotNone(automation_lease)
        real_log = __import__("api.tasks", fromlist=["_log"])._log

        def observe_log(current_task_id, message):
            real_log(current_task_id, message)
            if "等待自动重登释放" in message:
                waiting_logged.set()

        def run():
            try:
                _run_register(task_id, req)
            except BaseException as exc:
                thread_errors.append(exc)

        worker = threading.Thread(target=run)
        try:
            with mock.patch(
                "api.tasks.chatgpt_task_gate",
                gate,
            ), mock.patch(
                "api.tasks._run_register_inner"
            ) as inner, mock.patch(
                "api.tasks._log",
                side_effect=observe_log,
            ), mock.patch(
                "api.tasks.sms_pool_service.release_task"
            ) as release_sms:
                worker.start()
                self.assertTrue(waiting_logged.wait(timeout=1))
                _task_store.control_for(task_id).request_stop()
                worker.join(timeout=1)

            self.assertFalse(worker.is_alive())
            self.assertEqual(thread_errors, [])
            self.assertEqual(_task_store.snapshot(task_id)["status"], "stopped")
            self.assertEqual(gate.snapshot()["foreground_waiters"], 0)
            inner.assert_not_called()
            release_sms.assert_called_once_with(task_id)
        finally:
            gate.leave_automation(automation_lease)

    def test_non_chatgpt_register_does_not_enter_chatgpt_gate(self):
        task_id = f"task-non-chatgpt-{uuid.uuid4().hex}"
        req = self._build_request(platform="fake")
        _create_task_record(task_id, req, "manual", None)
        gate = mock.Mock()
        gate.enter_foreground.side_effect = AssertionError("unexpected gate entry")

        def run_inner(current_task_id, current_req):
            del current_req
            _task_store.finish(
                current_task_id,
                status="done",
                success=1,
                registered=1,
                skipped=0,
                errors=[],
            )

        with mock.patch(
            "api.tasks.chatgpt_task_gate",
            gate,
        ), mock.patch(
            "api.tasks._run_register_inner",
            side_effect=run_inner,
        ) as inner:
            _run_register(task_id, req)

        inner.assert_called_once_with(task_id, req)
        gate.enter_foreground.assert_not_called()
        gate.leave_foreground.assert_not_called()

    def test_chatgpt_register_enqueue_failures_are_terminal(self):
        failures = ("thread_construct", "thread_start", "background_add")

        for stage in failures:
            with self.subTest(stage=stage):
                suffix = uuid.uuid4().hex
                task_id = f"task_{suffix}"
                req = self._build_request(platform="chatgpt")
                failure = RuntimeError(f"{stage} failed")
                thread = mock.Mock()
                background_tasks = None
                if stage == "thread_construct":
                    runner_patch = mock.patch(
                        "api.tasks.threading.Thread",
                        side_effect=failure,
                    )
                elif stage == "thread_start":
                    thread.start.side_effect = failure
                    runner_patch = mock.patch(
                        "api.tasks.threading.Thread",
                        return_value=thread,
                    )
                else:
                    background_tasks = mock.Mock()
                    background_tasks.add_task.side_effect = failure
                    runner_patch = mock.patch(
                        "api.tasks.threading.Thread"
                    )

                with mock.patch(
                    "api.tasks.uuid.uuid4",
                    return_value=mock.Mock(hex=suffix),
                ), runner_patch:
                    with self.assertRaisesRegex(RuntimeError, f"{stage} failed"):
                        __import__(
                            "api.tasks",
                            fromlist=["enqueue_register_task"],
                        ).enqueue_register_task(
                            req,
                            background_tasks=background_tasks,
                        )

                snapshot = _task_store.snapshot(task_id)
                self.assertEqual(snapshot["status"], "failed")
                self.assertTrue(snapshot["control"]["stop_requested"])
                self.assertIn(f"{stage} failed", snapshot["error"])
                self.assertFalse(
                    _task_store.has_active(platform="chatgpt", source="manual")
                )

    def test_chatgpt_initial_snapshot_failure_is_terminal_and_releases_sms(self):
        task_id = f"task-chatgpt-initial-persist-{uuid.uuid4().hex}"
        source = f"initial-persist-{uuid.uuid4().hex}"
        req = self._build_request(
            platform="chatgpt",
            extra={
                "mail_provider": "fake",
                "chatgpt_existing_account_use_sms_pool": True,
            },
        )
        _create_task_record(task_id, req, source, None)
        gate = ChatGPTTaskGate()

        with mock.patch(
            "api.tasks.chatgpt_task_gate",
            gate,
        ), mock.patch(
            "api.tasks._persist_task_snapshot",
            side_effect=RuntimeError("initial snapshot failed"),
        ), mock.patch(
            "api.tasks.sms_pool_service.release_task"
        ) as release_sms:
            _run_register(task_id, req)

        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(snapshot["status"], "failed")
        self.assertIn("initial snapshot failed", snapshot["error"])
        self.assertFalse(_task_store.has_active(source=source))
        self.assertEqual(gate.snapshot()["foreground_active"], 0)
        release_sms.assert_called_once_with(task_id)

    def test_chatgpt_normal_final_snapshot_failure_preserves_done_and_releases_sms(self):
        task_id = f"task-chatgpt-final-done-{uuid.uuid4().hex}"
        req = self._build_request(
            platform="chatgpt",
            extra={
                "mail_provider": "fake",
                "chatgpt_existing_account_use_sms_pool": True,
                "chatgpt_existing_account_leadbee_base_urls": [
                    "https://sms.example.com/box"
                ],
                "chatgpt_sms_pool_item_ids": [71],
            },
        )
        _create_task_record(task_id, req, "manual", None)
        gate = ChatGPTTaskGate()

        def fail_done_snapshot(current_task_id):
            if _task_store.snapshot(current_task_id)["status"] == "done":
                raise RuntimeError("done snapshot failed")

        with mock.patch(
            "api.tasks.chatgpt_task_gate",
            gate,
        ), mock.patch(
            "api.tasks._persist_task_snapshot",
            side_effect=fail_done_snapshot,
        ), mock.patch(
            "core.registry.get",
            return_value=_FakeChatGPTWorkspacePlatform,
        ), mock.patch(
            "core.base_mailbox.create_mailbox",
            return_value=_FakeMailbox(),
        ), mock.patch(
            "core.db.save_account",
            side_effect=lambda account: account,
        ), mock.patch(
            "api.tasks._save_task_log",
        ), mock.patch(
            "api.tasks.sms_pool_service.finalize"
        ), mock.patch(
            "api.tasks.sms_pool_service.release_task"
        ) as release_sms:
            _FakeChatGPTWorkspacePlatform.reset_counter()
            _run_register(task_id, req)

        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(snapshot["status"], "done")
        self.assertEqual(snapshot["success"], 1)
        self.assertEqual(snapshot.get("error", ""), "")
        self.assertEqual(gate.snapshot()["foreground_active"], 0)
        release_sms.assert_called_once_with(task_id)

    def test_chatgpt_error_final_snapshot_failure_preserves_failed_and_releases_sms(self):
        task_id = f"task-chatgpt-final-failed-{uuid.uuid4().hex}"
        req = self._build_request(
            platform="chatgpt",
            extra={
                "mail_provider": "fake",
                "chatgpt_existing_account_use_sms_pool": True,
            },
        )
        _create_task_record(task_id, req, "manual", None)
        gate = ChatGPTTaskGate()

        def fail_failed_snapshot(current_task_id):
            if _task_store.snapshot(current_task_id)["status"] == "failed":
                raise RuntimeError("failed snapshot failed")

        with mock.patch(
            "api.tasks.chatgpt_task_gate",
            gate,
        ), mock.patch(
            "api.tasks._persist_task_snapshot",
            side_effect=fail_failed_snapshot,
        ), mock.patch(
            "core.registry.get",
            side_effect=RuntimeError("register setup failed"),
        ), mock.patch(
            "api.tasks.sms_pool_service.release_task"
        ) as release_sms:
            _run_register(task_id, req)

        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(snapshot["status"], "failed")
        self.assertIn("register setup failed", snapshot["error"])
        self.assertEqual(gate.snapshot()["foreground_active"], 0)
        release_sms.assert_called_once_with(task_id)


if __name__ == "__main__":
    unittest.main()
