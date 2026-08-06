import unittest
import uuid
from types import SimpleNamespace
from unittest import mock

from pydantic import ValidationError
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from core.db import AccountModel


class ChatGPTAccountHardeningTaskTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)
        with Session(self.engine) as session:
            for email in ("first@example.com", "second@example.com"):
                account = AccountModel(
                    platform="chatgpt",
                    email=email,
                    password="password",
                    token="access-token",
                )
                account.set_extra({"access_token": "access-token"})
                session.add(account)
            session.add(
                AccountModel(
                    platform="cursor",
                    email="other@example.com",
                    password="password",
                )
            )
            session.commit()
            self.account_ids = [
                int(row.id)
                for row in session.exec(select(AccountModel)).all()
                if row.platform == "chatgpt"
            ]

    def _task_id(self):
        return f"test_hardening_{uuid.uuid4().hex}"

    def test_request_defaults_to_one_and_caps_concurrency_at_two(self):
        from api.tasks import ChatGPTHardeningTaskRequest

        request = ChatGPTHardeningTaskRequest()
        self.assertEqual(request.account_ids, [])
        self.assertEqual(request.concurrency, 1)
        self.assertFalse(request.dry_run)
        self.assertEqual(ChatGPTHardeningTaskRequest(concurrency=2).concurrency, 2)
        with self.assertRaises(ValidationError):
            ChatGPTHardeningTaskRequest(concurrency=3)

    def test_omitted_ids_select_every_chatgpt_account(self):
        from api.tasks import _resolve_chatgpt_hardening_account_ids

        selected = _resolve_chatgpt_hardening_account_ids(
            [],
            database_engine=self.engine,
        )
        self.assertEqual(selected, self.account_ids)

    def test_task_record_contains_resumable_counters(self):
        from api.tasks import _create_chatgpt_hardening_task_record, _task_store

        task_id = self._task_id()
        with mock.patch("api.tasks._persist_task_snapshot"):
            _create_chatgpt_hardening_task_record(
                task_id,
                self.account_ids,
                concurrency=1,
                dry_run=True,
            )
        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(snapshot["source"], "account_hardening")
        self.assertEqual(snapshot["meta"]["completed_account_ids"], [])
        for key in (
            "ready_before",
            "hardened",
            "recovered_secret",
            "pending_password",
            "missing_mfa_material",
            "failed",
        ):
            self.assertEqual(snapshot["meta"][key], 0)

    def test_runner_updates_all_counters_and_checkpoints(self):
        from api.tasks import (
            _create_chatgpt_hardening_task_record,
            _run_chatgpt_hardening_task,
            _task_store,
        )

        outcomes = {
            self.account_ids[0]: SimpleNamespace(
                status="ready",
                outcome="hardened",
                email="first@example.com",
                message="",
            ),
            self.account_ids[1]: SimpleNamespace(
                status="missing_mfa_material",
                outcome="missing_mfa_material",
                email="second@example.com",
                message="",
            ),
        }

        class FakeService:
            def __init__(self, **_kwargs):
                pass

            def harden_saved_account(self, account_id, **_kwargs):
                return outcomes[account_id]

        task_id = self._task_id()
        with mock.patch("api.tasks._persist_task_snapshot"):
            _create_chatgpt_hardening_task_record(
                task_id,
                self.account_ids,
                concurrency=2,
                dry_run=False,
            )
        with mock.patch(
            "services.chatgpt_account_hardening.ChatGPTAccountHardeningService",
            FakeService,
        ), mock.patch(
            "api.tasks.engine",
            self.engine,
        ), mock.patch(
            "api.tasks._persist_task_snapshot_best_effort",
        ), mock.patch(
            "api.tasks._save_task_log",
        ):
            _run_chatgpt_hardening_task(
                task_id,
                self.account_ids,
                concurrency=2,
                dry_run=False,
            )

        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(snapshot["status"], "done")
        self.assertEqual(snapshot["progress"], "2/2")
        self.assertEqual(snapshot["meta"]["hardened"], 1)
        self.assertEqual(snapshot["meta"]["missing_mfa_material"], 1)
        self.assertEqual(
            sorted(snapshot["meta"]["completed_account_ids"]),
            sorted(self.account_ids),
        )

    def test_stop_before_dispatch_terminalizes_without_calling_service(self):
        from api.tasks import (
            _create_chatgpt_hardening_task_record,
            _run_chatgpt_hardening_task,
            _task_store,
        )

        task_id = self._task_id()
        with mock.patch("api.tasks._persist_task_snapshot"):
            _create_chatgpt_hardening_task_record(
                task_id,
                self.account_ids,
                concurrency=1,
                dry_run=False,
            )
        _task_store.control_for(task_id).request_stop()
        service = mock.Mock()
        with mock.patch(
            "services.chatgpt_account_hardening.ChatGPTAccountHardeningService",
            return_value=service,
        ), mock.patch(
            "api.tasks.engine",
            self.engine,
        ), mock.patch(
            "api.tasks._persist_task_snapshot_best_effort",
        ):
            _run_chatgpt_hardening_task(
                task_id,
                self.account_ids,
                concurrency=1,
                dry_run=False,
            )

        self.assertEqual(_task_store.snapshot(task_id)["status"], "stopped")
        service.harden_saved_account.assert_not_called()

    def test_pending_password_uses_email_relogin_then_retries_hardening(self):
        from api.tasks import (
            _create_chatgpt_hardening_task_record,
            _run_chatgpt_hardening_task,
            _task_store,
        )

        account_id = self.account_ids[0]
        pending = SimpleNamespace(
            status="pending_password",
            outcome="pending_password",
            email="first@example.com",
            message="",
        )
        ready = SimpleNamespace(
            status="ready",
            outcome="hardened",
            email="first@example.com",
            message="",
        )
        service = mock.Mock()
        service.harden_saved_account.side_effect = [pending, ready]
        task_id = self._task_id()
        with mock.patch("api.tasks._persist_task_snapshot"):
            _create_chatgpt_hardening_task_record(
                task_id,
                [account_id],
                concurrency=1,
                dry_run=False,
            )
        with mock.patch(
            "services.chatgpt_account_hardening.ChatGPTAccountHardeningService",
            return_value=service,
        ), mock.patch(
            "services.chatgpt_relogin.relogin_chatgpt_account",
            return_value={"ok": True, "relogin_ok": True},
        ) as relogin, mock.patch(
            "api.tasks.engine",
            self.engine,
        ), mock.patch(
            "api.tasks._persist_task_snapshot_best_effort",
        ), mock.patch(
            "api.tasks._save_task_log",
        ):
            _run_chatgpt_hardening_task(
                task_id,
                [account_id],
                concurrency=1,
                dry_run=False,
            )

        relogin.assert_called_once()
        self.assertEqual(service.harden_saved_account.call_count, 2)
        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(snapshot["meta"]["hardened"], 1)
        self.assertEqual(snapshot["meta"]["pending_password"], 0)

    def test_expired_access_token_relogin_then_retries_hardening(self):
        from api.tasks import (
            _create_chatgpt_hardening_task_record,
            _run_chatgpt_hardening_task,
            _task_store,
        )

        account_id = self.account_ids[0]
        expired = SimpleNamespace(
            status="failed",
            outcome="failed",
            email="first@example.com",
            message="ChatGPTMFAError",
        )
        ready = SimpleNamespace(
            status="ready",
            outcome="hardened",
            email="first@example.com",
            message="",
        )
        service = mock.Mock()
        service.harden_saved_account.side_effect = [expired, ready]
        task_id = self._task_id()
        with mock.patch("api.tasks._persist_task_snapshot"):
            _create_chatgpt_hardening_task_record(
                task_id,
                [account_id],
                concurrency=1,
                dry_run=False,
            )
        with mock.patch(
            "services.chatgpt_account_hardening.ChatGPTAccountHardeningService",
            return_value=service,
        ), mock.patch(
            "services.chatgpt_relogin.relogin_chatgpt_account",
            return_value={"ok": True, "relogin_ok": True},
        ) as relogin, mock.patch(
            "api.tasks.engine",
            self.engine,
        ), mock.patch(
            "api.tasks._persist_task_snapshot_best_effort",
        ), mock.patch(
            "api.tasks._save_task_log",
        ):
            _run_chatgpt_hardening_task(
                task_id,
                [account_id],
                concurrency=1,
                dry_run=False,
            )

        relogin.assert_called_once()
        self.assertEqual(service.harden_saved_account.call_count, 2)
        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(snapshot["meta"]["hardened"], 1)
        self.assertEqual(snapshot["meta"]["failed"], 0)

    def test_missing_mfa_material_with_original_mailbox_relogin_then_retries(self):
        from api.tasks import (
            _create_chatgpt_hardening_task_record,
            _run_chatgpt_hardening_task,
            _task_store,
        )

        account_id = self.account_ids[0]
        missing = SimpleNamespace(
            status="missing_mfa_material",
            outcome="missing_mfa_material",
            email="first@example.com",
            message="",
        )
        ready = SimpleNamespace(
            status="ready",
            outcome="hardened",
            email="first@example.com",
            message="",
        )
        service = mock.Mock()
        service.harden_saved_account.side_effect = [missing, ready]
        task_id = self._task_id()
        with mock.patch("api.tasks._persist_task_snapshot"):
            _create_chatgpt_hardening_task_record(
                task_id,
                [account_id],
                concurrency=1,
                dry_run=False,
            )
        with mock.patch(
            "services.chatgpt_account_hardening.ChatGPTAccountHardeningService",
            return_value=service,
        ), mock.patch(
            "services.chatgpt_relogin.is_saved_chatgpt_account_relogin_eligible",
            return_value=True,
        ) as eligible, mock.patch(
            "services.chatgpt_relogin.relogin_chatgpt_account",
            return_value={"ok": True, "relogin_ok": True},
        ) as relogin, mock.patch(
            "api.tasks.engine",
            self.engine,
        ), mock.patch(
            "api.tasks._persist_task_snapshot_best_effort",
        ), mock.patch(
            "api.tasks._save_task_log",
        ):
            _run_chatgpt_hardening_task(
                task_id,
                [account_id],
                concurrency=1,
                dry_run=False,
            )

        eligible.assert_called_once_with(account_id, database_engine=self.engine)
        relogin.assert_called_once()
        self.assertEqual(service.harden_saved_account.call_count, 2)
        snapshot = _task_store.snapshot(task_id)
        self.assertEqual(snapshot["meta"]["hardened"], 1)
        self.assertEqual(snapshot["meta"]["missing_mfa_material"], 0)


if __name__ == "__main__":
    unittest.main()
