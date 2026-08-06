import unittest
from datetime import datetime, timezone

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from core.db import AccountModel


class ChatGPTAccountHardeningPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)
        account = AccountModel(
            platform="chatgpt",
            email="User@Example.com",
            password="existing-password",
            token="access-token",
            created_at=datetime(2026, 8, 6, tzinfo=timezone.utc),
            updated_at=datetime(2026, 8, 6, tzinfo=timezone.utc),
        )
        account.set_extra(
            {
                "access_token": "access-token",
                "refresh_token": "refresh-token",
            }
        )
        with Session(self.engine) as session:
            session.add(account)
            session.commit()
            session.refresh(account)
            self.account_id = int(account.id)
            self.created_at = account.created_at
            self.updated_at = account.updated_at

    def _load(self):
        with Session(self.engine) as session:
            return session.get(AccountModel, self.account_id)

    def test_claim_is_compare_and_swap_and_rejects_other_owner(self):
        from core.db import claim_chatgpt_account_hardening

        claimed = claim_chatgpt_account_hardening(
            self.account_id,
            expected_email="user@example.com",
            expected_created_at=self.created_at,
            expected_updated_at=self.updated_at,
            owner="task-1",
            database_engine=self.engine,
        )
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.get_extra()["mfa_hardening_owner"], "task-1")

        rejected = claim_chatgpt_account_hardening(
            self.account_id,
            expected_email="USER@example.com",
            expected_created_at=self.created_at,
            expected_updated_at=claimed.updated_at,
            owner="task-2",
            database_engine=self.engine,
        )
        self.assertIsNone(rejected)

        stale = claim_chatgpt_account_hardening(
            self.account_id,
            expected_email="user@example.com",
            expected_created_at=self.created_at,
            expected_updated_at=self.updated_at,
            owner="task-1",
            database_engine=self.engine,
        )
        self.assertIsNone(stale)

    def test_stages_pending_secret_without_changing_tokens(self):
        from core.db import (
            claim_chatgpt_account_hardening,
            update_chatgpt_account_hardening,
        )

        claimed = claim_chatgpt_account_hardening(
            self.account_id,
            expected_email="user@example.com",
            expected_created_at=self.created_at,
            expected_updated_at=self.updated_at,
            owner="task-1",
            database_engine=self.engine,
        )
        staged = update_chatgpt_account_hardening(
            self.account_id,
            expected_email="user@example.com",
            expected_created_at=self.created_at,
            expected_updated_at=claimed.updated_at,
            owner="task-1",
            extra_updates={
                "mfa_hardening_status": "confirming",
                "mfa_pending_secret": "JBSWY3DPEHPK3PXP",
            },
            database_engine=self.engine,
        )

        self.assertIsNotNone(staged)
        self.assertEqual(staged.token, "access-token")
        self.assertEqual(staged.get_extra()["refresh_token"], "refresh-token")
        self.assertEqual(
            staged.get_extra()["mfa_pending_secret"],
            "JBSWY3DPEHPK3PXP",
        )

    def test_promotes_secret_atomically_and_releases_owner(self):
        from core.db import (
            claim_chatgpt_account_hardening,
            promote_chatgpt_mfa_secret,
            update_chatgpt_account_hardening,
        )

        claimed = claim_chatgpt_account_hardening(
            self.account_id,
            expected_email="user@example.com",
            expected_created_at=self.created_at,
            expected_updated_at=self.updated_at,
            owner="task-1",
            database_engine=self.engine,
        )
        staged = update_chatgpt_account_hardening(
            self.account_id,
            expected_email="user@example.com",
            expected_created_at=self.created_at,
            expected_updated_at=claimed.updated_at,
            owner="task-1",
            extra_updates={
                "mfa_hardening_status": "confirming",
                "mfa_pending_secret": "JBSWY3DPEHPK3PXP",
            },
            database_engine=self.engine,
        )
        promoted = promote_chatgpt_mfa_secret(
            self.account_id,
            expected_email="user@example.com",
            expected_created_at=self.created_at,
            expected_updated_at=staged.updated_at,
            owner="task-1",
            secret="JBSWY3DPEHPK3PXP",
            database_engine=self.engine,
        )

        extra = promoted.get_extra()
        self.assertEqual(extra["totp_secret"], "JBSWY3DPEHPK3PXP")
        self.assertEqual(extra["account_type"], "chatgpt_password_totp")
        self.assertEqual(extra["mfa_hardening_status"], "ready")
        self.assertNotIn("mfa_pending_secret", extra)
        self.assertNotIn("mfa_hardening_owner", extra)
        self.assertTrue(extra["mfa_enabled_at"].endswith("+00:00"))

    def test_wrong_owner_or_stale_snapshot_cannot_mutate_account(self):
        from core.db import (
            claim_chatgpt_account_hardening,
            update_chatgpt_account_hardening,
        )

        claimed = claim_chatgpt_account_hardening(
            self.account_id,
            expected_email="user@example.com",
            expected_created_at=self.created_at,
            expected_updated_at=self.updated_at,
            owner="task-1",
            database_engine=self.engine,
        )
        wrong_owner = update_chatgpt_account_hardening(
            self.account_id,
            expected_email="user@example.com",
            expected_created_at=self.created_at,
            expected_updated_at=claimed.updated_at,
            owner="task-2",
            extra_updates={"mfa_hardening_status": "ready"},
            database_engine=self.engine,
        )
        self.assertIsNone(wrong_owner)

        first = update_chatgpt_account_hardening(
            self.account_id,
            expected_email="user@example.com",
            expected_created_at=self.created_at,
            expected_updated_at=claimed.updated_at,
            owner="task-1",
            extra_updates={"mfa_hardening_status": "pending_password"},
            database_engine=self.engine,
        )
        self.assertIsNotNone(first)
        stale = update_chatgpt_account_hardening(
            self.account_id,
            expected_email="user@example.com",
            expected_created_at=self.created_at,
            expected_updated_at=claimed.updated_at,
            owner="task-1",
            extra_updates={"mfa_hardening_status": "ready"},
            database_engine=self.engine,
        )
        self.assertIsNone(stale)
        current = self._load()
        self.assertEqual(
            current.get_extra()["mfa_hardening_status"],
            "pending_password",
        )
        self.assertEqual(current.token, "access-token")

    def test_release_owner_preserves_failure_status_and_credentials(self):
        from core.db import (
            claim_chatgpt_account_hardening,
            update_chatgpt_account_hardening,
        )

        claimed = claim_chatgpt_account_hardening(
            self.account_id,
            expected_email="user@example.com",
            expected_created_at=self.created_at,
            expected_updated_at=self.updated_at,
            owner="task-1",
            database_engine=self.engine,
        )
        released = update_chatgpt_account_hardening(
            self.account_id,
            expected_email="user@example.com",
            expected_created_at=self.created_at,
            expected_updated_at=claimed.updated_at,
            owner="task-1",
            extra_updates={"mfa_hardening_status": "missing_mfa_material"},
            release_owner=True,
            database_engine=self.engine,
        )

        extra = released.get_extra()
        self.assertNotIn("mfa_hardening_owner", extra)
        self.assertEqual(extra["mfa_hardening_status"], "missing_mfa_material")
        self.assertEqual(released.password, "existing-password")
        self.assertEqual(released.token, "access-token")
        self.assertEqual(extra["refresh_token"], "refresh-token")


if __name__ == "__main__":
    unittest.main()
