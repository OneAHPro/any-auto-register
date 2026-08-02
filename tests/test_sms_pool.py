import unittest
import tempfile
import threading
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi import BackgroundTasks
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session, create_engine, select

from api.tasks import (
    CHATGPT_BIND_PHONE_FLAG,
    CHATGPT_LEADBEE_CODES_KEY,
    CHATGPT_USE_SMS_POOL_FLAG,
    RegisterTaskRequest,
    _attach_sms_pool_reservation,
    _build_chatgpt_retry_request,
    _complete_chatgpt_leadbee_verification,
    _chatgpt_binding_public,
    _create_task_record,
    enqueue_register_task,
    _prepare_register_request,
    _run_register,
    _task_store,
)
from api.sms_pool import (
    SmsPoolImportRequest,
    get_sms_pool_stats,
    import_sms_pool_items,
    list_sms_pool_items,
)
from core.db import ChatGPTAttemptBindingModel, SmsPoolItemModel
from core.sms_pool import SmsPoolExhaustedError, SmsPoolService
from services.chatgpt_phone_verification import InteractivePhoneVerificationBroker
from tests.test_chatgpt_login_with_phone import (
    _ExistingAccountPlatform,
    _LoginMailbox,
)


class SmsPoolServiceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)
        self.pool = SmsPoolService(self.engine)

    def test_imports_code_only_and_code_with_custom_receive_url(self):
        result = self.pool.import_text(
            "\n".join(
                [
                    "bei-sms-FIRST",
                    "bei-sms-SECOND----https://sms.example.com/custom-box/",
                    "bei-sms-FIRST",
                    "",
                ]
            ),
            default_base_url="https://sms.leadbee.cn/smsbox/",
        )

        self.assertEqual(result["imported"], 2)
        self.assertEqual(result["duplicates"], 1)
        self.assertEqual(result["invalid"], [])
        with Session(self.engine) as session:
            rows = session.exec(
                select(SmsPoolItemModel).order_by(SmsPoolItemModel.id)
            ).all()
        self.assertEqual([row.code for row in rows], ["bei-sms-FIRST", "bei-sms-SECOND"])
        self.assertEqual(
            [row.base_url for row in rows],
            ["https://sms.leadbee.cn/smsbox", "https://sms.example.com/custom-box"],
        )
        self.assertTrue(all(row.status == "unused" for row in rows))

    def test_import_reports_invalid_receive_urls_without_persisting_them(self):
        result = self.pool.import_text(
            "good-card\ninvalid-card----ftp://sms.example.com/box",
            default_base_url="https://sms.example.com/default",
        )

        self.assertEqual(result["imported"], 1)
        self.assertEqual(result["duplicates"], 0)
        self.assertEqual(len(result["invalid"]), 1)
        self.assertEqual(result["invalid"][0]["line"], 2)
        self.assertNotIn("invalid-card", str(result["invalid"]))
        self.assertEqual(self.pool.stats()["total"], 1)

    def test_reservation_is_exclusive_and_finalization_tracks_actual_consumption(self):
        self.pool.import_text(
            "card-one\ncard-two",
            default_base_url="https://sms.example.com/box",
        )

        reserved = self.pool.reserve(task_id="task-a", count=2)

        self.assertEqual([item.code for item in reserved], ["card-one", "card-two"])
        self.assertTrue(all(item.status == "reserved" for item in reserved))
        with self.assertRaisesRegex(SmsPoolExhaustedError, "可用卡密不足"):
            self.pool.reserve(task_id="task-b", count=1)

        self.pool.finalize(
            item_id=int(reserved[0].id),
            task_id="task-a",
            consumed=True,
            account_email="used@example.com",
        )
        self.pool.finalize(
            item_id=int(reserved[1].id),
            task_id="task-a",
            consumed=False,
        )

        with Session(self.engine) as session:
            first = session.get(SmsPoolItemModel, reserved[0].id)
            second = session.get(SmsPoolItemModel, reserved[1].id)
            self.assertEqual(first.status, "used")
            self.assertEqual(first.used_by_email, "used@example.com")
            self.assertIsNotNone(first.used_at)
            self.assertEqual(second.status, "unused")
            self.assertEqual(second.reserved_task_id, "")
        self.assertEqual(
            self.pool.stats(),
            {"total": 2, "unused": 1, "reserved": 0, "used": 1},
        )

    def test_insufficient_reservation_is_atomic(self):
        self.pool.import_text(
            "only-card",
            default_base_url="https://sms.example.com/box",
        )

        with self.assertRaises(SmsPoolExhaustedError):
            self.pool.reserve(task_id="task-too-large", count=2)

        self.assertEqual(self.pool.stats()["unused"], 1)
        self.assertEqual(self.pool.stats()["reserved"], 0)

    def test_replacement_reservation_skips_previously_attempted_cards(self):
        self.pool.import_text(
            "first-card\nsecond-card",
            default_base_url="https://sms.example.com/box",
        )
        first = self.pool.reserve(task_id="task-replacement", count=1)[0]
        self.assertTrue(
            self.pool.finalize(
                item_id=first.id,
                task_id="task-replacement",
                consumed=False,
            )
        )

        replacement = self.pool.reserve(
            task_id="task-replacement",
            count=1,
            exclude_item_ids={int(first.id)},
        )[0]

        self.assertEqual(replacement.code, "second-card")

    def test_separate_service_instances_cannot_reserve_the_same_card(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "sms-pool.db"
            first_engine = create_engine(
                f"sqlite:///{db_path}",
                connect_args={"check_same_thread": False, "timeout": 2},
            )
            SQLModel.metadata.create_all(first_engine)
            first = SmsPoolService(first_engine)
            second = SmsPoolService(
                create_engine(
                    f"sqlite:///{db_path}",
                    connect_args={"check_same_thread": False, "timeout": 2},
                )
            )
            first.import_text(
                "shared-card",
                default_base_url="https://sms.example.com/box",
            )
            barrier = threading.Barrier(2)
            outcomes = []

            def reserve(service, task_id):
                barrier.wait()
                try:
                    service.reserve(task_id=task_id, count=1)
                    outcomes.append("reserved")
                except SmsPoolExhaustedError:
                    outcomes.append("exhausted")

            threads = [
                threading.Thread(target=reserve, args=(first, "task-one")),
                threading.Thread(target=reserve, args=(second, "task-two")),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)

            self.assertEqual(sorted(outcomes), ["exhausted", "reserved"])

    def test_api_import_and_list_never_return_full_card_secrets(self):
        with (
            mock.patch("api.sms_pool.engine", self.engine),
            mock.patch("api.sms_pool.sms_pool_service", self.pool),
        ):
            imported = import_sms_pool_items(
                SmsPoolImportRequest(
                    content="bei-sms-API-SECRET-0001",
                    default_base_url="https://sms.example.com/box",
                )
            )
            response = list_sms_pool_items(status=None, page=1, page_size=50)
            stats = get_sms_pool_stats()

        self.assertEqual(imported["imported"], 1)
        self.assertEqual(stats["unused"], 1)
        serialized = str(response)
        self.assertNotIn("bei-sms-API-SECRET-0001", serialized)
        self.assertIn("bei-****-0001", serialized)

    def test_recovery_releases_reservations_left_by_an_interrupted_process(self):
        self.pool.import_text(
            "card-one",
            default_base_url="https://sms.example.com/box",
        )
        self.pool.reserve(task_id="task-interrupted", count=1)

        recovered = self.pool.recover_interrupted()

        self.assertEqual(recovered, 1)
        self.assertEqual(self.pool.stats()["unused"], 1)

    def test_recovery_never_requeues_a_card_after_provider_work_started(self):
        self.pool.import_text(
            "active-card\nnot-started-card",
            default_base_url="https://sms.example.com/box",
        )
        reserved = self.pool.reserve(task_id="task-interrupted", count=2)
        self.assertTrue(
            self.pool.mark_active(
                item_id=reserved[0].id,
                task_id="task-interrupted",
                account_email="active@example.com",
            )
        )
        self.assertEqual(self.pool.stats()["reserved"], 2)
        self.assertEqual(self.pool.stats()["used"], 0)

        recovered = self.pool.recover_interrupted()

        self.assertEqual(recovered, 2)
        with Session(self.engine) as session:
            active = session.get(SmsPoolItemModel, reserved[0].id)
            not_started = session.get(SmsPoolItemModel, reserved[1].id)
            self.assertEqual(active.status, "used")
            self.assertEqual(active.used_by_email, "active@example.com")
            self.assertIsNotNone(active.used_at)
            self.assertEqual(not_started.status, "unused")

    def test_active_card_without_confirmed_restoration_is_conservatively_used(self):
        self.pool.import_text(
            "active-card",
            default_base_url="https://sms.example.com/box",
        )
        reserved = self.pool.reserve(task_id="task-active", count=1)[0]
        self.assertTrue(
            self.pool.mark_active(
                item_id=reserved.id,
                task_id="task-active",
                account_email="active@example.com",
            )
        )

        self.assertTrue(
            self.pool.finalize(
                item_id=reserved.id,
                task_id="task-active",
                consumed=False,
            )
        )

        with Session(self.engine) as session:
            row = session.get(SmsPoolItemModel, reserved.id)
            self.assertEqual(row.status, "used")
            self.assertEqual(row.used_by_email, "active@example.com")
            self.assertIsNotNone(row.used_at)

    def test_active_card_is_released_only_after_confirmed_provider_restoration(self):
        self.pool.import_text(
            "restored-card",
            default_base_url="https://sms.example.com/box",
        )
        reserved = self.pool.reserve(task_id="task-restored", count=1)[0]
        self.assertTrue(
            self.pool.mark_active(
                item_id=reserved.id,
                task_id="task-restored",
                account_email="restored@example.com",
            )
        )

        self.assertTrue(
            self.pool.finalize(
                item_id=reserved.id,
                task_id="task-restored",
                consumed=False,
                restoration_confirmed=True,
            )
        )

        with Session(self.engine) as session:
            row = session.get(SmsPoolItemModel, reserved.id)
            self.assertEqual(row.status, "unused")
            self.assertEqual(row.used_by_email, "")
            self.assertIsNone(row.used_at)


class SmsPoolTaskIntegrationTests(unittest.TestCase):
    def setUp(self):
        _ExistingAccountPlatform.reset()
        persistence = mock.patch("api.tasks._persist_task_snapshot")
        persistence.start()
        self.addCleanup(persistence.stop)

    @staticmethod
    def _pool_request(*, count=2):
        return RegisterTaskRequest(
            platform="chatgpt",
            count=count,
            concurrency=count,
            proxy="http://proxy.local:8080",
            extra={
                "mail_provider": "microsoft",
                "chatgpt_existing_account_login_only": True,
                CHATGPT_BIND_PHONE_FLAG: True,
                "chatgpt_existing_account_use_sms_pool": True,
            },
        )

    def test_prepare_accepts_pool_mode_without_browser_supplied_card_secrets(self):
        prepared = _prepare_register_request(self._pool_request())

        self.assertTrue(prepared.extra["chatgpt_existing_account_use_sms_pool"])
        self.assertNotIn("chatgpt_existing_account_leadbee_codes", prepared.extra)

    def test_pool_tasks_created_in_the_same_millisecond_get_distinct_ids(self):
        with (
            mock.patch("api.tasks.time.time", return_value=1.0),
            mock.patch("api.tasks._attach_sms_pool_reservation"),
            mock.patch("api.tasks._create_task_record"),
        ):
            first = enqueue_register_task(
                self._pool_request(count=1),
                background_tasks=BackgroundTasks(),
            )
            second = enqueue_register_task(
                self._pool_request(count=1),
                background_tasks=BackgroundTasks(),
            )

        self.assertNotEqual(first, second)

    def test_attaching_reservation_injects_per_attempt_codes_urls_and_ids(self):
        prepared = _prepare_register_request(self._pool_request())
        reserved = [
            SimpleNamespace(
                id=11,
                code="card-one",
                base_url="https://sms-one.example.com/box",
            ),
            SimpleNamespace(
                id=12,
                code="card-two",
                base_url="https://sms-two.example.com/box",
            ),
        ]

        with mock.patch("api.tasks.sms_pool_service.reserve", return_value=reserved) as reserve:
            _attach_sms_pool_reservation("task-pool", prepared)

        reserve.assert_called_once_with(task_id="task-pool", count=2)
        self.assertEqual(
            prepared.extra["chatgpt_existing_account_leadbee_codes"],
            ["card-one", "card-two"],
        )
        self.assertEqual(
            prepared.extra["chatgpt_existing_account_leadbee_base_urls"],
            ["https://sms-one.example.com/box", "https://sms-two.example.com/box"],
        )
        self.assertEqual(prepared.extra["chatgpt_sms_pool_item_ids"], [11, 12])

    def test_pool_retry_keeps_the_email_binding_but_reserves_a_fresh_card(self):
        binding = ChatGPTAttemptBindingModel(
            id=41,
            task_id="task-original",
            attempt_index=0,
            email="bound@example.com",
            leadbee_code="already-used-card",
            status="failed",
            mailbox_context_json=(
                '{"provider":"microsoft","sms_pool_managed":true,'
                '"sms_pool_item_id":11}'
            ),
        )
        with mock.patch(
            "core.config_store.config_store.get_all",
            return_value={"default_executor": "headless"},
        ):
            retry_request = _build_chatgpt_retry_request([binding])

        self.assertTrue(retry_request.extra[CHATGPT_USE_SMS_POOL_FLAG])
        self.assertNotIn(CHATGPT_LEADBEE_CODES_KEY, retry_request.extra)
        self.assertEqual(
            retry_request.extra["chatgpt_retry_bindings"][0]["email"],
            "bound@example.com",
        )

        with mock.patch("core.config_store.config_store.get", return_value=""):
            prepared = _prepare_register_request(retry_request)
        fresh = [
            SimpleNamespace(
                id=12,
                code="fresh-card",
                base_url="https://sms.example.com/fresh-box",
            )
        ]
        with mock.patch("api.tasks.sms_pool_service.reserve", return_value=fresh):
            _attach_sms_pool_reservation("task-retry", prepared)

        self.assertEqual(
            prepared.extra[CHATGPT_LEADBEE_CODES_KEY],
            ["fresh-card"],
        )

    def test_phone_session_receives_the_reserved_items_receive_url(self):
        manager = mock.Mock()
        manager.start.return_value = {
            "session_id": "phone-session",
            "status": "completed",
            "exchange_code_consumed": True,
            "logs": [],
        }

        provider_started = mock.Mock()
        with mock.patch(
            "services.chatgpt_phone_verification.phone_verification_manager",
            manager,
        ):
            result = _complete_chatgpt_leadbee_verification(
                task_id="task-pool",
                account_id=9,
                leadbee_code="card-secret",
                leadbee_base_url="https://sms.example.com/custom-box",
                on_provider_start=provider_started,
                control=mock.Mock(),
                attempt_id=1,
            )

        self.assertEqual(result["status"], "completed")
        manager.start.assert_called_once_with(
            9,
            leadbee_code="card-secret",
            leadbee_base_url="https://sms.example.com/custom-box",
            on_provider_start=provider_started,
            provider_lock_already_held=True,
        )
        provider_started.assert_not_called()

    def test_receiving_an_sms_marks_the_pool_card_consumed_immediately_once(self):
        consumed = mock.Mock()
        broker = InteractivePhoneVerificationBroker(
            account_id=9,
            provider="leadbee",
            request_fingerprint="fingerprint",
            on_exchange_code_consumed=consumed,
        )

        broker.mark_automatic_code_received()
        broker.mark_automatic_code_received()

        consumed.assert_called_once_with()
        self.assertTrue(broker.snapshot()["exchange_code_consumed"])

    def test_retryable_binding_redacts_a_card_echoed_inside_the_error_message(self):
        row = SimpleNamespace(
            id=1,
            task_id="task-secret",
            attempt_index=0,
            email="user@example.com",
            account_id=0,
            stage="phone",
            status="failed",
            error="provider rejected card-secret-value",
            leadbee_code="card-secret-value",
            retry_count=0,
        )

        public = _chatgpt_binding_public(row)

        self.assertNotIn("card-secret-value", str(public))
        self.assertIn("[卡密已隐藏]", public["error"])

    def test_task_preserves_provider_lifecycle_when_finalizing_pool_cards(self):
        task_id = "task-pool-finalize"
        req = self._pool_request()
        req.count = 3
        req.concurrency = 1
        req.extra.update(
            {
                "chatgpt_existing_account_leadbee_codes": [
                    "card-one",
                    "card-two",
                    "card-three",
                ],
                "chatgpt_existing_account_leadbee_base_urls": [
                    "https://sms-one.example.com/box",
                    "https://sms-two.example.com/box",
                    "https://sms-three.example.com/box",
                ],
                "chatgpt_sms_pool_item_ids": [11, 12, 13],
            }
        )
        _create_task_record(task_id, req, "manual", None)
        saved = []

        def save_account(account):
            extra = dict(account.extra or {})
            row = SimpleNamespace(
                id=len(saved) + 1,
                platform=account.platform,
                email=account.email,
                extra=extra,
                extra_json=json.dumps(extra, ensure_ascii=False),
                created_at=datetime(2026, 8, 3, tzinfo=timezone.utc),
            )
            saved.append(row)
            return row

        phone_results = [
            {
                "status": "failed",
                "message": "消费回调已触发，但结果快照延迟",
                "phone_verified": False,
                "exchange_code_consumed": False,
            },
            {
                "status": "completed",
                "message": "已有手机号，卡密未使用",
                "phone_verified": False,
                "exchange_code_consumed": False,
            },
            {
                "status": "failed",
                "message": (
                    "获取手机号失败: LeadBee 当前暂时无可用号码，"
                    "卡密已自动释放"
                ),
                "phone_verified": False,
                "phone": "",
                "provider_error_code": "CARD_NOT_IN_SESSION",
                "exchange_code_consumed": False,
                "exchange_code_restoration_confirmed": True,
            },
            {
                "status": "completed",
                "message": "原卡重新排队后手机验证完成",
                "phone_verified": True,
                "phone": "+15555550123",
                "exchange_code_consumed": True,
                "exchange_code_restoration_confirmed": False,
            },
        ]

        def complete_phone(**kwargs):
            result = phone_results.pop(0)
            if kwargs["leadbee_code"] == "card-one":
                kwargs["on_provider_start"]()
                kwargs["on_exchange_code_consumed"]()
            elif kwargs["leadbee_code"] == "card-three":
                kwargs["on_provider_start"]()
                kwargs["on_exchange_code_consumed"]()
                if result.get("provider_error_code") == "CARD_NOT_IN_SESSION":
                    kwargs["on_exchange_code_restored"]()
            if str(result.get("status") or "").lower() == "completed":
                saved[int(kwargs["account_id"]) - 1].extra["refresh_token"] = "rt"
            return result

        with (
            mock.patch("core.registry.get", return_value=_ExistingAccountPlatform),
            mock.patch("core.base_mailbox.create_mailbox", side_effect=lambda **_: _LoginMailbox()),
            mock.patch("core.db.save_account", side_effect=save_account),
            mock.patch(
                "core.db.save_account_with_creation_state",
                side_effect=lambda account: (save_account(account), True),
            ),
            mock.patch("core.db.delete_incomplete_chatgpt_account", return_value=True),
            mock.patch(
                "api.tasks._complete_chatgpt_leadbee_verification",
                side_effect=complete_phone,
            ) as complete,
            mock.patch("api.tasks._refresh_saved_chatgpt_login", return_value=""),
            mock.patch(
                "api.tasks._reload_saved_account",
                side_effect=lambda _account_id, fallback: fallback,
            ),
            mock.patch("api.tasks._auto_upload_integrations"),
            mock.patch("api.tasks._save_task_log"),
            mock.patch("api.tasks._upsert_chatgpt_attempt_binding") as persist_binding,
            mock.patch(
                "api.tasks.sms_pool_service.mark_active",
                return_value=True,
            ) as mark_active,
            mock.patch(
                "api.tasks.sms_pool_service.reserve",
                side_effect=SmsPoolExhaustedError("no spare card"),
            ) as reserve_replacement,
            mock.patch("api.tasks.sms_pool_service.finalize") as finalize,
            mock.patch("api.tasks.sms_pool_service.release_task"),
            mock.patch("api.tasks.time.sleep"),
            mock.patch("core.proxy_pool.proxy_pool.report_success"),
            mock.patch("core.proxy_pool.proxy_pool.report_fail"),
            mock.patch("core.config_store.config_store.get_all", return_value={}),
        ):
            _run_register(task_id, req)

        self.assertEqual(
            [call.kwargs["leadbee_base_url"] for call in complete.call_args_list],
            [
                "https://sms-one.example.com/box",
                "https://sms-two.example.com/box",
                "https://sms-three.example.com/box",
                "https://sms-three.example.com/box",
            ],
        )
        self.assertEqual(
            [call.kwargs["item_id"] for call in mark_active.call_args_list],
            [11, 13, 13],
        )
        # Provider restoration must supersede an earlier conservative
        # "unusable" callback.  Settlement happens once per card so the
        # temporary callback cannot leave a restored card marked as used.
        self.assertEqual(finalize.call_count, 3)
        finalized = {
            item_id: [
                call.kwargs
                for call in finalize.call_args_list
                if call.kwargs["item_id"] == item_id
            ]
            for item_id in (11, 12, 13)
        }
        self.assertTrue(all(call["consumed"] for call in finalized[11]))
        self.assertTrue(all(not call["consumed"] for call in finalized[12]))
        self.assertTrue(all(call["consumed"] for call in finalized[13]))
        self.assertTrue(
            all(not call["restoration_confirmed"] for call in finalized[12])
        )
        self.assertTrue(
            all(not call["restoration_confirmed"] for call in finalized[13])
        )
        self.assertTrue(all(call["task_id"] == task_id for call in finalized[11]))
        self.assertTrue(all(call["task_id"] == task_id for call in finalized[12]))
        self.assertTrue(all(call["task_id"] == task_id for call in finalized[13]))
        pool_contexts = [
            call.kwargs["mailbox_context"]
            for call in persist_binding.call_args_list
            if call.kwargs.get("mailbox_context")
        ]
        self.assertEqual(
            {context["sms_pool_item_id"] for context in pool_contexts},
            {11, 12, 13},
        )
        self.assertTrue(all(context["sms_pool_managed"] for context in pool_contexts))
        reserve_replacement.assert_not_called()

    def test_task_switches_to_spare_pool_card_without_repeating_email_login(self):
        task_id = "task-pool-card-failover"
        req = self._pool_request(count=1)
        req.extra.update(
            {
                "chatgpt_existing_account_leadbee_codes": ["occupied-card"],
                "chatgpt_existing_account_leadbee_base_urls": [
                    "https://sms-one.example.com/box"
                ],
                "chatgpt_sms_pool_item_ids": [11],
            }
        )
        _create_task_record(task_id, req, "manual", None)
        saved = []

        def save_account(account):
            extra = dict(account.extra or {})
            row = SimpleNamespace(
                id=1,
                platform=account.platform,
                email=account.email,
                extra=extra,
                extra_json=json.dumps(extra, ensure_ascii=False),
                created_at=datetime(2026, 8, 3, tzinfo=timezone.utc),
            )
            saved.append(row)
            return row

        phone_codes = []

        def complete_phone(**kwargs):
            phone_codes.append(kwargs["leadbee_code"])
            kwargs["on_provider_start"]()
            if kwargs["leadbee_code"] == "occupied-card":
                kwargs["on_exchange_code_consumed"]()
                return {
                    "status": "failed",
                    "message": "兑换码已使用",
                    "provider_error_code": "CARD_ALREADY_USED",
                    "phone": "",
                    "exchange_code_consumed": True,
                    "exchange_code_restoration_confirmed": False,
                }
            kwargs["on_exchange_code_consumed"]()
            saved[0].extra["refresh_token"] = "new-refresh-token"
            return {
                "status": "completed",
                "message": "手机验证完成，Refresh Token 已保存",
                "phone": "+15555550123",
                "phone_verified": True,
                "exchange_code_consumed": True,
            }

        replacement = SimpleNamespace(
            id=12,
            code="replacement-card",
            base_url="https://sms-two.example.com/box",
        )

        with (
            mock.patch("core.registry.get", return_value=_ExistingAccountPlatform),
            mock.patch(
                "core.base_mailbox.create_mailbox",
                side_effect=lambda **_: _LoginMailbox(),
            ),
            mock.patch("core.db.save_account", side_effect=save_account),
            mock.patch(
                "core.db.save_account_with_creation_state",
                side_effect=lambda account: (save_account(account), True),
            ),
            mock.patch(
                "core.db.delete_incomplete_chatgpt_account",
                return_value=True,
            ) as delete_incomplete,
            mock.patch(
                "api.tasks._complete_chatgpt_leadbee_verification",
                side_effect=complete_phone,
            ),
            mock.patch("api.tasks._refresh_saved_chatgpt_login", return_value=""),
            mock.patch(
                "api.tasks._reload_saved_account",
                side_effect=lambda _account_id, fallback: saved[0],
            ),
            mock.patch("api.tasks._auto_upload_integrations"),
            mock.patch("api.tasks._save_task_log"),
            mock.patch("api.tasks._upsert_chatgpt_attempt_binding") as persist_binding,
            mock.patch(
                "api.tasks.sms_pool_service.reserve",
                return_value=[replacement],
            ) as reserve,
            mock.patch(
                "api.tasks.sms_pool_service.mark_active",
                return_value=True,
            ),
            mock.patch(
                "api.tasks.sms_pool_service.finalize",
                return_value=True,
            ) as finalize,
            mock.patch("api.tasks.sms_pool_service.release_task"),
            mock.patch("core.proxy_pool.proxy_pool.report_success"),
            mock.patch("core.proxy_pool.proxy_pool.report_fail"),
            mock.patch("core.config_store.config_store.get_all", return_value={}),
        ):
            _run_register(task_id, req)

        self.assertEqual(_ExistingAccountPlatform._counter, 1)
        self.assertEqual(phone_codes, ["occupied-card", "replacement-card"])
        reserve.assert_called_once_with(
            task_id=task_id,
            count=1,
            exclude_item_ids={11},
        )
        delete_incomplete.assert_not_called()
        self.assertTrue(
            any(
                call.kwargs.get("item_id") == 11
                and call.kwargs.get("consumed") is True
                and call.kwargs.get("restoration_confirmed") is False
                for call in finalize.call_args_list
            )
        )
        self.assertTrue(
            any(
                call.kwargs.get("item_id") == 12
                and call.kwargs.get("consumed") is True
                for call in finalize.call_args_list
            )
        )
        self.assertTrue(
            any(
                call.kwargs.get("leadbee_code") == "replacement-card"
                and (call.kwargs.get("mailbox_context") or {}).get(
                    "sms_pool_item_id"
                )
                == 12
                for call in persist_binding.call_args_list
            )
        )


if __name__ == "__main__":
    unittest.main()
