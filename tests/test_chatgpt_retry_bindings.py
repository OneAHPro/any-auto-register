import json
import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

from fastapi import BackgroundTasks, FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session, create_engine, select

import api.tasks as tasks_module
from api.tasks import (
    ChatGPTRetryFailedTaskRequest,
    RegisterTaskRequest,
    _bind_chatgpt_retry_mailbox,
    _build_chatgpt_retry_request,
    get_retryable_task_bindings,
    _load_chatgpt_retry_mailbox_context,
    retry_failed_task_bindings,
    _retryable_chatgpt_bindings,
    _upsert_chatgpt_attempt_binding,
)
from core.applemail_pool import (
    load_applemail_pool_snapshot,
    save_applemail_pool_json,
)
from core.base_mailbox import AppleMailMailbox, OutlookMailbox
from core.db import (
    AccountModel,
    ChatGPTAttemptBindingModel,
    OutlookAccountModel,
    _recover_chatgpt_attempt_bindings,
)


class ChatGPTRetryBindingTests(unittest.TestCase):
    def test_retry_mailbox_context_is_loaded_by_matching_binding_and_email_only(self):
        test_engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(test_engine)
        context = {
            "provider": "microsoft",
            "email": "bound@example.com",
            "account_id": "mailbox-7",
            "extra": {
                "provider": "microsoft",
                "account_type": "mailapi_url",
                "password": "fixture-secret",
                "mailapi_url": "https://mail.example.test/TOKEN",
            },
        }
        with Session(test_engine) as session:
            row = ChatGPTAttemptBindingModel(
                task_id="task-original",
                attempt_index=0,
                email="bound@example.com",
                leadbee_code="aar_" + "a" * 32,
                status="failed",
                mailbox_context_json=json.dumps(context),
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            binding_id = int(row.id)

        with mock.patch("api.tasks.engine", test_engine):
            restored = _load_chatgpt_retry_mailbox_context(
                binding_id,
                "BOUND@example.com",
            )
            mismatched = _load_chatgpt_retry_mailbox_context(
                binding_id,
                "other@example.com",
            )

        self.assertEqual(restored, context)
        self.assertEqual(mismatched, {})

    def test_retry_promotes_saved_password_and_managed_totp_over_legacy_mailapi_context(self):
        test_engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(test_engine)
        context = {
            "provider": "microsoft",
            "email": "managed@example.com",
            "account_id": "mailbox-8",
            "extra": {
                "provider": "microsoft",
                "account_type": "mailapi_url",
                "mailapi_url": "https://mail.example.test/TOKEN",
                "totp_secret": "JBSWY3DPEHPK3PXP",
                "mfa_recovery_code": "RECOVERY-CODE",
                "chatgpt_mfa_managed": True,
            },
        }
        with Session(test_engine) as session:
            account = AccountModel(
                platform="chatgpt",
                email="managed@example.com",
                password="Saved-ChatGPT-Password",
            )
            account.set_extra({"mailbox_login_context": context})
            session.add(account)
            session.commit()
            session.refresh(account)
            row = ChatGPTAttemptBindingModel(
                task_id="task-original",
                attempt_index=0,
                email="managed@example.com",
                account_id=int(account.id),
                leadbee_code="aar_" + "a" * 32,
                status="failed",
                mailbox_context_json=json.dumps(context),
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            binding_id = int(row.id)

        with mock.patch("api.tasks.engine", test_engine):
            restored = _load_chatgpt_retry_mailbox_context(
                binding_id,
                "managed@example.com",
            )

        self.assertEqual(
            restored["extra"]["account_type"],
            "chatgpt_password_totp",
        )
        self.assertEqual(
            restored["extra"]["password"],
            "Saved-ChatGPT-Password",
        )
        self.assertEqual(
            restored["extra"]["totp_secret"],
            "JBSWY3DPEHPK3PXP",
        )
        self.assertEqual(
            restored["extra"]["mail_api_url"],
            "https://mail.example.test/TOKEN",
        )

    def test_api_phone_auto_retry_requires_released_terminal_order(self):
        result = {
            "status": "failed",
            "provider_cleanup_settled": True,
            "phone_verified": False,
            "provider_error_code": "",
        }
        diagnostic = {
            "failure_stage": "openai_send",
            "safe_error_code": "OPENAI_SEND_RETRY_EXHAUSTED",
            "http_status": 504,
            "provider_retry_count": 2,
            "order_status": "CANCELED",
            "billing_status": "RELEASED",
            "replacement_count": 0,
            "recovery_status": "released",
        }

        self.assertTrue(
            tasks_module._chatgpt_api_phone_retry_allowed(
                result,
                diagnostic,
                retry_count=0,
            )
        )
        unsafe_cases = (
            ({**result, "provider_cleanup_settled": False}, diagnostic, 0),
            ({**result, "phone_verified": True}, diagnostic, 0),
            ({**result, "ownership_conflict": True}, diagnostic, 0),
            ({**result, "finalization_pending": True}, diagnostic, 0),
            (result, {**diagnostic, "order_status": "WAITING_CODE"}, 0),
            (result, {**diagnostic, "billing_status": "CAPTURED"}, 0),
            (result, {**diagnostic, "recovery_status": "pending"}, 0),
            (
                {
                    **result,
                    "provider_error_code": "LEADBEE_API_CAPACITY_EXHAUSTED",
                },
                diagnostic,
                0,
            ),
            (result, diagnostic, 1),
        )
        for unsafe_result, unsafe_diagnostic, retry_count in unsafe_cases:
            with self.subTest(
                result=unsafe_result,
                diagnostic=unsafe_diagnostic,
                retry_count=retry_count,
            ):
                self.assertFalse(
                    tasks_module._chatgpt_api_phone_retry_allowed(
                        unsafe_result,
                        unsafe_diagnostic,
                        retry_count=retry_count,
                    )
                )

    def test_provider_diagnostic_is_sanitized_before_persist_and_exposed(self):
        test_engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(test_engine)
        secret = "raw-provider-secret"

        with mock.patch("api.tasks.engine", test_engine):
            row = _upsert_chatgpt_attempt_binding(
                task_id="task-diagnostic",
                attempt_index=0,
                email="bound@example.com",
                leadbee_code="aar_" + "a" * 32,
                stage="phone",
                status="failed",
                mailbox_context={
                    "provider": "microsoft",
                    "leadbee_api": True,
                    "phone_auto_retry_count": 1,
                    "phone_diagnostic": {
                        "failure_stage": "openai_send",
                        "safe_error_code": "OPENAI_SEND_RETRY_EXHAUSTED",
                        "http_status": 504,
                        "provider_retry_count": 2,
                        "order_status": "CANCELED",
                        "billing_status": "RELEASED",
                        "replacement_count": 0,
                        "recovery_status": "released",
                        "raw_body": secret,
                        "order_id": secret,
                    },
                },
            )

        self.assertNotIn(secret, row.mailbox_context_json)
        public = tasks_module._chatgpt_binding_public(row)
        self.assertEqual(public["phone_auto_retry_count"], 1)
        self.assertEqual(
            public["provider_diagnostic"],
            {
                "failure_stage": "openai_send",
                "safe_error_code": "OPENAI_SEND_RETRY_EXHAUSTED",
                "http_status": 504,
                "provider_retry_count": 2,
                "order_status": "CANCELED",
                "billing_status": "RELEASED",
                "replacement_count": 0,
                "recovery_status": "released",
            },
        )

    def test_mfa_secrets_are_not_duplicated_into_retry_binding(self):
        test_engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(test_engine)

        with mock.patch("api.tasks.engine", test_engine):
            row = _upsert_chatgpt_attempt_binding(
                task_id="task-mfa-secret",
                attempt_index=0,
                email="bound@example.com",
                leadbee_code="aar_" + "a" * 32,
                mailbox_context={
                    "provider": "microsoft",
                    "email": "bound@example.com",
                    "extra": {
                        "totp_secret": "MUST-NOT-BE-DUPLICATED",
                        "mfa_recovery_code": "RECOVERY-MUST-NOT-BE-DUPLICATED",
                        "chatgpt_mfa_managed": True,
                    },
                },
            )

        self.assertNotIn("MUST-NOT-BE-DUPLICATED", row.mailbox_context_json)
        self.assertNotIn("RECOVERY-MUST-NOT-BE-DUPLICATED", row.mailbox_context_json)
        self.assertIn("chatgpt_mfa_managed", row.mailbox_context_json)

    def test_sanitized_password_totp_retry_reclaims_exact_applemail_credentials(self):
        target_email = "target@example.com"
        target_password = "target-password"
        target_totp = "JBSWY3DPEHPK3PXP"
        with tempfile.TemporaryDirectory() as tmp_dir:
            save_applemail_pool_json(
                json.dumps(
                    [
                        {
                            "email": "decoy@example.com",
                            "password": "decoy-password",
                            "totp_secret": "GEZDGNBVGY3TQOJQ",
                            "account_type": "chatgpt_password_totp",
                        },
                        {
                            "email": target_email,
                            "password": target_password,
                            "totp_secret": target_totp,
                            "account_type": "chatgpt_password_totp",
                        },
                    ]
                ),
                pool_dir=tmp_dir,
                filename="retry.json",
            )
            original_mailbox = AppleMailMailbox(
                pool_dir=tmp_dir,
                pool_file="retry.json",
            )
            original_account = original_mailbox.get_email_by_address(target_email)
            mailbox_context = {
                "provider": "applemail",
                "email": target_email,
                "account_id": original_account.account_id,
                "extra": dict(original_account.extra),
            }

            test_engine = create_engine(
                "sqlite://",
                connect_args={"check_same_thread": False},
                poolclass=StaticPool,
            )
            SQLModel.metadata.create_all(test_engine)
            with mock.patch("api.tasks.engine", test_engine):
                row = _upsert_chatgpt_attempt_binding(
                    task_id="task-password-totp-retry",
                    attempt_index=0,
                    email=target_email,
                    leadbee_code="aar_" + "a" * 32,
                    status="failed",
                    mailbox_context=mailbox_context,
                )

            self.assertTrue(original_mailbox.requeue_account(original_account))
            persisted_context = json.loads(row.mailbox_context_json)
            self.assertEqual(
                persisted_context["extra"]["password"],
                target_password,
            )
            self.assertNotIn("totp_secret", persisted_context["extra"])
            self.assertNotIn(target_totp, row.mailbox_context_json)

            retry_mailbox = _bind_chatgpt_retry_mailbox(
                AppleMailMailbox(pool_dir=tmp_dir, pool_file="retry.json"),
                persisted_context,
                target_email,
            )
            reclaimed = retry_mailbox.get_email()

            self.assertEqual(reclaimed.email, target_email)
            self.assertEqual(reclaimed.extra["password"], target_password)
            self.assertEqual(reclaimed.extra["totp_secret"], target_totp)
            reclaimed_by_address = retry_mailbox.get_email_by_address(
                target_email.upper()
            )
            self.assertEqual(reclaimed_by_address.email, target_email)
            self.assertEqual(
                reclaimed_by_address.extra["totp_secret"],
                target_totp,
            )
            snapshot = load_applemail_pool_snapshot(
                pool_dir=tmp_dir,
                pool_file="retry.json",
            )
            self.assertEqual(
                [item["email"] for item in snapshot["items"]],
                ["decoy@example.com"],
            )

            self.assertTrue(retry_mailbox.requeue_account(reclaimed))
            requeued = load_applemail_pool_snapshot(
                pool_dir=tmp_dir,
                pool_file="retry.json",
            )
            self.assertEqual(
                {item["email"] for item in requeued["items"]},
                {"decoy@example.com", target_email},
            )

    def test_rehydrated_claim_stays_claimed_until_refresh_token_is_saved(self):
        target_email = "deferred@example.com"
        target_password = "deferred-password"
        target_totp = "JBSWY3DPEHPK3PXP"
        with tempfile.TemporaryDirectory() as tmp_dir:
            save_applemail_pool_json(
                json.dumps(
                    [{
                        "email": target_email,
                        "password": target_password,
                        "totp_secret": target_totp,
                        "account_type": "chatgpt_password_totp",
                    }]
                ),
                pool_dir=tmp_dir,
                filename="deferred.json",
            )
            source = AppleMailMailbox(
                pool_dir=tmp_dir,
                pool_file="deferred.json",
            )
            claimed = source.get_email_by_address(target_email)
            self.assertTrue(source.requeue_account(claimed))
            context = {
                "provider": "applemail",
                "email": target_email,
                "extra": {
                    "account_type": "chatgpt_password_totp",
                    "password": target_password,
                    "pool_file": "deferred.json",
                },
            }
            retry = _bind_chatgpt_retry_mailbox(
                AppleMailMailbox(pool_dir=tmp_dir, pool_file="deferred.json"),
                context,
                target_email,
            )
            reclaimed = retry.get_email()

            # Access Token may be saved before phone/Refresh Token completion;
            # this must not burn the rehydrated claim.
            self.assertTrue(retry.mark_account_used(reclaimed))
            self.assertEqual(
                load_applemail_pool_snapshot(
                    pool_dir=tmp_dir,
                    pool_file="deferred.json",
                )["count"],
                0,
            )
            self.assertTrue(retry.requeue_account(reclaimed))
            self.assertEqual(
                load_applemail_pool_snapshot(
                    pool_dir=tmp_dir,
                    pool_file="deferred.json",
                )["count"],
                1,
            )

    def test_rehydrated_claim_is_consumed_by_final_account_refresh_token(self):
        target_email = "complete@example.com"
        with tempfile.TemporaryDirectory() as tmp_dir:
            save_applemail_pool_json(
                json.dumps(
                    [{
                        "email": target_email,
                        "password": "complete-password",
                        "totp_secret": "JBSWY3DPEHPK3PXP",
                        "account_type": "chatgpt_password_totp",
                    }]
                ),
                pool_dir=tmp_dir,
                filename="complete.json",
            )
            retry = _bind_chatgpt_retry_mailbox(
                AppleMailMailbox(pool_dir=tmp_dir, pool_file="complete.json"),
                {
                    "provider": "applemail",
                    "email": target_email,
                    "extra": {
                        "account_type": "chatgpt_password_totp",
                        "password": "complete-password",
                        "pool_file": "complete.json",
                    },
                },
                target_email,
            )
            retry.get_email()
            final_account = AccountModel(
                platform="chatgpt",
                email=target_email,
                password="complete-password",
            )
            final_account.set_extra({"refresh_token": "saved-refresh-token"})

            self.assertTrue(retry.mark_account_used(final_account))
            records = json.loads(
                open(f"{tmp_dir}/complete.json", encoding="utf-8").read()
            )
            self.assertEqual(records[0]["pool_state"], "used")
            self.assertFalse(records[0]["enabled"])

    def test_mailapi_retry_reclaims_and_binds_exact_outlook_row(self):
        test_engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(test_engine)
        email = "retry-mailapi@example.com"
        with Session(test_engine) as session:
            session.add(
                OutlookAccountModel(
                    email=email,
                    password="",
                    account_type="mailapi_url",
                    mailapi_url="https://mail.example.test/inbox/token",
                )
            )
            session.commit()

        source = OutlookMailbox()
        with mock.patch("core.db.engine", test_engine):
            claimed = source.get_email_by_address(email)
            context = {
                "provider": "microsoft",
                "email": email,
                "account_id": claimed.account_id,
                "extra": dict(claimed.extra),
            }
            self.assertTrue(source.requeue_account(claimed))
            retry = _bind_chatgpt_retry_mailbox(
                OutlookMailbox(),
                context,
                email,
            )
            reclaimed = retry.get_email()
            self.assertNotEqual(
                reclaimed.extra["_outlook_lease_version"],
                context["extra"]["_outlook_lease_version"],
            )
            final_account = AccountModel(
                platform="chatgpt",
                email=email,
                password="chatgpt-password",
                status="registered",
            )
            final_account.set_extra({"refresh_token": "saved-refresh-token"})
            with Session(test_engine) as session:
                session.add(final_account)
                session.commit()
                session.refresh(final_account)
                final_id = int(final_account.id)
            self.assertTrue(retry.mark_account_used(final_account))

        with Session(test_engine) as session:
            row = session.exec(select(OutlookAccountModel)).one()
            self.assertEqual(row.state, "bound")
            self.assertFalse(row.enabled)
            self.assertEqual(row.bound_account_id, final_id)

    def test_mailapi_retry_reuses_still_owned_outlook_lease(self):
        test_engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(test_engine)
        email = "live-lease@example.com"
        with Session(test_engine) as session:
            session.add(
                OutlookAccountModel(
                    email=email,
                    password="",
                    account_type="mailapi_url",
                    mailapi_url="https://mail.example.test/inbox/token",
                )
            )
            session.commit()

        with mock.patch("core.db.engine", test_engine), mock.patch(
            "api.tasks.engine",
            test_engine,
        ):
            claimed = OutlookMailbox().get_email_by_address(email)
            context = {
                "provider": "microsoft",
                "email": email,
                "account_id": claimed.account_id,
                "extra": dict(claimed.extra),
            }
            retry = _bind_chatgpt_retry_mailbox(
                OutlookMailbox(),
                context,
                email,
            )
            reused = retry.get_email()
            self.assertEqual(
                reused.extra["_outlook_lease_owner"],
                context["extra"]["_outlook_lease_owner"],
            )
            final_account = AccountModel(
                platform="chatgpt",
                email=email,
                password="chatgpt-password",
                status="registered",
            )
            final_account.set_extra(
                {
                    "refresh_token": "saved-refresh-token",
                    "mailbox_login_context": context,
                }
            )
            with Session(test_engine) as session:
                session.add(final_account)
                session.commit()
                session.refresh(final_account)
                final_id = int(final_account.id)
            self.assertTrue(retry.mark_account_used(final_account))

        with Session(test_engine) as session:
            row = session.exec(select(OutlookAccountModel)).one()
            self.assertEqual(row.state, "bound")
            self.assertEqual(row.bound_account_id, final_id)

    def test_legacy_mailapi_retry_backfills_incarnation_after_release(self):
        test_engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(test_engine)
        email = "legacy-released@example.com"
        with Session(test_engine) as session:
            session.add(
                OutlookAccountModel(
                    email=email,
                    password="",
                    account_type="mailapi_url",
                    mailapi_url="https://mail.example.test/inbox/token",
                )
            )
            session.commit()
        with mock.patch("core.db.engine", test_engine), mock.patch(
            "api.tasks.engine",
            test_engine,
        ):
            source = OutlookMailbox()
            claimed = source.get_email_by_address(email)
            context = {
                "provider": "microsoft",
                "email": email,
                "account_id": claimed.account_id,
                "extra": dict(claimed.extra),
            }
            context["extra"].pop("_outlook_created_at", None)
            self.assertTrue(source.requeue_account(claimed))
            retry = _bind_chatgpt_retry_mailbox(
                OutlookMailbox(),
                context,
                email,
            )
            reclaimed = retry.get_email()

        self.assertTrue(reclaimed.extra["_outlook_created_at"])
        self.assertEqual(reclaimed.email, email)

    def test_legacy_mailapi_retry_backfills_still_owned_lease(self):
        test_engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(test_engine)
        email = "legacy-leased@example.com"
        with Session(test_engine) as session:
            session.add(
                OutlookAccountModel(
                    email=email,
                    password="",
                    account_type="mailapi_url",
                    mailapi_url="https://mail.example.test/inbox/token",
                )
            )
            session.commit()
        with mock.patch("core.db.engine", test_engine), mock.patch(
            "api.tasks.engine",
            test_engine,
        ):
            claimed = OutlookMailbox().get_email_by_address(email)
            context = {
                "provider": "microsoft",
                "email": email,
                "account_id": claimed.account_id,
                "extra": dict(claimed.extra),
            }
            context["extra"].pop("_outlook_created_at", None)
            retry = _bind_chatgpt_retry_mailbox(
                OutlookMailbox(),
                context,
                email,
            )
            reused = retry.get_email()

        self.assertTrue(reused.extra["_outlook_created_at"])
        self.assertEqual(reused.email, email)

    def test_mailapi_retry_reuses_row_bound_to_same_account(self):
        test_engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(test_engine)
        email = "bound-retry@example.com"
        with Session(test_engine) as session:
            session.add(
                OutlookAccountModel(
                    email=email,
                    password="",
                    account_type="mailapi_url",
                    mailapi_url="https://mail.example.test/inbox/token",
                )
            )
            session.commit()
        with mock.patch("core.db.engine", test_engine), mock.patch(
            "api.tasks.engine",
            test_engine,
        ):
            mailbox = OutlookMailbox()
            claimed = mailbox.get_email_by_address(email)
            account = AccountModel(
                platform="chatgpt",
                email=email,
                password="chatgpt-password",
                status="registered",
            )
            context = {
                "provider": "microsoft",
                "email": email,
                "account_id": claimed.account_id,
                "extra": dict(claimed.extra),
            }
            account.set_extra(
                {
                    "refresh_token": "saved-refresh-token",
                    "mailbox_login_context": context,
                }
            )
            with Session(test_engine) as session:
                session.add(account)
                session.commit()
                session.refresh(account)
                account_id = int(account.id)
            context["extra"]["chatgpt_local_account_id"] = account_id
            claimed.extra["chatgpt_local_account_id"] = account_id
            self.assertTrue(mailbox.mark_account_used(claimed))
            retry = _bind_chatgpt_retry_mailbox(
                OutlookMailbox(),
                context,
                email,
            )
            reused = retry.get_email()
            self.assertEqual(reused.email, email)
            self.assertEqual(
                reused.extra["mailapi_url"],
                "https://mail.example.test/inbox/token",
            )
            self.assertTrue(retry.mark_account_used(account))

        with Session(test_engine) as session:
            row = session.exec(select(OutlookAccountModel)).one()
            self.assertEqual(row.state, "bound")
            self.assertEqual(row.bound_account_id, account_id)

    def test_mailapi_retry_keeps_self_contained_context_after_pool_row_deleted(self):
        test_engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(test_engine)
        email = "deleted-row@example.com"
        context = {
            "provider": "microsoft",
            "email": email,
            "account_id": "999",
            "extra": {
                "provider": "microsoft",
                "account_type": "mailapi_url",
                "mailapi_url": "https://mail.example.test/inbox/token",
                "_outlook_row_id": "999",
                "_outlook_state": "leased",
                "_outlook_lease_owner": "original-owner",
                "_outlook_lease_version": 1,
            },
        }
        retry = _bind_chatgpt_retry_mailbox(
            OutlookMailbox(),
            context,
            email,
        )
        with mock.patch("core.db.engine", test_engine), mock.patch(
            "api.tasks.engine",
            test_engine,
        ):
            restored = retry.get_email()

        self.assertEqual(restored.email, email)
        self.assertEqual(
            restored.extra["mailapi_url"],
            "https://mail.example.test/inbox/token",
        )

    def test_mailapi_retry_does_not_swap_to_reimported_same_email_row(self):
        test_engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(test_engine)
        email = "reimported@example.com"
        with Session(test_engine) as session:
            original = OutlookAccountModel(
                email=email,
                password="",
                account_type="mailapi_url",
                mailapi_url="https://old.example.test/inbox/token",
            )
            session.add(original)
            session.commit()
            session.refresh(original)
            original_id = int(original.id)
            original_created_at = original.created_at.isoformat()
            session.delete(original)
            session.commit()
            replacement = OutlookAccountModel(
                email=email,
                password="",
                account_type="mailapi_url",
                mailapi_url="https://new.example.test/inbox/token",
                created_at=datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
            )
            session.add(replacement)
            session.commit()
            session.refresh(replacement)
            self.assertEqual(int(replacement.id), original_id)
        context = {
            "provider": "microsoft",
            "email": email,
            "account_id": str(original_id),
            "extra": {
                "provider": "microsoft",
                "account_type": "mailapi_url",
                "mailapi_url": "https://old.example.test/inbox/token",
                "_outlook_row_id": str(original_id),
                "_outlook_state": "leased",
                "_outlook_lease_owner": "old-owner",
                "_outlook_lease_version": 1,
                "_outlook_created_at": original_created_at,
            },
        }
        retry = _bind_chatgpt_retry_mailbox(
            OutlookMailbox(),
            context,
            email,
        )
        with mock.patch("core.db.engine", test_engine), mock.patch(
            "api.tasks.engine",
            test_engine,
        ):
            with self.assertRaisesRegex(RuntimeError, "身份已变化"):
                retry.get_email()
        with Session(test_engine) as session:
            replacement = session.get(OutlookAccountModel, original_id)
            self.assertEqual(replacement.state, "available")
            self.assertTrue(replacement.enabled)

    def test_build_retry_request_preserves_email_card_order(self):
        bindings = [
            {
                "id": 41,
                "email": "first@example.com",
                "leadbee_code": "bei-sms-FIRST",
                "mail_provider": "microsoft",
                "status": "failed",
                "mfa_rotation_requested": True,
            },
            {
                "id": 42,
                "email": "second@example.com",
                "leadbee_code": "bei-sms-SECOND",
                "status": "failed",
                "mfa_rotation_requested": True,
            },
        ]

        with mock.patch(
            "core.config_store.config_store.get_all",
            return_value={
                "default_executor": "headless",
                "default_captcha_solver": "yescaptcha",
            },
        ):
            request = _build_chatgpt_retry_request(bindings)

        self.assertEqual(request.platform, "chatgpt")
        self.assertEqual(request.count, 2)
        self.assertEqual(request.concurrency, 1)
        self.assertEqual(
            request.extra["chatgpt_existing_account_leadbee_codes"],
            ["bei-sms-FIRST", "bei-sms-SECOND"],
        )
        self.assertEqual(
            request.extra["chatgpt_retry_bindings"],
            [
                {
                    "id": 41,
                    "email": "first@example.com",
                    "leadbee_code": "bei-sms-FIRST",
                    "mail_provider": "microsoft",
                    "mfa_rotation_requested": True,
                },
                {
                    "id": 42,
                    "email": "second@example.com",
                    "leadbee_code": "bei-sms-SECOND",
                    "mfa_rotation_requested": True,
                },
            ],
        )
        self.assertTrue(
            request.extra["chatgpt_existing_account_rotate_mfa"]
        )
        self.assertTrue(
            request.extra[
                "chatgpt_existing_account_skip_managed_mfa_rotation"
            ]
        )

    def test_build_retry_request_preserves_disabled_mfa_rotation(self):
        bindings = [
            {
                "id": 41,
                "email": "first@example.com",
                "leadbee_code": "bei-sms-FIRST",
                "mfa_rotation_requested": False,
            }
        ]

        with mock.patch(
            "core.config_store.config_store.get_all",
            return_value={"default_executor": "headless"},
        ):
            request = _build_chatgpt_retry_request(bindings)

        self.assertFalse(request.extra["chatgpt_existing_account_rotate_mfa"])
        self.assertFalse(
            request.extra["chatgpt_existing_account_skip_managed_mfa_rotation"]
        )

    def test_build_retry_request_uses_requested_concurrency_bounded_by_count(self):
        bindings = [
            {
                "id": 41,
                "email": "first@example.com",
                "leadbee_code": "bei-sms-FIRST",
            },
            {
                "id": 42,
                "email": "second@example.com",
                "leadbee_code": "bei-sms-SECOND",
            },
        ]

        with mock.patch(
            "core.config_store.config_store.get_all",
            return_value={"default_executor": "headless"},
        ):
            request = _build_chatgpt_retry_request(bindings, concurrency=10)

        self.assertEqual(request.concurrency, 2)

    def test_retry_route_keeps_empty_body_compatible_and_accepts_concurrency(self):
        app = FastAPI()
        app.include_router(tasks_module.router)
        client = TestClient(app)
        row = SimpleNamespace(id=41)

        def build_request(_rows, concurrency=1):
            return RegisterTaskRequest(
                platform="chatgpt",
                count=1,
                concurrency=concurrency,
            )

        with (
            mock.patch(
                "api.tasks._get_task_snapshot",
                return_value={"platform": "chatgpt", "status": "done"},
            ),
            mock.patch(
                "api.tasks._retryable_chatgpt_bindings",
                return_value=[row],
            ),
            mock.patch(
                "api.tasks._build_chatgpt_retry_request",
                side_effect=build_request,
            ) as build,
            mock.patch("api.tasks.Session") as session,
            mock.patch(
                "api.tasks.enqueue_register_task",
                side_effect=["task-default", "task-concurrent"],
            ),
        ):
            session.return_value.__enter__.return_value.get.return_value = None
            default_response = client.post("/tasks/task-failed/retry-failed")
            concurrent_response = client.post(
                "/tasks/task-failed/retry-failed",
                json={"concurrency": 4},
            )

        self.assertEqual(default_response.status_code, 200)
        self.assertEqual(default_response.json()["concurrency"], 1)
        self.assertEqual(concurrent_response.status_code, 200)
        self.assertEqual(concurrent_response.json()["concurrency"], 4)
        self.assertEqual(
            [call.kwargs["concurrency"] for call in build.call_args_list],
            [1, 4],
        )

    def test_retry_request_rejects_non_integer_concurrency(self):
        for invalid in (True, 1.0, "1"):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                ChatGPTRetryFailedTaskRequest(concurrency=invalid)

    def test_failed_binding_is_persisted_with_email_and_card_for_later_retry(self):
        test_engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(test_engine)

        with mock.patch("api.tasks.engine", test_engine):
            _upsert_chatgpt_attempt_binding(
                task_id="task-original",
                attempt_index=3,
                email="bound@example.com",
                leadbee_code="bei-sms-BOUND",
                stage="phone",
                status="failed",
                error="temporary phone failure",
                mailbox_context={"provider": "microsoft"},
            )
            rows = _retryable_chatgpt_bindings("task-original")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].email, "bound@example.com")
        self.assertEqual(rows[0].leadbee_code, "bei-sms-BOUND")
        self.assertEqual(rows[0].attempt_index, 3)

    def test_late_binding_writer_does_not_restore_deleted_account_id(self):
        test_engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(test_engine)

        with mock.patch("api.tasks.engine", test_engine):
            row = _upsert_chatgpt_attempt_binding(
                task_id="task-late-writer",
                attempt_index=0,
                email="deleted@example.com",
                account_id=77,
                leadbee_code="bei-sms-LATE",
                status="failed",
            )

        self.assertEqual(row.account_id, 0)

    def test_reused_account_id_does_not_resolve_other_email_binding(self):
        test_engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(test_engine)
        replacement = AccountModel(
            id=77,
            platform="chatgpt",
            email="replacement@example.com",
            password="password",
        )
        replacement.set_extra({"refresh_token": "replacement-rt"})
        binding = ChatGPTAttemptBindingModel(
            task_id="task-reused-id",
            attempt_index=0,
            email="deleted@example.com",
            account_id=77,
            leadbee_code="bei-sms-RETRY",
            status="failed",
        )
        with Session(test_engine) as session:
            session.add(replacement)
            session.add(binding)
            session.commit()

        with mock.patch("api.tasks.engine", test_engine):
            retryable = _retryable_chatgpt_bindings("task-reused-id")

        self.assertEqual(len(retryable), 1)
        self.assertEqual(retryable[0].email, "deleted@example.com")
        self.assertEqual(retryable[0].status, "failed")

    def test_service_restart_returns_interrupted_bindings_to_failed_state(self):
        test_engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(test_engine)
        with Session(test_engine) as session:
            session.add(
                ChatGPTAttemptBindingModel(
                    task_id="task-interrupted",
                    attempt_index=0,
                    email="retry@example.com",
                    leadbee_code="bei-sms-RETRY",
                    status="retrying",
                )
            )
            session.commit()

        with mock.patch("core.db.engine", test_engine):
            _recover_chatgpt_attempt_bindings()

        with Session(test_engine) as session:
            row = session.exec(select(ChatGPTAttemptBindingModel)).one()
            self.assertEqual(row.status, "failed")
            self.assertIn("服务重启", row.error)

    def test_retry_endpoint_hides_card_and_queues_the_bound_retry(self):
        test_engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(test_engine)
        with Session(test_engine) as session:
            session.add(
                ChatGPTAttemptBindingModel(
                    task_id="task-failed",
                    attempt_index=0,
                    email="bound@example.com",
                    leadbee_code="bei-sms-SECRET-CODE",
                    status="failed",
                    mailbox_context_json='{"provider":"microsoft"}',
                )
            )
            session.commit()

        snapshot = {
            "id": "task-failed",
            "platform": "chatgpt",
            "status": "done",
        }
        with (
            mock.patch("api.tasks.engine", test_engine),
            mock.patch("api.tasks._get_task_snapshot", return_value=snapshot),
            mock.patch(
                "core.config_store.config_store.get_all",
                return_value={"default_executor": "headless"},
            ),
            mock.patch(
                "api.tasks.enqueue_register_task",
                return_value="task-retry",
            ) as enqueue,
        ):
            public = get_retryable_task_bindings("task-failed")
            result = retry_failed_task_bindings(
                "task-failed",
                BackgroundTasks(),
            )

        self.assertEqual(public["count"], 1)
        self.assertNotIn("leadbee_code", public["items"][0])
        self.assertEqual(result["task_id"], "task-retry")
        queued_request = enqueue.call_args.args[0]
        self.assertEqual(
            queued_request.extra["chatgpt_retry_bindings"][0],
            {
                "id": 1,
                "email": "bound@example.com",
                "leadbee_code": "bei-sms-SECRET-CODE",
                "mail_provider": "microsoft",
                "mfa_rotation_requested": False,
            },
        )
        self.assertFalse(
            queued_request.extra["chatgpt_existing_account_rotate_mfa"]
        )
        with Session(test_engine) as session:
            row = session.exec(select(ChatGPTAttemptBindingModel)).one()
            self.assertEqual(row.status, "retrying")


if __name__ == "__main__":
    unittest.main()
