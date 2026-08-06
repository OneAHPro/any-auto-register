import unittest
import inspect
from types import SimpleNamespace
from unittest import mock

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from core.base_platform import Account
from core import db as db_module
from core.db import AccountModel, save_account


class ChatGPTAccountPersistenceTests(unittest.TestCase):
    @staticmethod
    def _test_engine():
        test_engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(test_engine)
        return test_engine

    def test_post_persistence_hardening_hook_is_best_effort_and_secret_free(self):
        from api.tasks import _harden_saved_chatgpt_account_best_effort

        saved = AccountModel(
            id=17,
            platform="chatgpt",
            email="user@example.com",
            password="password",
            token="access-token",
        )
        service = mock.Mock()
        service.harden_authenticated_account.return_value = SimpleNamespace(
            status="ready",
            outcome="hardened",
            message="",
        )
        logs = []
        with mock.patch(
            "services.chatgpt_account_hardening.ChatGPTAccountHardeningService",
            return_value=service,
        ), mock.patch("api.tasks._log", side_effect=lambda _task, line: logs.append(line)):
            status = _harden_saved_chatgpt_account_best_effort("task-1", saved)

        self.assertEqual(status, "ready")
        service.harden_authenticated_account.assert_called_once_with(17)
        self.assertTrue(any("MFA" in line for line in logs))

        service.harden_authenticated_account.side_effect = RuntimeError(
            "secret=JBSWY3DPEHPK3PXP"
        )
        with mock.patch(
            "services.chatgpt_account_hardening.ChatGPTAccountHardeningService",
            return_value=service,
        ), mock.patch("api.tasks._log") as log:
            status = _harden_saved_chatgpt_account_best_effort("task-2", saved)

        self.assertEqual(status, "hardening_pending")
        rendered = " ".join(str(call) for call in log.call_args_list)
        self.assertNotIn("JBSWY3DPEHPK3PXP", rendered)

    def test_at_only_login_preserves_existing_rt_and_account_metadata(self):
        test_engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(test_engine)
        existing = AccountModel(
            platform="chatgpt",
            email="existing@example.com",
            password="old-password",
            token="old-at",
        )
        existing.set_extra(
            {
                "access_token": "old-at",
                "refresh_token": "existing-rt",
                "sync_statuses": {"cpa": {"uploaded": True}},
            }
        )
        with Session(test_engine) as session:
            session.add(existing)
            session.commit()

        staged_login = Account(
            platform="chatgpt",
            email="existing@example.com",
            password="mailbox-password",
            token="new-at",
            extra={
                "access_token": "new-at",
                "refresh_token": "",
                "session_token": "new-session",
                "chatgpt_token_source": "existing_account_web_login",
                "chatgpt_phone_verification_required": True,
            },
        )

        with mock.patch("core.db.engine", test_engine):
            saved = save_account(staged_login)

        self.assertEqual(saved.token, "new-at")
        self.assertEqual(saved.get_extra()["access_token"], "new-at")
        self.assertEqual(saved.get_extra()["refresh_token"], "existing-rt")
        self.assertEqual(
            saved.get_extra()["sync_statuses"],
            {"cpa": {"uploaded": True}},
        )
        self.assertEqual(saved.get_extra()["session_token"], "new-session")

    def test_save_account_reports_whether_this_call_created_the_row(self):
        test_engine = self._test_engine()
        saver = getattr(db_module, "save_account_with_creation_state", None)
        self.assertIsNotNone(saver, "missing atomic account creation-state result")

        first = Account(
            platform="chatgpt",
            email="new@example.com",
            password="password",
            token="access-token",
            extra={"access_token": "access-token", "refresh_token": ""},
        )
        second = Account(
            platform="chatgpt",
            email="new@example.com",
            password="password",
            token="new-access-token",
            extra={
                "access_token": "new-access-token",
                "refresh_token": "",
                "chatgpt_token_source": "existing_account_web_login",
            },
        )

        with mock.patch("core.db.engine", test_engine):
            first_saved, first_created = saver(first)
            second_saved, second_created = saver(second)

        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(first_saved.id, second_saved.id)

    def test_failed_phone_oauth_prepare_clears_stale_resume_context(self):
        test_engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(test_engine)
        existing = AccountModel(
            platform="chatgpt",
            email="existing@example.com",
            password="old-password",
            token="old-at",
        )
        existing.set_extra(
            {
                "access_token": "old-at",
                "refresh_token": "existing-rt",
                "oauth_resume_context": {
                    "version": 2,
                    "oauth_state": "stale-state",
                },
            }
        )
        with Session(test_engine) as session:
            session.add(existing)
            session.commit()

        staged_login = Account(
            platform="chatgpt",
            email="existing@example.com",
            password="",
            token="new-at",
            extra={
                "access_token": "new-at",
                "refresh_token": "",
                "chatgpt_token_source": "existing_account_web_login",
                "phone_oauth_ready": False,
                "phone_oauth_prepare_error": "OAuth bootstrap failed",
            },
        )

        with mock.patch("core.db.engine", test_engine):
            saved = save_account(staged_login)

        saved_extra = saved.get_extra()
        self.assertFalse(saved_extra["phone_oauth_ready"])
        self.assertEqual(saved_extra["refresh_token"], "existing-rt")
        self.assertNotIn("oauth_resume_context", saved_extra)

    def test_targeted_cleanup_deletes_only_the_same_incomplete_chatgpt_row(self):
        test_engine = self._test_engine()
        incomplete = AccountModel(
            platform="chatgpt",
            email="incomplete@example.com",
            password="password",
        )
        incomplete.set_extra({"access_token": "at", "refresh_token": ""})
        valid = AccountModel(
            platform="chatgpt",
            email="valid@example.com",
            password="password",
        )
        valid.set_extra({"refresh_token": "rt-valid"})
        other = AccountModel(
            platform="qwen",
            email="other@example.com",
            password="password",
        )
        with Session(test_engine) as session:
            session.add_all([incomplete, valid, other])
            session.commit()
            session.refresh(incomplete)
            session.refresh(valid)
            session.refresh(other)
            identities = {
                row.email: (
                    int(row.id),
                    row.email,
                    row.created_at,
                    row.extra_json,
                )
                for row in (incomplete, valid, other)
            }

        cleanup = getattr(
            db_module,
            "delete_incomplete_chatgpt_account",
            None,
        )
        self.assertIsNotNone(cleanup, "missing targeted incomplete-account cleanup")

        (
            incomplete_id,
            incomplete_email,
            incomplete_created_at,
            incomplete_extra_json,
        ) = identities[
            "incomplete@example.com"
        ]
        self.assertTrue(
            cleanup(
                incomplete_id,
                expected_email=incomplete_email,
                expected_created_at=incomplete_created_at,
                expected_extra_json=incomplete_extra_json,
                database_engine=test_engine,
            )
        )
        for email in ("valid@example.com", "other@example.com"):
            (
                account_id,
                expected_email,
                expected_created_at,
                expected_extra_json,
            ) = identities[email]
            self.assertFalse(
                cleanup(
                    account_id,
                    expected_email=expected_email,
                    expected_created_at=expected_created_at,
                    expected_extra_json=expected_extra_json,
                    database_engine=test_engine,
                )
            )

        with Session(test_engine) as session:
            self.assertIsNone(session.get(AccountModel, incomplete_id))
            self.assertIsNotNone(
                session.get(AccountModel, identities["valid@example.com"][0])
            )
            self.assertIsNotNone(
                session.get(AccountModel, identities["other@example.com"][0])
            )

    def test_targeted_cleanup_preserves_a_row_changed_after_this_attempt_saved_it(self):
        test_engine = self._test_engine()
        account = AccountModel(
            platform="chatgpt",
            email="changed@example.com",
            password="password",
            extra_json='{"refresh_token":"","attempt":"original"}',
        )
        with Session(test_engine) as session:
            session.add(account)
            session.commit()
            session.refresh(account)
            account_id = int(account.id)
            created_at = account.created_at
            original_extra_json = account.extra_json

            account.extra_json = (
                '{"refresh_token":"","updated_by":"another-flow"}'
            )
            session.add(account)
            session.commit()

        cleanup = getattr(db_module, "delete_incomplete_chatgpt_account", None)
        self.assertIsNotNone(cleanup)
        self.assertIn(
            "expected_extra_json",
            inspect.signature(cleanup).parameters,
            "cleanup must compare the exact extra snapshot saved by this attempt",
        )
        self.assertFalse(
            cleanup(
                account_id,
                expected_email="changed@example.com",
                expected_created_at=created_at,
                expected_extra_json=original_extra_json,
                database_engine=test_engine,
            )
        )

        with Session(test_engine) as session:
            preserved = session.get(AccountModel, account_id)
            self.assertIsNotNone(preserved)
            self.assertIn("another-flow", preserved.extra_json)

    def test_bulk_cleanup_removes_historical_incomplete_chatgpt_rows_only(self):
        test_engine = self._test_engine()
        rows = [
            AccountModel(
                platform="chatgpt",
                email="empty@example.com",
                password="password",
                extra_json='{"refresh_token":""}',
            ),
            AccountModel(
                platform="chatgpt",
                email="malformed@example.com",
                password="password",
                extra_json="not-json",
            ),
            AccountModel(
                platform="chatgpt",
                email="valid@example.com",
                password="password",
                extra_json='{"refresh_token":"rt-valid"}',
            ),
            AccountModel(
                platform="chatgpt",
                email="legacy-valid@example.com",
                password="password",
                extra_json='{"refreshToken":"rt-legacy"}',
            ),
            AccountModel(
                platform="chatgpt",
                email="blank-snake-valid-camel@example.com",
                password="password",
                extra_json='{"refresh_token":"   ","refreshToken":"rt-camel"}',
            ),
            AccountModel(
                platform="qwen",
                email="other@example.com",
                password="password",
                extra_json="{}",
            ),
        ]
        with Session(test_engine) as session:
            session.add_all(rows)
            session.commit()

        cleanup = getattr(
            db_module,
            "purge_incomplete_chatgpt_accounts",
            None,
        )
        self.assertIsNotNone(cleanup, "missing historical incomplete-account cleanup")
        self.assertEqual(cleanup(database_engine=test_engine), 2)

        with Session(test_engine) as session:
            remaining = session.exec(select(AccountModel)).all()
        self.assertEqual(
            {row.email for row in remaining},
            {
                "valid@example.com",
                "legacy-valid@example.com",
                "blank-snake-valid-camel@example.com",
                "other@example.com",
            },
        )


if __name__ == "__main__":
    unittest.main()
