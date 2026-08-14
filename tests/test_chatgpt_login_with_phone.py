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
    _build_chatgpt_retry_request,
    _prepare_register_request,
    _run_register,
    _task_action_terms,
    _task_store,
)
from core.base_platform import Account, AccountStatus, BasePlatform
from core.sms_pool import SmsPoolExhaustedError
from core.task_runtime import StopTaskRequested
from platforms.chatgpt.leadbee_runtime import LeadBeeCapacityExhausted


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
    phone_oauth_browser_context_available = True
    register_emails = []

    @classmethod
    def reset(cls):
        with cls._lock:
            cls._counter = 0
            cls.seen_extras = []
            cls.phone_oauth_ready = True
            cls.phone_oauth_ready_sequence = []
            cls.phone_oauth_prepare_error = ""
            cls.phone_oauth_browser_context_available = True
            cls.register_emails = []

    def __init__(self, config=None, mailbox=None):
        super().__init__(config)
        self.mailbox = mailbox

    def register(self, email=None, password=None):
        with type(self)._lock:
            type(self)._counter += 1
            index = type(self)._counter
            type(self).register_emails.append(email)
            type(self).seen_extras.append(dict(self.config.extra or {}))
            ready = (
                bool(type(self).phone_oauth_ready_sequence[index - 1])
                if index <= len(type(self).phone_oauth_ready_sequence)
                else bool(type(self).phone_oauth_ready)
            )
        account_email = str(email or f"existing-{index}@example.com")
        return Account(
            platform="chatgpt",
            email=account_email,
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
                    {
                        "version": 2,
                        "attempt": index,
                        "code_verifier": f"verifier-{index}",
                        "oauth_state": f"state-{index}",
                        "flow_state": {"page_type": "add_phone"},
                    }
                    if ready
                    else {}
                ),
                "oauth_browser_context": (
                    {
                        "version": 1,
                        "attempt": index,
                        "cookies": [
                            {"name": "login_session", "value": f"cookie-{index}"}
                        ],
                    }
                    if type(self).phone_oauth_browser_context_available
                    else {}
                ),
                "mailbox_login_context": {
                    "provider": "microsoft",
                    "email": account_email,
                    "account_id": str(index),
                    "extra": {"account_type": "mailapi_url"},
                },
            },
        )

    def check_valid(self, account):
        return True


class ExistingAccountLoginWithPhoneRequestTests(unittest.TestCase):
    def setUp(self):
        config = patch("core.config_store.config_store.get_all", return_value={})
        config.start()
        self.addCleanup(config.stop)

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
            _prepare_register_request(self._request(codes=[" card-one ", "card-one"]))

        self.assertNotIn("card-one", str(ctx.exception.detail))

    def test_prepare_discards_accidental_codes_when_option_is_disabled(self):
        prepared = _prepare_register_request(
            self._request(enabled=False, codes=["must-not-survive"])
        )

        self.assertNotIn(
            "chatgpt_existing_account_leadbee_codes",
            prepared.extra,
        )

    def test_prepare_api_mode_allows_missing_codes_and_generates_refs(self):
        request = self._request(codes=[])
        request.extra["chatgpt_existing_account_leadbee_api"] = True
        request.extra.pop("chatgpt_existing_account_leadbee_codes", None)
        with patch(
            "core.config_store.config_store.get_all",
            return_value={
                "leadbee_api_enabled": "yes",
                "leadbee_api_key": "fixture-key",
                "leadbee_api_secret": "fixture-secret",
                "leadbee_api_product_id": "fixture-product",
            },
        ):
            prepared = _prepare_register_request(request)

        self.assertTrue(prepared.extra["chatgpt_existing_account_leadbee_api"])
        refs = prepared.extra["chatgpt_existing_account_leadbee_client_order_ids"]
        self.assertEqual(len(refs), request.count)
        self.assertEqual(len(set(refs)), request.count)
        self.assertTrue(
            all(__import__("re").fullmatch(r"aar_[0-9a-f]{32}", ref) for ref in refs)
        )
        self.assertNotIn("chatgpt_existing_account_leadbee_codes", prepared.extra)

    def test_prepare_auto_selects_complete_global_api_without_request_marker(self):
        request = self._request(codes=[])
        request.extra.pop("chatgpt_existing_account_leadbee_codes", None)
        with patch(
            "core.config_store.config_store.get_all",
            return_value={
                "leadbee_api_enabled": "1",
                "leadbee_api_key": "fixture-key",
                "leadbee_api_secret": "fixture-secret",
                "leadbee_api_product_id": "fixture-product",
            },
        ):
            prepared = _prepare_register_request(request)

        self.assertTrue(prepared.extra["chatgpt_existing_account_leadbee_api"])
        refs = prepared.extra["chatgpt_existing_account_leadbee_client_order_ids"]
        self.assertEqual(len(refs), request.count)
        self.assertEqual(len(set(refs)), request.count)
        self.assertNotIn("chatgpt_existing_account_leadbee_codes", prepared.extra)

    def test_explicit_api_fallback_pool_generates_api_refs_without_reserving_cards(
        self,
    ):
        request = self._request(codes=[])
        request.extra.update(
            {
                "chatgpt_existing_account_sms_mode": "api_fallback_pool",
                "chatgpt_existing_account_use_sms_pool": True,
            }
        )
        request.extra.pop("chatgpt_existing_account_leadbee_codes", None)
        config = {
            "leadbee_api_enabled": "1",
            "leadbee_api_key": "fixture-key",
            "leadbee_api_secret": "fixture-secret",
            "leadbee_api_product_id": "fixture-product",
            "mail_provider": "microsoft",
        }

        with patch(
            "core.config_store.config_store.get_all",
            return_value=config,
        ):
            prepared = _prepare_register_request(request)

        self.assertEqual(
            prepared.extra["chatgpt_existing_account_sms_mode"],
            "api_fallback_pool",
        )
        self.assertTrue(prepared.extra["chatgpt_existing_account_leadbee_api"])
        self.assertFalse(prepared.extra["chatgpt_existing_account_use_sms_pool"])
        self.assertEqual(
            len(prepared.extra["chatgpt_existing_account_leadbee_client_order_ids"]),
            request.count,
        )
        self.assertNotIn("chatgpt_existing_account_leadbee_codes", prepared.extra)

    def test_explicit_pool_wins_even_when_global_api_is_enabled(self):
        request = self._request(codes=[])
        request.extra["chatgpt_existing_account_sms_mode"] = "pool"
        request.extra.pop("chatgpt_existing_account_leadbee_codes", None)
        config = {
            "leadbee_api_enabled": "1",
            "leadbee_api_key": "fixture-key",
            "leadbee_api_secret": "fixture-secret",
            "leadbee_api_product_id": "fixture-product",
        }

        with patch(
            "core.config_store.config_store.get_all",
            return_value=config,
        ):
            prepared = _prepare_register_request(request)

        self.assertEqual(prepared.extra["chatgpt_existing_account_sms_mode"], "pool")
        self.assertTrue(prepared.extra["chatgpt_existing_account_use_sms_pool"])
        self.assertNotIn("chatgpt_existing_account_leadbee_api", prepared.extra)
        self.assertNotIn(
            "chatgpt_existing_account_leadbee_client_order_ids",
            prepared.extra,
        )

    def test_explicit_none_disables_phone_provider_and_removes_legacy_fields(self):
        request = self._request(codes=["card-one", "card-two"])
        request.extra["chatgpt_existing_account_sms_mode"] = "none"
        request.extra["chatgpt_existing_account_use_sms_pool"] = True

        prepared = _prepare_register_request(request)

        self.assertEqual(prepared.extra["chatgpt_existing_account_sms_mode"], "none")
        self.assertFalse(
            prepared.extra["chatgpt_existing_account_bind_phone_and_get_rt"]
        )
        for key in (
            "chatgpt_existing_account_leadbee_api",
            "chatgpt_existing_account_use_sms_pool",
            "chatgpt_existing_account_leadbee_codes",
            "chatgpt_existing_account_leadbee_client_order_ids",
            "chatgpt_sms_pool_item_ids",
        ):
            self.assertNotIn(key, prepared.extra)

    def test_prepare_rejects_unknown_explicit_sms_mode(self):
        request = self._request()
        request.extra["chatgpt_existing_account_sms_mode"] = "surprise"

        with self.assertRaisesRegex(HTTPException, "接码方式"):
            _prepare_register_request(request)

    def test_existing_account_login_concurrency_is_capped_at_fifty(self):
        request = self._request(count=1, enabled=False)
        request.concurrency = 51
        request.extra["chatgpt_existing_account_sms_mode"] = "none"

        with self.assertRaisesRegex(HTTPException, "50"):
            _prepare_register_request(request)

    def test_prepare_global_api_rejects_sms_pool_without_reserving(self):
        request = self._request(codes=[])
        request.extra.pop("chatgpt_existing_account_leadbee_codes", None)
        request.extra["chatgpt_existing_account_use_sms_pool"] = True
        with (
            patch(
                "core.config_store.config_store.get_all",
                return_value={
                    "leadbee_api_enabled": "1",
                    "leadbee_api_key": "fixture-key",
                    "leadbee_api_secret": "fixture-secret",
                    "leadbee_api_product_id": "fixture-product",
                },
            ),
            patch("api.tasks.sms_pool_service.reserve") as reserve,
            self.assertRaisesRegex(HTTPException, "不能与 SMS 接码池混用") as ctx,
        ):
            _prepare_register_request(request)
        self.assertEqual(ctx.exception.status_code, 400)
        reserve.assert_not_called()

    def test_prepare_global_api_enabled_but_incomplete_is_not_legacy(self):
        request = self._request(codes=[])
        request.extra.pop("chatgpt_existing_account_leadbee_codes", None)
        with (
            patch(
                "core.config_store.config_store.get_all",
                return_value={
                    "leadbee_api_enabled": "1",
                    "leadbee_api_key": "fixture-key",
                },
            ),
            self.assertRaises(HTTPException) as ctx,
        ):
            _prepare_register_request(request)
        self.assertEqual(ctx.exception.status_code, 409)

    def test_prepare_disabled_global_api_keeps_legacy_cards(self):
        with patch(
            "core.config_store.config_store.get_all",
            return_value={"leadbee_api_enabled": "0"},
        ):
            prepared = _prepare_register_request(
                self._request(codes=["legacy-one", "legacy-two"])
            )
        self.assertNotIn("chatgpt_existing_account_leadbee_api", prepared.extra)
        self.assertEqual(
            prepared.extra["chatgpt_existing_account_leadbee_codes"],
            ["legacy-one", "legacy-two"],
        )

    def test_prepare_config_read_failure_keeps_legacy_cards(self):
        with patch(
            "core.config_store.config_store.get_all",
            side_effect=RuntimeError("fixture config unavailable"),
        ):
            prepared = _prepare_register_request(
                self._request(codes=["legacy-one", "legacy-two"])
            )

        self.assertNotIn("chatgpt_existing_account_leadbee_api", prepared.extra)
        self.assertEqual(
            prepared.extra["chatgpt_existing_account_leadbee_codes"],
            ["legacy-one", "legacy-two"],
        )

    def test_prepare_config_read_failure_rejects_explicit_api_mode(self):
        request = self._request(codes=[])
        request.extra["chatgpt_existing_account_leadbee_api"] = True
        request.extra.pop("chatgpt_existing_account_leadbee_codes", None)

        with (
            patch(
                "core.config_store.config_store.get_all",
                side_effect=RuntimeError("fixture config unavailable"),
            ),
            self.assertRaises(HTTPException) as ctx,
        ):
            _prepare_register_request(request)

        self.assertEqual(ctx.exception.status_code, 409)

    def test_prepare_api_mode_rejects_incomplete_config(self):
        request = self._request(codes=[])
        request.extra["chatgpt_existing_account_leadbee_api"] = True
        request.extra.pop("chatgpt_existing_account_leadbee_codes", None)
        with (
            patch(
                "core.config_store.config_store.get_all",
                return_value={
                    "leadbee_api_enabled": "1",
                    "leadbee_api_key": "fixture-key",
                },
            ),
            self.assertRaises(HTTPException),
        ):
            _prepare_register_request(request)

    def test_api_retry_keeps_mailbox_binding_but_reissues_refs(self):
        old_refs = ["aar_" + "1" * 32, "aar_" + "2" * 32]
        bindings = [
            {"id": 11, "email": "same-1@example.com", "leadbee_code": old_refs[0]},
            {"id": 12, "email": "same-2@example.com", "leadbee_code": old_refs[1]},
        ]
        config = {
            "leadbee_api_enabled": "true",
            "leadbee_api_key": "fixture-key",
            "leadbee_api_secret": "fixture-secret",
            "leadbee_api_product_id": "fixture-product",
            "mail_provider": "microsoft",
        }
        with patch("core.config_store.config_store.get_all", return_value=config):
            retry = _build_chatgpt_retry_request(bindings)
            prepared = _prepare_register_request(retry)
        refs = prepared.extra["chatgpt_existing_account_leadbee_client_order_ids"]
        self.assertEqual(len(refs), 2)
        self.assertTrue(set(refs).isdisjoint(old_refs))
        self.assertEqual(
            [item["email"] for item in prepared.extra["chatgpt_retry_bindings"]],
            ["same-1@example.com", "same-2@example.com"],
        )

    def test_retry_preserves_api_fallback_mode_after_a_card_attempt(self):
        bindings = [
            {
                "id": 13,
                "account_id": 43,
                "email": "fallback@example.com",
                "leadbee_code": "previous-card",
                "use_sms_pool": True,
                "sms_mode": "api_fallback_pool",
            }
        ]
        config = {
            "leadbee_api_enabled": "1",
            "leadbee_api_key": "fixture-key",
            "leadbee_api_secret": "fixture-secret",
            "leadbee_api_product_id": "fixture-product",
            "mail_provider": "microsoft",
        }

        with patch("core.config_store.config_store.get_all", return_value=config):
            retry = _build_chatgpt_retry_request(bindings)
            prepared = _prepare_register_request(retry)

        self.assertEqual(
            prepared.extra["chatgpt_existing_account_sms_mode"],
            "api_fallback_pool",
        )
        self.assertTrue(prepared.extra["chatgpt_existing_account_leadbee_api"])
        self.assertFalse(prepared.extra["chatgpt_existing_account_use_sms_pool"])
        self.assertNotEqual(
            prepared.extra["chatgpt_existing_account_leadbee_client_order_ids"][0],
            "previous-card",
        )


class LeadBeeTaskCancellationTests(unittest.TestCase):
    def test_api_completion_passes_client_order_without_card_code(self):
        manager = Mock()
        manager.start.return_value = {
            "session_id": "api-session",
            "status": "completed",
            "provider_cleanup_settled": True,
            "logs": [],
            "expires_in": 600,
        }
        with patch(
            "services.chatgpt_phone_verification.phone_verification_manager",
            manager,
        ):
            result = _complete_chatgpt_leadbee_verification(
                task_id="task-api-completion",
                account_id=101,
                leadbee_code="",
                leadbee_api=True,
                client_order_id="aar_" + "a" * 32,
                control=Mock(),
                attempt_id=101,
            )
        self.assertEqual(result["status"], "completed")
        kwargs = manager.start.call_args.kwargs
        self.assertTrue(kwargs["leadbee_api"])
        self.assertEqual(kwargs["client_order_id"], "aar_" + "a" * 32)
        self.assertNotIn("leadbee_code", kwargs)

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

    def test_card_provider_wait_can_continue_beyond_former_thirty_second_limit(self):
        clock = {"now": 0.0}

        class DelayedProviderSlot:
            def __init__(self):
                self.calls = 0
                self.releases = 0

            def acquire(self, **_kwargs):
                self.calls += 1
                if self.calls == 1:
                    return False
                clock["now"] = 31.0
                return True

            def release(self):
                self.releases += 1

        class Manager:
            ttl_seconds = 600

            @staticmethod
            def start(_account_id, **_kwargs):
                return {
                    "session_id": "phone-session-queued-card",
                    "status": "completed",
                    "provider_cleanup_settled": True,
                    "reused": True,
                    "logs": [],
                    "expires_in": 569,
                }

        provider_slot = DelayedProviderSlot()
        with (
            patch(
                "services.chatgpt_phone_verification.phone_verification_manager",
                Manager(),
            ),
            patch(
                "services.chatgpt_phone_verification.leadbee_phone_flow_lock",
                provider_slot,
            ),
            patch("api.tasks.time.monotonic", side_effect=lambda: clock["now"]),
        ):
            result = _complete_chatgpt_leadbee_verification(
                task_id="task-queued-card",
                account_id=303,
                leadbee_code="fixture-card",
                control=Mock(),
                attempt_id=1,
            )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(provider_slot.calls, 2)
        self.assertEqual(provider_slot.releases, 1)
        self.assertEqual(clock["now"], 31.0)

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

        with (
            patch(
                "services.chatgpt_phone_verification.phone_verification_manager",
                manager,
            ),
            patch(
                "api.tasks.time.monotonic",
                side_effect=[100.0, 100.0, 107.0],
            ),
            patch("api.tasks.time.sleep"),
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
        api_mode=False,
        retry_bindings=None,
        saved_account_ids=None,
        sms_mode="",
        capacity_error=False,
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
            },
        )
        if api_mode:
            req.extra["chatgpt_existing_account_leadbee_api"] = True
            req.extra["chatgpt_existing_account_leadbee_client_order_ids"] = [
                "aar_" + f"{index + 1:032x}" for index in range(count)
            ]
            if sms_mode:
                req.extra["chatgpt_existing_account_sms_mode"] = sms_mode
        else:
            req.extra["chatgpt_existing_account_leadbee_codes"] = (
                ["card-secret-one", "card-secret-two"]
                if count == 2
                else [f"card-secret-{index + 1}" for index in range(count)]
            )
        if retry_bindings:
            req.extra["chatgpt_retry_bindings"] = retry_bindings
        _task_store.create(
            task_id,
            platform="chatgpt",
            total=req.count,
            source="manual",
            meta=None,
        )
        saved = []

        def save_account(account):
            account_id = (
                saved_account_ids[len(saved)]
                if saved_account_ids is not None
                else len(saved) + 1
            )
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

        class CapacityLease:
            def commit(self):
                return None

            def release(self):
                return None

            def quarantine(self):
                return None

        capacity_side_effect = (
            LeadBeeCapacityExhausted("fixture capacity exhausted")
            if capacity_error
            else None
        )

        with (
            patch("core.registry.get", return_value=_ExistingAccountPlatform),
            patch(
                "core.base_mailbox.create_mailbox",
                side_effect=lambda **_: _LoginMailbox(),
            ),
            patch("core.db.save_account", side_effect=save_account),
            patch(
                "core.db.save_account_with_creation_state",
                side_effect=save_account_with_creation_state,
            ),
            patch(
                "core.db.delete_incomplete_chatgpt_account",
                return_value=True,
            ) as cleanup_incomplete,
            patch(
                "core.config_store.config_store.get_all",
                return_value=(
                    {
                        "leadbee_api_enabled": "1",
                        "leadbee_api_key": "api-key-secret",
                        "leadbee_api_secret": "api-secret-secret",
                        "leadbee_api_product_id": "api-product",
                    }
                    if api_mode
                    else {
                        "leadbee_code": "global-card-that-must-not-leak",
                        "chatgpt_leadbee_code": "global-card-alias-that-must-not-leak",
                    }
                ),
            ),
            patch(
                "api.tasks._complete_chatgpt_leadbee_verification",
                side_effect=complete_and_persist,
            ) as complete,
            patch(
                "api.tasks._reserve_chatgpt_leadbee_api_capacity",
                side_effect=capacity_side_effect,
                return_value=CapacityLease(),
            ),
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

    def test_api_capacity_exhaustion_reserves_one_card_and_reuses_phone_stage(self):
        row = SimpleNamespace(
            id=71,
            code="fallback-card-secret",
            base_url="https://sms.example.com/box",
        )
        persisted = []

        def persist(**kwargs):
            persisted.append(dict(kwargs))
            return SimpleNamespace()

        def complete(**kwargs):
            kwargs["on_provider_start"]()
            return {
                "status": "completed",
                "message": "手机验证完成",
                "phone_verified": True,
                "exchange_code_consumed": True,
                "account_id": kwargs["account_id"],
            }

        with (
            patch(
                "api.tasks._upsert_chatgpt_attempt_binding",
                side_effect=persist,
            ),
            patch(
                "api.tasks.sms_pool_service.reserve",
                return_value=[row],
            ) as reserve,
            patch(
                "api.tasks.sms_pool_service.mark_active",
                return_value=True,
            ),
            patch(
                "api.tasks.sms_pool_service.finalize",
                return_value=True,
            ),
            patch("api.tasks.sms_pool_service.release_task"),
        ):
            snapshot, _saved, provider, _logs, _cleanup = self._run(
                "task-chatgpt-api-fallback-card",
                api_mode=True,
                count=1,
                sms_mode="api_fallback_pool",
                capacity_error=True,
                completion=complete,
            )

        reserve.assert_called_once_with(
            task_id="task-chatgpt-api-fallback-card",
            count=1,
            exclude_item_ids=set(),
        )
        self.assertEqual(provider.call_count, 1)
        call = provider.call_args.kwargs
        self.assertFalse(call["leadbee_api"])
        self.assertEqual(call["leadbee_code"], "fallback-card-secret")
        self.assertEqual(snapshot["success"], 1)
        self.assertTrue(
            any(
                row["leadbee_code"] == "fallback-card-secret"
                and row["mailbox_context"].get("sms_pool_managed") is True
                for row in persisted
            )
        )
        self.assertIn("API 余额不足，已切换 SMS 卡密接码", str(snapshot["logs"]))

    def test_api_capacity_exhaustion_without_card_fails_without_provider_call(self):
        with (
            patch(
                "api.tasks.sms_pool_service.reserve",
                side_effect=SmsPoolExhaustedError("fixture empty"),
            ) as reserve,
            patch("api.tasks.sms_pool_service.release_task"),
        ):
            snapshot, _saved, provider, _logs, _cleanup = self._run(
                "task-chatgpt-api-fallback-empty",
                api_mode=True,
                count=1,
                sms_mode="api_fallback_pool",
                capacity_error=True,
                completion=lambda **_kwargs: {
                    "status": "completed",
                },
            )

        reserve.assert_called_once()
        provider.assert_not_called()
        self.assertEqual(snapshot["success"], 0)
        self.assertIn(
            "API 余额不足且卡密池无可用卡密",
            str(snapshot["errors"]),
        )

    def test_explicit_remote_balance_rejection_can_fallback_once(self):
        row = SimpleNamespace(
            id=72,
            code="fallback-card-remote",
            base_url="https://sms.example.com/box",
        )

        def complete(**kwargs):
            if kwargs["leadbee_api"]:
                return {
                    "status": "failed",
                    "message": "LeadBee API 服务返回错误",
                    "provider_error_code": "LEADBEE_API_CAPACITY_EXHAUSTED",
                    "provider_started": True,
                }
            kwargs["on_provider_start"]()
            return {
                "status": "completed",
                "message": "手机验证完成",
                "phone_verified": True,
                "exchange_code_consumed": True,
            }

        with (
            patch(
                "api.tasks.sms_pool_service.reserve",
                return_value=[row],
            ) as reserve,
            patch(
                "api.tasks.sms_pool_service.mark_active",
                return_value=True,
            ),
            patch(
                "api.tasks.sms_pool_service.finalize",
                return_value=True,
            ),
            patch("api.tasks.sms_pool_service.release_task"),
        ):
            snapshot, _saved, provider, _logs, _cleanup = self._run(
                "task-chatgpt-api-remote-balance",
                api_mode=True,
                count=1,
                sms_mode="api_fallback_pool",
                completion=complete,
            )

        self.assertEqual(provider.call_count, 2)
        self.assertTrue(provider.call_args_list[0].kwargs["leadbee_api"])
        self.assertFalse(provider.call_args_list[1].kwargs["leadbee_api"])
        reserve.assert_called_once()
        self.assertEqual(snapshot["success"], 1)

    def test_ambiguous_api_failure_never_reserves_a_fallback_card(self):
        with (
            patch("api.tasks.sms_pool_service.reserve") as reserve,
            patch("api.tasks.sms_pool_service.release_task"),
        ):
            snapshot, _saved, provider, _logs, _cleanup = self._run(
                "task-chatgpt-api-ambiguous-no-fallback",
                api_mode=True,
                count=1,
                sms_mode="api_fallback_pool",
                completion=lambda **_kwargs: {
                    "status": "failed",
                    "message": "LeadBee API 自动接码失败",
                    "provider_error_code": "LEADBEE_API_ERROR",
                    "provider_started": True,
                },
            )

        self.assertEqual(provider.call_count, 1)
        reserve.assert_not_called()
        self.assertEqual(snapshot["success"], 0)

    def test_fallback_card_binding_failure_releases_card_before_provider(self):
        row = SimpleNamespace(
            id=73,
            code="fallback-card-binding",
            base_url="https://sms.example.com/box",
        )

        def persist(**kwargs):
            if kwargs["leadbee_code"] == "fallback-card-binding":
                raise RuntimeError("fixture fallback binding failure")
            return SimpleNamespace()

        with (
            patch(
                "api.tasks._upsert_chatgpt_attempt_binding",
                side_effect=persist,
            ),
            patch(
                "api.tasks.sms_pool_service.reserve",
                return_value=[row],
            ),
            patch(
                "api.tasks.sms_pool_service.finalize",
                return_value=True,
            ) as finalize,
            patch("api.tasks.sms_pool_service.release_task"),
        ):
            snapshot, _saved, provider, _logs, _cleanup = self._run(
                "task-chatgpt-api-fallback-binding-fail",
                api_mode=True,
                count=1,
                sms_mode="api_fallback_pool",
                capacity_error=True,
                completion=lambda **_kwargs: {"status": "completed"},
            )

        provider.assert_not_called()
        finalize.assert_called_once_with(
            item_id=73,
            task_id="task-chatgpt-api-fallback-binding-fail",
            consumed=False,
        )
        self.assertEqual(snapshot["success"], 0)

    def test_api_batch_uses_unique_refs_without_sms_pool_calls(self):
        with (
            patch("api.tasks.sms_pool_service.reserve") as reserve,
            patch("api.tasks.sms_pool_service.mark_active") as mark_active,
            patch("api.tasks.sms_pool_service.mark_restored") as mark_restored,
            patch("api.tasks.sms_pool_service.finalize") as finalize,
            patch("api.tasks.sms_pool_service.release_task") as release_task,
        ):
            snapshot, _saved, complete, _logs, _cleanup = self._run(
                "task-chatgpt-login-phone-api",
                api_mode=True,
                completion=lambda **kwargs: {
                    "status": "completed",
                    "message": "LeadBee API 状态已更新 api-secret-secret",
                    "phone_verified": True,
                    "exchange_code_consumed": False,
                    "account_id": kwargs["account_id"],
                },
            )
        for method in (reserve, mark_active, mark_restored, finalize, release_task):
            method.assert_not_called()
        refs = [call.kwargs["client_order_id"] for call in complete.call_args_list]
        self.assertEqual(len(refs), 2)
        self.assertEqual(len(set(refs)), 2)
        self.assertTrue(all(ref.startswith("aar_") for ref in refs))
        self.assertTrue(all(call.kwargs["leadbee_api"] for call in complete.call_args_list))
        self.assertTrue(all("leadbee_code" not in call.kwargs or not call.kwargs["leadbee_code"] for call in complete.call_args_list))
        self.assertEqual(snapshot["success"], 2)
        self.assertNotIn("api-secret-secret", str(snapshot))

    def test_api_binding_persistence_failure_blocks_all_provider_orders(self):
        with patch(
            "api.tasks._upsert_chatgpt_attempt_binding",
            side_effect=RuntimeError("fixture database unavailable"),
        ):
            snapshot, _saved, complete, _logs, _cleanup = self._run(
                "task-chatgpt-api-binding-fail-closed",
                api_mode=True,
                completion=lambda **_: {
                    "status": "completed",
                    "message": "LeadBee API 状态已更新",
                },
            )

        complete.assert_not_called()
        self.assertEqual(snapshot["success"], 0)
        self.assertEqual(len(snapshot["errors"]), 2)

    def test_legacy_card_binding_persistence_failure_still_runs_provider(self):
        with patch(
            "api.tasks._upsert_chatgpt_attempt_binding",
            side_effect=RuntimeError("fixture database unavailable"),
        ):
            snapshot, _saved, complete, _logs, _cleanup = self._run(
                "task-chatgpt-card-binding-best-effort",
                api_mode=False,
                count=1,
                completion=lambda **kwargs: {
                    "status": "completed",
                    "message": "手机验证完成",
                    "account_id": kwargs["account_id"],
                },
            )

        self.assertEqual(complete.call_count, 1)
        self.assertEqual(snapshot["success"], 1)

    def test_api_partial_binding_failure_blocks_only_undurable_ref(self):
        persisted = []

        def persist(**kwargs):
            if kwargs["attempt_index"] == 1:
                raise RuntimeError("fixture partial persistence failure")
            persisted.append(dict(kwargs))
            return SimpleNamespace()

        with patch(
            "api.tasks._upsert_chatgpt_attempt_binding",
            side_effect=persist,
        ):
            snapshot, _saved, complete, _logs, _cleanup = self._run(
                "task-chatgpt-api-binding-partial-failure",
                api_mode=True,
                completion=lambda **kwargs: {
                    "status": "completed",
                    "message": "LeadBee API 状态已更新",
                    "account_id": kwargs["account_id"],
                },
            )

        self.assertEqual(complete.call_count, 1)
        self.assertEqual(snapshot["success"], 1)
        self.assertEqual(len(snapshot["errors"]), 1)
        self.assertTrue(any(item["attempt_index"] == 0 for item in persisted))

    def test_api_retry_binding_preserves_original_account_id(self):
        persisted = []

        def persist(**kwargs):
            persisted.append(dict(kwargs))
            return SimpleNamespace()

        retry = [{
            "id": 7,
            "account_id": 42,
            "email": "retry-42@example.com",
            "leadbee_api": True,
            "leadbee_code": "aar_" + "f" * 32,
        }]
        with patch(
            "api.tasks._upsert_chatgpt_attempt_binding",
            side_effect=persist,
        ):
            snapshot, _saved, complete, _logs, _cleanup = self._run(
                "task-chatgpt-api-retry-account-id",
                api_mode=True,
                count=1,
                retry_bindings=retry,
                persist_refresh_token_on_completion=False,
                completion=lambda **_: {
                    "status": "failed",
                    "message": "LeadBee API fixture failure",
                },
            )

        self.assertEqual(snapshot["success"], 0)
        self.assertEqual(complete.call_args.kwargs["account_id"], 1)
        self.assertTrue(persisted)
        terminal_rows = [
            item
            for item in persisted
            if item.get("stage") in {"phone", "completed"}
            or item.get("status") in {"failed", "success"}
        ]
        self.assertTrue(terminal_rows)
        self.assertEqual(
            {int(item.get("account_id") or 0) for item in terminal_rows},
            {1},
        )

    def test_retry_account_id_is_retained_when_saved_identity_matches(self):
        persisted = []

        def persist(**kwargs):
            persisted.append(dict(kwargs))
            return SimpleNamespace()

        retry = [{
            "id": 7,
            "account_id": 42,
            "email": "retry-42@example.com",
            "leadbee_api": True,
            "leadbee_code": "aar_" + "f" * 32,
        }]
        with patch(
            "api.tasks._upsert_chatgpt_attempt_binding",
            side_effect=persist,
        ):
            _snapshot, _saved, complete, _logs, _cleanup = self._run(
                "task-chatgpt-api-retry-account-id-valid",
                api_mode=True,
                count=1,
                retry_bindings=retry,
                saved_account_ids=[42],
                persist_refresh_token_on_completion=False,
                completion=lambda **_: {
                    "status": "failed",
                    "message": "LeadBee API fixture failure",
                },
            )

        self.assertEqual(complete.call_args.kwargs["account_id"], 42)
        self.assertEqual(
            {int(item.get("account_id") or 0) for item in persisted},
            {42},
        )

    def test_api_reused_session_reconciles_actual_client_order_id(self):
        persisted = []
        actual_ref = "aar_" + "a" * 32

        def persist(**kwargs):
            persisted.append(dict(kwargs))
            return SimpleNamespace()

        with patch(
            "api.tasks._upsert_chatgpt_attempt_binding",
            side_effect=persist,
        ):
            snapshot, _saved, complete, _logs, _cleanup = self._run(
                "task-chatgpt-api-reused-session-ref",
                api_mode=True,
                count=1,
                completion=lambda **kwargs: {
                    "status": "completed",
                    "message": "LeadBee API 状态已更新",
                    "account_id": kwargs["account_id"],
                    "reused": True,
                    "client_order_id": actual_ref,
                },
            )

        self.assertEqual(snapshot["success"], 1)
        success_rows = [item for item in persisted if item.get("status") == "success"]
        self.assertEqual(len(success_rows), 1)
        self.assertEqual(success_rows[0]["leadbee_code"], actual_ref)

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
        _ExistingAccountPlatform.phone_oauth_browser_context_available = False
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

    def test_unprepared_phone_oauth_uses_browser_snapshot_without_fresh_login(self):
        _ExistingAccountPlatform.phone_oauth_ready = False

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
        self.assertEqual(_ExistingAccountPlatform._counter, 1)
        self.assertEqual(
            _ExistingAccountPlatform.register_emails,
            [None],
        )
        self.assertEqual(_LoginMailbox.requeued, [])
        complete.assert_called_once()
        cleanup_incomplete.assert_not_called()
        joined_logs = "\n".join(snapshot["logs"])
        self.assertNotIn("重新建立一次 OAuth 登录会话", joined_logs)


if __name__ == "__main__":
    unittest.main()
