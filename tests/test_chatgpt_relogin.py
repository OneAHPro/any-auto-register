import threading
import time
import tempfile
import unittest
from contextlib import nullcontext
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from core.base_mailbox import MailboxAccount, OutlookMailbox
from core.db import AccountModel, OutlookAccountModel
from core.task_runtime import RegisterTaskControl, StopTaskRequested
from platforms.chatgpt.oauth_client import OAuthClient
from platforms.chatgpt.utils import FlowState
from services.chatgpt_account_state import ChatGPTAccountDeactivatedError
from services.chatgpt_relogin import (
    ChatGPTMailboxOTPTimeoutError,
    _build_email_service,
    _load_saved_account,
    _login_with_saved_credentials,
    _recover_url_login_credentials,
    is_saved_chatgpt_account_relogin_eligible,
    list_auto_maintenance_account_ids,
    list_relogin_eligible_account_ids,
    refresh_or_relogin_chatgpt_account,
    relogin_chatgpt_account,
)


class ChatGPTReloginTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)
        self.account_id = self._add_account()

    def _add_account(self, *, extra=None, password="chatgpt-password") -> int:
        account = AccountModel(
            platform="chatgpt",
            email="demo@example.com",
            password=password,
            user_id="old-user",
            token="old-at",
            status="invalid",
        )
        account.set_extra(
            extra
            or {
                "access_token": "old-at",
                "refresh_token": "old-rt",
                "id_token": "old-id",
                "chatgpt_local": {"auth": {"state": "access_token_invalidated"}},
                "mailbox_login_context": {
                    "provider": "microsoft",
                    "email": "demo@example.com",
                    "account_id": "mailbox-1",
                    "extra": {
                        "client_id": "mail-client",
                        "refresh_token": "mail-refresh",
                    },
                },
                "sync_statuses": {"cpa": {"uploaded": True}},
                "keep_me": "preserved",
            }
        )
        with Session(self.engine) as session:
            session.add(account)
            session.commit()
            session.refresh(account)
            return int(account.id)

    def _add_eligibility_account(
        self,
        email: str,
        *,
        password: str = "",
        extra: dict | None = None,
        platform: str = "chatgpt",
        status: str = "registered",
        account_refresh_token: str | None = "saved-chatgpt-rt",
    ) -> int:
        account = AccountModel(
            platform=platform,
            email=email,
            password=password,
            status=status,
        )
        account_extra = dict(extra or {})
        if platform == "chatgpt" and account_refresh_token is not None:
            account_extra.setdefault("refresh_token", account_refresh_token)
        account.set_extra(account_extra)
        with Session(self.engine) as session:
            session.add(account)
            session.commit()
            session.refresh(account)
            return int(account.id)

    def test_password_only_account_without_mailbox_context_is_not_eligible(self):
        account_id = self._add_eligibility_account(
            "password@example.com",
            password="saved-password",
        )

        self.assertFalse(
            is_saved_chatgpt_account_relogin_eligible(
                account_id,
                database_engine=self.engine,
            )
        )

    def test_load_saved_account_recovers_yisen_mailapi_token(self):
        email = "relogin@yisen.uk"
        account_id = self._add_eligibility_account(
            email,
            password="chatgpt-password",
            extra={"refresh_token": "saved-chatgpt-rt"},
        )
        with Session(self.engine) as session:
            session.add(
                OutlookAccountModel(
                    email=email,
                    password="mail-password",
                    account_type="mailapi_url",
                    mailapi_url=(
                        "https://mail.yisen.uk/api/mails"
                        "?login=relogin%40yisen.uk&limit=20&offset=0"
                    ),
                    mailapi_token="header.payload.relogin-signature",
                )
            )
            session.commit()

        with mock.patch("services.chatgpt_relogin.engine", self.engine):
            saved = _load_saved_account(account_id)

        self.assertEqual(
            saved["mailbox_context"]["extra"]["mailapi_token"],
            "header.payload.relogin-signature",
        )

    def test_password_totp_context_is_eligible(self):
        account_id = self._add_eligibility_account(
            "mfa@example.com",
            password="saved-password",
            extra={
                "mailbox_login_context": {
                    "provider": "chatgpt_credentials",
                    "email": "mfa@example.com",
                    "extra": {
                        "account_type": "chatgpt_password_totp",
                        "totp_secret": "MFA-SECRET",
                    },
                }
            },
        )

        self.assertTrue(
            is_saved_chatgpt_account_relogin_eligible(
                account_id,
                database_engine=self.engine,
            )
        )

    def test_password_totp_pool_context_uses_recovery_rules(self):
        account_id = self._add_eligibility_account(
            "pool-mfa@example.com",
            password="saved-password",
            extra={
                "mailbox_login_context": {
                    "provider": "chatgpt_credentials",
                    "email": "pool-mfa@example.com",
                    "extra": {
                        "account_type": "chatgpt_password_totp",
                        "pool_file": "mfa.json",
                    },
                }
            },
        )

        with mock.patch(
            "services.chatgpt_relogin.load_applemail_pool_records",
            return_value=(
                SimpleNamespace(name="mfa.json"),
                [
                    {
                        "email": "pool-mfa@example.com",
                        "password": "saved-password",
                        "totp_secret": "MFA-SECRET",
                        "account_type": "chatgpt_password_totp",
                    }
                ],
            ),
        ):
            self.assertTrue(
                is_saved_chatgpt_account_relogin_eligible(
                    account_id,
                    database_engine=self.engine,
                    config={"applemail_pool_dir": "mail"},
                )
            )

    def test_saved_mailbox_and_mailapi_contexts_are_eligible(self):
        mailbox_id = self._add_eligibility_account(
            "mailbox@example.com",
            extra={
                "mailbox_login_context": {
                    "provider": "microsoft",
                    "email": "mailbox@example.com",
                    "extra": {
                        "client_id": "mail-client",
                        "refresh_token": "mail-refresh",
                    },
                }
            },
        )
        mailapi_id = self._add_eligibility_account(
            "mailapi@example.com",
            extra={
                "mailbox_login_context": {
                    "provider": "microsoft",
                    "email": "mailapi@example.com",
                    "extra": {
                        "account_type": "mailapi_url",
                        "mailapi_url": "https://mail.example.test/messages",
                    },
                }
            },
        )

        self.assertTrue(
            is_saved_chatgpt_account_relogin_eligible(
                mailbox_id,
                database_engine=self.engine,
            )
        )
        self.assertTrue(
            is_saved_chatgpt_account_relogin_eligible(
                mailapi_id,
                database_engine=self.engine,
            )
        )

    def test_saved_mailbox_credentials_without_account_refresh_token_are_not_eligible(self):
        account_id = self._add_eligibility_account(
            "incomplete@example.com",
            account_refresh_token=None,
            extra={
                "mailbox_login_context": {
                    "provider": "microsoft",
                    "email": "incomplete@example.com",
                    "extra": {
                        "client_id": "mail-client",
                        "refresh_token": "mail-refresh",
                    },
                }
            },
        )

        self.assertFalse(
            is_saved_chatgpt_account_relogin_eligible(
                account_id,
                database_engine=self.engine,
            )
        )

    def test_legacy_camel_case_account_refresh_token_is_eligible(self):
        account_id = self._add_eligibility_account(
            "legacy-rt@example.com",
            account_refresh_token=None,
            extra={
                "refresh_token": "   ",
                "refreshToken": "saved-legacy-rt",
                "mailbox_login_context": {
                    "provider": "microsoft",
                    "email": "legacy-rt@example.com",
                    "extra": {
                        "client_id": "mail-client",
                        "refresh_token": "mail-refresh",
                    },
                },
            },
        )

        self.assertTrue(
            is_saved_chatgpt_account_relogin_eligible(
                account_id,
                database_engine=self.engine,
            )
        )

    def test_generic_provider_context_uses_saved_identity_and_global_config(self):
        skymail_id = self._add_eligibility_account(
            "saved-skymail@example.com",
            extra={
                "mailbox_login_context": {
                    "provider": "skymail",
                    "email": "saved-skymail@example.com",
                    "account_id": "saved-skymail@example.com",
                    "extra": {},
                }
            },
        )
        laoudo_id = self._add_eligibility_account(
            "saved-laoudo@example.com",
            extra={
                "mailbox_login_context": {
                    "provider": "laoudo",
                    "email": "saved-laoudo@example.com",
                    "account_id": "mailbox-account-id",
                    "extra": {},
                }
            },
        )

        self.assertTrue(
            is_saved_chatgpt_account_relogin_eligible(
                skymail_id,
                database_engine=self.engine,
                config={
                    "skymail_api_base": "https://mail.example.test",
                    "skymail_token": "mail-token",
                    "skymail_domain": "example.test",
                },
            )
        )
        self.assertTrue(
            is_saved_chatgpt_account_relogin_eligible(
                laoudo_id,
                database_engine=self.engine,
                config={"laoudo_auth": "mail-token"},
            )
        )

    def test_skymail_saved_identity_without_token_is_not_eligible(self):
        account_id = self._add_eligibility_account(
            "missing-skymail-token@example.com",
            extra={
                "mailbox_login_context": {
                    "provider": "skymail",
                    "email": "missing-skymail-token@example.com",
                    "account_id": "missing-skymail-token@example.com",
                    "extra": {},
                }
            },
        )

        self.assertFalse(
            is_saved_chatgpt_account_relogin_eligible(
                account_id,
                database_engine=self.engine,
                config={
                    "skymail_api_base": "https://mail.example.test",
                    "skymail_domain": "example.test",
                },
            )
        )

    def test_configured_generic_providers_require_receive_configuration(self):
        cases = [
            (
                "cloudmail",
                {"cloudmail_api_base": "https://mail.example.test"},
                {
                    "cloudmail_api_base": "https://mail.example.test",
                    "cloudmail_admin_password": "mail-password",
                },
            ),
            ("laoudo", {}, {"laoudo_auth": "mail-token"}),
            ("luckmail", {}, {"luckmail_api_key": "mail-token"}),
            ("maliapi", {}, {"maliapi_api_key": "mail-token"}),
            (
                "opentrashmail",
                {},
                {"opentrashmail_api_url": "https://mail.example.test"},
            ),
            (
                "cfworker",
                {},
                {"cfworker_api_url": "https://mail.example.test"},
            ),
        ]

        for index, (provider, incomplete, complete) in enumerate(cases):
            with self.subTest(provider=provider):
                account_id = self._add_eligibility_account(
                    f"configured-{index}@example.com",
                    extra={
                        "mailbox_login_context": {
                            "provider": provider,
                            "email": f"configured-{index}@example.com",
                            "account_id": f"mailbox-{index}",
                            "extra": {},
                        }
                    },
                )
                self.assertFalse(
                    is_saved_chatgpt_account_relogin_eligible(
                        account_id,
                        database_engine=self.engine,
                        config=incomplete,
                    )
                )
                self.assertTrue(
                    is_saved_chatgpt_account_relogin_eligible(
                        account_id,
                        database_engine=self.engine,
                        config=complete,
                    )
                )

    def test_public_generic_providers_need_only_persisted_identity(self):
        for index, provider in enumerate(
            ("tempmail_lol", "duckmail", "gptmail")
        ):
            with self.subTest(provider=provider):
                account_id = self._add_eligibility_account(
                    f"public-{index}@example.com",
                    extra={
                        "mailbox_login_context": {
                            "provider": provider,
                            "email": f"public-{index}@example.com",
                            "account_id": f"mailbox-{index}",
                            "extra": {},
                        }
                    },
                )
                self.assertTrue(
                    is_saved_chatgpt_account_relogin_eligible(
                        account_id,
                        database_engine=self.engine,
                        config={},
                    )
                )

    def test_session_bound_generic_providers_are_not_reconstructable(self):
        cases = [
            (
                "freemail",
                {
                    "freemail_api_url": "https://mail.example.test",
                    "freemail_admin_token": "mail-token",
                },
            ),
            ("moemail", {"moemail_api_key": "mail-token"}),
        ]

        for index, (provider, config) in enumerate(cases):
            with self.subTest(provider=provider):
                account_id = self._add_eligibility_account(
                    f"session-bound-{index}@example.com",
                    extra={
                        "mailbox_login_context": {
                            "provider": provider,
                            "email": f"session-bound-{index}@example.com",
                            "account_id": f"mailbox-{index}",
                            "extra": {},
                        }
                    },
                )
                self.assertFalse(
                    is_saved_chatgpt_account_relogin_eligible(
                        account_id,
                        database_engine=self.engine,
                        config=config,
                    )
                )

    def test_generic_provider_context_with_only_unrelated_password_is_not_eligible(self):
        account_id = self._add_eligibility_account(
            "generic-password@example.com",
            extra={
                "mailbox_login_context": {
                    "provider": "skymail",
                    "extra": {"password": "unrelated-password"},
                }
            },
        )

        self.assertFalse(
            is_saved_chatgpt_account_relogin_eligible(
                account_id,
                database_engine=self.engine,
                config={
                    "skymail_api_base": "https://mail.example.test",
                    "skymail_token": "mail-token",
                    "skymail_domain": "example.test",
                },
            )
        )

    def test_applemail_password_only_context_is_not_eligible(self):
        account_id = self._add_eligibility_account(
            "apple-password@example.com",
            extra={
                "mailbox_login_context": {
                    "provider": "applemail",
                    "email": "apple-password@example.com",
                    "extra": {"password": "mail-password"},
                }
            },
        )

        self.assertFalse(
            is_saved_chatgpt_account_relogin_eligible(
                account_id,
                database_engine=self.engine,
            )
        )

    def test_applemail_oauth_context_is_eligible(self):
        account_id = self._add_eligibility_account(
            "apple-oauth@example.com",
            extra={
                "mailbox_login_context": {
                    "provider": "applemail",
                    "email": "apple-oauth@example.com",
                    "extra": {
                        "client_id": "mail-client",
                        "refresh_token": "mail-refresh",
                    },
                }
            },
        )

        self.assertTrue(
            is_saved_chatgpt_account_relogin_eligible(
                account_id,
                database_engine=self.engine,
            )
        )

    def test_icloud_password_only_context_is_not_eligible(self):
        account_id = self._add_eligibility_account(
            "icloud-password@example.com",
            extra={
                "mailbox_login_context": {
                    "provider": "icloud",
                    "email": "icloud-password@example.com",
                    "extra": {
                        "account_type": "icloud_web",
                        "password": "mail-password",
                    },
                }
            },
        )

        self.assertFalse(
            is_saved_chatgpt_account_relogin_eligible(
                account_id,
                database_engine=self.engine,
            )
        )

    def test_icloud_password_mfa_context_is_eligible(self):
        account_id = self._add_eligibility_account(
            "icloud-mfa@example.com",
            extra={
                "mailbox_login_context": {
                    "provider": "icloud",
                    "email": "icloud-mfa@example.com",
                    "extra": {
                        "account_type": "icloud_web",
                        "password": "mail-password",
                        "mfa_secret": "MFA-SECRET",
                    },
                }
            },
        )

        self.assertTrue(
            is_saved_chatgpt_account_relogin_eligible(
                account_id,
                database_engine=self.engine,
            )
        )

    def test_mailbox_context_without_provider_is_not_eligible(self):
        account_id = self._add_eligibility_account(
            "providerless@example.com",
            extra={
                "mailbox_login_context": {
                    "email": "providerless@example.com",
                    "extra": {
                        "client_id": "mail-client",
                        "refresh_token": "mail-refresh",
                    },
                }
            },
        )

        self.assertFalse(
            is_saved_chatgpt_account_relogin_eligible(
                account_id,
                database_engine=self.engine,
            )
        )

    def test_password_only_chatgpt_credentials_context_is_not_eligible(self):
        account_id = self._add_eligibility_account(
            "password-no-mfa@example.com",
            password="saved-password",
            extra={
                "mailbox_login_context": {
                    "provider": "chatgpt_credentials",
                    "email": "password-no-mfa@example.com",
                    "extra": {
                        "account_type": "chatgpt_password_totp",
                    },
                }
            },
        )

        self.assertFalse(
            is_saved_chatgpt_account_relogin_eligible(
                account_id,
                database_engine=self.engine,
            )
        )

    def test_enabled_outlook_fallback_is_eligible_without_mutating_account(self):
        account_id = self._add_eligibility_account("fallback@example.com")
        with Session(self.engine) as session:
            session.add(
                OutlookAccountModel(
                    email="fallback@example.com",
                    password="mail-password",
                    client_id="mail-client",
                    refresh_token="mail-refresh",
                    enabled=True,
                )
            )
            session.commit()

        self.assertTrue(
            is_saved_chatgpt_account_relogin_eligible(
                account_id,
                database_engine=self.engine,
            )
        )
        with Session(self.engine) as session:
            saved = session.get(AccountModel, account_id)
            self.assertNotIn("mailbox_login_context", saved.get_extra())

    def test_missing_account_and_missing_credentials_are_not_eligible(self):
        account_id = self._add_eligibility_account("empty@example.com")

        self.assertFalse(
            is_saved_chatgpt_account_relogin_eligible(
                account_id,
                database_engine=self.engine,
            )
        )
        self.assertFalse(
            is_saved_chatgpt_account_relogin_eligible(
                999_999,
                database_engine=self.engine,
            )
        )

    def test_invalid_status_remains_relogin_eligible(self):
        account_id = self._add_eligibility_account(
            "invalid@example.com",
            password="saved-password",
            status="invalid",
            extra={
                "mailbox_login_context": {
                    "provider": "chatgpt_credentials",
                    "email": "invalid@example.com",
                    "extra": {
                        "account_type": "chatgpt_password_totp",
                        "totp_secret": "MFA-SECRET",
                    },
                }
            },
        )

        self.assertTrue(
            is_saved_chatgpt_account_relogin_eligible(
                account_id,
                database_engine=self.engine,
            )
        )

    def test_eligible_account_ids_are_filtered_and_deterministic(self):
        password_mfa_id = self._add_eligibility_account(
            "z-password@example.com",
            password="saved-password",
            extra={
                "mailbox_login_context": {
                    "provider": "chatgpt_credentials",
                    "email": "z-password@example.com",
                    "extra": {
                        "account_type": "chatgpt_password_totp",
                        "totp_secret": "MFA-SECRET",
                    },
                }
            },
        )
        self._add_eligibility_account("missing@example.com")
        mailbox_id = self._add_eligibility_account(
            "a-mailbox@example.com",
            extra={
                "mailbox_login_context": {
                    "provider": "microsoft",
                    "extra": {
                        "client_id": "mail-client",
                        "refresh_token": "mail-refresh",
                    },
                }
            },
        )
        self._add_eligibility_account(
            "other@example.com",
            password="saved-password",
            platform="other",
        )

        self.assertEqual(
            list_relogin_eligible_account_ids(database_engine=self.engine),
            [self.account_id, password_mfa_id, mailbox_id],
        )

    def test_build_email_service_discards_persisted_oauth_access_token_cache(self):
        saved = {
            "email": "demo@example.com",
            "extra": {},
            "mailbox_context": {
                "provider": "microsoft",
                "email": "demo@example.com",
                "account_id": "mailbox-1",
                "extra": {
                    "client_id": "mail-client",
                    "refresh_token": "mail-refresh",
                    "_oauth_token_cache": {
                        "imap": {
                            "access_token": "stale-access-token",
                            "expires_at": 9_999_999_999,
                        }
                    },
                },
            },
        }
        mailbox = mock.Mock()

        with mock.patch(
            "services.chatgpt_relogin.create_mailbox",
            return_value=mailbox,
        ):
            service = _build_email_service(
                saved,
                {},
                log_fn=None,
            )

        self.assertNotIn("_oauth_token_cache", service._account.extra)
        self.assertNotIn(
            "_oauth_token_cache",
            service.get_mailbox_metadata()["extra"],
        )

    def test_microsoft_mailapi_password_reset_keeps_outlook_backend(self):
        saved = {
            "email": "reset@example.com",
            "password": "",
            "extra": {},
            "mailbox_context": {
                "provider": "microsoft",
                "email": "reset@example.com",
                "account_id": "mailbox-1",
                "extra": {
                    "account_type": "mailapi_url",
                    "mailapi_url": "https://mail.example.test/reset",
                    "totp_secret": "JBSWY3DPEHPK3PXP",
                    "mfa_recovery_code": "RECOVERY-CODE",
                    "chatgpt_mfa_managed": True,
                },
            },
        }
        mailbox = OutlookMailbox()
        mailbox.get_current_ids = mock.Mock(return_value=set())

        with mock.patch(
            "services.chatgpt_relogin.create_mailbox",
            return_value=mailbox,
        ) as create_mailbox_mock:
            service = _build_email_service(
                saved,
                {},
                log_fn=None,
                force_password_reset=True,
            )

        create_mailbox_mock.assert_called_once_with(
            "microsoft",
            extra=mock.ANY,
            proxy=None,
        )
        email_info = service.create_email()
        self.assertEqual(
            email_info["account_type"],
            "chatgpt_password_reset_url_mail",
        )
        self.assertTrue(email_info["password_reset_required"])
        self.assertGreaterEqual(len(email_info["new_password"]), 12)
        self.assertEqual(
            email_info["totp_secret"],
            "JBSWY3DPEHPK3PXP",
        )
        self.assertEqual(
            email_info["mfa_recovery_code"],
            "RECOVERY-CODE",
        )
        metadata_extra = service.get_mailbox_metadata()["extra"]
        self.assertEqual(
            metadata_extra["mfa_recovery_code"],
            "RECOVERY-CODE",
        )
        self.assertTrue(metadata_extra["chatgpt_mfa_managed"])

    def test_persisted_reset_url_service_prefers_email_mfa_without_totp_url(self):
        from services.chatgpt_relogin import _PersistedEmailService

        service = _PersistedEmailService(
            mailbox=mock.Mock(),
            mailbox_account=MailboxAccount(
                email="reset@example.com",
                account_id="reset@example.com",
                extra={
                    "account_type": "chatgpt_password_reset_url_mail",
                    "mail_api_url": "https://mail.example.test/mail",
                },
            ),
            mailbox_context={},
            provider="chatgpt_credentials",
            log_fn=None,
        )

        self.assertFalse(service.supports_totp_code())

    def test_persisted_email_service_limits_each_call_without_spending_total_budget(self):
        from services.chatgpt_relogin import _PersistedEmailService

        mailbox = mock.Mock()
        mailbox.get_current_ids.return_value = set()
        mailbox.wait_for_code.side_effect = TimeoutError("mailbox wait expired")
        mailbox.pause_active_slot_for_mailbox_wait.return_value = nullcontext(True)
        logs = []
        service = _PersistedEmailService(
            mailbox=mailbox,
            mailbox_account=MailboxAccount(
                email="slow@example.com",
                account_id="slow@example.com",
                extra={},
            ),
            mailbox_context={},
            provider="fixture",
            log_fn=logs.append,
            otp_timeout_seconds=300,
        )
        service.create_email()
        self.assertTrue(service._baseline_ready.wait(timeout=1))

        with self.assertRaises(TimeoutError):
            service.get_verification_code(timeout=30)
        with self.assertRaises(TimeoutError):
            service.get_verification_code(timeout=30)

        self.assertEqual(
            [call.kwargs["timeout"] for call in mailbox.wait_for_code.call_args_list],
            [20, 10, 30],
        )
        self.assertEqual(
            [
                call.kwargs["poll_interval"]
                for call in mailbox.wait_for_code.call_args_list
            ],
            [3, 10, 10],
        )
        self.assertEqual(
            mailbox.pause_active_slot_for_mailbox_wait.call_count,
            2,
        )
        self.assertTrue(any("后台等待" in message for message in logs))

    def test_url_mailbox_service_binds_task_control_and_timeout_budget(self):
        saved = {
            "email": "url@example.com",
            "password": "saved-password",
            "extra": {},
            "mailbox_context": {
                "provider": "chatgpt_credentials",
                "email": "url@example.com",
                "extra": {
                    "account_type": "chatgpt_password_url_otp",
                    "password": "saved-password",
                    "mail_api_url": "https://mail.example.test/messages/token",
                },
            },
        }
        mailbox = mock.Mock()
        task_control = mock.Mock()

        with mock.patch(
            "services.chatgpt_relogin.create_mailbox",
            return_value=mailbox,
        ):
            service = _build_email_service(
                saved,
                {"mailbox_otp_timeout_seconds": 75},
                log_fn=None,
                task_control=task_control,
                attempt_id=42,
            )

        self.assertIs(mailbox._task_control, task_control)
        self.assertEqual(mailbox._task_attempt_token, 42)
        self.assertEqual(service._otp_remaining_seconds, 75.0)

    def test_remote_mfa_service_logs_in_without_mail_receiver(self):
        lookup_url = (
            "https://2fa.nloop.cc/api/mfa/lookup"
            "?email=user%2Balias%40gmail.com"
        )
        saved = {
            "email": "user+alias@gmail.com",
            "password": "saved-password",
            "extra": {},
            "mailbox_context": {
                "provider": "chatgpt_credentials",
                "email": "user+alias@gmail.com",
                "extra": {
                    "account_type": "chatgpt_password_remote_totp",
                    "password": "saved-password",
                    "totp_url": lookup_url,
                },
            },
        }
        mailbox = mock.Mock()
        mailbox.get_totp_code.return_value = "654321"

        with mock.patch(
            "services.chatgpt_relogin.create_mailbox",
            return_value=mailbox,
        ):
            service = _build_email_service(saved, {}, log_fn=None)

        email_info = service.create_email()

        self.assertEqual(
            email_info["account_type"],
            "chatgpt_password_remote_totp",
        )
        self.assertEqual(email_info["password"], "saved-password")
        self.assertEqual(email_info["totp_url"], lookup_url)
        self.assertEqual(service.get_totp_code(), "654321")
        self.assertFalse(service.supports_email_verification())
        mailbox.get_current_ids.assert_not_called()

    def test_google_federated_service_relogin_uses_saved_password_without_mail_receiver(self):
        saved = {
            "email": "worker@custom-google-domain.example",
            "password": "supplier-password",
            "extra": {},
            "mailbox_context": {
                "provider": "chatgpt_credentials",
                "email": "worker@custom-google-domain.example",
                "extra": {
                    "account_type": "chatgpt_google_password",
                    "password": "supplier-password",
                    "totp_secret": "JBSWY3DPEHPK3PXP",
                    "mfa_recovery_code": "RECOVERY-CODE",
                },
            },
        }

        service = _build_email_service(saved, {}, log_fn=None)
        email_info = service.create_email()

        self.assertEqual(
            email_info["account_type"],
            "chatgpt_google_password",
        )
        self.assertEqual(email_info["password"], "supplier-password")
        self.assertEqual(email_info["totp_secret"], "JBSWY3DPEHPK3PXP")
        self.assertEqual(email_info["mfa_recovery_code"], "RECOVERY-CODE")
        self.assertFalse(service.supports_email_verification())

        self.assertTrue(
            service.commit_mfa_rotation(
                totp_secret="NEW-TOTP-SECRET",
                recovery_code="NEW-RECOVERY-CODE",
                rotated_at="2026-08-21T03:00:00Z",
            )
        )
        stored_extra = service.get_mailbox_metadata()["extra"]
        self.assertEqual(stored_extra["totp_secret"], "NEW-TOTP-SECRET")
        self.assertEqual(
            stored_extra["mfa_recovery_code"],
            "NEW-RECOVERY-CODE",
        )

    def test_password_totp_with_mail_url_reads_email_otp_during_relogin(self):
        mail_api_url = "https://mail.example.test/messages/token"
        saved = {
            "email": "mfa-mail@example.com",
            "password": "saved-password",
            "extra": {},
            "mailbox_context": {
                "provider": "chatgpt_credentials",
                "email": "mfa-mail@example.com",
                "extra": {
                    "account_type": "chatgpt_password_totp",
                    "password": "saved-password",
                    "totp_secret": "JBSWY3DPEHPK3PXP",
                    "mfa_recovery_code": "RECOVERY-CODE",
                    "mail_api_url": mail_api_url,
                    "mailapi_token": "yisen-mailapi-token",
                    "pool_file": "mfa-mail.json",
                },
            },
        }
        mailbox = mock.Mock()
        mailbox.get_email_by_address.return_value = MailboxAccount(
            email="mfa-mail@example.com",
            account_id="mfa-mail@example.com",
            extra={},
        )
        mailbox.get_current_ids.return_value = set()
        mailbox.wait_for_code.return_value = "123456"
        mailbox.pause_active_slot_for_mailbox_wait.return_value = nullcontext(
            True
        )

        with mock.patch(
            "services.chatgpt_relogin.create_mailbox",
            return_value=mailbox,
        ):
            service = _build_email_service(
                saved,
                {
                    "applemail_pool_dir": "mail",
                    "mailbox_otp_timeout_seconds": 75,
                },
                log_fn=None,
            )

        email_info = service.create_email()
        self.assertEqual(email_info["account_type"], "chatgpt_password_totp")
        self.assertEqual(email_info["password"], "saved-password")
        self.assertEqual(email_info["totp_secret"], "JBSWY3DPEHPK3PXP")
        self.assertEqual(
            email_info["mfa_recovery_code"],
            "RECOVERY-CODE",
        )
        self.assertEqual(email_info["mail_api_url"], mail_api_url)
        self.assertEqual(
            service._account.extra["mailapi_token"],
            "yisen-mailapi-token",
        )
        self.assertTrue(service._baseline_ready.wait(timeout=1))
        self.assertEqual(
            service.get_verification_code(
                email="mfa-mail@example.com",
                timeout=30,
            ),
            "123456",
        )
        mailbox.wait_for_code.assert_called_once()

    def test_legacy_mailapi_context_promotes_saved_password_and_managed_totp(self):
        mail_api_url = "https://mail.example.test/messages/token"
        saved = {
            "email": "legacy@example.com",
            "password": "Saved-ChatGPT-Password",
            "extra": {},
            "mailbox_context": {
                "provider": "applemail",
                "email": "legacy@example.com",
                "account_id": "legacy@example.com",
                "extra": {
                    "account_type": "mailapi_url",
                    "mailapi_url": mail_api_url,
                    "totp_secret": "JBSWY3DPEHPK3PXP",
                    "mfa_recovery_code": "RECOVERY-CODE",
                    "chatgpt_mfa_managed": True,
                },
            },
        }
        mailbox = mock.Mock()
        mailbox.get_current_ids.return_value = set()

        with mock.patch(
            "services.chatgpt_relogin.create_mailbox",
            return_value=mailbox,
        ):
            service = _build_email_service(saved, {}, log_fn=None)

        email_info = service.create_email()
        self.assertEqual(
            email_info["account_type"],
            "chatgpt_password_totp",
        )
        self.assertEqual(
            email_info["password"],
            "Saved-ChatGPT-Password",
        )
        self.assertEqual(email_info["totp_secret"], "JBSWY3DPEHPK3PXP")
        self.assertEqual(email_info["mail_api_url"], mail_api_url)

    def test_passwordless_legacy_mailapi_bootstraps_and_returns_saved_password(self):
        saved = {
            "email": "bootstrap@example.com",
            "password": "",
            "extra": {},
            "mailbox_context": {
                "provider": "applemail",
                "email": "bootstrap@example.com",
                "account_id": "bootstrap@example.com",
                "extra": {
                    "account_type": "mailapi_url",
                    "mailapi_url": "https://mail.example.test/messages/token",
                    "totp_secret": "JBSWY3DPEHPK3PXP",
                    "chatgpt_mfa_managed": True,
                },
            },
        }
        mailbox = mock.Mock()
        mailbox.get_current_ids.return_value = set()
        mailbox.commit_password_reset.return_value = True

        class Adapter:
            def run(self, context):
                email_info = context.email_service.create_email()
                generated = email_info["new_password"]
                assert email_info["password_reset_required"] is True
                assert context.email_service.commit_password_reset(generated)
                return SimpleNamespace(
                    success=True,
                    error_message="",
                    access_token="new-at",
                    refresh_token="new-rt",
                    id_token="new-id",
                    session_token="new-session",
                    workspace_id="workspace-1",
                    account_id="new-user",
                    source="existing_account_web_login",
                    metadata={},
                )

        with mock.patch(
            "services.chatgpt_relogin.config_store.get_all",
            return_value={"default_executor": "headless"},
        ), mock.patch(
            "services.chatgpt_relogin.create_mailbox",
            return_value=mailbox,
        ), mock.patch(
            "services.chatgpt_relogin.build_chatgpt_registration_mode_adapter",
            return_value=Adapter(),
        ):
            tokens = _login_with_saved_credentials(saved, rotate_mfa=True)

        self.assertGreaterEqual(len(tokens["password"]), 12)
        metadata_extra = tokens["metadata"]["mailbox_login_context"]["extra"]
        self.assertEqual(metadata_extra["password"], tokens["password"])
        self.assertFalse(metadata_extra["password_reset_required"])
        self.assertEqual(
            metadata_extra["account_type"],
            "chatgpt_password_reset_url_mail",
        )

    def test_saved_login_uses_mailbox_timeout_as_all_outer_otp_budgets(self):
        saved = {
            "email": "demo@example.com",
            "password": "saved-password",
            "extra": {},
            "mailbox_context": {
                "provider": "fixture",
                "email": "demo@example.com",
                "extra": {},
            },
        }
        email_service = mock.Mock()
        email_service.get_mailbox_metadata.return_value = saved["mailbox_context"]
        adapter = mock.Mock()
        adapter.run.return_value = SimpleNamespace(
            success=True,
            error_message="",
            access_token="new-at",
            refresh_token="new-rt",
            id_token="new-id",
            session_token="new-session",
            workspace_id="workspace-1",
            account_id="new-user",
            metadata={},
        )

        with mock.patch(
            "services.chatgpt_relogin.config_store.get_all",
            return_value={
                "mailbox_otp_timeout_seconds": 75,
                "chatgpt_register_otp_wait_seconds": 600,
            },
        ), mock.patch(
            "services.chatgpt_relogin._build_email_service",
            return_value=email_service,
        ), mock.patch(
            "services.chatgpt_relogin.build_chatgpt_registration_mode_adapter",
            return_value=adapter,
        ) as build_adapter:
            _login_with_saved_credentials(saved)

        adapter_config = build_adapter.call_args.args[0]
        for key in (
            "chatgpt_oauth_otp_wait_seconds",
            "chatgpt_otp_wait_seconds",
            "chatgpt_register_otp_wait_seconds",
            "chatgpt_register_otp_resend_wait_seconds",
        ):
            self.assertEqual(adapter_config[key], 75)

    def test_saved_login_classifies_zero_code_otp_failure_after_full_budget(self):
        saved = {
            "email": "demo@example.com",
            "password": "saved-password",
            "extra": {},
            "mailbox_context": {
                "provider": "fixture",
                "email": "demo@example.com",
                "extra": {},
            },
        }
        adapter = mock.Mock()
        adapter.run.return_value = SimpleNamespace(
            success=False,
            error_message=(
                "[stage=otp] OAuth 阶段 OTP 验证失败，"
                "已尝试 0 个验证码，等待窗口 180s"
            ),
        )

        with mock.patch(
            "services.chatgpt_relogin.config_store.get_all",
            return_value={"mailbox_otp_timeout_seconds": 180},
        ), mock.patch(
            "services.chatgpt_relogin._build_email_service",
            return_value=mock.Mock(),
        ), mock.patch(
            "services.chatgpt_relogin.build_chatgpt_registration_mode_adapter",
            return_value=adapter,
        ), mock.patch(
            "services.chatgpt_relogin.time.monotonic",
            side_effect=[100.0, 280.0],
        ):
            with self.assertRaises(Exception) as raised:
                _login_with_saved_credentials(saved)

        self.assertEqual(
            type(raised.exception).__name__,
            "ChatGPTMailboxOTPTimeoutError",
        )
        self.assertEqual(raised.exception.wait_seconds, 180)
        self.assertGreaterEqual(raised.exception.elapsed_seconds, 180)

    def test_saved_login_keeps_short_or_nonempty_otp_failure_as_runtime_error(self):
        saved = {
            "email": "demo@example.com",
            "password": "saved-password",
            "extra": {},
            "mailbox_context": {
                "provider": "fixture",
                "email": "demo@example.com",
                "extra": {},
            },
        }
        cases = (
            (
                180,
                179.0,
                "[stage=otp] OAuth 阶段 OTP 验证失败，"
                "已尝试 0 个验证码，等待窗口 180s",
            ),
            (
                180,
                180.0,
                "[stage=otp] OAuth 阶段 OTP 验证失败，"
                "已尝试 1 个验证码，等待窗口 180s",
            ),
            (180, 180.0, "[stage=mfa] 远程 2FA 获取失败: HTTP 404"),
            (
                75,
                75.0,
                "[stage=otp] OAuth 阶段 OTP 验证失败，"
                "已尝试 0 个验证码，等待窗口 75s",
            ),
        )
        for wait_seconds, elapsed, detail in cases:
            with self.subTest(
                wait_seconds=wait_seconds,
                elapsed=elapsed,
                detail=detail,
            ):
                adapter = mock.Mock()
                adapter.run.return_value = SimpleNamespace(
                    success=False,
                    error_message=detail,
                )
                with mock.patch(
                    "services.chatgpt_relogin.config_store.get_all",
                    return_value={
                        "mailbox_otp_timeout_seconds": wait_seconds,
                    },
                ), mock.patch(
                    "services.chatgpt_relogin._build_email_service",
                    return_value=mock.Mock(),
                ), mock.patch(
                    "services.chatgpt_relogin."
                    "build_chatgpt_registration_mode_adapter",
                    return_value=adapter,
                ), mock.patch(
                    "services.chatgpt_relogin.time.monotonic",
                    side_effect=[100.0, 100.0 + elapsed],
                ):
                    with self.assertRaises(RuntimeError) as raised:
                        _login_with_saved_credentials(saved)

                self.assertEqual(type(raised.exception), RuntimeError)

    def test_legacy_reset_url_credentials_use_saved_account_password(self):
        saved = {
            "email": "legacy-reset@example.com",
            "password": "Saved-ChatGPT-Password",
            "extra": {},
        }
        mailbox_context = {
            "provider": "chatgpt_credentials",
            "email": "legacy-reset@example.com",
            "extra": {
                "account_type": "chatgpt_password_reset_url_mail",
                "pool_file": "legacy-reset.json",
            },
        }
        pool_record = {
            "email": "legacy-reset@example.com",
            "account_type": "chatgpt_password_reset_url_mail",
            "password": "",
            "password_reset_required": True,
            "mail_api_url": "https://mail.example.test/mail?token=MAIL_SECRET",
        }

        with mock.patch(
            "services.chatgpt_relogin.load_applemail_pool_records",
            return_value=(SimpleNamespace(name="legacy-reset.json"), [pool_record]),
        ):
            credentials = _recover_url_login_credentials(
                saved,
                mailbox_context,
                {"applemail_pool_dir": "mail"},
            )

        self.assertEqual(credentials["password"], "Saved-ChatGPT-Password")
        self.assertFalse(credentials["password_reset_required"])
        self.assertIn("MAIL_SECRET", credentials["mail_api_url"])

    def test_used_reset_url_record_can_generate_and_persist_a_new_password(self):
        from core.applemail_pool import (
            load_applemail_pool_records,
            save_applemail_pool_json,
        )
        from core.base_mailbox import AppleMailMailbox

        with tempfile.TemporaryDirectory() as tmp_dir:
            save_applemail_pool_json(
                "reset-again@example.com----登陆请点击忘记密码----"
                "https://mail.example.test/mail?token=MAIL_SECRET",
                pool_dir=tmp_dir,
                filename="reset-again.json",
            )
            pool = AppleMailMailbox(
                pool_dir=tmp_dir,
                pool_file="reset-again.json",
            )
            claimed = pool.get_email()
            self.assertTrue(pool.mark_account_used(claimed))
            saved = {
                "email": "reset-again@example.com",
                "password": "",
                "extra": {},
                "mailbox_context": {
                    "provider": "chatgpt_credentials",
                    "email": "reset-again@example.com",
                    "extra": {
                        "account_type": "chatgpt_password_reset_url_mail",
                        "pool_file": "reset-again.json",
                    },
                },
            }

            service = _build_email_service(
                saved,
                {"applemail_pool_dir": tmp_dir},
                log_fn=None,
            )
            service._mailbox.get_current_ids = mock.Mock(return_value=set())
            email_info = service.create_email()
            generated = email_info["new_password"]
            self.assertGreaterEqual(len(generated), 12)
            self.assertTrue(email_info["password_reset_required"])
            self.assertTrue(service.commit_password_reset(generated))

            metadata_extra = service.get_mailbox_metadata()["extra"]
            self.assertEqual(metadata_extra["password"], generated)
            self.assertFalse(metadata_extra["password_reset_required"])
            self.assertNotIn("new_password", metadata_extra)
            _path, records = load_applemail_pool_records(
                pool_dir=tmp_dir,
                pool_file="reset-again.json",
            )
            self.assertEqual(records[0]["password"], generated)
            self.assertEqual(records[0]["pool_state"], "used")

    def test_force_password_reset_discards_rejected_saved_password(self):
        from core.applemail_pool import save_applemail_pool_json
        from core.base_mailbox import AppleMailMailbox

        with tempfile.TemporaryDirectory() as tmp_dir:
            save_applemail_pool_json(
                "reset-rejected@example.com----登陆请点击忘记密码----"
                "https://mail.example.test/mail?token=MAIL_SECRET",
                pool_dir=tmp_dir,
                filename="reset-rejected.json",
            )
            pool = AppleMailMailbox(
                pool_dir=tmp_dir,
                pool_file="reset-rejected.json",
            )
            claimed = pool.get_email()
            self.assertTrue(pool.mark_account_used(claimed))
            self.assertTrue(
                pool.commit_password_reset(
                    claimed,
                    "Stale-Saved-Password",
                )
            )
            saved = {
                "email": "reset-rejected@example.com",
                "password": "Stale-Saved-Password",
                "extra": {},
                "mailbox_context": {
                    "provider": "chatgpt_credentials",
                    "email": "reset-rejected@example.com",
                    "extra": {
                        "account_type": "chatgpt_password_reset_url_mail",
                        "pool_file": "reset-rejected.json",
                    },
                },
            }

            service = _build_email_service(
                saved,
                {"applemail_pool_dir": tmp_dir},
                log_fn=None,
                force_password_reset=True,
            )
            service._mailbox.get_current_ids = mock.Mock(return_value=set())
            email_info = service.create_email()

            self.assertEqual(email_info["password"], "")
            self.assertTrue(email_info["password_reset_required"])
            self.assertGreaterEqual(len(email_info["new_password"]), 12)
            self.assertNotEqual(
                email_info["new_password"],
                "Stale-Saved-Password",
            )

    def test_explicit_saved_password_rejection_retries_with_forced_reset(self):
        saved = {
            "email": "reset-rejected@example.com",
            "password": "Stale-Saved-Password",
            "extra": {},
            "mailbox_context": {
                "provider": "chatgpt_credentials",
                "email": "reset-rejected@example.com",
                "extra": {
                    "account_type": "chatgpt_password_reset_url_mail",
                    "pool_file": "reset-rejected.json",
                },
            },
        }
        normal_service = mock.Mock()
        reset_service = mock.Mock()
        reset_service.get_mailbox_metadata.return_value = saved["mailbox_context"]

        class Adapter:
            def __init__(self):
                self.calls = 0

            def run(self, _context):
                self.calls += 1
                if self.calls == 1:
                    return SimpleNamespace(
                        success=False,
                        error_message=(
                            "[stage=authorize_continue] 密码验证失败: HTTP 401 "
                            "(invalid_credentials)"
                        ),
                    )
                return SimpleNamespace(
                    success=True,
                    error_message="",
                    access_token="new-at",
                    refresh_token="new-rt",
                    id_token="new-id",
                    session_token="new-session",
                    workspace_id="workspace-1",
                    account_id="new-user",
                    metadata={},
                )

        adapter = Adapter()
        logs = []
        with mock.patch(
            "services.chatgpt_relogin.config_store.get_all",
            return_value={"default_executor": "headless"},
        ), mock.patch(
            "services.chatgpt_relogin._build_email_service",
            side_effect=[normal_service, reset_service],
        ) as build_service, mock.patch(
            "services.chatgpt_relogin.build_chatgpt_registration_mode_adapter",
            return_value=adapter,
        ):
            tokens = _login_with_saved_credentials(saved, log_fn=logs.append)

        self.assertEqual(tokens["refresh_token"], "new-rt")
        self.assertEqual(adapter.calls, 2)
        self.assertEqual(build_service.call_count, 2)
        self.assertFalse(
            build_service.call_args_list[0].kwargs.get(
                "force_password_reset", False
            )
        )
        self.assertTrue(
            build_service.call_args_list[1].kwargs["force_password_reset"]
        )
        self.assertTrue(any("忘记密码" in message for message in logs))

    def test_transient_login_failure_does_not_force_password_reset(self):
        saved = {
            "email": "reset-timeout@example.com",
            "password": "Saved-Password",
            "extra": {},
            "mailbox_context": {
                "provider": "chatgpt_credentials",
                "email": "reset-timeout@example.com",
                "extra": {
                    "account_type": "chatgpt_password_reset_url_mail",
                    "pool_file": "reset-timeout.json",
                },
            },
        }
        adapter = mock.Mock()
        adapter.run.return_value = SimpleNamespace(
            success=False,
            error_message="[stage=authorize_continue] ReadTimeout",
        )

        with mock.patch(
            "services.chatgpt_relogin.config_store.get_all",
            return_value={"default_executor": "headless"},
        ), mock.patch(
            "services.chatgpt_relogin._build_email_service",
            return_value=mock.Mock(),
        ) as build_service, mock.patch(
            "services.chatgpt_relogin.build_chatgpt_registration_mode_adapter",
            return_value=adapter,
        ):
            with self.assertRaisesRegex(RuntimeError, "ReadTimeout"):
                _login_with_saved_credentials(saved)

        self.assertEqual(build_service.call_count, 1)

    @staticmethod
    def _fresh_tokens():
        return {
            "access_token": "new-at",
            "refresh_token": "new-rt",
            "id_token": "new-id",
            "session_token": "new-session",
            "workspace_id": "workspace-1",
            "account_id": "new-user",
        }

    def test_full_relogin_persists_new_tokens_before_forced_codex2api_sync(self):
        observed = {}

        def sync(account, *, force=False, replace_existing=False):
            with Session(self.engine) as session:
                saved = session.get(AccountModel, self.account_id)
                observed["saved_rt"] = saved.get_extra()["refresh_token"]
                observed["saved_at"] = saved.token
            observed["force"] = force
            observed["replace_existing"] = replace_existing
            observed["upload_rt"] = account.get_extra()["refresh_token"]
            return {"name": "Codex2API", "ok": True, "msg": "远端账号已更新"}

        with mock.patch("services.chatgpt_relogin.engine", self.engine), mock.patch(
            "services.chatgpt_relogin._login_with_saved_credentials",
            return_value=self._fresh_tokens(),
        ), mock.patch(
            "services.chatgpt_relogin.sync_codex2api_account",
            side_effect=sync,
        ):
            result = relogin_chatgpt_account(self.account_id)

        self.assertTrue(result["ok"])
        self.assertEqual(result["stage"], "completed")
        self.assertEqual(observed["saved_rt"], "new-rt")
        self.assertEqual(observed["saved_at"], "new-at")
        self.assertEqual(observed["upload_rt"], "new-rt")
        self.assertTrue(observed["force"])
        self.assertTrue(observed["replace_existing"])

        with Session(self.engine) as session:
            saved = session.get(AccountModel, self.account_id)
            extra = saved.get_extra()
            self.assertEqual(saved.token, "new-at")
            self.assertEqual(saved.user_id, "new-user")
            self.assertEqual(saved.status, "registered")
            self.assertEqual(extra["refresh_token"], "new-rt")
            self.assertEqual(extra["id_token"], "new-id")
            self.assertEqual(extra["keep_me"], "preserved")
            self.assertEqual(extra["sync_statuses"]["cpa"], {"uploaded": True})
            self.assertNotIn("chatgpt_local", extra)

    def test_full_relogin_passes_mfa_rotation_intent_to_login_engine(self):
        tokens = self._fresh_tokens()
        tokens["metadata"] = {
            "mfa_rotation": {
                "managed": True,
                "rotated_at": "2026-08-19T00:00:00+00:00",
            }
        }
        with mock.patch(
            "services.chatgpt_relogin._login_with_saved_credentials",
            return_value=tokens,
        ) as login, mock.patch(
            "services.chatgpt_relogin.sync_codex2api_account",
            return_value={"name": "Codex2API", "ok": True, "msg": "ok"},
        ), mock.patch("services.chatgpt_relogin.engine", self.engine):
            result = relogin_chatgpt_account(self.account_id, rotate_mfa=True)

        self.assertTrue(result["ok"])
        self.assertTrue(result["mfa_rotated"])
        self.assertTrue(login.call_args.kwargs["rotate_mfa"])

    def test_full_relogin_repairs_legacy_managed_mfa_without_totp_secret(self):
        with Session(self.engine) as session:
            account = session.get(AccountModel, self.account_id)
            extra = account.get_extra()
            extra["mailbox_login_context"] = {
                "provider": "chatgpt_credentials",
                "email": account.email,
                "extra": {
                    "account_type": "chatgpt_google_password",
                    "password": "supplier-password",
                    "chatgpt_mfa_managed": True,
                    "mfa_recovery_code": "RECOVERY-CODE",
                },
            }
            account.set_extra(extra)
            session.add(account)
            session.commit()

        tokens = self._fresh_tokens()
        tokens["metadata"] = {
            "mfa_rotation": {
                "managed": True,
                "rotated_at": "2026-08-21T03:00:00+00:00",
            }
        }
        with mock.patch(
            "services.chatgpt_relogin._login_with_saved_credentials",
            return_value=tokens,
        ) as login, mock.patch(
            "services.chatgpt_relogin.sync_codex2api_account",
            return_value={"name": "Codex2API", "ok": True, "msg": "ok"},
        ), mock.patch("services.chatgpt_relogin.engine", self.engine):
            result = relogin_chatgpt_account(self.account_id)

        self.assertTrue(result["ok"])
        self.assertTrue(result["mfa_rotated"])
        self.assertTrue(login.call_args.kwargs["rotate_mfa"])

    def test_full_relogin_auto_enrolls_mfa_for_passwordless_mailapi_account(self):
        with Session(self.engine) as session:
            account = session.get(AccountModel, self.account_id)
            account.password = ""
            extra = account.get_extra()
            extra["mailbox_login_context"] = {
                "provider": "microsoft",
                "email": account.email,
                "account_id": account.email,
                "extra": {
                    "account_type": "mailapi_url",
                    "mailapi_url": "https://mail.example.test/messages/token",
                },
            }
            account.set_extra(extra)
            session.add(account)
            session.commit()

        tokens = self._fresh_tokens()
        tokens["password"] = "Generated-ChatGPT-Password"
        tokens["metadata"] = {
            "mfa_rotation": {
                "managed": True,
                "rotated_at": "2026-08-21T03:45:00+00:00",
            }
        }
        with mock.patch(
            "services.chatgpt_relogin._login_with_saved_credentials",
            return_value=tokens,
        ) as login, mock.patch(
            "services.chatgpt_relogin.sync_codex2api_account",
            return_value={"name": "Codex2API", "ok": True, "msg": "ok"},
        ), mock.patch("services.chatgpt_relogin.engine", self.engine):
            result = relogin_chatgpt_account(self.account_id)

        self.assertTrue(result["ok"])
        self.assertTrue(result["mfa_rotated"])
        self.assertTrue(login.call_args.kwargs["rotate_mfa"])

    def test_full_relogin_rejects_unconfirmed_mfa_rotation(self):
        sync = mock.Mock()
        with mock.patch("services.chatgpt_relogin.engine", self.engine), mock.patch(
            "services.chatgpt_relogin._login_with_saved_credentials",
            return_value=self._fresh_tokens(),
        ), mock.patch(
            "services.chatgpt_relogin.sync_codex2api_account",
            sync,
        ):
            result = relogin_chatgpt_account(self.account_id, rotate_mfa=True)

        self.assertFalse(result["ok"])
        self.assertFalse(result["relogin_ok"])
        self.assertEqual(result["stage"], "relogin")
        self.assertIn("未确认新 MFA 已激活", result["message"])
        sync.assert_not_called()

    def test_mfa_rotation_remains_confirmed_when_codex2api_sync_fails(self):
        tokens = self._fresh_tokens()
        tokens["metadata"] = {
            "mfa_rotation": {
                "managed": True,
                "rotated_at": "2026-08-19T00:00:00+00:00",
            }
        }
        with mock.patch("services.chatgpt_relogin.engine", self.engine), mock.patch(
            "services.chatgpt_relogin._login_with_saved_credentials",
            return_value=tokens,
        ), mock.patch(
            "services.chatgpt_relogin.sync_codex2api_account",
            return_value={"name": "Codex2API", "ok": False, "msg": "模型不支持"},
        ):
            result = relogin_chatgpt_account(self.account_id, rotate_mfa=True)

        self.assertFalse(result["ok"])
        self.assertTrue(result["relogin_ok"])
        self.assertTrue(result["mfa_rotated"])
        self.assertEqual(result["stage"], "codex2api_sync")

    def test_full_relogin_persists_password_changed_by_reset_flow(self):
        tokens = self._fresh_tokens()
        tokens["password"] = "Replacement-ChatGPT-Password"

        with mock.patch("services.chatgpt_relogin.engine", self.engine), mock.patch(
            "services.chatgpt_relogin._login_with_saved_credentials",
            return_value=tokens,
        ), mock.patch(
            "services.chatgpt_relogin.sync_codex2api_account",
            return_value={"name": "Codex2API", "ok": True, "msg": "ok"},
        ):
            result = relogin_chatgpt_account(self.account_id)

        self.assertTrue(result["ok"])
        with Session(self.engine) as session:
            saved = session.get(AccountModel, self.account_id)
            self.assertEqual(saved.password, "Replacement-ChatGPT-Password")

    def test_login_failure_keeps_old_tokens_and_does_not_sync(self):
        sync = mock.Mock()
        with mock.patch("services.chatgpt_relogin.engine", self.engine), mock.patch(
            "services.chatgpt_relogin._login_with_saved_credentials",
            side_effect=RuntimeError("邮箱验证码校验失败"),
        ), mock.patch(
            "services.chatgpt_relogin.sync_codex2api_account",
            sync,
        ):
            result = relogin_chatgpt_account(self.account_id)

        self.assertFalse(result["ok"])
        self.assertFalse(result["relogin_ok"])
        self.assertEqual(result["stage"], "relogin")
        self.assertIn("邮箱验证码校验失败", result["message"])
        sync.assert_not_called()
        with Session(self.engine) as session:
            saved = session.get(AccountModel, self.account_id)
            self.assertEqual(saved.token, "old-at")
            self.assertEqual(saved.get_extra()["refresh_token"], "old-rt")

    def test_password_verify_deactivation_removes_account_through_login_stack(self):
        response = mock.Mock()
        response.status_code = 403
        response.text = (
            '{"error":{"message":'
            '"You do not have an account because it has been deleted or deactivated. '
            'If you believe this was an error, please contact us through our help center."}}'
        )
        response.json.return_value = {
            "error": {
                "message": (
                    "You do not have an account because it has been deleted or "
                    "deactivated. If you believe this was an error, please contact "
                    "us through our help center."
                ),
            }
        }

        oauth_client = OAuthClient({}, verbose=False)
        oauth_client.session.post = mock.Mock(return_value=response)
        oauth_client._recreate_session = mock.Mock()
        oauth_client._bootstrap_oauth_session = mock.Mock(
            return_value="https://auth.openai.com/log-in/password"
        )
        oauth_client._submit_authorize_continue = mock.Mock(
            return_value=FlowState(
                page_type="login_password",
                current_url="https://auth.openai.com/log-in/password",
                continue_url="https://auth.openai.com/log-in/password",
            )
        )

        email_service = mock.Mock()
        email_service.service_type = SimpleNamespace(value="fixture")
        email_service.create_email.return_value = {"email": "demo@example.com"}
        sync = mock.Mock()

        with mock.patch(
            "services.chatgpt_relogin.engine",
            self.engine,
        ), mock.patch(
            "services.chatgpt_relogin.config_store.get_all",
            return_value={"default_executor": "headless"},
        ), mock.patch(
            "services.chatgpt_relogin._build_email_service",
            return_value=email_service,
        ), mock.patch(
            "platforms.chatgpt.refresh_token_registration_engine."
            "RefreshTokenRegistrationEngine._build_oauth_client",
            return_value=oauth_client,
        ), mock.patch(
            "platforms.chatgpt.oauth_client.get_sentinel_token_via_browser",
            return_value="browser-token",
        ), mock.patch(
            "services.chatgpt_relogin.sync_codex2api_account",
            sync,
        ):
            result = relogin_chatgpt_account(self.account_id)

        self.assertFalse(result["ok"])
        self.assertTrue(result["account_removed"])
        self.assertEqual(result["stage"], "account_removed")
        sync.assert_not_called()
        with Session(self.engine) as session:
            self.assertIsNone(session.get(AccountModel, self.account_id))

    def test_password_verify_nested_error_type_is_explicit_deactivation(self):
        response = mock.Mock()
        response.status_code = 403
        response.text = '{"error":{"type":"account_deactivated"}}'
        response.json.return_value = {
            "error": {
                "type": "account_deactivated",
                "message": "Account unavailable",
            }
        }
        oauth_client = OAuthClient({}, verbose=False)
        oauth_client.session.post = mock.Mock(return_value=response)
        oauth_client._browser_pause = mock.Mock()

        with mock.patch(
            "platforms.chatgpt.oauth_client.get_sentinel_token_via_browser",
            return_value="browser-token",
        ):
            with self.assertRaises(ChatGPTAccountDeactivatedError):
                oauth_client._submit_password_verify(
                    "saved-password",
                    "device-id",
                )

    def test_password_verify_diagnostic_does_not_remove_account_through_login_stack(self):
        response = mock.Mock()
        response.status_code = 403
        response.text = (
            '{"error":{"message":"diagnostic: upstream did not say account '
            'has been deleted or deactivated"}}'
        )
        response.json.return_value = {
            "error": {
                "message": (
                    "diagnostic: upstream did not say account has been deleted "
                    "or deactivated"
                ),
            }
        }

        oauth_client = OAuthClient({}, verbose=False)
        oauth_client.session.post = mock.Mock(return_value=response)
        oauth_client._recreate_session = mock.Mock()
        oauth_client._bootstrap_oauth_session = mock.Mock(
            return_value="https://auth.openai.com/log-in/password"
        )
        oauth_client._submit_authorize_continue = mock.Mock(
            return_value=FlowState(
                page_type="login_password",
                current_url="https://auth.openai.com/log-in/password",
                continue_url="https://auth.openai.com/log-in/password",
            )
        )

        email_service = mock.Mock()
        email_service.service_type = SimpleNamespace(value="fixture")
        email_service.create_email.return_value = {"email": "demo@example.com"}
        sync = mock.Mock()

        with mock.patch(
            "services.chatgpt_relogin.engine",
            self.engine,
        ), mock.patch(
            "services.chatgpt_relogin.config_store.get_all",
            return_value={"default_executor": "headless"},
        ), mock.patch(
            "services.chatgpt_relogin._build_email_service",
            return_value=email_service,
        ), mock.patch(
            "platforms.chatgpt.refresh_token_registration_engine."
            "RefreshTokenRegistrationEngine._build_oauth_client",
            return_value=oauth_client,
        ), mock.patch(
            "platforms.chatgpt.oauth_client.get_sentinel_token_via_browser",
            return_value="browser-token",
        ), mock.patch(
            "services.chatgpt_relogin.sync_codex2api_account",
            sync,
        ):
            result = relogin_chatgpt_account(self.account_id)

        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "relogin")
        self.assertNotIn("account_removed", result)
        sync.assert_not_called()
        with Session(self.engine) as session:
            self.assertIsNotNone(session.get(AccountModel, self.account_id))

    def test_deactivated_signal_from_login_log_interrupts_and_deletes_account(self):
        logs = []
        sync = mock.Mock()

        def login(saved, **kwargs):
            kwargs["log_fn"](
                '[22:01:53] [登录链路] OTP 无效: {"错误":{"消息":'
                '"你没有账号，因为它已被删除或停用。如果您认为这是错误，'
                '请通过电话联系我们"}}'
            )
            raise ChatGPTAccountDeactivatedError()

        with mock.patch("services.chatgpt_relogin.engine", self.engine), mock.patch(
            "services.chatgpt_relogin._login_with_saved_credentials",
            side_effect=login,
        ), mock.patch(
            "services.chatgpt_relogin.sync_codex2api_account",
            sync,
        ):
            result = relogin_chatgpt_account(self.account_id, log_fn=logs.append)

        self.assertFalse(result["ok"])
        self.assertFalse(result["relogin_ok"])
        self.assertTrue(result["account_removed"])
        self.assertEqual(result["stage"], "account_removed")
        self.assertIn("已被删除或停用", result["message"])
        self.assertTrue(any("OTP 无效" in line for line in logs))
        sync.assert_not_called()
        with Session(self.engine) as session:
            self.assertIsNone(session.get(AccountModel, self.account_id))

    def test_automatic_otp_timeout_removes_local_account(self):
        sync = mock.Mock()
        timeout_error = ChatGPTMailboxOTPTimeoutError(
            "[stage=otp] OAuth 阶段 OTP 验证失败，"
            "已尝试 0 个验证码，等待窗口 180s",
            wait_seconds=180,
            elapsed_seconds=180.0,
        )

        with mock.patch("services.chatgpt_relogin.engine", self.engine), mock.patch(
            "services.chatgpt_relogin._login_with_saved_credentials",
            side_effect=timeout_error,
        ), mock.patch(
            "services.chatgpt_relogin.sync_codex2api_account",
            sync,
        ):
            try:
                result = relogin_chatgpt_account(
                    self.account_id,
                    remove_on_mailbox_otp_timeout=True,
                    codex2api_delete_on_account_remove_enabled=False,
                )
            except TypeError as exc:
                self.fail(f"missing timeout cleanup API: {exc}")

        self.assertFalse(result["ok"])
        self.assertTrue(result["account_removed"])
        self.assertEqual(result["stage"], "account_removed")
        self.assertEqual(result["removal_reason"], "mailbox_otp_timeout")
        self.assertIn("180", result["message"])
        self.assertIn("自动移除", result["message"])
        sync.assert_not_called()
        with Session(self.engine) as session:
            self.assertIsNone(session.get(AccountModel, self.account_id))

    def test_manual_otp_timeout_keeps_local_account(self):
        timeout_error = ChatGPTMailboxOTPTimeoutError(
            "[stage=otp] OAuth 阶段 OTP 验证失败，"
            "已尝试 0 个验证码，等待窗口 180s",
            wait_seconds=180,
            elapsed_seconds=180.0,
        )

        with mock.patch("services.chatgpt_relogin.engine", self.engine), mock.patch(
            "services.chatgpt_relogin._login_with_saved_credentials",
            side_effect=timeout_error,
        ):
            result = relogin_chatgpt_account(self.account_id)

        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "relogin")
        self.assertNotIn("account_removed", result)
        with Session(self.engine) as session:
            self.assertIsNotNone(session.get(AccountModel, self.account_id))

    def test_ordinary_invalid_otp_log_does_not_delete_account(self):
        sync = mock.Mock()

        def login(saved, **kwargs):
            kwargs["log_fn"]("[登录链路] OTP 无效: 验证码错误")
            raise RuntimeError("邮箱验证码校验失败")

        with mock.patch("services.chatgpt_relogin.engine", self.engine), mock.patch(
            "services.chatgpt_relogin._login_with_saved_credentials",
            side_effect=login,
        ), mock.patch(
            "services.chatgpt_relogin.sync_codex2api_account",
            sync,
        ):
            result = relogin_chatgpt_account(self.account_id)

        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "relogin")
        self.assertNotIn("account_removed", result)
        sync.assert_not_called()
        with Session(self.engine) as session:
            self.assertIsNotNone(session.get(AccountModel, self.account_id))

    def test_diagnostic_log_and_exception_text_do_not_delete_account(self):
        sync = mock.Mock()

        def login(saved, **kwargs):
            kwargs["log_fn"](
                "diagnostic: expected account_deleted but received timeout"
            )
            raise RuntimeError(
                "diagnostic: upstream did not say account has been deleted or deactivated"
            )

        with mock.patch("services.chatgpt_relogin.engine", self.engine), mock.patch(
            "services.chatgpt_relogin._login_with_saved_credentials",
            side_effect=login,
        ), mock.patch(
            "services.chatgpt_relogin.sync_codex2api_account",
            sync,
        ):
            result = relogin_chatgpt_account(self.account_id)

        self.assertEqual(result["stage"], "relogin")
        sync.assert_not_called()
        with Session(self.engine) as session:
            self.assertIsNotNone(session.get(AccountModel, self.account_id))

    def test_typed_deactivated_error_deletes_account_without_log_callback(self):
        sync = mock.Mock()
        with mock.patch("services.chatgpt_relogin.engine", self.engine), mock.patch(
            "services.chatgpt_relogin._login_with_saved_credentials",
            side_effect=ChatGPTAccountDeactivatedError(),
        ), mock.patch(
            "services.chatgpt_relogin.sync_codex2api_account",
            sync,
        ):
            result = relogin_chatgpt_account(self.account_id)

        self.assertTrue(result["account_removed"])
        self.assertEqual(result["stage"], "account_removed")
        sync.assert_not_called()
        with Session(self.engine) as session:
            self.assertIsNone(session.get(AccountModel, self.account_id))

    def test_deactivated_account_delegates_to_remote_first_removal_with_frozen_setting(self):
        removal = mock.Mock(
            return_value={
                "ok": True,
                "status": "deleted",
                "local_deleted": True,
                "error_code": "",
                "message": "账号已删除",
            }
        )
        control = RegisterTaskControl()
        attempt_id = control.start_attempt()
        with Session(self.engine) as session:
            account = session.get(AccountModel, self.account_id)
            expected_created_at = account.created_at
            expected_updated_at = account.updated_at

        with mock.patch("services.chatgpt_relogin.engine", self.engine), mock.patch(
            "services.chatgpt_relogin._login_with_saved_credentials",
            side_effect=ChatGPTAccountDeactivatedError(),
        ), mock.patch(
            "services.chatgpt_relogin.remove_account",
            removal,
        ):
            result = relogin_chatgpt_account(
                self.account_id,
                task_control=control,
                attempt_id=attempt_id,
                codex2api_delete_on_account_remove_enabled=True,
            )

        self.assertTrue(result["account_removed"])
        self.assertEqual(result["stage"], "account_removed")
        removal.assert_called_once_with(
            self.account_id,
            database_engine=self.engine,
            already_locked=True,
            expected_created_at=expected_created_at,
            expected_updated_at=expected_updated_at,
            task_control=control,
            attempt_id=attempt_id,
            codex2api_delete_on_account_remove_enabled=True,
        )

    def test_remote_removal_failure_keeps_account_and_reports_stable_failure(self):
        removal = mock.Mock(
            return_value={
                "ok": False,
                "status": "remote_failed",
                "local_deleted": False,
                "error_code": "codex2api_delete_failed",
                "message": "Codex2API 认证删除未完成",
            }
        )
        with mock.patch("services.chatgpt_relogin.engine", self.engine), mock.patch(
            "services.chatgpt_relogin._login_with_saved_credentials",
            side_effect=ChatGPTAccountDeactivatedError(),
        ), mock.patch(
            "services.chatgpt_relogin.remove_account",
            removal,
        ):
            result = relogin_chatgpt_account(
                self.account_id,
                codex2api_delete_on_account_remove_enabled=True,
            )

        self.assertFalse(result["account_removed"])
        self.assertEqual(result["stage"], "account_remove_failed")
        self.assertEqual(result["error_code"], "codex2api_delete_failed")
        self.assertIn("Codex2API 认证删除未完成", result["message"])
        with Session(self.engine) as session:
            self.assertIsNotNone(session.get(AccountModel, self.account_id))

    def test_database_delete_failure_is_reported_without_claiming_account_removed(self):
        sync = mock.Mock()

        def login(saved, **kwargs):
            kwargs["log_fn"]("账号已被删除或停用")
            raise ChatGPTAccountDeactivatedError()

        with mock.patch("services.chatgpt_relogin.engine", self.engine), mock.patch(
            "services.chatgpt_relogin._login_with_saved_credentials",
            side_effect=login,
        ), mock.patch.object(
            Session,
            "commit",
            side_effect=RuntimeError("数据库写入失败"),
        ), mock.patch(
            "services.chatgpt_relogin.sync_codex2api_account",
            sync,
        ):
            result = relogin_chatgpt_account(self.account_id)

        self.assertFalse(result["account_removed"])
        self.assertEqual(result["stage"], "account_remove_failed")
        self.assertIn("本地记录删除失败", result["message"])
        sync.assert_not_called()
        with Session(self.engine) as session:
            self.assertIsNotNone(session.get(AccountModel, self.account_id))

    def test_reused_account_id_does_not_delete_a_replacement_account(self):
        sync = mock.Mock()

        def login(saved, **kwargs):
            with Session(self.engine) as session:
                original = session.get(AccountModel, self.account_id)
                session.delete(original)
                session.commit()
                replacement = AccountModel(
                    platform="chatgpt",
                    email="replacement@example.com",
                    password="replacement-password",
                    status="registered",
                )
                session.add(replacement)
                session.commit()
                session.refresh(replacement)
                self.assertEqual(replacement.id, self.account_id)
            kwargs["log_fn"]("账号已被删除或停用")
            raise ChatGPTAccountDeactivatedError()

        with mock.patch("services.chatgpt_relogin.engine", self.engine), mock.patch(
            "services.chatgpt_relogin._login_with_saved_credentials",
            side_effect=login,
        ), mock.patch(
            "services.chatgpt_relogin.sync_codex2api_account",
            sync,
        ):
            result = relogin_chatgpt_account(self.account_id)

        self.assertFalse(result["account_removed"])
        self.assertEqual(result["stage"], "account_remove_failed")
        self.assertIn("记录已发生变化", result["message"])
        sync.assert_not_called()
        with Session(self.engine) as session:
            replacement = session.get(AccountModel, self.account_id)
            self.assertIsNotNone(replacement)
            self.assertEqual(replacement.email, "replacement@example.com")

    def test_concurrent_same_row_refresh_prevents_stale_deactivation_delete(self):
        sync = mock.Mock()
        concurrent_updated_at = datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone.utc)

        def login(saved, **kwargs):
            with Session(self.engine) as session:
                current = session.get(AccountModel, self.account_id)
                current.token = "concurrent-access-token"
                current.updated_at = concurrent_updated_at
                extra = dict(current.get_extra() or {})
                extra["refresh_token"] = "concurrent-refresh-token"
                current.set_extra(extra)
                session.add(current)
                session.commit()
            kwargs["log_fn"]("账号已被删除或停用")
            raise ChatGPTAccountDeactivatedError()

        with mock.patch("services.chatgpt_relogin.engine", self.engine), mock.patch(
            "services.chatgpt_relogin._login_with_saved_credentials",
            side_effect=login,
        ), mock.patch(
            "services.chatgpt_relogin.sync_codex2api_account",
            sync,
        ):
            result = relogin_chatgpt_account(self.account_id)

        self.assertFalse(result["account_removed"])
        self.assertEqual(result["stage"], "account_remove_failed")
        self.assertIn("记录已发生变化", result["message"])
        sync.assert_not_called()
        with Session(self.engine) as session:
            current = session.get(AccountModel, self.account_id)
            self.assertIsNotNone(current)
            self.assertEqual(current.token, "concurrent-access-token")
            self.assertEqual(
                current.get_extra()["refresh_token"],
                "concurrent-refresh-token",
            )

    def test_reused_account_id_does_not_receive_fresh_tokens_or_sync(self):
        sync = mock.Mock(
            return_value={"name": "Codex2API", "ok": True, "msg": "ok"}
        )

        def login(saved, **kwargs):
            del saved, kwargs
            with Session(self.engine) as session:
                original = session.get(AccountModel, self.account_id)
                session.delete(original)
                session.commit()
                replacement = AccountModel(
                    platform="chatgpt",
                    email="replacement@example.com",
                    password="replacement-password",
                    token="replacement-at",
                    user_id="replacement-user",
                    status="registered",
                )
                replacement.set_extra({"refresh_token": "replacement-rt"})
                session.add(replacement)
                session.commit()
                session.refresh(replacement)
                self.assertEqual(replacement.id, self.account_id)
            return self._fresh_tokens()

        with mock.patch("services.chatgpt_relogin.engine", self.engine), mock.patch(
            "services.chatgpt_relogin._login_with_saved_credentials",
            side_effect=login,
        ), mock.patch(
            "services.chatgpt_relogin.sync_codex2api_account",
            sync,
        ):
            result = relogin_chatgpt_account(self.account_id)

        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "relogin")
        self.assertIn("记录已发生变化", result["message"])
        sync.assert_not_called()
        with Session(self.engine) as session:
            replacement = session.get(AccountModel, self.account_id)
            self.assertIsNotNone(replacement)
            self.assertEqual(replacement.email, "replacement@example.com")
            self.assertEqual(replacement.token, "replacement-at")
            self.assertEqual(replacement.user_id, "replacement-user")
            self.assertEqual(replacement.get_extra()["refresh_token"], "replacement-rt")

    def test_log_observer_failure_does_not_hide_completed_account_removal(self):
        with mock.patch("services.chatgpt_relogin.engine", self.engine), mock.patch(
            "services.chatgpt_relogin._login_with_saved_credentials",
            side_effect=ChatGPTAccountDeactivatedError(),
        ), mock.patch(
            "services.chatgpt_relogin.sync_codex2api_account",
        ) as sync:
            result = relogin_chatgpt_account(
                self.account_id,
                log_fn=mock.Mock(side_effect=RuntimeError("日志观察器失败")),
            )

        self.assertTrue(result["account_removed"])
        self.assertEqual(result["stage"], "account_removed")
        sync.assert_not_called()
        with Session(self.engine) as session:
            self.assertIsNone(session.get(AccountModel, self.account_id))

    def test_parallel_relogins_serialize_codex2api_replacement(self):
        login_barrier = threading.Barrier(2)
        sync_state_lock = threading.Lock()
        sync_active = 0
        sync_max_active = 0
        results = []

        def load(account_id):
            return {
                "id": int(account_id),
                "email": f"account-{account_id}@example.com",
                "created_at": None,
                "password": "password",
                "extra": {},
                "mailbox_context": {"provider": "microsoft"},
            }

        def login(saved, **kwargs):
            login_barrier.wait(timeout=1)
            return self._fresh_tokens()

        def persist(account_id, tokens, **kwargs):
            del tokens, kwargs
            return SimpleNamespace(
                id=account_id,
                email=f"account-{account_id}@example.com",
            )

        def sync(account, **kwargs):
            del account, kwargs
            nonlocal sync_active, sync_max_active
            with sync_state_lock:
                sync_active += 1
                sync_max_active = max(sync_max_active, sync_active)
            time.sleep(0.05)
            with sync_state_lock:
                sync_active -= 1
            return {"name": "Codex2API", "ok": True, "msg": "ok"}

        with mock.patch(
            "services.chatgpt_relogin._load_saved_account",
            side_effect=load,
        ), mock.patch(
            "services.chatgpt_relogin._login_with_saved_credentials",
            side_effect=login,
        ), mock.patch(
            "services.chatgpt_relogin._persist_fresh_tokens",
            side_effect=persist,
        ), mock.patch(
            "services.chatgpt_relogin.sync_codex2api_account",
            side_effect=sync,
        ) as sync_mock:
            workers = [
                threading.Thread(
                    target=lambda account_id=account_id: results.append(
                        relogin_chatgpt_account(account_id)
                    )
                )
                for account_id in (101, 102)
            ]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=2)

        self.assertEqual(len(results), 2)
        self.assertTrue(all(result["ok"] for result in results))
        self.assertEqual(sync_mock.call_count, 2)
        self.assertEqual(sync_max_active, 1)

    def test_sync_failure_reports_partial_failure_but_keeps_fresh_tokens(self):
        with mock.patch("services.chatgpt_relogin.engine", self.engine), mock.patch(
            "services.chatgpt_relogin._login_with_saved_credentials",
            return_value=self._fresh_tokens(),
        ), mock.patch(
            "services.chatgpt_relogin.sync_codex2api_account",
            return_value={"name": "Codex2API", "ok": False, "msg": "远端更新超时"},
        ):
            result = relogin_chatgpt_account(self.account_id)

        self.assertFalse(result["ok"])
        self.assertTrue(result["relogin_ok"])
        self.assertEqual(result["stage"], "codex2api_sync")
        self.assertIn("远端更新超时", result["message"])
        with Session(self.engine) as session:
            saved = session.get(AccountModel, self.account_id)
            self.assertEqual(saved.get_extra()["refresh_token"], "new-rt")

    def test_sync_exception_still_reports_relogin_as_successful(self):
        with mock.patch("services.chatgpt_relogin.engine", self.engine), mock.patch(
            "services.chatgpt_relogin._login_with_saved_credentials",
            return_value=self._fresh_tokens(),
        ), mock.patch(
            "services.chatgpt_relogin.sync_codex2api_account",
            side_effect=RuntimeError("同步状态落库失败"),
        ):
            result = relogin_chatgpt_account(self.account_id)

        self.assertFalse(result["ok"])
        self.assertTrue(result["relogin_ok"])
        self.assertEqual(result["stage"], "codex2api_sync")
        self.assertIn("同步状态落库失败", result["message"])
        with Session(self.engine) as session:
            saved = session.get(AccountModel, self.account_id)
            self.assertEqual(saved.get_extra()["refresh_token"], "new-rt")

    def test_fresh_login_clears_stale_optional_identity_tokens(self):
        with Session(self.engine) as session:
            saved = session.get(AccountModel, self.account_id)
            extra = saved.get_extra()
            extra.update(
                {
                    "session_token": "old-session",
                    "workspace_id": "old-workspace",
                    "account_id": "old-account",
                    "idToken": "old-id-camel",
                    "workspaceId": "old-workspace-camel",
                    "accountId": "old-account-camel",
                }
            )
            saved.set_extra(extra)
            session.add(saved)
            session.commit()

        fresh_tokens = {
            "access_token": "new-at",
            "refresh_token": "new-rt",
            "id_token": "",
            "session_token": "",
            "workspace_id": "",
            "account_id": "",
        }
        with mock.patch("services.chatgpt_relogin.engine", self.engine), mock.patch(
            "services.chatgpt_relogin._login_with_saved_credentials",
            return_value=fresh_tokens,
        ), mock.patch(
            "services.chatgpt_relogin.sync_codex2api_account",
            return_value={"name": "Codex2API", "ok": True, "msg": "ok"},
        ):
            result = relogin_chatgpt_account(self.account_id)

        self.assertTrue(result["ok"])
        with Session(self.engine) as session:
            saved = session.get(AccountModel, self.account_id)
            extra = saved.get_extra()
            self.assertEqual(saved.user_id, "")
            for key in (
                "id_token",
                "idToken",
                "session_token",
                "workspace_id",
                "workspaceId",
                "account_id",
                "accountId",
            ):
                self.assertNotIn(key, extra)

    def test_same_account_cannot_run_two_relogins_concurrently(self):
        first_login_started = threading.Event()
        release_first_login = threading.Event()
        first_result = []

        def login(saved, **kwargs):
            first_login_started.set()
            release_first_login.wait(timeout=2)
            return self._fresh_tokens()

        with mock.patch("services.chatgpt_relogin.engine", self.engine), mock.patch(
            "services.chatgpt_relogin._login_with_saved_credentials",
            side_effect=login,
        ) as login_mock, mock.patch(
            "services.chatgpt_relogin.sync_codex2api_account",
            return_value={"name": "Codex2API", "ok": True, "msg": "ok"},
        ) as sync:
            worker = threading.Thread(
                target=lambda: first_result.append(
                    relogin_chatgpt_account(self.account_id)
                )
            )
            worker.start()
            self.assertTrue(first_login_started.wait(timeout=1))
            overlapping = relogin_chatgpt_account(self.account_id)
            release_first_login.set()
            worker.join(timeout=2)

        self.assertFalse(overlapping["ok"])
        self.assertIn("正在重登", overlapping["message"])
        self.assertTrue(first_result[0]["ok"])
        self.assertEqual(login_mock.call_count, 1)
        sync.assert_called_once()

    def test_task_interruption_is_propagated_instead_of_reported_as_login_failure(self):
        with mock.patch("services.chatgpt_relogin.engine", self.engine), mock.patch(
            "services.chatgpt_relogin._login_with_saved_credentials",
            side_effect=StopTaskRequested(),
        ), mock.patch(
            "services.chatgpt_relogin.sync_codex2api_account",
        ) as sync:
            with self.assertRaises(StopTaskRequested):
                relogin_chatgpt_account(self.account_id)

        sync.assert_not_called()

    def test_stop_after_login_does_not_persist_fresh_tokens(self):
        control = RegisterTaskControl()
        sync = mock.Mock()

        def login(saved, **kwargs):
            del saved, kwargs
            control.request_stop()
            return self._fresh_tokens()

        with mock.patch("services.chatgpt_relogin.engine", self.engine), mock.patch(
            "services.chatgpt_relogin._login_with_saved_credentials",
            side_effect=login,
        ), mock.patch(
            "services.chatgpt_relogin.sync_codex2api_account",
            sync,
        ):
            with self.assertRaises(StopTaskRequested):
                relogin_chatgpt_account(
                    self.account_id,
                    task_control=control,
                    attempt_id=control.start_attempt(),
                )

        sync.assert_not_called()
        with Session(self.engine) as session:
            saved = session.get(AccountModel, self.account_id)
            self.assertEqual(saved.token, "old-at")
            self.assertEqual(saved.get_extra()["refresh_token"], "old-rt")

    def test_stop_after_token_persist_still_completes_codex2api_sync(self):
        control = RegisterTaskControl()
        sync = mock.Mock(
            return_value={"name": "Codex2API", "ok": True, "msg": "ok"}
        )

        def persist(account_id, tokens, **kwargs):
            del tokens, kwargs
            control.request_stop()
            return SimpleNamespace(
                id=account_id,
                email="demo@example.com",
            )

        with mock.patch("services.chatgpt_relogin.engine", self.engine), mock.patch(
            "services.chatgpt_relogin._login_with_saved_credentials",
            return_value=self._fresh_tokens(),
        ), mock.patch(
            "services.chatgpt_relogin._persist_fresh_tokens",
            side_effect=persist,
        ), mock.patch(
            "services.chatgpt_relogin.sync_codex2api_account",
            sync,
        ):
            result = relogin_chatgpt_account(
                self.account_id,
                task_control=control,
                attempt_id=control.start_attempt(),
            )

        self.assertTrue(result["ok"])
        sync.assert_called_once()

    def test_missing_mailbox_context_fails_with_account_specific_message(self):
        with Session(self.engine) as session:
            saved = session.get(AccountModel, self.account_id)
            saved.set_extra({"access_token": "old-at", "refresh_token": "old-rt"})
            session.add(saved)
            session.commit()

        with mock.patch("services.chatgpt_relogin.engine", self.engine), mock.patch(
            "services.chatgpt_relogin.sync_codex2api_account",
        ) as sync:
            result = relogin_chatgpt_account(self.account_id)

        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "relogin")
        self.assertIn("demo@example.com", result["message"])
        self.assertIn("邮箱登录凭据", result["message"])
        sync.assert_not_called()

    def test_password_totp_context_recovers_secret_from_original_pool_record(self):
        with Session(self.engine) as session:
            saved = session.get(AccountModel, self.account_id)
            saved.set_extra(
                {
                    "access_token": "old-at",
                    "refresh_token": "old-rt",
                    "mailbox_login_context": {
                        "provider": "chatgpt_credentials",
                        "email": "demo@example.com",
                        "extra": {
                            "account_type": "chatgpt_password_totp",
                            "pool_file": "mfa.json",
                        },
                    },
                }
            )
            session.add(saved)
            session.commit()

        captured = {}

        class Adapter:
            def run(self, context):
                captured["email_info"] = context.email_service.create_email()
                captured["extra"] = context.extra_config
                return SimpleNamespace(
                    success=True,
                    error_message="",
                    access_token="new-at",
                    refresh_token="new-rt",
                    id_token="new-id",
                    session_token="new-session",
                    workspace_id="workspace-1",
                    account_id="new-user",
                )

        with mock.patch("services.chatgpt_relogin.engine", self.engine), mock.patch(
            "services.chatgpt_relogin.load_applemail_pool_records",
            return_value=(
                SimpleNamespace(name="mfa.json"),
                [
                    {
                        "email": "demo@example.com",
                        "password": "chatgpt-password",
                        "totp_secret": "JBSWY3DPEHPK3PXP",
                        "account_type": "chatgpt_password_totp",
                    }
                ],
            ),
        ), mock.patch(
            "services.chatgpt_relogin.build_chatgpt_registration_mode_adapter",
            return_value=Adapter(),
        ), mock.patch(
            "services.chatgpt_relogin.config_store.get_all",
            return_value={"applemail_pool_dir": "mail", "default_executor": "headless"},
        ), mock.patch(
            "services.chatgpt_relogin.sync_codex2api_account",
            return_value={"name": "Codex2API", "ok": True, "msg": "ok"},
        ):
            result = relogin_chatgpt_account(self.account_id)

        self.assertTrue(result["ok"])
        self.assertEqual(captured["email_info"]["password"], "chatgpt-password")
        self.assertEqual(
            captured["email_info"]["totp_secret"],
            "JBSWY3DPEHPK3PXP",
        )
        self.assertTrue(captured["extra"]["chatgpt_existing_account_login_only"])
        self.assertEqual(
            captured["extra"]["chatgpt_existing_account_login_stage"],
            "refresh_token",
        )

    def test_totp_recovery_rejects_same_email_with_wrong_pool_record_type(self):
        with Session(self.engine) as session:
            saved = session.get(AccountModel, self.account_id)
            saved.set_extra(
                {
                    "access_token": "old-at",
                    "refresh_token": "old-rt",
                    "mailbox_login_context": {
                        "provider": "chatgpt_credentials",
                        "email": "demo@example.com",
                        "extra": {
                            "account_type": "chatgpt_password_totp",
                            "pool_file": "mfa.json",
                        },
                    },
                }
            )
            session.add(saved)
            session.commit()

        with mock.patch("services.chatgpt_relogin.engine", self.engine), mock.patch(
            "services.chatgpt_relogin.load_applemail_pool_records",
            return_value=(
                SimpleNamespace(name="mfa.json"),
                [
                    {
                        "email": "demo@example.com",
                        "password": "apple-password",
                        "mfa_secret": "JBSWY3DPEHPK3PXP",
                        "account_type": "icloud_web",
                    }
                ],
            ),
        ), mock.patch(
            "services.chatgpt_relogin.config_store.get_all",
            return_value={"applemail_pool_dir": "mail"},
        ), mock.patch(
            "services.chatgpt_relogin.build_chatgpt_registration_mode_adapter",
        ) as adapter, mock.patch(
            "services.chatgpt_relogin.sync_codex2api_account",
        ) as sync:
            result = relogin_chatgpt_account(self.account_id)

        self.assertFalse(result["ok"])
        self.assertIn("凭据类型", result["message"])
        adapter.assert_not_called()
        sync.assert_not_called()

    def test_relogin_persists_mailbox_context_updated_during_otp_login(self):
        updated_context = {
            "provider": "microsoft",
            "email": "demo@example.com",
            "account_id": "mailbox-1",
            "extra": {
                "client_id": "mail-client",
                "refresh_token": "mail-refresh-rotated",
            },
        }

        class EmailService:
            service_type = SimpleNamespace(value="microsoft")

            def get_mailbox_metadata(self):
                return updated_context

        class Adapter:
            def run(self, context):
                return SimpleNamespace(
                    success=True,
                    error_message="",
                    access_token="new-at",
                    refresh_token="new-rt",
                    id_token="new-id",
                    session_token="new-session",
                    workspace_id="workspace-1",
                    account_id="new-user",
                    metadata={},
                )

        with mock.patch("services.chatgpt_relogin.engine", self.engine), mock.patch(
            "services.chatgpt_relogin._build_email_service",
            return_value=EmailService(),
        ), mock.patch(
            "services.chatgpt_relogin.build_chatgpt_registration_mode_adapter",
            return_value=Adapter(),
        ), mock.patch(
            "services.chatgpt_relogin.config_store.get_all",
            return_value={"default_executor": "headless"},
        ), mock.patch(
            "services.chatgpt_relogin.sync_codex2api_account",
            return_value={"name": "Codex2API", "ok": True, "msg": "ok"},
        ):
            result = relogin_chatgpt_account(self.account_id)

        self.assertTrue(result["ok"])
        with Session(self.engine) as session:
            saved = session.get(AccountModel, self.account_id)
            self.assertEqual(
                saved.get_extra()["mailbox_login_context"],
                updated_context,
            )

    def test_relogin_reuses_global_mailbox_endpoint_configuration(self):
        with Session(self.engine) as session:
            saved = session.get(AccountModel, self.account_id)
            extra = saved.get_extra()
            extra["mailbox_login_context"] = {
                "provider": "applemail",
                "email": "demo@example.com",
                "account_id": "demo@example.com",
                "extra": {
                    "client_id": "mail-client",
                    "refresh_token": "mail-refresh",
                },
            }
            saved.set_extra(extra)
            session.add(saved)
            session.commit()

        class Mailbox:
            def get_current_ids(self, account):
                return set()

            def wait_for_code(self, account, **kwargs):
                return "123456"

        class Adapter:
            def run(self, context):
                context.email_service.create_email()
                return SimpleNamespace(
                    success=True,
                    error_message="",
                    access_token="new-at",
                    refresh_token="new-rt",
                    id_token="new-id",
                    session_token="new-session",
                    workspace_id="workspace-1",
                    account_id="new-user",
                    metadata={},
                )

        with mock.patch("services.chatgpt_relogin.engine", self.engine), mock.patch(
            "services.chatgpt_relogin.create_mailbox",
            return_value=Mailbox(),
        ) as create_mailbox_mock, mock.patch(
            "services.chatgpt_relogin.build_chatgpt_registration_mode_adapter",
            return_value=Adapter(),
        ), mock.patch(
            "services.chatgpt_relogin.config_store.get_all",
            return_value={
                "default_executor": "headless",
                "applemail_base_url": "https://mail.example.test",
            },
        ), mock.patch(
            "services.chatgpt_relogin.sync_codex2api_account",
            return_value={"name": "Codex2API", "ok": True, "msg": "ok"},
        ):
            result = relogin_chatgpt_account(self.account_id)

        self.assertTrue(result["ok"])
        mailbox_config = create_mailbox_mock.call_args.kwargs["extra"]
        self.assertEqual(
            mailbox_config["applemail_base_url"],
            "https://mail.example.test",
        )
        self.assertEqual(mailbox_config["client_id"], "mail-client")

    def test_auto_maintenance_includes_visible_account_with_rt_but_no_login_context(self):
        account_id = self._add_eligibility_account(
            "rt-only@example.com",
            extra={"refresh_token": "rt-only-token"},
        )

        account_ids = list_auto_maintenance_account_ids(
            database_engine=self.engine,
        )

        self.assertIn(account_id, account_ids)

    def test_auto_maintenance_treats_missing_rt_as_full_login_candidate(self):
        account_id = self._add_eligibility_account(
            "missing-rt-login@example.com",
            account_refresh_token=None,
            extra={},
        )
        full_login_result = {
            "ok": True,
            "relogin_ok": True,
            "stage": "completed",
            "account_id": account_id,
            "email": "missing-rt-login@example.com",
            "message": "完整登录并同步成功",
        }

        with mock.patch("services.chatgpt_relogin.engine", self.engine), mock.patch(
            "services.chatgpt_relogin._relogin_chatgpt_account_locked",
            return_value=full_login_result,
        ) as full_login:
            result = refresh_or_relogin_chatgpt_account(account_id)

        full_login.assert_called_once()
        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "full_login")
        self.assertEqual(result["refresh_state"], "invalid")
        self.assertEqual(result["refresh_error_code"], "missing_refresh_token")

    def test_auto_refresh_success_persists_rotated_tokens_and_syncs_without_login(self):
        refresh_result = SimpleNamespace(
            success=True,
            state="valid",
            access_token="refreshed-at",
            refresh_token="rotated-rt",
            expires_at=None,
            http_status=200,
            error_code="",
            error_message="",
        )

        with mock.patch("services.chatgpt_relogin.engine", self.engine), mock.patch(
            "services.chatgpt_relogin.TokenRefreshManager"
        ) as manager_class, mock.patch(
            "services.chatgpt_relogin._relogin_chatgpt_account_locked"
        ) as full_login, mock.patch(
            "services.chatgpt_relogin.sync_codex2api_account",
            return_value={"name": "Codex2API", "ok": True, "msg": "ok"},
        ) as sync:
            manager_class.return_value.refresh_by_oauth_token.return_value = refresh_result
            result = refresh_or_relogin_chatgpt_account(self.account_id)

        self.assertTrue(result["ok"])
        self.assertTrue(result["refresh_ok"])
        self.assertEqual(result["mode"], "refresh_token")
        full_login.assert_not_called()
        sync.assert_called_once()
        with Session(self.engine) as session:
            saved = session.get(AccountModel, self.account_id)
            self.assertEqual(saved.token, "refreshed-at")
            extra = saved.get_extra()
            self.assertEqual(extra["access_token"], "refreshed-at")
            self.assertEqual(extra["refresh_token"], "rotated-rt")
            self.assertEqual(extra["id_token"], "old-id")

    def test_auto_refresh_explicit_invalid_falls_back_to_full_login(self):
        refresh_result = SimpleNamespace(
            success=False,
            state="invalid",
            access_token="",
            refresh_token="",
            expires_at=None,
            http_status=400,
            error_code="invalid_grant",
            error_message="OAuth token 刷新失败: HTTP 400 (invalid_grant)",
        )
        full_result = {
            "ok": True,
            "relogin_ok": True,
            "stage": "completed",
            "account_id": self.account_id,
            "email": "demo@example.com",
            "message": "重登并同步 Codex2API 成功",
        }

        with mock.patch("services.chatgpt_relogin.engine", self.engine), mock.patch(
            "services.chatgpt_relogin.TokenRefreshManager"
        ) as manager_class, mock.patch(
            "services.chatgpt_relogin._relogin_chatgpt_account_locked",
            return_value=full_result,
        ) as full_login, mock.patch(
            "services.chatgpt_relogin.sync_codex2api_account"
        ) as sync:
            manager_class.return_value.refresh_by_oauth_token.return_value = refresh_result
            result = refresh_or_relogin_chatgpt_account(self.account_id)

        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "full_login")
        self.assertEqual(result["refresh_state"], "invalid")
        full_login.assert_called_once()
        sync.assert_not_called()

    def test_auto_refresh_transient_failure_preserves_tokens_and_does_not_login(self):
        refresh_result = SimpleNamespace(
            success=False,
            state="transient_error",
            access_token="",
            refresh_token="",
            expires_at=None,
            http_status=503,
            error_code="server_error",
            error_message="OAuth token 刷新失败: HTTP 503 (server_error)",
        )

        with mock.patch("services.chatgpt_relogin.engine", self.engine), mock.patch(
            "services.chatgpt_relogin.TokenRefreshManager"
        ) as manager_class, mock.patch(
            "services.chatgpt_relogin._relogin_chatgpt_account_locked"
        ) as full_login, mock.patch(
            "services.chatgpt_relogin.sync_codex2api_account"
        ) as sync:
            manager_class.return_value.refresh_by_oauth_token.return_value = refresh_result
            result = refresh_or_relogin_chatgpt_account(self.account_id)

        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "refresh_deferred")
        self.assertEqual(result["refresh_state"], "transient_error")
        full_login.assert_not_called()
        sync.assert_not_called()
        with Session(self.engine) as session:
            saved = session.get(AccountModel, self.account_id)
            self.assertEqual(saved.token, "old-at")
            self.assertEqual(saved.get_extra()["refresh_token"], "old-rt")

    def test_auto_refresh_sync_failure_is_reported_after_local_tokens_are_saved(self):
        refresh_result = SimpleNamespace(
            success=True,
            state="valid",
            access_token="refreshed-at",
            refresh_token="rotated-rt",
            expires_at=None,
            http_status=200,
            error_code="",
            error_message="",
        )

        with mock.patch("services.chatgpt_relogin.engine", self.engine), mock.patch(
            "services.chatgpt_relogin.TokenRefreshManager"
        ) as manager_class, mock.patch(
            "services.chatgpt_relogin._relogin_chatgpt_account_locked"
        ) as full_login, mock.patch(
            "services.chatgpt_relogin.sync_codex2api_account",
            return_value={"name": "Codex2API", "ok": False, "msg": "remote test failed"},
        ):
            manager_class.return_value.refresh_by_oauth_token.return_value = refresh_result
            result = refresh_or_relogin_chatgpt_account(self.account_id)

        self.assertFalse(result["ok"])
        self.assertTrue(result["refresh_ok"])
        self.assertEqual(result["stage"], "codex2api_sync")
        full_login.assert_not_called()
        with Session(self.engine) as session:
            saved = session.get(AccountModel, self.account_id)
            self.assertEqual(saved.get_extra()["refresh_token"], "rotated-rt")


if __name__ == "__main__":
    unittest.main()
