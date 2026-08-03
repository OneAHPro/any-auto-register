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

    def test_recovery_quarantines_card_after_provider_work_started(self):
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
            self.assertEqual(active.status, "active")
            self.assertEqual(active.used_by_email, "active@example.com")
            self.assertIsNone(active.used_at)
            self.assertEqual(active.reserved_task_id, "")
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

    def test_provider_restoration_returns_active_card_to_task_reservation(self):
        self.pool.import_text(
            "restored-before-retry",
            default_base_url="https://sms.example.com/box",
        )
        reserved = self.pool.reserve(task_id="task-retry-wait", count=1)[0]
        self.assertTrue(
            self.pool.mark_active(
                item_id=reserved.id,
                task_id="task-retry-wait",
                account_email="waiting@example.com",
            )
        )

        self.assertTrue(
            self.pool.mark_restored(
                item_id=reserved.id,
                task_id="task-retry-wait",
            )
        )

        with Session(self.engine) as session:
            row = session.get(SmsPoolItemModel, reserved.id)
            self.assertEqual(row.status, "reserved")
            self.assertEqual(row.reserved_task_id, "task-retry-wait")
            self.assertEqual(row.used_by_email, "")
            self.assertIsNone(row.used_at)

        # Simulate the service restarting while the task is waiting to retry.
        # Persisting the restoration as ``reserved`` must survive process loss
        # and be recovered as an unused card.
        self.assertEqual(self.pool.recover_interrupted(), 1)
        self.assertEqual(self.pool.stats()["unused"], 1)

    def test_new_provider_attempt_after_restoration_is_quarantined_on_restart(self):
        self.pool.import_text(
            "restored-then-retried",
            default_base_url="https://sms.example.com/box",
        )
        reserved = self.pool.reserve(task_id="task-retry-active", count=1)[0]
        self.assertTrue(
            self.pool.mark_active(
                item_id=reserved.id,
                task_id="task-retry-active",
                account_email="retried@example.com",
            )
        )
        self.assertTrue(
            self.pool.mark_restored(
                item_id=reserved.id,
                task_id="task-retry-active",
            )
        )

        # A fresh provider call moves the card back to ``active``. If that
        # attempt is interrupted without another restoration callback, keep
        # it quarantined instead of reissuing or permanently burning it.
        self.assertTrue(
            self.pool.mark_active(
                item_id=reserved.id,
                task_id="task-retry-active",
                account_email="retried@example.com",
            )
        )
        self.assertEqual(self.pool.recover_interrupted(), 1)

        with Session(self.engine) as session:
            row = session.get(SmsPoolItemModel, reserved.id)
            self.assertEqual(row.status, "active")
            self.assertEqual(row.used_by_email, "retried@example.com")
            self.assertIsNone(row.used_at)
            self.assertEqual(row.reserved_task_id, "")

    def test_task_release_persists_explicit_reserved_card_quarantine(self):
        self.pool.import_text(
            "conflicted-card",
            default_base_url="https://sms.example.com/box",
        )
        reserved = self.pool.reserve(task_id="task-conflict", count=1)[0]

        released = self.pool.release_task(
            "task-conflict",
            quarantine_item_ids={int(reserved.id)},
        )

        self.assertEqual(released, 1)
        with Session(self.engine) as session:
            row = session.get(SmsPoolItemModel, reserved.id)
            self.assertEqual(row.status, "active")
            self.assertEqual(row.reserved_task_id, "")
            self.assertIsNone(row.reserved_at)
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
            "provider_cleanup_settled": True,
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
        manager.start.assert_called_once()
        start_args, start_kwargs = manager.start.call_args
        self.assertEqual(start_args, (9,))
        tracked_provider_start = start_kwargs.pop("on_provider_start")
        provider_lock_handoff = start_kwargs.pop("on_provider_lock_handoff")
        self.assertTrue(callable(tracked_provider_start))
        self.assertTrue(callable(provider_lock_handoff))
        self.assertIsNot(tracked_provider_start, provider_started)
        self.assertEqual(
            start_kwargs,
            {
                "leadbee_code": "card-secret",
                "leadbee_base_url": "https://sms.example.com/custom-box",
                "provider_lock_already_held": True,
            },
        )
        provider_started.assert_not_called()

    def test_reused_phone_session_quarantines_card_when_start_callback_raises(self):
        manager = mock.Mock()
        manager.start.return_value = {
            "session_id": "phone-session-existing-owner",
            "status": "starting",
            "message": "existing provider flow",
            "reused": True,
            "provider_started": True,
            "provider_cleanup_settled": False,
            "logs": [],
        }
        provider_slot = threading.BoundedSemaphore(1)
        provider_started = mock.Mock(
            side_effect=RuntimeError("pool state persistence failed")
        )

        with mock.patch(
            "services.chatgpt_phone_verification.phone_verification_manager",
            manager,
        ), mock.patch(
            "services.chatgpt_phone_verification.leadbee_phone_flow_lock",
            provider_slot,
        ):
            result = _complete_chatgpt_leadbee_verification(
                task_id="task-pool-reused-conflict",
                account_id=9,
                leadbee_code="card-secret",
                on_provider_start=provider_started,
                control=mock.Mock(),
                attempt_id=1,
            )

        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["provider_started"])
        self.assertTrue(result["provider_cleanup_settled"])
        self.assertEqual(result["exchange_code_settlement"], "active_unknown")
        self.assertTrue(result["ownership_conflict"])
        self.assertEqual(result["provider_start_callback_error"], "RuntimeError")
        provider_started.assert_called_once_with()
        self.assertTrue(provider_slot.acquire(blocking=False))
        provider_slot.release()

    def test_reused_phone_session_replays_known_settlement_without_unknown_override(self):
        cases = [
            (
                "restored",
                {"provider_started": True, "exchange_code_settlement": "restored"},
                True,
                False,
            ),
            (
                "consumed",
                {"provider_started": True, "exchange_code_settlement": "consumed"},
                False,
                True,
            ),
            (
                "unusable",
                {"provider_started": True, "exchange_code_settlement": "unusable"},
                False,
                True,
            ),
            (
                "not-started",
                {
                    "provider_started": False,
                    "provider_cleanup_settled": True,
                    "exchange_code_settlement": "",
                },
                False,
                False,
            ),
        ]

        for name, published, expect_restored, expect_consumed in cases:
            with self.subTest(name=name):
                manager = mock.Mock()
                manager.start.return_value = {
                    "session_id": f"phone-session-{name}",
                    "status": "persisting",
                    "message": "existing provider flow",
                    "reused": True,
                    "provider_cleanup_settled": False,
                    "logs": [],
                    **published,
                }
                provider_slot = threading.BoundedSemaphore(1)
                provider_started = mock.Mock()
                restored = mock.Mock()
                consumed = mock.Mock()

                with mock.patch(
                    "services.chatgpt_phone_verification.phone_verification_manager",
                    manager,
                ), mock.patch(
                    "services.chatgpt_phone_verification.leadbee_phone_flow_lock",
                    provider_slot,
                ):
                    result = _complete_chatgpt_leadbee_verification(
                        task_id=f"task-pool-known-{name}",
                        account_id=9,
                        leadbee_code="card-secret",
                        on_provider_start=provider_started,
                        on_exchange_code_restored=restored,
                        on_exchange_code_consumed=consumed,
                        control=mock.Mock(),
                        attempt_id=1,
                    )

                self.assertEqual(
                    result["exchange_code_settlement"],
                    published.get("exchange_code_settlement", ""),
                )
                self.assertNotEqual(
                    result["exchange_code_settlement"],
                    "active_unknown",
                )
                self.assertEqual(restored.called, expect_restored)
                self.assertEqual(consumed.called, expect_consumed)
                self.assertEqual(
                    provider_started.called,
                    bool(published.get("provider_started")),
                )
                self.assertTrue(provider_slot.acquire(blocking=False))
                provider_slot.release()

    def test_phone_helper_waits_for_terminal_provider_cleanup(self):
        manager = mock.Mock()
        events = []

        class ProviderSlot:
            def acquire(self, **_kwargs):
                events.append("acquire")
                return True

            def release(self):
                events.append("release")

        provider_slot = ProviderSlot()

        initial = {
            "session_id": "phone-session-cleanup",
            "status": "failed",
            "message": "provider state unknown",
            "provider_started": True,
            "provider_cleanup_settled": False,
            "exchange_code_settlement": "active_unknown",
            "logs": [],
            "expires_in": 600,
        }
        settled = {
            **initial,
            "provider_cleanup_settled": True,
        }

        def start(*_args, **kwargs):
            provider_start = kwargs.get("on_provider_start")
            if callable(provider_start):
                provider_start()
            events.append("provider_started")
            kwargs["on_provider_lock_handoff"]()
            events.append("handoff")
            return initial

        def status(*_args):
            provider_slot.release()
            events.append("cleanup_settled")
            return settled

        manager.start.side_effect = start
        manager.status.side_effect = status

        with mock.patch(
            "services.chatgpt_phone_verification.phone_verification_manager",
            manager,
        ), mock.patch(
            "services.chatgpt_phone_verification.leadbee_phone_flow_lock",
            provider_slot,
        ), mock.patch("api.tasks.time.sleep"):
            result = _complete_chatgpt_leadbee_verification(
                task_id="task-pool-cleanup",
                account_id=9,
                leadbee_code="card-secret",
                control=mock.Mock(),
                attempt_id=1,
            )

        manager.status.assert_called_once_with(9, "phone-session-cleanup")
        self.assertTrue(result["provider_cleanup_settled"])
        self.assertEqual(result["exchange_code_settlement"], "active_unknown")
        self.assertEqual(
            events,
            [
                "acquire",
                "provider_started",
                "handoff",
                "release",
                "cleanup_settled",
            ],
        )

    def test_phone_helper_bounds_refresh_token_finalization_wait(self):
        manager = mock.Mock()
        provider_slot = threading.BoundedSemaphore(1)
        persisting = {
            "session_id": "phone-session-persisting",
            "status": "persisting",
            "message": "saving refresh token",
            "provider_started": True,
            "provider_cleanup_settled": True,
            "exchange_code_settlement": "consumed",
            "exchange_code_consumed": True,
            "logs": [],
            "expires_in": 600,
        }

        def start(*_args, **kwargs):
            provider_slot.release()
            kwargs["on_provider_lock_handoff"]("phone-session-persisting")
            return persisting

        manager.start.side_effect = start
        with mock.patch(
            "services.chatgpt_phone_verification.phone_verification_manager",
            manager,
        ), mock.patch(
            "services.chatgpt_phone_verification.leadbee_phone_flow_lock",
            provider_slot,
        ), mock.patch(
            "api.tasks.CHATGPT_PHONE_FINALIZATION_WAIT_SECONDS",
            0.0,
        ):
            result = _complete_chatgpt_leadbee_verification(
                task_id="task-pool-persist-timeout",
                account_id=9,
                leadbee_code="card-secret",
                control=mock.Mock(),
                attempt_id=1,
            )

        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["finalization_pending"])
        self.assertTrue(result["provider_cleanup_settled"])
        self.assertEqual(result["exchange_code_settlement"], "consumed")
        manager.status.assert_not_called()
        self.assertTrue(provider_slot.acquire(blocking=False))
        provider_slot.release()

    def test_post_start_log_error_waits_for_cleanup_without_leaking_slot(self):
        from services.chatgpt_phone_verification import (
            ChatGPTPhoneVerificationManager,
        )

        provider_slot = threading.BoundedSemaphore(1)
        worker_started = threading.Event()
        release_worker = threading.Event()

        def automatic_runner(_account_id, _exchange_code, broker):
            broker.mark_provider_started()
            worker_started.set()
            release_worker.wait(timeout=1)
            broker.mark_exchange_code_active_unknown("provider state unknown")
            raise RuntimeError("provider failed after observer error")

        manager = ChatGPTPhoneVerificationManager(
            automatic_flow_runner=automatic_runner,
            token_persister=lambda *_args: None,
            status_refresher=lambda _account_id: None,
            start_timeout_seconds=0.01,
        )
        results = []
        errors = []

        def run_helper():
            try:
                results.append(
                    _complete_chatgpt_leadbee_verification(
                        task_id="task-provider-observer-error",
                        account_id=19,
                        leadbee_code="card-secret",
                        control=mock.Mock(),
                        attempt_id=1,
                    )
                )
            except BaseException as exc:
                errors.append(exc)

        with mock.patch(
            "services.chatgpt_phone_verification.leadbee_phone_flow_lock",
            provider_slot,
        ), mock.patch(
            "services.chatgpt_phone_verification.phone_verification_manager",
            manager,
        ), mock.patch(
            "api.tasks._log",
            side_effect=RuntimeError("task log persistence unavailable"),
        ), mock.patch("api.tasks.time.sleep"):
            helper = threading.Thread(target=run_helper)
            helper.start()
            self.assertTrue(worker_started.wait(timeout=1))
            self.assertFalse(provider_slot.acquire(blocking=False))
            release_worker.set()
            helper.join(timeout=1)

            self.assertFalse(helper.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(len(results), 1)
            result = results[0]
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["exchange_code_settlement"], "active_unknown")
            self.assertTrue(result["provider_cleanup_settled"])
            self.assertTrue(provider_slot.acquire(timeout=1))
            provider_slot.release()

    def test_post_handoff_log_error_bounds_blocked_token_persistence(self):
        from services.chatgpt_phone_verification import (
            ChatGPTPhoneVerificationManager,
        )

        provider_slot = threading.BoundedSemaphore(1)
        persister_started = threading.Event()
        release_persister = threading.Event()

        def automatic_runner(_account_id, _exchange_code, broker):
            broker.mark_provider_started()
            return {"refresh_token": "new-rt"}

        def token_persister(_account_id, _tokens):
            persister_started.set()
            release_persister.wait(timeout=2)

        manager = ChatGPTPhoneVerificationManager(
            automatic_flow_runner=automatic_runner,
            token_persister=token_persister,
            status_refresher=lambda _account_id: None,
            start_timeout_seconds=0.05,
        )
        results = []
        errors = []

        def run_helper():
            try:
                results.append(
                    _complete_chatgpt_leadbee_verification(
                        task_id="task-provider-persist-observer-error",
                        account_id=29,
                        leadbee_code="card-secret",
                        control=mock.Mock(),
                        attempt_id=1,
                    )
                )
            except BaseException as exc:
                errors.append(exc)

        try:
            with mock.patch(
                "services.chatgpt_phone_verification.leadbee_phone_flow_lock",
                provider_slot,
            ), mock.patch(
                "services.chatgpt_phone_verification.phone_verification_manager",
                manager,
            ), mock.patch(
                "api.tasks._log",
                side_effect=RuntimeError("task log persistence unavailable"),
            ), mock.patch(
                "api.tasks.CHATGPT_PHONE_FINALIZATION_WAIT_SECONDS",
                0.0,
            ):
                helper = threading.Thread(target=run_helper)
                helper.start()
                self.assertTrue(persister_started.wait(timeout=1))
                helper.join(timeout=0.5)

                self.assertFalse(helper.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual(len(results), 1)
                self.assertEqual(results[0]["status"], "failed")
                self.assertTrue(results[0]["finalization_pending"])
                self.assertTrue(results[0]["provider_cleanup_settled"])
                self.assertTrue(provider_slot.acquire(blocking=False))
                provider_slot.release()
        finally:
            release_persister.set()

        session_id = next(iter(manager._sessions))
        completed = manager._sessions[session_id].wait_until_terminal(1)
        self.assertEqual(completed["status"], "completed")

    def test_task_provider_slot_wait_has_wall_clock_deadline(self):
        clock = {"now": 0.0}
        acquire_calls = 0

        class BusyProviderSlots:
            def acquire(self, **_kwargs):
                nonlocal acquire_calls
                acquire_calls += 1
                clock["now"] += 31.0
                if acquire_calls > 1:
                    raise AssertionError("task slot wait did not honor deadline")
                return False

            def release(self):
                raise AssertionError("unacquired provider slot was released")

        manager = mock.Mock(ttl_seconds=61)
        control = mock.Mock()
        with mock.patch(
            "services.chatgpt_phone_verification.leadbee_phone_flow_lock",
            BusyProviderSlots(),
        ), mock.patch(
            "services.chatgpt_phone_verification.phone_verification_manager",
            manager,
        ), mock.patch(
            "api.tasks.time.monotonic",
            side_effect=lambda: clock["now"],
        ):
            with self.assertRaisesRegex(RuntimeError, "排队.*超时"):
                _complete_chatgpt_leadbee_verification(
                    task_id="task-pool-slot-timeout",
                    account_id=9,
                    leadbee_code="card-secret",
                    control=control,
                    attempt_id=1,
                )

        manager.start.assert_not_called()

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
        req.count = 4
        req.concurrency = 1
        req.extra.update(
            {
                "chatgpt_existing_account_leadbee_codes": [
                    "card-one",
                    "card-two",
                    "card-three",
                    "card-four",
                ],
                "chatgpt_existing_account_leadbee_base_urls": [
                    "https://sms-one.example.com/box",
                    "https://sms-two.example.com/box",
                    "https://sms-three.example.com/box",
                    "https://sms-four.example.com/box",
                ],
                "chatgpt_sms_pool_item_ids": [11, 12, 13, 14],
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
        ]
        stop_during_restored_delay = False

        def complete_phone(**kwargs):
            nonlocal stop_during_restored_delay
            result = phone_results.pop(0)
            if kwargs["leadbee_code"] == "card-one":
                kwargs["on_provider_start"]()
                kwargs["on_exchange_code_consumed"]()
            elif kwargs["leadbee_code"] == "card-three":
                kwargs["on_provider_start"]()
                kwargs["on_exchange_code_consumed"]()
                if result.get("provider_error_code") == "CARD_NOT_IN_SESSION":
                    kwargs["on_exchange_code_restored"]()
            elif kwargs["leadbee_code"] == "card-four":
                kwargs["on_provider_start"]()
                kwargs["on_exchange_code_restored"]()
                stop_during_restored_delay = True
            if str(result.get("status") or "").lower() == "completed":
                saved[int(kwargs["account_id"]) - 1].extra["refresh_token"] = "rt"
            return result

        def interrupt_restored_retry(_seconds):
            if stop_during_restored_delay:
                _task_store.control_for(task_id).request_stop()

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
                "api.tasks.sms_pool_service.mark_restored",
                return_value=True,
            ) as mark_restored,
            mock.patch(
                "api.tasks.sms_pool_service.reserve",
                side_effect=SmsPoolExhaustedError("no spare card"),
            ) as reserve_replacement,
            mock.patch("api.tasks.sms_pool_service.finalize") as finalize,
            mock.patch("api.tasks.sms_pool_service.release_task"),
            mock.patch(
                "api.tasks.time.sleep",
                side_effect=interrupt_restored_retry,
            ),
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
                "https://sms-four.example.com/box",
            ],
        )
        self.assertEqual(
            [call.kwargs["item_id"] for call in mark_active.call_args_list],
            [11, 13, 13, 14],
        )
        self.assertEqual(
            [call.kwargs["item_id"] for call in mark_restored.call_args_list],
            [13, 14],
        )
        # Provider restoration must supersede an earlier conservative
        # "unusable" callback.  Settlement happens once per card so the
        # temporary callback cannot leave a restored card marked as used.
        self.assertEqual(finalize.call_count, 4)
        finalized = {
            item_id: [
                call.kwargs
                for call in finalize.call_args_list
                if call.kwargs["item_id"] == item_id
            ]
            for item_id in (11, 12, 13, 14)
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
        self.assertTrue(all(not call["consumed"] for call in finalized[14]))
        self.assertTrue(
            all(call["restoration_confirmed"] for call in finalized[14])
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
            {11, 12, 13, 14},
        )
        self.assertTrue(all(context["sms_pool_managed"] for context in pool_contexts))
        reserve_replacement.assert_not_called()

    def test_task_keeps_unknown_active_card_quarantined(self):
        task_id = "task-pool-active-unknown"
        req = self._pool_request(count=1)
        req.extra.update(
            {
                "chatgpt_existing_account_leadbee_codes": ["pending-card"],
                "chatgpt_existing_account_leadbee_base_urls": [
                    "https://sms.example.com/box"
                ],
                "chatgpt_sms_pool_item_ids": [11],
            }
        )
        _create_task_record(task_id, req, "manual", None)

        def save_account(account):
            extra = dict(account.extra or {})
            return SimpleNamespace(
                id=1,
                platform=account.platform,
                email=account.email,
                extra=extra,
                extra_json=json.dumps(extra, ensure_ascii=False),
                created_at=datetime(2026, 8, 3, tzinfo=timezone.utc),
            )

        def complete_phone(**kwargs):
            kwargs["on_provider_start"]()
            return {
                "status": "failed",
                "message": "服务端仍未确认卡密恢复",
                "phone_verified": False,
                "exchange_code_consumed": False,
                "exchange_code_unusable": False,
                "exchange_code_settlement": "active_unknown",
            }

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
            ),
            mock.patch(
                "api.tasks._complete_chatgpt_leadbee_verification",
                side_effect=complete_phone,
            ),
            mock.patch("api.tasks._refresh_saved_chatgpt_login", return_value=""),
            mock.patch("api.tasks._auto_upload_integrations"),
            mock.patch("api.tasks._save_task_log"),
            mock.patch("api.tasks._upsert_chatgpt_attempt_binding"),
            mock.patch(
                "api.tasks.sms_pool_service.mark_active",
                return_value=True,
            ) as mark_active,
            mock.patch("api.tasks.sms_pool_service.finalize") as finalize,
            mock.patch("api.tasks.sms_pool_service.release_task"),
            mock.patch("core.proxy_pool.proxy_pool.report_success"),
            mock.patch("core.proxy_pool.proxy_pool.report_fail"),
            mock.patch("core.config_store.config_store.get_all", return_value={}),
        ):
            _run_register(task_id, req)

        mark_active.assert_called_once_with(
            item_id=11,
            task_id=task_id,
            account_email=mock.ANY,
        )
        finalize.assert_not_called()

    def test_reused_session_callback_failure_is_quarantined_after_task_cleanup(self):
        task_id = "task-pool-reuse-callback-failure"
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(engine)
        pool = SmsPoolService(engine)
        pool.import_text(
            "duplicated-active-card",
            default_base_url="https://sms.example.com/box",
        )
        reserved = pool.reserve(task_id=task_id, count=1)[0]

        req = self._pool_request(count=1)
        req.extra.update(
            {
                "chatgpt_existing_account_leadbee_codes": [reserved.code],
                "chatgpt_existing_account_leadbee_base_urls": [reserved.base_url],
                "chatgpt_sms_pool_item_ids": [int(reserved.id)],
            }
        )
        _create_task_record(task_id, req, "manual", None)

        def save_account(account):
            extra = dict(account.extra or {})
            return SimpleNamespace(
                id=1,
                platform=account.platform,
                email=account.email,
                extra=extra,
                extra_json=json.dumps(extra, ensure_ascii=False),
                created_at=datetime(2026, 8, 3, tzinfo=timezone.utc),
            )

        manager = mock.Mock()
        manager.start.return_value = {
            "session_id": "existing-provider-owner",
            "status": "starting",
            "message": "existing provider flow",
            "reused": True,
            "provider_started": True,
            "provider_cleanup_settled": False,
            "logs": [],
        }
        provider_slot = threading.BoundedSemaphore(1)

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
            mock.patch("api.tasks._refresh_saved_chatgpt_login", return_value=""),
            mock.patch("api.tasks._auto_upload_integrations"),
            mock.patch("api.tasks._save_task_log"),
            mock.patch("api.tasks._upsert_chatgpt_attempt_binding"),
            mock.patch(
                "api.tasks._requeue_chatgpt_login_mailbox",
                return_value=True,
            ) as requeue_mailbox,
            mock.patch("api.tasks.sms_pool_service", pool),
            mock.patch.object(pool, "mark_active", return_value=False) as mark_active,
            mock.patch(
                "services.chatgpt_phone_verification.phone_verification_manager",
                manager,
            ),
            mock.patch(
                "services.chatgpt_phone_verification.leadbee_phone_flow_lock",
                provider_slot,
            ),
            mock.patch("core.proxy_pool.proxy_pool.report_success"),
            mock.patch("core.proxy_pool.proxy_pool.report_fail"),
            mock.patch("core.config_store.config_store.get_all", return_value={}),
        ):
            _run_register(task_id, req)

        mark_active.assert_called_once_with(
            item_id=int(reserved.id),
            task_id=task_id,
            account_email=mock.ANY,
        )
        delete_incomplete.assert_not_called()
        requeue_mailbox.assert_not_called()
        with Session(engine) as session:
            row = session.get(SmsPoolItemModel, reserved.id)
            self.assertEqual(row.status, "active")
            self.assertEqual(row.reserved_task_id, "")
            self.assertIsNone(row.reserved_at)
            self.assertIsNone(row.used_at)
        self.assertEqual(pool.stats()["unused"], 0)

    def test_finalization_timeout_preserves_account_and_mailbox_until_persister_finishes(self):
        from services.chatgpt_phone_verification import (
            ChatGPTPhoneVerificationManager,
        )

        task_id = "task-pool-finalization-pending"
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(engine)
        pool = SmsPoolService(engine)
        pool.import_text(
            "not-activated-card",
            default_base_url="https://sms.example.com/box",
        )
        reserved = pool.reserve(task_id=task_id, count=1)[0]

        req = self._pool_request(count=1)
        req.extra.update(
            {
                "chatgpt_existing_account_leadbee_codes": [reserved.code],
                "chatgpt_existing_account_leadbee_base_urls": [reserved.base_url],
                "chatgpt_sms_pool_item_ids": [int(reserved.id)],
            }
        )
        _create_task_record(task_id, req, "manual", None)

        saved_rows = []

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
            saved_rows.append(row)
            return row

        persister_started = threading.Event()
        release_persister = threading.Event()
        persistence_finished = threading.Event()

        def token_persister(_account_id, _tokens):
            persister_started.set()
            release_persister.wait(timeout=2)
            persistence_finished.set()

        manager = ChatGPTPhoneVerificationManager(
            automatic_flow_runner=lambda *_args: {"refresh_token": "late-rt"},
            token_persister=token_persister,
            status_refresher=lambda _account_id: None,
            start_timeout_seconds=0.05,
        )
        provider_slot = threading.BoundedSemaphore(1)

        try:
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
                mock.patch("api.tasks._refresh_saved_chatgpt_login", return_value=""),
                mock.patch("api.tasks._auto_upload_integrations"),
                mock.patch("api.tasks._save_task_log"),
                mock.patch("api.tasks._upsert_chatgpt_attempt_binding"),
                mock.patch(
                    "api.tasks._requeue_chatgpt_login_mailbox",
                    return_value=True,
                ) as requeue_mailbox,
                mock.patch("api.tasks.sms_pool_service", pool),
                mock.patch(
                    "services.chatgpt_phone_verification.phone_verification_manager",
                    manager,
                ),
                mock.patch(
                    "services.chatgpt_phone_verification.leadbee_phone_flow_lock",
                    provider_slot,
                ),
                mock.patch(
                    "api.tasks.CHATGPT_PHONE_FINALIZATION_WAIT_SECONDS",
                    0.0,
                ),
                mock.patch("core.proxy_pool.proxy_pool.report_success"),
                mock.patch("core.proxy_pool.proxy_pool.report_fail"),
                mock.patch("core.config_store.config_store.get_all", return_value={}),
            ):
                _run_register(task_id, req)

            self.assertTrue(persister_started.is_set())
            self.assertFalse(persistence_finished.is_set())
            self.assertGreaterEqual(len(saved_rows), 1)
            delete_incomplete.assert_not_called()
            requeue_mailbox.assert_not_called()

            release_persister.set()
            self.assertTrue(persistence_finished.wait(timeout=1))
            session_id = next(iter(manager._sessions))
            completed = manager._sessions[session_id].wait_until_terminal(1)
            self.assertEqual(completed["status"], "completed")
        finally:
            release_persister.set()

        with Session(engine) as session:
            row = session.get(SmsPoolItemModel, reserved.id)
            self.assertEqual(row.status, "unused")
            self.assertEqual(row.reserved_task_id, "")

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
