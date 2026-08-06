import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from core.db import AccountModel, ChatGPTAttemptBindingModel
from platforms.chatgpt.account_hardening import MFAEnrollment, MFAInventory


SECRET = "JBSWY3DPEHPK3PXP"


class FakeMFAClient:
    def __init__(self, *, inventory=None, activate_hook=None, secret=SECRET):
        self.inventory = inventory or MFAInventory(False, False, "", ())
        self.activate_hook = activate_hook
        self.secret = secret
        self.enroll_calls = 0
        self.activate_calls = []
        self.disable_calls = []

    def get_inventory(self):
        return self.inventory

    def start_totp_enrollment(self):
        self.enroll_calls += 1
        return MFAEnrollment("session-1", self.secret)

    def activate_totp_enrollment(self, session_id, code):
        self.activate_calls.append((session_id, code))
        if self.activate_hook:
            return self.activate_hook(session_id, code)
        return True

    def disable_factor(self, factor_id):
        self.disable_calls.append(factor_id)
        self.inventory = MFAInventory(False, False, "", ())
        return True


class ChatGPTAccountHardeningServiceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)

    def _create_account(self, *, password="password", extra=None):
        account = AccountModel(
            platform="chatgpt",
            email="user@example.com",
            password=password,
            token="access-token",
            user_id="account-123",
        )
        account.set_extra(
            {
                "access_token": "access-token",
                "refresh_token": "refresh-token",
                **dict(extra or {}),
            }
        )
        with Session(self.engine) as session:
            session.add(account)
            session.commit()
            session.refresh(account)
            return int(account.id)

    def _load(self, account_id):
        with Session(self.engine) as session:
            return session.get(AccountModel, account_id)

    def _service(self, client, **kwargs):
        from services.chatgpt_account_hardening import (
            ChatGPTAccountHardeningService,
        )

        return ChatGPTAccountHardeningService(
            database_engine=self.engine,
            mfa_client_factory=lambda **_client_kwargs: client,
            **kwargs,
        )

    def test_ready_account_is_skipped_without_remote_call(self):
        account_id = self._create_account(
            extra={
                "account_type": "chatgpt_password_totp",
                "totp_secret": SECRET,
                "mfa_hardening_status": "ready",
                "password_remote_verified_at": "2026-08-06T00:00:00+00:00",
            }
        )

        result = self._service(FakeMFAClient()).harden_saved_account(account_id)

        self.assertEqual(result.status, "ready")
        self.assertEqual(result.outcome, "ready_before")

    def test_legacy_ready_without_remote_password_marker_is_revalidated(self):
        account_id = self._create_account(
            extra={
                "account_type": "chatgpt_password_totp",
                "totp_secret": SECRET,
                "mfa_hardening_status": "ready",
            }
        )
        inventory = MFAInventory(
            True,
            True,
            "factor-1",
            ({"id": "factor-1", "factor_type": "totp"},),
        )

        result = self._service(
            FakeMFAClient(inventory=inventory),
            candidate_validator=lambda _account, candidate: candidate == SECRET,
        ).harden_saved_account(account_id)

        self.assertEqual(result.outcome, "recovered_secret")
        self.assertIn(
            "password_remote_verified_at",
            self._load(account_id).get_extra(),
        )

    def test_missing_password_is_persisted_only_after_reset_succeeds(self):
        account_id = self._create_account(password="")
        observed_passwords = []

        def reset_password(account, new_password):
            observed_passwords.append(self._load(account_id).password)
            self.assertGreaterEqual(len(new_password), 20)
            return True

        client = FakeMFAClient()
        result = self._service(
            client,
            password_reset_callback=reset_password,
            candidate_validator=lambda _account, candidate: candidate == SECRET,
        ).harden_saved_account(account_id)

        self.assertEqual(observed_passwords, [""])
        self.assertEqual(result.status, "ready")
        self.assertTrue(self._load(account_id).password)
        self.assertEqual(client.enroll_calls, 1)

    def test_password_reset_failure_preserves_empty_password_and_tokens(self):
        account_id = self._create_account(password="")
        client = FakeMFAClient()
        result = self._service(
            client,
            password_reset_callback=lambda _account, _password: False,
        ).harden_saved_account(account_id)

        self.assertEqual(result.status, "pending_password")
        current = self._load(account_id)
        self.assertEqual(current.password, "")
        self.assertEqual(current.token, "access-token")
        self.assertEqual(current.get_extra()["refresh_token"], "refresh-token")
        self.assertEqual(client.enroll_calls, 0)

    def test_enrollment_secret_is_staged_before_remote_activation(self):
        account_id = self._create_account()

        def assert_staged(_session_id, code):
            current = self._load(account_id)
            self.assertEqual(current.get_extra()["mfa_pending_secret"], SECRET)
            self.assertEqual(
                current.get_extra()["mfa_hardening_status"],
                "confirming",
            )
            self.assertEqual(len(code), 6)
            return True

        client = FakeMFAClient(activate_hook=assert_staged)
        result = self._service(
            client,
            candidate_validator=lambda _account, candidate: candidate == SECRET,
        ).harden_saved_account(account_id)

        self.assertEqual(result.outcome, "hardened")
        current = self._load(account_id)
        self.assertEqual(current.get_extra()["totp_secret"], SECRET)
        self.assertNotIn("mfa_pending_secret", current.get_extra())
        self.assertIn("password_remote_verified_at", current.get_extra())

    def test_new_enrollment_is_not_promoted_until_password_totp_login_validates(self):
        account_id = self._create_account()
        client = FakeMFAClient()

        result = self._service(
            client,
            candidate_validator=lambda _account, _candidate: False,
        ).harden_saved_account(account_id)

        self.assertEqual(result.status, "confirming")
        current = self._load(account_id)
        extra = current.get_extra()
        self.assertEqual(extra["mfa_pending_secret"], SECRET)
        self.assertNotIn("totp_secret", extra)
        self.assertNotIn("password_remote_verified_at", extra)

    def test_pending_secret_is_recovered_after_activation_crash(self):
        account_id = self._create_account()
        first_client = FakeMFAClient(
            activate_hook=lambda _session, _code: (_ for _ in ()).throw(
                RuntimeError("activation interrupted")
            )
        )
        first = self._service(first_client).harden_saved_account(account_id)
        self.assertEqual(first.status, "confirming")
        self.assertEqual(
            self._load(account_id).get_extra()["mfa_pending_secret"],
            SECRET,
        )

        enabled_inventory = MFAInventory(
            True,
            True,
            "factor-1",
            ({"id": "factor-1", "factor_type": "totp"},),
        )
        second_client = FakeMFAClient(inventory=enabled_inventory)
        validated = []
        second = self._service(
            second_client,
            candidate_validator=lambda _account, candidate: (
                validated.append(candidate) or candidate == SECRET
            ),
        ).harden_saved_account(account_id)

        self.assertEqual(second.outcome, "recovered_secret")
        self.assertEqual(validated, [SECRET])
        self.assertEqual(self._load(account_id).get_extra()["totp_secret"], SECRET)

    def test_pending_secret_is_not_promoted_when_login_validation_rejects_it(self):
        account_id = self._create_account(
            extra={
                "mfa_pending_secret": SECRET,
                "mfa_hardening_session_id": "session-1",
                "mfa_hardening_status": "confirming",
            }
        )
        enabled_inventory = MFAInventory(
            True,
            True,
            "factor-1",
            ({"id": "factor-1", "factor_type": "totp"},),
        )

        result = self._service(
            FakeMFAClient(inventory=enabled_inventory),
            candidate_validator=lambda _account, _candidate: False,
        ).harden_saved_account(account_id)

        self.assertEqual(result.outcome, "missing_mfa_material")
        extra = self._load(account_id).get_extra()
        self.assertNotIn("totp_secret", extra)
        self.assertEqual(extra["mfa_pending_secret"], SECRET)

    def test_existing_remote_mfa_recovers_candidate_from_mailbox_context(self):
        account_id = self._create_account(
            extra={
                "mailbox_login_context": {
                    "provider": "chatgpt_credentials",
                    "email": "user@example.com",
                    "extra": {"mfa_secret": SECRET},
                }
            }
        )
        inventory = MFAInventory(
            True,
            True,
            "factor-1",
            ({"id": "factor-1", "factor_type": "totp"},),
        )
        validated = []

        def validate(account, candidate):
            validated.append((account.email, candidate))
            return True

        result = self._service(
            FakeMFAClient(inventory=inventory),
            candidate_validator=validate,
        ).harden_saved_account(account_id)

        self.assertEqual(result.outcome, "recovered_secret")
        self.assertEqual(validated, [("user@example.com", SECRET)])
        self.assertEqual(self._load(account_id).get_extra()["totp_secret"], SECRET)

    def test_existing_remote_mfa_recovers_candidate_from_sqlite_backup(self):
        account_id = self._create_account(extra={"mailbox_login_context": {}})
        inventory = MFAInventory(
            True,
            True,
            "factor-1",
            ({"id": "factor-1", "factor_type": "totp"},),
        )
        with tempfile.TemporaryDirectory() as tmp:
            backup = Path(tmp) / "account_manager.backup.db"
            connection = sqlite3.connect(backup)
            try:
                connection.execute(
                    "CREATE TABLE accounts (email TEXT, platform TEXT, extra_json TEXT)"
                )
                connection.execute(
                    "INSERT INTO accounts VALUES (?, ?, ?)",
                    (
                        "USER@example.com",
                        "chatgpt",
                        json.dumps({"totp_secret": SECRET}),
                    ),
                )
                connection.commit()
            finally:
                connection.close()

            result = self._service(
                FakeMFAClient(inventory=inventory),
                candidate_validator=lambda _account, candidate: candidate == SECRET,
                backup_paths=[str(backup)],
            ).harden_saved_account(account_id)

        self.assertEqual(result.outcome, "recovered_secret")
        self.assertEqual(self._load(account_id).get_extra()["totp_secret"], SECRET)

    def test_existing_remote_mfa_recovers_case_insensitive_attempt_binding(self):
        account_id = self._create_account(extra={"mailbox_login_context": {}})
        with Session(self.engine) as session:
            session.add(
                ChatGPTAttemptBindingModel(
                    task_id="old-task",
                    attempt_index=1,
                    email="USER@EXAMPLE.COM",
                    account_id=account_id,
                    mailbox_context_json=json.dumps(
                        {
                            "email": "USER@EXAMPLE.COM",
                            "extra": {"mfa_secret": SECRET},
                        }
                    ),
                )
            )
            session.commit()
        inventory = MFAInventory(
            True,
            True,
            "factor-1",
            ({"id": "factor-1", "factor_type": "totp"},),
        )

        result = self._service(
            FakeMFAClient(inventory=inventory),
            candidate_validator=lambda _account, candidate: candidate == SECRET,
        ).harden_saved_account(account_id)

        self.assertEqual(result.outcome, "recovered_secret")
        self.assertEqual(self._load(account_id).get_extra()["totp_secret"], SECRET)

    def test_existing_remote_mfa_without_mailbox_or_secret_is_preserved(self):
        account_id = self._create_account(extra={"mailbox_login_context": {}})
        inventory = MFAInventory(
            True,
            True,
            "factor-1",
            ({"id": "factor-1", "factor_type": "totp"},),
        )
        client = FakeMFAClient(inventory=inventory)

        result = self._service(
            client,
            candidate_validator=lambda _account, candidate: candidate == SECRET,
        ).harden_saved_account(account_id)

        self.assertEqual(result.status, "missing_mfa_material")
        current = self._load(account_id)
        self.assertEqual(current.token, "access-token")
        self.assertEqual(current.get_extra()["refresh_token"], "refresh-token")
        self.assertEqual(
            current.get_extra()["mfa_hardening_status"],
            "missing_mfa_material",
        )
        self.assertEqual(client.enroll_calls, 0)
        self.assertEqual(client.disable_calls, [])

    def test_verified_email_fallback_resets_old_factor_then_enrolls_new_totp(self):
        account_id = self._create_account(
            extra={
                "mfa_reenrollment_required": True,
                "mfa_email_fallback_verified_at": "2026-08-06T00:00:00+00:00",
                "mailbox_login_context": {
                    "provider": "microsoft",
                    "email": "user@example.com",
                    "extra": {"refresh_token": "mail-refresh"},
                },
            }
        )
        inventory = MFAInventory(
            True,
            True,
            "factor-old",
            ({"id": "factor-old", "factor_type": "totp"},),
        )
        client = FakeMFAClient(inventory=inventory)

        result = self._service(
            client,
            candidate_validator=lambda _account, candidate: candidate == SECRET,
        ).harden_saved_account(account_id)

        self.assertEqual(result.status, "ready")
        self.assertEqual(client.disable_calls, ["factor-old"])
        self.assertEqual(client.enroll_calls, 1)
        current = self._load(account_id)
        extra = current.get_extra()
        self.assertEqual(extra["totp_secret"], SECRET)
        self.assertNotIn("mfa_reenrollment_required", extra)
        self.assertNotIn("mfa_email_fallback_verified_at", extra)

    def test_dry_run_classifies_without_claim_or_mutation(self):
        account_id = self._create_account(password="")
        client = FakeMFAClient()

        result = self._service(client).harden_saved_account(
            account_id,
            dry_run=True,
        )

        self.assertEqual(result.status, "needs_password")
        current = self._load(account_id)
        self.assertNotIn("mfa_hardening_owner", current.get_extra())
        self.assertEqual(client.enroll_calls, 0)


if __name__ == "__main__":
    unittest.main()
