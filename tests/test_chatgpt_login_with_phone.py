import threading
import unittest
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import HTTPException

from api.tasks import (
    RegisterTaskRequest,
    _complete_chatgpt_leadbee_verification,
    _prepare_register_request,
    _run_register,
    _task_action_terms,
    _task_store,
)
from core.base_platform import Account, AccountStatus, BasePlatform
from core.task_runtime import StopTaskRequested


class _LoginMailbox:
    _lock = threading.Lock()
    marked_used = []
    bound_claim_scopes = []
    requeued = []

    @classmethod
    def reset(cls):
        with cls._lock:
            cls.marked_used = []
            cls.bound_claim_scopes = []
            cls.requeued = []

    def bind_claim_scope(self, scope):
        with type(self)._lock:
            type(self).bound_claim_scopes.append(scope)

    def mark_account_used(self, account):
        with type(self)._lock:
            type(self).marked_used.append(str(getattr(account, "email", "") or ""))
        return True

    def requeue_account(self, account):
        with type(self)._lock:
            type(self).requeued.append(str(getattr(account, "email", "") or ""))
        return True


class _ExistingAccountPlatform(BasePlatform):
    name = "chatgpt"
    display_name = "ChatGPT"

    _lock = threading.Lock()
    _counter = 0
    seen_extras = []
    phone_oauth_ready = True
    phone_oauth_ready_sequence = []
    phone_oauth_prepare_error = ""

    @classmethod
    def reset(cls):
        with cls._lock:
            cls._counter = 0
            cls.seen_extras = []
            cls.phone_oauth_ready = True
            cls.phone_oauth_ready_sequence = []
            cls.phone_oauth_prepare_error = ""

    def __init__(self, config=None, mailbox=None):
        super().__init__(config)
        self.mailbox = mailbox

    def register(self, email=None, password=None):
        with type(self)._lock:
            type(self)._counter += 1
            index = type(self)._counter
            type(self).seen_extras.append(dict(self.config.extra or {}))
            ready = (
                bool(type(self).phone_oauth_ready_sequence[index - 1])
                if index <= len(type(self).phone_oauth_ready_sequence)
                else bool(type(self).phone_oauth_ready)
            )
        return Account(
            platform="chatgpt",
            email=f"existing-{index}@example.com",
            password=password or "mail-password",
            token=f"access-token-{index}",
            status=AccountStatus.REGISTERED,
            extra={
                "access_token": f"access-token-{index}",
                "refresh_token": "",
                "chatgpt_token_source": "existing_account_web_login",
                "phone_oauth_ready": ready,
                "phone_oauth_prepare_error": type(self).phone_oauth_prepare_error,
                "oauth_resume_context": (
                    {"version": 1, "attempt": index}
                    if ready
                    else {}
                ),
                "mailbox_login_context": {
                    "provider": "microsoft",
                    "email": f"existing-{index}@example.com",
                    "account_id": str(index),
                    "extra": {"account_type": "mailapi_url"},
                },
            },
        )

    def check_valid(self, account):
        return True


class ExistingAccountLoginWithPhoneRequestTests(unittest.TestCase):
    def _request(self, *, count=2, codes=None, enabled=True):
        return RegisterTaskRequest(
            platform="chatgpt",
            count=count,
            concurrency=count,
            extra={
                "mail_provider": "microsoft",
                "chatgpt_existing_account_login_only": True,
                "chatgpt_existing_account_login_stage": "access_token",
                "chatgpt_existing_account_bind_phone_and_get_rt": enabled,
                "chatgpt_existing_account_leadbee_codes": (
                    codes if codes is not None else ["card-one", "card-two"]
                ),
            },
        )

    def test_prepare_normalizes_one_leadbee_code_per_account(self):
        prepared = _prepare_register_request(
            self._request(codes=[" card-one ", "", "card-two"])
        )

        self.assertEqual(
            prepared.extra["chatgpt_existing_account_leadbee_codes"],
            ["card-one", "card-two"],
        )
        self.assertEqual(_task_action_terms(prepared), ("登录并接码", "登录并接码"))

    def test_prepare_rejects_a_card_count_that_differs_from_login_count(self):
        with self.assertRaisesRegex(HTTPException, "卡密数量需与登录数量一致"):
            _prepare_register_request(self._request(codes=["only-one-card"]))

    def test_prepare_rejects_duplicate_leadbee_codes_after_trimming(self):
        with self.assertRaisesRegex(HTTPException, "卡密不能重复") as ctx:
            _prepare_register_request(
                self._request(codes=[" card-one ", "card-one"])
            )

        self.assertNotIn("card-one", str(ctx.exception.detail))

    def test_prepare_discards_accidental_codes_when_option_is_disabled(self):
        prepared = _prepare_register_request(
            self._request(enabled=False, codes=["must-not-survive"])
        )

        self.assertNotIn(
            "chatgpt_existing_account_leadbee_codes",
            prepared.extra,
        )


class LeadBeeTaskCancellationTests(unittest.TestCase):
    def test_phone_flows_use_parallel_provider_slots(self):
        first_started = threading.Event()
        second_started = threading.Event()
        first_pool_marked_active = threading.Event()
        second_pool_marked_active = threading.Event()
        release_first = threading.Event()
        results = {}
        errors = []

        class Manager:
            def start(self, account_id, **_kwargs):
                _kwargs["on_provider_start"]()
                if account_id == 101:
                    first_started.set()
                    return {
                        "session_id": "phone-session-first",
                        "status": "starting",
                        "logs": [],
                        "expires_in": 600,
                    }
                second_started.set()
                return {
                    "session_id": "phone-session-second",
                    "status": "completed",
                    "provider_cleanup_settled": True,
                    "logs": [],
                    "expires_in": 600,
                }

            def status(self, account_id, _session_id):
                if account_id == 101:
                    release_first.wait(timeout=2)
                return {
                    "session_id": "phone-session-first",
                    "status": "completed",
                    "provider_cleanup_settled": True,
                    "logs": [],
                    "expires_in": 600,
                }

        manager = Manager()

        def run(account_id):
            try:
                results[account_id] = _complete_chatgpt_leadbee_verification(
                    task_id="task-serialized-leadbee",
                    account_id=account_id,
                    leadbee_code=f"card-{account_id}",
                    on_provider_start=(
                        first_pool_marked_active.set
                        if account_id == 101
                        else second_pool_marked_active.set
                    ),
                    control=Mock(),
                    attempt_id=account_id,
                )
            except BaseException as exc:
                errors.append(exc)

        with patch(
            "services.chatgpt_phone_verification.phone_verification_manager",
            manager,
        ), patch("api.tasks.time.sleep"):
            first = threading.Thread(target=run, args=(101,))
            second = threading.Thread(target=run, args=(102,))
            first.start()
            self.assertTrue(first_started.wait(timeout=1))
            self.assertTrue(first_pool_marked_active.is_set())
            second.start()
            self.assertTrue(second_started.wait(timeout=0.5))
            self.assertTrue(second_pool_marked_active.is_set())
            release_first.set()
            first.join(timeout=2)
            second.join(timeout=2)

        self.assertEqual(errors, [])
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertTrue(second_started.is_set())
        self.assertTrue(second_pool_marked_active.is_set())
        self.assertEqual(results[101]["status"], "completed")
        self.assertEqual(results[102]["status"], "completed")

    def test_stop_request_finishes_an_activated_card_instead_of_burning_it(self):
        manager = Mock()
        manager.start.return_value = {
            "session_id": "phone-session-1",
            "status": "starting",
            "logs": [],
            "expires_in": 600,
        }
        manager.status.return_value = {
            **manager.start.return_value,
            "status": "completed",
            "message": "手机验证完成，Refresh Token 已保存",
            "exchange_code_consumed": True,
            "provider_cleanup_settled": True,
        }
        control = Mock()
        control.checkpoint.side_effect = lambda **_: (
            (_ for _ in ()).throw(StopTaskRequested())
            if manager.start.called
            else None
        )

        with patch(
            "services.chatgpt_phone_verification.phone_verification_manager",
            manager,
        ), patch("api.tasks.time.sleep"):
            result = _complete_chatgpt_leadbee_verification(
                task_id="task-stop-leadbee",
                account_id=7,
                leadbee_code="card-secret",
                control=control,
                attempt_id=3,
            )

        self.assertEqual(result["status"], "completed")
        manager.cancel.assert_not_called()

    def test_wait_timeout_cancels_the_background_phone_session(self):
        manager = Mock()
        manager.start.return_value = {
            "session_id": "phone-session-timeout",
            "status": "starting",
            "logs": [],
            "expires_in": 1,
        }
        manager.cancel.return_value = {
            **manager.start.return_value,
            "status": "failed",
            "message": (
                "LeadBee 自动接码等待超时，服务端卡密终态不可确认；"
                "卡密保持隔离等待人工核对"
            ),
            "provider_started": True,
            "provider_cleanup_settled": True,
            "exchange_code_settlement": "active_unknown",
            "exchange_code_unusable": False,
        }

        with patch(
            "services.chatgpt_phone_verification.phone_verification_manager",
            manager,
        ), patch("api.tasks.time.monotonic", side_effect=[100.0, 100.0, 107.0]):
            result = _complete_chatgpt_leadbee_verification(
                task_id="task-timeout-leadbee",
                account_id=8,
                leadbee_code="card-secret",
                control=Mock(),
                attempt_id=4,
            )

        self.assertEqual(result["status"], "failed")
        self.assertIn("超时", result["message"])
        self.assertIn("卡密保持隔离", result["message"])
        self.assertEqual(result["exchange_code_settlement"], "active_unknown")
        self.assertFalse(result["exchange_code_unusable"])
        manager.cancel.assert_called_once_with(
            8,
            "phone-session-timeout",
            message="LeadBee 自动接码等待超时，后台任务已取消",
        )

    def test_wait_timeout_uses_provider_cleanup_settlement_from_cancel(self):
        manager = Mock()
        manager.start.return_value = {
            "session_id": "phone-session-restored-timeout",
            "status": "starting",
            "logs": [],
            "expires_in": 1,
        }
        manager.cancel.return_value = {
            **manager.start.return_value,
            "status": "failed",
            "message": "LeadBee 自动接码等待超时，后台任务已取消",
            "provider_cleanup_settled": True,
            "exchange_code_restoration_confirmed": True,
            "exchange_code_unusable": False,
        }

        with patch(
            "services.chatgpt_phone_verification.phone_verification_manager",
            manager,
        ), patch("api.tasks.time.monotonic", side_effect=[100.0, 100.0, 107.0]):
            result = _complete_chatgpt_leadbee_verification(
                task_id="task-restored-timeout-leadbee",
                account_id=11,
                leadbee_code="card-secret",
                control=Mock(),
                attempt_id=7,
            )

        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["provider_cleanup_settled"])
        self.assertTrue(result["exchange_code_restoration_confirmed"])
        self.assertFalse(result["exchange_code_unusable"])

    def test_stop_during_persistence_waits_for_current_account_to_complete(self):
        manager = Mock()
        manager.start.return_value = {
            "session_id": "phone-session-persisting-stop",
            "status": "persisting",
            "logs": [],
            "expires_in": 600,
            "phone_verified": True,
            "exchange_code_consumed": True,
        }
        manager.cancel.return_value = dict(manager.start.return_value)
        manager.status.return_value = {
            **manager.start.return_value,
            "status": "completed",
            "message": "手机验证完成，Refresh Token 已保存",
            "provider_cleanup_settled": True,
        }
        control = Mock()
        control.checkpoint.side_effect = lambda **_: (
            (_ for _ in ()).throw(StopTaskRequested())
            if manager.start.called
            else None
        )

        with patch(
            "services.chatgpt_phone_verification.phone_verification_manager",
            manager,
        ), patch("api.tasks.time.sleep"):
            result = _complete_chatgpt_leadbee_verification(
                task_id="task-stop-during-persistence",
                account_id=9,
                leadbee_code="card-secret",
                control=control,
                attempt_id=5,
            )

        self.assertEqual(result["status"], "completed")
        manager.cancel.assert_not_called()
        self.assertGreaterEqual(control.checkpoint.call_count, 2)
        control.checkpoint.assert_called_with(attempt_id=5)

    def test_timeout_during_persistence_waits_for_current_account_to_complete(self):
        manager = Mock()
        manager.start.return_value = {
            "session_id": "phone-session-persisting-timeout",
            "status": "persisting",
            "logs": [],
            "expires_in": 1,
            "phone_verified": True,
            "exchange_code_consumed": True,
        }
        manager.cancel.return_value = dict(manager.start.return_value)
        manager.status.return_value = {
            **manager.start.return_value,
            "status": "completed",
            "message": "手机验证完成，Refresh Token 已保存",
            "provider_cleanup_settled": True,
        }

        with patch(
            "services.chatgpt_phone_verification.phone_verification_manager",
            manager,
        ), patch(
            "api.tasks.time.monotonic",
            side_effect=[100.0, 100.0, 107.0],
        ), patch(
            "api.tasks.time.sleep"
        ):
            result = _complete_chatgpt_leadbee_verification(
                task_id="task-timeout-during-persistence",
                account_id=10,
                leadbee_code="card-secret",
                control=Mock(),
                attempt_id=6,
            )

        self.assertEqual(result["status"], "completed")
        manager.cancel.assert_called_once_with(
            10,
            "phone-session-persisting-timeout",
            message="LeadBee 自动接码等待超时，后台任务已取消",
        )


class ExistingAccountLoginWithPhoneTaskTests(unittest.TestCase):
    def setUp(self):
        _ExistingAccountPlatform.reset()
        _LoginMailbox.reset()

    def _run(
        self,
        task_id,
        *,
        completion,
        account_was_created=True,
        persist_refresh_token_on_completion=True,
        count=2,
    ):
        req = RegisterTaskRequest(
            platform="chatgpt",
            count=count,
            concurrency=count,
            proxy="http://proxy.local:8080",
            extra={
                "mail_provider": "microsoft",
                "chatgpt_existing_account_login_only": True,
                "chatgpt_existing_account_login_stage": "access_token",
                "chatgpt_existing_account_bind_phone_and_get_rt": True,
                "chatgpt_existing_account_leadbee_codes": (
                    ["card-secret-one", "card-secret-two"]
                    if count == 2
                    else [f"card-secret-{index + 1}" for index in range(count)]
                ),
            },
        )
        _task_store.create(
            task_id,
            platform="chatgpt",
            total=req.count,
            source="manual",
            meta=None,
        )
        saved = []

        def save_account(account):
            account_id = len(saved) + 1
            extra = dict(account.extra or {})
            row = SimpleNamespace(
                id=account_id,
                platform=account.platform,
                email=account.email,
                extra=extra,
                extra_json=json.dumps(extra, ensure_ascii=False),
                created_at=datetime(2026, 8, 3, tzinfo=timezone.utc),
            )
            saved.append(row)
            return row

        def save_account_with_creation_state(account):
            return save_account(account), account_was_created

        def complete_and_persist(**kwargs):
            result = completion(**kwargs)
            if (
                persist_refresh_token_on_completion
                and str(result.get("status") or "").lower() == "completed"
            ):
                saved[int(kwargs["account_id"]) - 1].extra["refresh_token"] = (
                    f"refresh-token-{kwargs['account_id']}"
                )
            return result

        with (
            patch("core.registry.get", return_value=_ExistingAccountPlatform),
            patch("core.base_mailbox.create_mailbox", side_effect=lambda **_: _LoginMailbox()),
            patch("core.db.save_account", side_effect=save_account),
            patch(
                "core.db.save_account_with_creation_state",
                side_effect=save_account_with_creation_state,
            ),
            patch(
                "core.db.delete_incomplete_chatgpt_account",
                return_value=True,
            ) as cleanup_incomplete,
            patch("core.config_store.config_store.get_all", return_value={
                "leadbee_code": "global-card-that-must-not-leak",
                "chatgpt_leadbee_code": "global-card-alias-that-must-not-leak",
            }),
            patch(
                "api.tasks._complete_chatgpt_leadbee_verification",
                side_effect=complete_and_persist,
            ) as complete,
            patch(
                "api.tasks._reload_saved_account",
                side_effect=lambda _account_id, fallback: fallback,
            ),
            patch("api.tasks._refresh_saved_chatgpt_login", return_value=""),
            patch("api.tasks._auto_upload_integrations"),
            patch("api.tasks._save_task_log") as save_task_log,
            patch("api.tasks._persist_task_snapshot"),
            patch("core.proxy_pool.proxy_pool.report_success"),
            patch("core.proxy_pool.proxy_pool.report_fail"),
        ):
            _run_register(task_id, req)

        return (
            _task_store.snapshot(task_id),
            saved,
            complete,
            save_task_log,
            cleanup_incomplete,
        )

    def test_concurrent_attempts_receive_distinct_codes_without_exposing_them(self):
        def completion(**kwargs):
            return {
                "status": "completed",
                "message": "手机验证完成，Refresh Token 已保存",
                "phone_verified": True,
                "exchange_code_consumed": True,
                "account_id": kwargs["account_id"],
            }

        snapshot, saved, complete, _save_task_log, cleanup_incomplete = self._run(
            "task-chatgpt-login-phone-success",
            completion=completion,
        )

        self.assertEqual(snapshot["success"], 2)
        self.assertEqual(snapshot["progress"], "2/2")
        self.assertEqual(
            {call.kwargs["leadbee_code"] for call in complete.call_args_list},
            {"card-secret-one", "card-secret-two"},
        )
        self.assertEqual(len(complete.call_args_list), 2)
        self.assertEqual(len(_LoginMailbox.bound_claim_scopes), 2)
        self.assertIs(
            _LoginMailbox.bound_claim_scopes[0],
            _LoginMailbox.bound_claim_scopes[1],
        )
        self.assertEqual(
            set(_LoginMailbox.marked_used),
            {"existing-1@example.com", "existing-2@example.com"},
        )
        cleanup_incomplete.assert_not_called()
        for extra in _ExistingAccountPlatform.seen_extras:
            self.assertNotIn("chatgpt_existing_account_leadbee_codes", extra)
            self.assertNotIn("leadbee_code", extra)
            self.assertNotIn("chatgpt_leadbee_code", extra)
        for row in saved:
            serialized = str(row.extra)
            self.assertNotIn("card-secret", serialized)
            self.assertNotIn("global-card", serialized)
        serialized_snapshot = str(snapshot)
        self.assertNotIn("card-secret", serialized_snapshot)
        self.assertNotIn("global-card", serialized_snapshot)

    def test_existing_phone_is_successful_and_reports_unused_card(self):
        snapshot, _saved, _complete, _save_task_log, cleanup_incomplete = self._run(
            "task-chatgpt-login-phone-not-required",
            completion=lambda **_: {
                "status": "completed",
                "message": (
                    "OpenAI 未要求新增手机号，LeadBee 兑换码未使用；"
                    "Refresh Token 已保存"
                ),
                "phone_verified": False,
                "exchange_code_consumed": False,
            },
        )

        self.assertEqual(snapshot["success"], 2)
        self.assertIn("兑换码未使用", "\n".join(snapshot["logs"]))
        cleanup_incomplete.assert_not_called()

    def test_completed_phone_result_without_persisted_rt_is_failed_and_removed(self):
        snapshot, _saved, _complete, _save_task_log, cleanup_incomplete = self._run(
            "task-chatgpt-login-phone-completed-without-rt",
            persist_refresh_token_on_completion=False,
            completion=lambda **_: {
                "status": "completed",
                "message": "手机验证完成",
                "phone_verified": True,
                "exchange_code_consumed": True,
            },
        )

        self.assertEqual(snapshot["success"], 0)
        self.assertEqual(len(snapshot["errors"]), 2)
        self.assertEqual(_LoginMailbox.marked_used, [])
        self.assertEqual(cleanup_incomplete.call_count, 2)
        self.assertIn("未保存 Refresh Token", "\n".join(snapshot["logs"]))

    def test_phone_failure_removes_each_new_incomplete_account(self):
        snapshot, saved, _complete, _save_task_log, cleanup_incomplete = self._run(
            "task-chatgpt-login-phone-failed",
            completion=lambda **_: {
                "status": "failed",
                "message": "LeadBee 获取手机号超时",
                "phone_verified": False,
                "exchange_code_consumed": False,
            },
        )

        self.assertEqual(snapshot["success"], 0)
        self.assertEqual(len(snapshot["errors"]), 2)
        self.assertEqual(_LoginMailbox.marked_used, [])
        self.assertEqual(len(saved), 2)
        self.assertTrue(all(row.extra.get("access_token") for row in saved))
        self.assertEqual(cleanup_incomplete.call_count, 2)
        self.assertEqual(
            {call.args[0] for call in cleanup_incomplete.call_args_list},
            {1, 2},
        )
        for call in cleanup_incomplete.call_args_list:
            self.assertTrue(call.kwargs["expected_email"].endswith("@example.com"))
            self.assertEqual(
                call.kwargs["expected_created_at"],
                datetime(2026, 8, 3, tzinfo=timezone.utc),
            )
        joined_logs = "\n".join(snapshot["logs"])
        self.assertIn("邮箱登录成功，但接码失败", joined_logs)
        self.assertNotIn("card-secret", joined_logs)

    def test_phone_failure_preserves_a_preexisting_account(self):
        snapshot, _saved, _complete, _save_task_log, cleanup_incomplete = self._run(
            "task-chatgpt-login-phone-existing-failed",
            account_was_created=False,
            completion=lambda **_: {
                "status": "failed",
                "message": "LeadBee 获取手机号超时",
                "phone_verified": False,
                "exchange_code_consumed": False,
            },
        )

        self.assertEqual(snapshot["success"], 0)
        cleanup_incomplete.assert_not_called()

    def test_unprepared_phone_oauth_saves_access_token_without_starting_leadbee(self):
        _ExistingAccountPlatform.phone_oauth_ready = False
        _ExistingAccountPlatform.phone_oauth_prepare_error = (
            "OAuth bootstrap did not reach an authenticated state"
        )

        (
            snapshot,
            saved,
            complete,
            save_task_log,
            cleanup_incomplete,
        ) = self._run(
            "task-chatgpt-login-phone-unprepared",
            completion=lambda **_: self.fail("LeadBee must not start without resume context"),
        )

        self.assertEqual(snapshot["success"], 0)
        self.assertEqual(len(snapshot["errors"]), 2)
        self.assertEqual(len(saved), 2)
        self.assertTrue(all(row.extra.get("access_token") for row in saved))
        complete.assert_not_called()
        self.assertEqual(cleanup_incomplete.call_count, 2)
        joined_logs = "\n".join(snapshot["logs"])
        self.assertIn("手机授权事务未就绪", joined_logs)
        self.assertNotIn("开始自动接码", joined_logs)
        self.assertNotIn("card-secret", joined_logs)
        failed_calls = [
            call
            for call in save_task_log.call_args_list
            if len(call.args) >= 3 and call.args[2] == "failed"
        ]
        self.assertEqual(len(failed_calls), 2)
        for call in failed_calls:
            self.assertTrue(call.kwargs["detail"]["partial_success"])
            self.assertTrue(call.kwargs["detail"]["access_token_saved"])
            self.assertFalse(call.kwargs["detail"]["exchange_code_consumed"])

    def test_unprepared_phone_oauth_retries_fresh_login_once(self):
        _ExistingAccountPlatform.phone_oauth_ready_sequence = [False, True]

        snapshot, saved, complete, _save_task_log, cleanup_incomplete = self._run(
            "task-chatgpt-login-phone-oauth-recovery",
            count=1,
            completion=lambda **_: {
                "status": "completed",
                "message": "手机验证完成，Refresh Token 已保存",
                "phone_verified": True,
                "exchange_code_consumed": True,
            },
        )

        self.assertEqual(snapshot["success"], 1)
        self.assertEqual(len(saved), 1)
        self.assertEqual(_ExistingAccountPlatform._counter, 2)
        complete.assert_called_once()
        cleanup_incomplete.assert_not_called()
        joined_logs = "\n".join(snapshot["logs"])
        self.assertIn("重新建立一次 OAuth 登录会话", joined_logs)


if __name__ == "__main__":
    unittest.main()
