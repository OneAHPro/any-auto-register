import threading
import time
import types
import unittest
from unittest import mock

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from core.db import AccountModel, OutlookAccountModel
import services.chatgpt_phone_verification as phone_verification_module

from services.chatgpt_phone_verification import (
    ChatGPTPhoneVerificationManager,
    InteractivePhoneVerificationBroker,
    PhoneVerificationCancelled,
    _load_account_and_email_service,
    merge_chatgpt_phone_tokens,
    normalize_e164_phone,
    run_interactive_phone_oauth_flow,
    run_leadbee_phone_oauth_flow,
    _take_phone_oauth_resume_context,
)


class _Account:
    def __init__(self):
        self.token = "existing-at"
        self.extra = {
            "access_token": "existing-at",
            "refresh_token": "",
            "chatgpt_phone_verification_required": True,
        }


class PhoneValidationTests(unittest.TestCase):
    def test_normalize_e164_phone_accepts_international_number(self):
        self.assertEqual(normalize_e164_phone(" +44 7456 344799 "), "+447456344799")

    def test_normalize_e164_phone_rejects_local_or_too_short_number(self):
        for value in ("13800138000", "+123", "abc"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "国际格式"):
                    normalize_e164_phone(value)

    def test_new_session_describes_oauth_resume_instead_of_email_relogin(self):
        broker = InteractivePhoneVerificationBroker(
            account_id=1,
            phone="+447456344799",
        )

        self.assertEqual(
            broker.snapshot()["message"],
            "正在恢复 OpenAI 授权会话并请求短信验证码",
        )

    def test_broker_snapshot_keeps_full_process_log(self):
        broker = InteractivePhoneVerificationBroker(
            account_id=1,
            phone="",
            provider="leadbee",
        )

        broker.mark_progress("正在恢复 OpenAI 登录会话")
        broker.mark_phone_acquired("+447456344799")
        broker.mark_automatic_sms_sent("+447456344799")
        broker.mark_automatic_code_received()

        logs = broker.snapshot().get("logs")
        self.assertIsInstance(logs, list)
        self.assertTrue(any("恢复 OpenAI 登录会话" in line for line in logs))
        self.assertTrue(any("已获取手机号" in line for line in logs))
        self.assertTrue(any("短信验证码已发送" in line for line in logs))
        self.assertTrue(any("已自动获取短信验证码" in line for line in logs))

    def test_provider_diagnostic_whitelists_safe_fields_and_redacts_unknown_values(self):
        broker = InteractivePhoneVerificationBroker(
            account_id=1,
            phone="",
            provider="leadbee",
            leadbee_api=True,
            client_order_id="aar_" + "a" * 32,
        )

        broker.mark_provider_diagnostic(
            failure_stage="openai_send",
            safe_error_code="OPENAI_SEND_RETRY_EXHAUSTED",
            http_status=504,
            provider_retry_count=2,
            order_status="CANCELED",
            billing_status="RELEASED",
            replacement_count=0,
            recovery_status="released",
            raw_body="secret-body",
            order_id="secret-order-id",
        )

        diagnostic = broker.snapshot()["provider_diagnostic"]
        self.assertEqual(
            diagnostic,
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
        self.assertNotIn("secret", str(broker.snapshot()))

    def test_unusable_exchange_code_is_structured_and_notified_once(self):
        settled = mock.Mock()
        broker = InteractivePhoneVerificationBroker(
            account_id=1,
            phone="",
            provider="leadbee",
            on_exchange_code_consumed=settled,
        )

        broker.mark_exchange_code_unusable("服务端未确认恢复")
        broker.mark_exchange_code_unusable("重复通知")

        snapshot = broker.snapshot()
        self.assertTrue(snapshot["exchange_code_unusable"])
        self.assertFalse(snapshot["exchange_code_consumed"])
        settled.assert_called_once_with()

    def test_provider_error_code_is_structured_and_keeps_first_failure(self):
        broker = InteractivePhoneVerificationBroker(
            account_id=1,
            phone="",
            provider="leadbee",
        )

        broker.mark_provider_error(
            "CARD_NOT_IN_SESSION",
            "当前会话无权操作该卡密",
        )
        broker.mark_provider_error(
            "CARD_ALREADY_USED",
            "后续取消请求返回的错误不应覆盖首次失败",
        )

        snapshot = broker.snapshot()
        self.assertEqual(snapshot["provider_error_code"], "CARD_NOT_IN_SESSION")
        self.assertIn("当前会话", snapshot["provider_error_message"])

    def test_provider_lifecycle_callbacks_are_structured_and_notified_once(self):
        provider_started = mock.Mock()
        restored = mock.Mock()
        broker = InteractivePhoneVerificationBroker(
            account_id=1,
            provider="leadbee",
            on_provider_start=provider_started,
            on_exchange_code_restored=restored,
        )

        broker.mark_provider_started()
        broker.mark_provider_started()
        broker.mark_exchange_code_restored()
        broker.mark_exchange_code_restored()

        snapshot = broker.snapshot()
        self.assertTrue(snapshot["provider_started"])
        self.assertTrue(snapshot["exchange_code_restoration_confirmed"])
        provider_started.assert_called_once_with()
        restored.assert_called_once_with()

    def test_completed_broker_does_not_retain_oauth_tokens_in_memory(self):
        broker = InteractivePhoneVerificationBroker(
            account_id=1,
            phone="+447456344799",
        )

        broker.mark_completed(
            {
                "access_token": "sensitive-access-token",
                "refresh_token": "sensitive-refresh-token",
            }
        )

        self.assertEqual(broker.tokens, {})


class TokenMergeTests(unittest.TestCase):
    def test_merge_updates_rt_and_new_at_only_after_success(self):
        account = _Account()

        merge_chatgpt_phone_tokens(
            account,
            {
                "access_token": "new-at",
                "refresh_token": "new-rt",
                "id_token": "new-id",
                "session_token": "new-session",
            },
        )

        self.assertEqual(account.token, "new-at")
        self.assertEqual(account.extra["access_token"], "new-at")
        self.assertEqual(account.extra["refresh_token"], "new-rt")
        self.assertEqual(account.extra["id_token"], "new-id")
        self.assertFalse(account.extra["chatgpt_phone_verification_required"])

    def test_merge_preserves_existing_at_when_result_only_contains_rt(self):
        account = _Account()

        merge_chatgpt_phone_tokens(account, {"refresh_token": "new-rt"})

        self.assertEqual(account.token, "existing-at")
        self.assertEqual(account.extra["access_token"], "existing-at")
        self.assertEqual(account.extra["refresh_token"], "new-rt")

    def test_merge_records_not_required_when_no_phone_verification_occurred(self):
        account = _Account()

        merge_chatgpt_phone_tokens(
            account,
            {
                "refresh_token": "new-rt",
                "_phone_verified": False,
                "_exchange_code_consumed": False,
            },
        )

        verification = account.extra["chatgpt_phone_verification"]
        self.assertEqual(verification["status"], "not_required")
        self.assertFalse(verification["phone_verified"])
        self.assertFalse(verification["exchange_code_consumed"])
        self.assertNotIn("phone_number", verification)
        self.assertFalse(account.extra["chatgpt_phone_verification_required"])


class PhoneVerificationPostProcessingTests(unittest.TestCase):
    def setUp(self):
        self.test_engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.test_engine)
        with Session(self.test_engine) as session:
            account = AccountModel(
                platform="chatgpt",
                email="existing@example.com",
                password="account-password",
                token="existing-at",
            )
            account.set_extra(
                {
                    "access_token": "existing-at",
                    "refresh_token": "",
                    "chatgpt_phone_verification_required": True,
                }
            )
            session.add(account)
            session.commit()
            session.refresh(account)
            self.account_id = account.id

    @staticmethod
    def _automatic_runner(_account_id, _exchange_code, broker):
        broker.mark_phone_acquired("+447456344799")
        broker.mark_automatic_sms_sent("+447456344799")
        broker.mark_automatic_code_received()
        broker.mark_phone_verified()
        return {"access_token": "new-at", "refresh_token": "new-rt"}

    def _manager(self):
        return ChatGPTPhoneVerificationManager(
            automatic_flow_runner=self._automatic_runner,
            start_timeout_seconds=2,
        )

    def _run_to_terminal(self, manager=None):
        manager = manager or self._manager()
        snapshot = manager.start(
            self.account_id,
            leadbee_code="bei-sms-DEMO-CODE",
        )
        deadline = time.monotonic() + 2
        while snapshot["status"] not in {"completed", "failed", "expired"}:
            if time.monotonic() >= deadline:
                self.fail("phone verification did not reach a terminal state")
            time.sleep(0.01)
            snapshot = manager.status(self.account_id, snapshot["session_id"])
        return snapshot

    def test_completed_phone_flow_resyncs_account_with_persisted_refresh_token(self):
        call_order = []
        with mock.patch("core.db.engine", self.test_engine), mock.patch(
            "services.chatgpt_account_refresh.refresh_chatgpt_account_by_id",
            side_effect=lambda _account_id: call_order.append("refresh"),
        ), mock.patch(
            "services.external_sync.sync_codex2api_account",
            side_effect=lambda _account: call_order.append("codex2api"),
            create=True,
        ) as codex2api_sync_mock, mock.patch(
            "services.external_sync.sync_account",
        ) as generic_sync_mock:
            result = self._run_to_terminal()

        self.assertEqual(result["status"], "completed")
        self.assertEqual(call_order, ["refresh", "codex2api"])
        codex2api_sync_mock.assert_called_once()
        generic_sync_mock.assert_not_called()
        synced_account = codex2api_sync_mock.call_args.args[0]
        self.assertEqual(synced_account.get_extra()["refresh_token"], "new-rt")

    def test_external_sync_exception_is_recorded_without_failing_phone_flow(self):
        config = {
            "codex2api_enabled": "1",
            "codex2api_api_url": "http://codex2api.local:8080",
            "codex2api_admin_key": "admin-key",
        }
        with mock.patch("core.db.engine", self.test_engine), mock.patch(
            "services.chatgpt_sync.engine",
            self.test_engine,
        ), mock.patch(
            "services.chatgpt_account_refresh.refresh_chatgpt_account_by_id",
            return_value={"auth": {"state": "access_token_valid"}},
        ), mock.patch(
            "core.config_store.config_store.get",
            side_effect=lambda key, default="": config.get(key, default),
        ), mock.patch(
            "platforms.chatgpt.codex2api_upload.upload_to_codex2api",
            side_effect=RuntimeError("temporary upstream failure"),
        ), mock.patch(
            "services.external_sync.sync_account",
        ) as generic_sync_mock:
            result = self._run_to_terminal()

        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["exchange_code_consumed"])
        generic_sync_mock.assert_not_called()
        with Session(self.test_engine) as session:
            account = session.get(AccountModel, self.account_id)
            extra = account.get_extra()

        self.assertEqual(extra["refresh_token"], "new-rt")
        self.assertEqual(extra["chatgpt_phone_verification"]["status"], "completed")
        self.assertTrue(
            extra["chatgpt_phone_verification"]["exchange_code_consumed"]
        )
        sync_state = extra["sync_statuses"]["codex2api"]
        self.assertFalse(sync_state["last_attempt_ok"])
        self.assertIn("自动同步异常", sync_state["last_message"])

    def test_sync_preparation_exception_does_not_fail_consumed_phone_flow(self):
        persisted = []
        manager = ChatGPTPhoneVerificationManager(
            automatic_flow_runner=self._automatic_runner,
            token_persister=lambda account_id, tokens: persisted.append(
                (account_id, tokens)
            ),
            start_timeout_seconds=2,
        )

        with mock.patch(
            "services.chatgpt_account_refresh.refresh_chatgpt_account_by_id",
            return_value={"auth": {"state": "access_token_valid"}},
        ), mock.patch(
            "sqlmodel.Session",
            side_effect=RuntimeError("database temporarily unavailable"),
        ):
            result = self._run_to_terminal(manager)

        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["phone_verified"])
        self.assertTrue(result["exchange_code_consumed"])
        self.assertEqual(persisted[0][1]["refresh_token"], "new-rt")


class MailboxContextRecoveryTests(unittest.TestCase):
    def test_legacy_account_uses_matching_reimported_mailbox_credentials(self):
        test_engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(test_engine)
        with Session(test_engine) as session:
            account = AccountModel(
                platform="chatgpt",
                email="legacy@example.com",
                password="chatgpt-password",
                token="existing-at",
            )
            account.set_extra({"access_token": "existing-at", "refresh_token": ""})
            session.add(account)
            session.add(
                OutlookAccountModel(
                    email="legacy@example.com",
                    password="mail-password",
                    client_id="mail-client",
                    refresh_token="mail-refresh",
                )
            )
            session.commit()
            session.refresh(account)
            account_id = account.id

        mailbox = mock.Mock()
        mailbox.get_current_ids.return_value = set()
        with mock.patch("core.db.engine", test_engine), mock.patch(
            "core.base_mailbox.create_mailbox",
            return_value=mailbox,
        ):
            email, password, extra, email_service = _load_account_and_email_service(
                account_id
            )

        self.assertEqual(email, "legacy@example.com")
        self.assertEqual(password, "chatgpt-password")
        context = extra["mailbox_login_context"]
        self.assertEqual(context["provider"], "microsoft")
        self.assertEqual(context["extra"]["client_id"], "mail-client")
        self.assertEqual(context["extra"]["refresh_token"], "mail-refresh")
        self.assertEqual(email_service.create_email()["email"], "legacy@example.com")
        with Session(test_engine) as session:
            saved = session.get(AccountModel, account_id)
            self.assertIn("mailbox_login_context", saved.get_extra())


class PhoneOAuthResumeTests(unittest.TestCase):
    def _run(self, resume_context):
        oauth_client = mock.Mock()
        oauth_client.login_and_get_tokens.return_value = {
            "access_token": "new-at",
            "refresh_token": "new-rt",
        }
        oauth_client.last_workspace_id = "workspace-1"
        broker = mock.Mock()
        loaded = (
            "existing@example.com",
            "account-password",
            {"proxy_used": "http://127.0.0.1:7890"},
        )

        with mock.patch(
            "services.chatgpt_phone_verification._load_account_context",
            return_value=loaded,
            create=True,
        ), mock.patch(
            "services.chatgpt_phone_verification._load_account_and_email_service",
            side_effect=AssertionError("手机续接不应恢复邮箱客户端"),
        ), mock.patch(
            "platforms.chatgpt.oauth_resume_cache.oauth_resume_cache.take",
            return_value=resume_context,
        ) as take, mock.patch(
            "platforms.chatgpt.oauth_client.OAuthClient",
            return_value=oauth_client,
        ), mock.patch(
            "core.config_store.config_store.get_all",
            return_value={},
        ):
            result = run_interactive_phone_oauth_flow(
                7,
                "+447456344799",
                broker,
            )

        return result, oauth_client, broker, take

    def test_phone_flow_reuses_cached_authenticated_browser_context(self):
        session = object()
        resume_context = types.SimpleNamespace(
            session=session,
            device_id="device-1",
            user_agent="UA",
            sec_ch_ua='"Chromium";v="136"',
            accept_language="en-US,en;q=0.9",
            impersonate="chrome136",
            code_verifier="prepared-verifier",
            oauth_state="prepared-state",
            flow_state=types.SimpleNamespace(page_type="add_phone"),
        )

        result, oauth_client, broker, take = self._run(resume_context)

        self.assertEqual(result["refresh_token"], "new-rt")
        take.assert_called_once_with("existing@example.com")
        oauth_client.adopt_browser_context.assert_called_once_with(
            session,
            device_id="device-1",
            user_agent="UA",
            sec_ch_ua='"Chromium";v="136"',
            accept_language="en-US,en;q=0.9",
        )
        login_kwargs = oauth_client.login_and_get_tokens.call_args.kwargs
        self.assertEqual(login_kwargs["device_id"], "device-1")
        self.assertFalse(login_kwargs["force_new_browser"])
        self.assertFalse(login_kwargs["resume_authenticated_session"])
        self.assertIs(login_kwargs["prepared_oauth_context"], resume_context)
        broker.mark_progress.assert_called_with(
            "正在续接登录时预建的手机授权事务并请求短信验证码"
        )

    def test_phone_flow_fails_without_relogin_when_cache_and_snapshot_are_missing(self):
        with self.assertRaisesRegex(RuntimeError, "授权事务不存在或已过期"):
            self._run(None)

    def test_phone_flow_recovers_fresh_pkce_from_browser_snapshot_without_mailbox(self):
        browser_snapshot = {"version": 1, "cookies": [{"name": "login_session"}]}
        browser_context = types.SimpleNamespace(
            session=object(),
            device_id="device-browser",
            user_agent="UA-browser",
            sec_ch_ua='"Chromium";v="136"',
            accept_language="en-US,en;q=0.9",
            impersonate="chrome136",
            code_verifier="",
            oauth_state="",
            flow_state=None,
        )
        prepared_context = types.SimpleNamespace(
            session=object(),
            device_id="device-browser",
            user_agent="UA-browser",
            sec_ch_ua='"Chromium";v="136"',
            accept_language="en-US,en;q=0.9",
            impersonate="chrome136",
            code_verifier="fresh-verifier",
            oauth_state="fresh-state",
            flow_state=types.SimpleNamespace(page_type="add_phone"),
        )
        oauth_client = mock.Mock()
        oauth_client.prepare_phone_verification_transaction.return_value = prepared_context
        oauth_client.login_and_get_tokens.return_value = {
            "access_token": "new-at",
            "refresh_token": "new-rt",
        }
        oauth_client.last_workspace_id = "workspace-1"
        broker = mock.Mock()

        with mock.patch(
            "services.chatgpt_phone_verification._load_account_context",
            return_value=(
                "existing@example.com",
                "account-password",
                {
                    "proxy_used": "http://127.0.0.1:7890",
                    "oauth_browser_context": browser_snapshot,
                },
            ),
        ), mock.patch(
            "services.chatgpt_phone_verification._load_account_and_email_service",
            side_effect=AssertionError("浏览器快照恢复不应加载邮箱客户端"),
        ), mock.patch(
            "platforms.chatgpt.oauth_resume_cache.oauth_resume_cache.take",
            return_value=None,
        ), mock.patch(
            "platforms.chatgpt.oauth_resume_cache.restore_oauth_resume_context",
            side_effect=lambda snapshot: (
                browser_context if snapshot is browser_snapshot else None
            ),
        ), mock.patch(
            "platforms.chatgpt.oauth_client.OAuthClient",
            return_value=oauth_client,
        ), mock.patch(
            "services.chatgpt_phone_verification._persist_prepared_phone_oauth_context",
            create=True,
        ) as persist, mock.patch(
            "core.config_store.config_store.get_all",
            return_value={},
        ):
            result = run_interactive_phone_oauth_flow(
                7,
                "+447456344799",
                broker,
            )

        self.assertEqual(result["refresh_token"], "new-rt")
        oauth_client.prepare_phone_verification_transaction.assert_called_once_with(
            email="existing@example.com",
            device_id="device-browser",
            user_agent="UA-browser",
            sec_ch_ua='"Chromium";v="136"',
            accept_language="en-US,en;q=0.9",
            impersonate="chrome136",
        )
        persist.assert_called_once_with(7, prepared_context)
        self.assertIs(
            oauth_client.login_and_get_tokens.call_args.kwargs[
                "prepared_oauth_context"
            ],
            prepared_context,
        )
        broker.mark_progress.assert_called_with(
            "已从认证浏览器快照恢复新的手机授权事务；正在请求短信验证码"
        )

    def test_retry_flag_forces_fresh_browser_oauth_transaction(self):
        old_context = types.SimpleNamespace(
            session=object(),
            device_id="device-old",
            user_agent="UA-old",
            sec_ch_ua='"Chromium";v="136"',
            accept_language="en-US,en;q=0.9",
            impersonate="chrome136",
            code_verifier="old-verifier",
            oauth_state="old-state",
            flow_state=types.SimpleNamespace(page_type="add_phone"),
        )
        browser_context = types.SimpleNamespace(
            session=object(),
            device_id="device-browser",
            user_agent="UA-browser",
            sec_ch_ua='"Chromium";v="136"',
            accept_language="en-US,en;q=0.9",
            impersonate="chrome136",
            code_verifier="",
            oauth_state="",
            flow_state=None,
        )
        fresh_context = types.SimpleNamespace(
            session=object(),
            device_id="device-browser",
            user_agent="UA-browser",
            sec_ch_ua='"Chromium";v="136"',
            accept_language="en-US,en;q=0.9",
            impersonate="chrome136",
            code_verifier="fresh-verifier",
            oauth_state="fresh-state",
            flow_state=types.SimpleNamespace(page_type="add_phone"),
        )
        oauth_client = mock.Mock()
        oauth_client.prepare_phone_verification_transaction.return_value = fresh_context
        broker = mock.Mock(phone_oauth_retry_count=1)
        persisted_snapshot = {"version": 2, "flow_state": {"page_type": "add_phone"}}
        browser_snapshot = {"version": 1, "cookies": [{"name": "login_session"}]}

        with mock.patch(
            "platforms.chatgpt.oauth_resume_cache.oauth_resume_cache.take",
            return_value=old_context,
        ), mock.patch(
            "platforms.chatgpt.oauth_resume_cache.restore_oauth_resume_context",
            side_effect=lambda snapshot: (
                browser_context if snapshot is browser_snapshot else old_context
            ),
        ) as restore, mock.patch(
            "platforms.chatgpt.oauth_client.OAuthClient",
            return_value=oauth_client,
        ):
            context, source = _take_phone_oauth_resume_context(
                "existing@example.com",
                {
                    "oauth_resume_context": persisted_snapshot,
                    "oauth_browser_context": browser_snapshot,
                },
                oauth_client=oauth_client,
                prefer_browser_recovery=True,
            )

        self.assertIs(context, fresh_context)
        self.assertEqual(source, "browser_recovered")
        self.assertEqual(restore.call_count, 2)
        oauth_client.adopt_browser_context.assert_called_once_with(
            browser_context.session,
            device_id="device-browser",
            user_agent="UA-browser",
            sec_ch_ua='"Chromium";v="136"',
            accept_language="en-US,en;q=0.9",
        )

    def test_leadbee_api_retry_prefers_fresh_account_login_context(self):
        fresh_context = types.SimpleNamespace(
            session=object(),
            device_id="device-fresh-login",
            user_agent="UA-fresh",
            sec_ch_ua='"Chromium";v="136"',
            accept_language="en-US,en;q=0.9",
            impersonate="chrome136",
            code_verifier="fresh-verifier",
            oauth_state="fresh-state",
            flow_state=types.SimpleNamespace(page_type="add_phone"),
        )
        oauth_client = mock.Mock()
        oauth_client.login_and_get_tokens.return_value = {
            "access_token": "new-at",
            "refresh_token": "new-rt",
        }
        oauth_client.last_workspace_id = "workspace-1"
        broker = InteractivePhoneVerificationBroker(
            account_id=7,
            provider="leadbee",
            leadbee_api=True,
            client_order_id="aar_0123456789abcdef0123456789abcdef",
        )
        broker.phone_oauth_retry_count = 1
        loaded = (
            "existing@example.com",
            "account-password",
            {
                "proxy_used": "http://127.0.0.1:7890",
                "oauth_browser_context": {
                    "version": 1,
                    "cookies": [{"name": "login_session"}],
                },
            },
        )

        with mock.patch(
            "services.chatgpt_phone_verification._load_account_context",
            return_value=loaded,
        ), mock.patch(
            "services.chatgpt_phone_verification._rebuild_phone_oauth_context_with_fresh_login",
            return_value=(fresh_context, "fresh_login"),
            create=True,
        ) as rebuild, mock.patch(
            "services.chatgpt_phone_verification._take_phone_oauth_resume_context",
            side_effect=AssertionError("fresh login context should be preferred"),
        ), mock.patch(
            "platforms.chatgpt.oauth_client.OAuthClient",
            return_value=oauth_client,
        ), mock.patch(
            "services.chatgpt_phone_verification._persist_prepared_phone_oauth_context"
        ) as persist, mock.patch(
            "core.config_store.config_store.get_all",
            return_value={
                "leadbee_api_enabled": "1",
                "leadbee_api_key": "fixture-api-key",
                "leadbee_api_secret": "fixture-api-secret",
                "leadbee_api_product_id": "fixture-product",
            },
        ):
            result = run_leadbee_phone_oauth_flow(7, broker.client_order_id, broker)

        self.assertEqual(result["refresh_token"], "new-rt")
        rebuild.assert_called_once_with(7, broker)
        persist.assert_called_once_with(7, fresh_context)
        self.assertIs(
            oauth_client.login_and_get_tokens.call_args.kwargs[
                "prepared_oauth_context"
            ],
            fresh_context,
        )

    def test_fresh_account_login_rebuilds_and_returns_prepared_context(self):
        helper = getattr(
            phone_verification_module,
            "_rebuild_phone_oauth_context_with_fresh_login",
            None,
        )
        self.assertIsNotNone(helper)
        prepared_context = types.SimpleNamespace(
            session=object(),
            device_id="device-fresh-login",
            user_agent="UA-fresh",
            sec_ch_ua='"Chromium";v="136"',
            accept_language="en-US,en;q=0.9",
            impersonate="chrome136",
            code_verifier="fresh-verifier",
            oauth_state="fresh-state",
            flow_state=types.SimpleNamespace(page_type="add_phone"),
        )
        resume_snapshot = {"version": 2, "flow_state": {"page_type": "add_phone"}}
        saved = {
            "email": "existing@example.com",
            "password": "account-password",
            "extra": {"proxy_used": "http://127.0.0.1:7890"},
            "mailbox_context": {
                "provider": "chatgpt_credentials",
                "email": "existing@example.com",
                "extra": {
                    "account_type": "chatgpt_password_totp",
                    "password": "account-password",
                    "totp_secret": "JBSWY3DPEHPK3PXP",
                },
            },
        }
        email_service = mock.Mock()
        adapter = mock.Mock()
        adapter.run.return_value = types.SimpleNamespace(
            success=True,
            error_message="",
            metadata={"oauth_resume_context": resume_snapshot},
        )
        broker = mock.Mock()

        with mock.patch(
            "services.chatgpt_relogin._load_saved_account",
            return_value=saved,
        ), mock.patch(
            "services.chatgpt_relogin._build_email_service",
            return_value=email_service,
        ) as build_email_service, mock.patch(
            "platforms.chatgpt.chatgpt_registration_mode_adapter.build_chatgpt_registration_mode_adapter",
            return_value=adapter,
        ) as build_adapter, mock.patch(
            "platforms.chatgpt.oauth_resume_cache.restore_oauth_resume_context",
            return_value=prepared_context,
        ), mock.patch(
            "core.config_store.config_store.get_all",
            return_value={"default_executor": "protocol"},
        ):
            context, source = helper(7, broker)

        self.assertIs(context, prepared_context)
        self.assertEqual(source, "fresh_login")
        build_email_service.assert_called_once()
        build_adapter.assert_called_once()
        login_context = adapter.run.call_args.args[0]
        self.assertEqual(login_context.email, "existing@example.com")
        self.assertEqual(login_context.password, "account-password")
        self.assertEqual(
            login_context.extra_config["chatgpt_existing_account_login_stage"],
            "access_token",
        )


    def test_leadbee_flow_passes_exchange_code_to_automatic_provider(self):
        oauth_client = mock.Mock()
        oauth_client.login_and_get_tokens.return_value = {
            "access_token": "new-at",
            "refresh_token": "new-rt",
        }
        oauth_client.last_workspace_id = "workspace-1"
        broker = mock.Mock()
        broker.leadbee_base_url = ""
        loaded = (
            "existing@example.com",
            "account-password",
            {"proxy_used": "http://127.0.0.1:7890"},
        )
        resume_context = types.SimpleNamespace(
            session=object(),
            device_id="device-1",
            user_agent="UA",
            sec_ch_ua='"Chromium";v="136"',
            accept_language="en-US,en;q=0.9",
            impersonate="chrome136",
            code_verifier="prepared-verifier",
            oauth_state="prepared-state",
            flow_state=types.SimpleNamespace(page_type="add_phone"),
        )

        with mock.patch(
            "services.chatgpt_phone_verification._load_account_context",
            return_value=loaded,
            create=True,
        ), mock.patch(
            "services.chatgpt_phone_verification._load_account_and_email_service",
            side_effect=AssertionError("手机续接不应恢复邮箱客户端"),
        ), mock.patch(
            "platforms.chatgpt.oauth_resume_cache.oauth_resume_cache.take",
            return_value=resume_context,
        ), mock.patch(
            "platforms.chatgpt.oauth_client.OAuthClient",
            return_value=oauth_client,
        ) as oauth_client_type, mock.patch(
            "core.config_store.config_store.get_all",
            return_value={
                "chatgpt_phone_number": "+19999999999",
                "leadbee_api_enabled": "1",
                "leadbee_api_key": "fixture-api-key",
                "leadbee_api_secret": "fixture-api-secret",
                "leadbee_api_product_id": "fixture-product",
                "leadbee_base_url": "https://stored-legacy.invalid",
            },
        ):
            result = run_leadbee_phone_oauth_flow(
                7,
                "bei-sms-DEMO-CODE",
                broker,
            )

        self.assertEqual(result["refresh_token"], "new-rt")
        oauth_config = oauth_client_type.call_args.args[0]
        self.assertEqual(oauth_config["chatgpt_phone_provider"], "leadbee")
        self.assertEqual(oauth_config["leadbee_code"], "bei-sms-DEMO-CODE")
        self.assertEqual(oauth_config["leadbee_api_enabled"], "0")
        self.assertEqual(
            oauth_config["leadbee_base_url"], "https://stored-legacy.invalid"
        )
        self.assertIs(oauth_config["chatgpt_phone_progress_broker"], broker)
        self.assertNotIn("chatgpt_phone_number", oauth_config)
        login_kwargs = oauth_client.login_and_get_tokens.call_args.kwargs
        self.assertTrue(login_kwargs["allow_phone_verification"])
        self.assertEqual(login_kwargs["login_source"], "automatic_phone_verification")

    def test_leadbee_api_flow_uses_stored_credentials_and_server_reference(self):
        api_key = "fixture-api-key"
        api_secret = "fixture-api-secret"
        client_order_id = "aar_0123456789abcdef0123456789abcdef"
        oauth_client = mock.Mock()
        oauth_client.login_and_get_tokens.return_value = {
            "access_token": "new-at",
            "refresh_token": "new-rt",
        }
        oauth_client.last_workspace_id = "workspace-1"
        broker = InteractivePhoneVerificationBroker(
            account_id=7,
            provider="leadbee",
            leadbee_api=True,
            client_order_id=client_order_id,
        )
        broker.leadbee_base_url = "https://arbitrary.invalid"
        loaded = (
            "existing@example.com",
            "account-password",
            {"proxy_used": "http://127.0.0.1:7890"},
        )
        resume_context = types.SimpleNamespace(
            session=object(),
            device_id="device-1",
            user_agent="UA",
            sec_ch_ua='"Chromium";v="136"',
            accept_language="en-US,en;q=0.9",
            impersonate="chrome136",
            code_verifier="prepared-verifier",
            oauth_state="prepared-state",
            flow_state=types.SimpleNamespace(page_type="add_phone"),
        )
        stored_config = {
            "leadbee_api_enabled": "1",
            "leadbee_api_key": api_key,
            "leadbee_api_secret": api_secret,
            "leadbee_api_product_id": "fixture-product",
            "leadbee_code": "stale-fixture-card",
            "leadbee_base_url": "https://stored-arbitrary.invalid",
            "chatgpt_phone_number": "+19999999999",
        }

        with mock.patch(
            "services.chatgpt_phone_verification._load_account_context",
            return_value=loaded,
            create=True,
        ), mock.patch(
            "platforms.chatgpt.oauth_resume_cache.oauth_resume_cache.take",
            return_value=resume_context,
        ), mock.patch(
            "platforms.chatgpt.oauth_client.OAuthClient",
            return_value=oauth_client,
        ) as oauth_client_type, mock.patch(
            "core.config_store.config_store.get_all",
            return_value=stored_config,
        ):
            result = run_leadbee_phone_oauth_flow(
                7,
                "ignored-legacy-exchange-code-argument",
                broker,
            )

        self.assertEqual(result["refresh_token"], "new-rt")
        oauth_config = oauth_client_type.call_args.args[0]
        self.assertEqual(oauth_config["leadbee_api_enabled"], "1")
        self.assertEqual(oauth_config["leadbee_api_key"], api_key)
        self.assertEqual(oauth_config["leadbee_api_secret"], api_secret)
        self.assertEqual(oauth_config["leadbee_api_product_id"], "fixture-product")
        self.assertEqual(oauth_config["leadbee_api_client_order_id"], client_order_id)
        self.assertEqual(oauth_config["chatgpt_phone_provider"], "leadbee")
        self.assertNotIn("leadbee_code", oauth_config)
        self.assertNotIn("leadbee_base_url", oauth_config)
        self.assertNotIn("chatgpt_phone_number", oauth_config)

    def test_leadbee_api_flow_rejects_incomplete_stored_configuration(self):
        broker = InteractivePhoneVerificationBroker(
            account_id=7,
            provider="leadbee",
            leadbee_api=True,
            client_order_id="aar_0123456789abcdef0123456789abcdef",
        )
        with mock.patch(
            "core.config_store.config_store.get_all",
            return_value={"leadbee_api_enabled": "1"},
        ):
            with self.assertRaisesRegex(ValueError, "配置不完整"):
                run_leadbee_phone_oauth_flow(
                    7,
                    broker.client_order_id,
                    broker,
                )


class PhoneVerificationManagerTests(unittest.TestCase):
    def test_api_retries_pre_provider_oauth_failure_once(self):
        attempts = []
        pre_provider_error = getattr(
            phone_verification_module,
            "PhoneOAuthPreProviderError",
            RuntimeError,
        )

        def runner(_account_id, _client_order_id, broker):
            attempts.append(int(getattr(broker, "phone_oauth_retry_count", 0)))
            if len(attempts) == 1:
                raise pre_provider_error("OAuth context returned log_in")
            return {"refresh_token": "fixture-rt"}

        manager = ChatGPTPhoneVerificationManager(
            automatic_flow_runner=runner,
            token_persister=lambda *_args: None,
            status_refresher=lambda *_args: None,
            start_timeout_seconds=1,
        )
        config = {
            "leadbee_api_enabled": "1",
            "leadbee_api_key": "fixture-api-key",
            "leadbee_api_secret": "fixture-api-secret",
            "leadbee_api_product_id": "fixture-product",
        }

        with mock.patch(
            "core.config_store.config_store.get_all",
            return_value=config,
        ), mock.patch(
            "services.chatgpt_phone_verification.time.sleep"
        ) as sleep:
            result = manager.start(301, leadbee_api=True)

        self.assertEqual(result["status"], "completed")
        self.assertEqual(attempts, [0, 1])
        self.assertFalse(result["provider_started"])
        self.assertEqual(result["provider_diagnostic"]["recovery_status"], "reconciled")
        sleep.assert_called_once_with(0.5)
        self.assertTrue(any("OAuth" in line for line in result["logs"]))

    def test_api_pre_provider_oauth_retry_is_bounded_and_marks_order_uncreated(self):
        attempts = 0
        pre_provider_error = getattr(
            phone_verification_module,
            "PhoneOAuthPreProviderError",
            RuntimeError,
        )

        def runner(*_args):
            nonlocal attempts
            attempts += 1
            raise pre_provider_error("OAuth context returned log_in")

        manager = ChatGPTPhoneVerificationManager(
            automatic_flow_runner=runner,
            token_persister=lambda *_args: None,
            status_refresher=lambda *_args: None,
            start_timeout_seconds=1,
        )
        config = {
            "leadbee_api_enabled": "1",
            "leadbee_api_key": "fixture-api-key",
            "leadbee_api_secret": "fixture-api-secret",
            "leadbee_api_product_id": "fixture-product",
        }

        with mock.patch(
            "core.config_store.config_store.get_all",
            return_value=config,
        ), mock.patch(
            "services.chatgpt_phone_verification.time.sleep"
        ) as sleep:
            result = manager.start(302, leadbee_api=True)

        self.assertEqual(attempts, 2)
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["provider_started"])
        self.assertEqual(
            result["message"],
            "OpenAI 手机 OAuth 会话恢复失败，LeadBee API 订单未创建",
        )
        self.assertEqual(
            result["provider_diagnostic"],
            {
                "failure_stage": "oauth_prepare",
                "safe_error_code": "OPENAI_OAUTH_CONTEXT_NOT_READY",
                "provider_retry_count": 1,
                "recovery_status": "failed",
            },
        )
        self.assertEqual(sleep.call_count, 1)

    def test_oauth_retry_does_not_overwrite_captured_order_diagnostic(self):
        attempts = 0

        def runner(_account_id, _client_order_id, broker):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise phone_verification_module.PhoneOAuthPreProviderError(
                    "OAuth context returned log_in"
                )
            broker.mark_provider_started()
            broker.mark_provider_diagnostic(
                failure_stage="openai_validate",
                order_status="COMPLETED",
                billing_status="CAPTURED",
                recovery_status="captured",
            )
            return {"refresh_token": "fixture-rt"}

        manager = ChatGPTPhoneVerificationManager(
            automatic_flow_runner=runner,
            token_persister=lambda *_args: None,
            status_refresher=lambda *_args: None,
            start_timeout_seconds=1,
        )
        config = {
            "leadbee_api_enabled": "1",
            "leadbee_api_key": "fixture-api-key",
            "leadbee_api_secret": "fixture-api-secret",
            "leadbee_api_product_id": "fixture-product",
        }

        with mock.patch(
            "core.config_store.config_store.get_all",
            return_value=config,
        ), mock.patch("services.chatgpt_phone_verification.time.sleep"):
            result = manager.start(303, leadbee_api=True)

        self.assertEqual(result["status"], "completed")
        self.assertEqual(attempts, 2)
        self.assertTrue(result["provider_started"])
        self.assertEqual(result["provider_diagnostic"]["order_status"], "COMPLETED")
        self.assertEqual(result["provider_diagnostic"]["billing_status"], "CAPTURED")
        self.assertEqual(result["provider_diagnostic"]["recovery_status"], "captured")

    def test_pre_provider_error_is_not_retried_after_provider_start(self):
        attempts = 0

        def runner(_account_id, _client_order_id, broker):
            nonlocal attempts
            attempts += 1
            broker.mark_provider_started()
            raise phone_verification_module.PhoneOAuthPreProviderError(
                "late OAuth classification"
            )

        manager = ChatGPTPhoneVerificationManager(
            automatic_flow_runner=runner,
            token_persister=lambda *_args: None,
            status_refresher=lambda *_args: None,
            start_timeout_seconds=1,
        )
        config = {
            "leadbee_api_enabled": "1",
            "leadbee_api_key": "fixture-api-key",
            "leadbee_api_secret": "fixture-api-secret",
            "leadbee_api_product_id": "fixture-product",
        }

        with mock.patch(
            "core.config_store.config_store.get_all",
            return_value=config,
        ), mock.patch(
            "services.chatgpt_phone_verification.time.sleep"
        ) as sleep:
            result = manager.start(304, leadbee_api=True)

        self.assertEqual(attempts, 1)
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["provider_started"])
        self.assertEqual(result["message"], "LeadBee API 自动接码失败")
        sleep.assert_not_called()

    def test_api_capacity_lease_is_attached_only_to_internal_broker(self):
        lease = object()
        observed = []

        def runner(_account_id, _client_order_id, broker):
            observed.append(getattr(broker, "leadbee_capacity_lease", None))
            return {"refresh_token": "fixture-rt"}

        manager = ChatGPTPhoneVerificationManager(
            automatic_flow_runner=runner,
            token_persister=lambda *_args: None,
            status_refresher=lambda *_args: None,
            start_timeout_seconds=1,
        )
        config = {
            "leadbee_api_enabled": "1",
            "leadbee_api_key": "fixture-api-key",
            "leadbee_api_secret": "fixture-api-secret",
            "leadbee_api_product_id": "fixture-product",
        }

        with mock.patch(
            "core.config_store.config_store.get_all",
            return_value=config,
        ):
            snapshot = manager.start(
                200,
                leadbee_api=True,
                leadbee_capacity_lease=lease,
            )

        self.assertEqual(snapshot["status"], "completed")
        self.assertEqual(observed, [lease])
        self.assertNotIn("capacity", repr(snapshot).lower())

    def test_api_and_exchange_cards_use_independent_provider_slots(self):
        class TrackingSlot:
            def __init__(self):
                self.acquire_calls = 0
                self.release_calls = 0

            def acquire(self, **_kwargs):
                self.acquire_calls += 1
                return True

            def release(self):
                self.release_calls += 1

        api_slot = TrackingSlot()
        card_slot = TrackingSlot()
        config = {
            "leadbee_api_enabled": "1",
            "leadbee_api_key": "fixture-api-key",
            "leadbee_api_secret": "fixture-api-secret",
            "leadbee_api_product_id": "fixture-product",
        }
        manager = ChatGPTPhoneVerificationManager(
            automatic_flow_runner=lambda *_args: {"refresh_token": "fixture-rt"},
            token_persister=lambda *_args: None,
            status_refresher=lambda *_args: None,
            start_timeout_seconds=1,
        )

        with (
            mock.patch(
                "services.chatgpt_phone_verification.leadbee_api_phone_flow_lock",
                api_slot,
            ),
            mock.patch(
                "services.chatgpt_phone_verification.leadbee_phone_flow_lock",
                card_slot,
            ),
            mock.patch(
                "core.config_store.config_store.get_all",
                return_value=config,
            ),
        ):
            manager.start(201, leadbee_api=True)
            manager.start(202, leadbee_code="fixture-card")

        self.assertEqual(api_slot.acquire_calls, 1)
        self.assertEqual(api_slot.release_calls, 1)
        self.assertEqual(card_slot.acquire_calls, 1)
        self.assertEqual(card_slot.release_calls, 1)

    def test_leadbee_api_start_generates_unique_stable_server_references(self):
        observed = []

        def runner(_account_id, client_order_id, broker):
            observed.append((client_order_id, broker.client_order_id))
            broker.mark_phone_verified()
            return {"refresh_token": "new-rt"}

        manager = ChatGPTPhoneVerificationManager(
            automatic_flow_runner=runner,
            token_persister=lambda *_args: None,
            status_refresher=lambda _account_id: None,
            start_timeout_seconds=1,
        )
        config = {
            "leadbee_api_enabled": "1",
            "leadbee_api_key": "fixture-api-key",
            "leadbee_api_secret": "fixture-api-secret",
            "leadbee_api_product_id": "fixture-product",
        }

        with mock.patch(
            "core.config_store.config_store.get_all",
            return_value=config,
        ):
            first = manager.start(101, leadbee_api=True)
            second = manager.start(102, leadbee_api=True)

        self.assertEqual(first["provider_mode"], "api")
        self.assertTrue(first["automatic"])
        self.assertTrue(first["leadbee_api"])
        self.assertRegex(first["client_order_id"], r"^aar_[0-9a-f]{32}$")
        self.assertRegex(second["client_order_id"], r"^aar_[0-9a-f]{32}$")
        self.assertNotEqual(first["client_order_id"], second["client_order_id"])
        self.assertEqual(
            observed,
            [
                (first["client_order_id"], first["client_order_id"]),
                (second["client_order_id"], second["client_order_id"]),
            ],
        )

    def test_leadbee_api_active_account_session_is_reused_without_new_reference(self):
        release_runner = threading.Event()
        runner_started = threading.Event()
        observed = []

        def runner(_account_id, client_order_id, _broker):
            observed.append(client_order_id)
            runner_started.set()
            release_runner.wait(timeout=1)
            return {"refresh_token": "new-rt"}

        manager = ChatGPTPhoneVerificationManager(
            automatic_flow_runner=runner,
            token_persister=lambda *_args: None,
            status_refresher=lambda _account_id: None,
            start_timeout_seconds=0,
        )
        config = {
            "leadbee_api_enabled": "1",
            "leadbee_api_key": "fixture-api-key",
            "leadbee_api_secret": "fixture-api-secret",
            "leadbee_api_product_id": "fixture-product",
        }

        try:
            with mock.patch(
                "core.config_store.config_store.get_all",
                return_value=config,
            ):
                first = manager.start(103, leadbee_api=True)
                self.assertTrue(runner_started.wait(timeout=1))
                second = manager.start(103, leadbee_api=True)
            self.assertTrue(second["reused"])
            self.assertEqual(first["session_id"], second["session_id"])
            self.assertEqual(first["client_order_id"], second["client_order_id"])
            self.assertEqual(observed, [first["client_order_id"]])
        finally:
            release_runner.set()

    def test_leadbee_api_incomplete_config_fails_before_broker_or_worker(self):
        cases = (
            ({"leadbee_api_enabled": "0"}, "未启用"),
            (
                {
                    "leadbee_api_enabled": "1",
                    "leadbee_api_key": "fixture-api-key",
                },
                "配置不完整",
            ),
        )
        for config, expected_error in cases:
            manager = ChatGPTPhoneVerificationManager(start_timeout_seconds=0)
            with self.subTest(expected_error=expected_error), mock.patch(
                "core.config_store.config_store.get_all",
                return_value=config,
            ), mock.patch(
                "services.chatgpt_phone_verification.threading.Thread"
            ) as thread_type, mock.patch(
                "services.chatgpt_phone_verification.leadbee_phone_flow_lock.acquire"
            ) as acquire:
                with self.assertRaisesRegex(ValueError, expected_error) as ctx:
                    manager.start(104, leadbee_api=True)

            thread_type.assert_not_called()
            acquire.assert_not_called()
            self.assertEqual(manager._sessions, {})
            self.assertNotIn("fixture-api-key", str(ctx.exception))

    def test_leadbee_api_runner_error_does_not_publish_credentials(self):
        api_key = "fixture-api-key"
        api_secret = "fixture-api-secret"

        def runner(*_args):
            raise RuntimeError(f"provider rejected {api_key} / {api_secret}")

        manager = ChatGPTPhoneVerificationManager(
            automatic_flow_runner=runner,
            start_timeout_seconds=1,
        )
        config = {
            "leadbee_api_enabled": "1",
            "leadbee_api_key": api_key,
            "leadbee_api_secret": api_secret,
            "leadbee_api_product_id": "fixture-product",
        }

        with mock.patch(
            "core.config_store.config_store.get_all",
            return_value=config,
        ):
            result = manager.start(105, leadbee_api=True)

        published = repr(result)
        self.assertNotIn(api_key, published)
        self.assertNotIn(api_secret, published)
        self.assertIn("LeadBee API", result["message"])

    def test_leadbee_api_does_not_fire_exchange_card_callbacks(self):
        consumed = mock.Mock()
        restored = mock.Mock()
        api_key = "fixture-api-key"
        api_secret = "fixture-api-secret"
        api_signature = "fixture-api-signature"
        phone = "+447456344799"
        verification_code = "846291"
        broker = InteractivePhoneVerificationBroker(
            account_id=106,
            provider="leadbee",
            leadbee_api=True,
            client_order_id="aar_0123456789abcdef0123456789abcdef",
            on_exchange_code_consumed=consumed,
            on_exchange_code_restored=restored,
        )

        broker.mark_automatic_code_received()
        broker.mark_exchange_code_restored()
        broker.mark_phone_acquired(phone)
        broker.mark_provider_error(
            f"AUTH_{api_key}_{api_secret}_{api_signature}_{phone}_{verification_code}",
            f"{api_key} / {api_secret} / {api_signature} / {phone} / {verification_code}",
        )
        unsafe_public_text = (
            f"{api_key} / {api_secret} / {api_signature} / {phone} / "
            f"{verification_code}"
        )
        broker.mark_progress(unsafe_public_text)
        broker.append_log(unsafe_public_text)
        broker.mark_failed(unsafe_public_text)

        snapshot = broker.snapshot()
        consumed.assert_not_called()
        restored.assert_not_called()
        self.assertEqual(snapshot["exchange_code_settlement"], "")
        self.assertFalse(snapshot["exchange_code_consumed"])
        self.assertEqual(snapshot["provider_error_code"], "LEADBEE_API_ERROR")
        self.assertEqual(snapshot["message"], "LeadBee API 自动接码失败")
        self.assertNotEqual(snapshot["phone"], phone)
        self.assertRegex(snapshot["phone"], r"^\+44\*+99$")
        published = repr(snapshot)
        for sensitive_value in (
            api_key,
            api_secret,
            api_signature,
            phone,
            verification_code,
        ):
            self.assertNotIn(sensitive_value, published)

    def test_leadbee_api_completion_logs_do_not_use_exchange_card_terms(self):
        broker = InteractivePhoneVerificationBroker(
            account_id=107,
            provider="leadbee",
            leadbee_api=True,
            client_order_id="aar_0123456789abcdef0123456789abcdef",
        )

        broker.begin_persisting()
        broker.cancel()
        broker.mark_completed({"refresh_token": "fixture-rt"})

        snapshot = broker.snapshot()
        published_copy = " ".join([snapshot["message"], *snapshot["logs"]])
        self.assertNotIn("兑换码", published_copy)
        self.assertNotIn("卡密", published_copy)
        self.assertIn("LeadBee API", snapshot["message"])

    def test_provider_slot_is_released_before_token_persistence_finishes(self):
        provider_slot = threading.BoundedSemaphore(1)
        persister_started = threading.Event()
        release_persister = threading.Event()

        def token_persister(_account_id, _tokens):
            persister_started.set()
            release_persister.wait(timeout=1)

        manager = ChatGPTPhoneVerificationManager(
            automatic_flow_runner=lambda *_args: {"refresh_token": "new-rt"},
            token_persister=token_persister,
            status_refresher=lambda _account_id: None,
            start_timeout_seconds=0.01,
        )

        with mock.patch(
            "services.chatgpt_phone_verification.leadbee_phone_flow_lock",
            provider_slot,
        ):
            started = manager.start(88, leadbee_code="bei-sms-PERSIST-BLOCK")
            self.assertTrue(persister_started.wait(timeout=1))
            self.assertTrue(started["provider_cleanup_settled"])
            self.assertTrue(provider_slot.acquire(blocking=False))
            provider_slot.release()
            release_persister.set()

            deadline = time.monotonic() + 1
            result = started
            while result["status"] not in {"completed", "failed"}:
                if time.monotonic() >= deadline:
                    self.fail("token persistence worker did not finish")
                time.sleep(0.01)
                result = manager.status(88, started["session_id"])

        self.assertEqual(result["status"], "completed")

    def test_blocked_provider_does_not_publish_cleanup_or_release_slot_early(self):
        provider_slot = threading.BoundedSemaphore(1)
        runner_started = threading.Event()
        release_runner = threading.Event()
        actual_provider_finished = threading.Event()

        def automatic_runner(_account_id, _exchange_code, broker):
            broker.mark_provider_started()
            runner_started.set()
            try:
                release_runner.wait(timeout=2)
                return {"refresh_token": "late-rt"}
            finally:
                actual_provider_finished.set()

        manager = ChatGPTPhoneVerificationManager(
            automatic_flow_runner=automatic_runner,
            token_persister=lambda *_args: None,
            status_refresher=lambda _account_id: None,
            start_timeout_seconds=0.01,
            command_timeout_seconds=0.05,
        )

        with mock.patch(
            "services.chatgpt_phone_verification.leadbee_phone_flow_lock",
            provider_slot,
        ):
            started = manager.start(87, leadbee_code="bei-sms-BLOCKED-RUNNER")
            self.assertTrue(runner_started.wait(timeout=1))
            cancelled = manager.cancel(87, started["session_id"])

            self.assertEqual(cancelled["status"], "failed")
            self.assertFalse(cancelled["provider_cleanup_settled"])
            self.assertFalse(actual_provider_finished.is_set())
            self.assertFalse(provider_slot.acquire(blocking=False))

            release_runner.set()
            cleanup_deadline = time.monotonic() + 1
            snapshot = cancelled
            while not snapshot["provider_cleanup_settled"]:
                if time.monotonic() >= cleanup_deadline:
                    self.fail("provider execution did not publish cleanup after returning")
                time.sleep(0.01)
                snapshot = manager.wait_for_provider_cleanup(
                    87,
                    started["session_id"],
                    timeout=0.01,
                )

            self.assertTrue(actual_provider_finished.is_set())
            self.assertTrue(provider_slot.acquire(blocking=False))
            provider_slot.release()

    def test_worker_start_failure_rolls_back_dead_session_mapping(self):
        manager = ChatGPTPhoneVerificationManager(start_timeout_seconds=0.01)
        worker = mock.Mock()
        worker.start.side_effect = RuntimeError("thread start failed")

        with mock.patch(
            "services.chatgpt_phone_verification.threading.Thread",
            return_value=worker,
        ):
            with self.assertRaisesRegex(RuntimeError, "thread start failed"):
                manager.start(89, leadbee_code="bei-sms-THREAD-FAIL")

        self.assertEqual(manager._sessions, {})
        self.assertEqual(manager._account_sessions, {})

    def test_worker_checkpoint_self_enforces_broker_expiry(self):
        broker = InteractivePhoneVerificationBroker(
            account_id=90,
            provider="leadbee",
        )
        broker.expires_at = time.time() - 1

        with self.assertRaises(PhoneVerificationCancelled):
            broker.raise_if_cancelled()

        snapshot = broker.snapshot()
        self.assertEqual(snapshot["status"], "failed")
        self.assertIn("已过期", snapshot["message"])

    def test_provider_slot_wait_expires_without_status_polling(self):
        clock = {"now": 100.0}
        acquire_calls = 0

        class BusyProviderSlots:
            def acquire(self, **_kwargs):
                nonlocal acquire_calls
                acquire_calls += 1
                clock["now"] += 61.0
                if acquire_calls > 1:
                    raise AssertionError("provider slot wait did not honor expiry")
                return False

            def release(self):
                raise AssertionError("unacquired provider slot was released")

        runner = mock.Mock()
        manager = ChatGPTPhoneVerificationManager(
            automatic_flow_runner=runner,
            token_persister=lambda *_args: None,
            status_refresher=lambda _account_id: None,
            ttl_seconds=120,
            start_timeout_seconds=1,
        )

        with mock.patch(
            "services.chatgpt_phone_verification.leadbee_phone_flow_lock",
            BusyProviderSlots(),
        ), mock.patch(
            "services.chatgpt_phone_verification.time.time",
            side_effect=lambda: clock["now"],
        ):
            result = manager.start(91, leadbee_code="bei-sms-SLOT-DEADLINE")

        self.assertEqual(result["status"], "failed")
        self.assertIn("排队", result["message"])
        self.assertIn("超时", result["message"])
        self.assertTrue(result["provider_cleanup_settled"])
        runner.assert_not_called()

    def test_unsettled_automatic_session_is_not_removed(self):
        manager = ChatGPTPhoneVerificationManager(command_timeout_seconds=0.01)
        broker = InteractivePhoneVerificationBroker(
            account_id=92,
            provider="leadbee",
        )
        manager._sessions[broker.session_id] = broker
        manager._account_sessions[broker.account_id] = broker.session_id

        with manager._lock:
            removed = manager._remove_session_locked(broker.session_id)

        self.assertFalse(removed)
        self.assertIn(broker.session_id, manager._sessions)
        broker.mark_exchange_code_active_unknown("provider state unknown")
        broker.mark_provider_cleanup_settled()

        with manager._lock:
            removed = manager._remove_session_locked(broker.session_id)

        self.assertTrue(removed)
        self.assertNotIn(broker.session_id, manager._sessions)

    def test_expired_status_preserves_active_unknown_settlement(self):
        manager = ChatGPTPhoneVerificationManager(command_timeout_seconds=0.01)
        broker = InteractivePhoneVerificationBroker(
            account_id=93,
            provider="leadbee",
        )
        broker.mark_exchange_code_active_unknown("provider state unknown")
        broker.mark_provider_cleanup_settled()
        broker.expires_at = time.time() - 1
        manager._sessions[broker.session_id] = broker
        manager._account_sessions[broker.account_id] = broker.session_id

        result = manager.status(broker.account_id, broker.session_id)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["exchange_code_settlement"], "active_unknown")
        self.assertTrue(result["provider_cleanup_settled"])

    def setUp(self):
        self.persisted = []
        self.refreshed = []

        def runner(account_id, phone, broker):
            broker.mark_code_sent(phone)
            while True:
                command = broker.wait_for_command()
                if command.kind == "resend":
                    broker.resolve_command(command.id, ok=True, message="验证码已重新发送")
                    continue
                if command.payload != "654321":
                    broker.resolve_command(command.id, ok=False, message="手机号验证码错误")
                    continue
                broker.mark_phone_verified()
                broker.resolve_command(command.id, ok=True, message="手机号验证通过")
                return {"access_token": "new-at", "refresh_token": "new-rt"}

        self.manager = ChatGPTPhoneVerificationManager(
            flow_runner=runner,
            token_persister=lambda account_id, tokens: self.persisted.append((account_id, tokens)),
            status_refresher=lambda account_id: self.refreshed.append(account_id),
            ttl_seconds=30,
            resend_cooldown_seconds=0,
            start_timeout_seconds=2,
            command_timeout_seconds=2,
        )

    def test_start_reuses_identical_active_session_without_sending_again(self):
        first = self.manager.start(7, "+447456344799")

        resumed = self.manager.start(7, "+447456344799")

        self.assertEqual(first["status"], "code_sent")
        self.assertEqual(resumed["session_id"], first["session_id"])
        self.assertEqual(resumed["phone"], "+447456344799")
        self.assertTrue(resumed["reused"])
        self.assertIn("未重复发送", resumed["message"])

    def test_start_rejects_different_phone_for_active_manual_session(self):
        first = self.manager.start(19, "+447456344799")

        try:
            with self.assertRaisesRegex(ValueError, "不同接码请求"):
                self.manager.start(19, "+447456344798")
        finally:
            self.manager.cancel(19, first["session_id"])

    def test_start_rejects_different_leadbee_code_for_active_session(self):
        runner_entered = threading.Event()
        release_runner = threading.Event()

        def automatic_runner(_account_id, _exchange_code, _broker):
            runner_entered.set()
            release_runner.wait(timeout=2)
            return {"access_token": "new-at", "refresh_token": "new-rt"}

        manager = ChatGPTPhoneVerificationManager(
            automatic_flow_runner=automatic_runner,
            token_persister=lambda *_args: None,
            status_refresher=lambda _account_id: None,
            start_timeout_seconds=0.01,
        )
        first = manager.start(20, leadbee_code="bei-sms-FIRST-CODE")
        self.assertTrue(runner_entered.wait(timeout=1))

        try:
            with self.assertRaisesRegex(ValueError, "不同接码请求") as ctx:
                manager.start(20, leadbee_code="bei-sms-SECOND-CODE")
            self.assertNotIn("FIRST", str(ctx.exception))
            self.assertNotIn("SECOND", str(ctx.exception))
        finally:
            manager.cancel(20, first["session_id"])
            release_runner.set()

    def test_start_rejects_cross_provider_request_for_active_session(self):
        first = self.manager.start(21, "+447456344799")

        try:
            with self.assertRaisesRegex(ValueError, "不同接码请求"):
                self.manager.start(21, leadbee_code="bei-sms-CROSS-MODE")
        finally:
            self.manager.cancel(21, first["session_id"])

    def test_invalid_code_keeps_session_and_valid_code_completes(self):
        started = self.manager.start(8, "+447456344799")

        invalid = self.manager.submit_code(8, started["session_id"], "111111")
        self.assertEqual(invalid["status"], "code_sent")
        self.assertEqual(invalid["message"], "手机号验证码错误")
        self.assertEqual(self.persisted, [])

        completed = self.manager.submit_code(8, started["session_id"], "654321")
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(self.persisted[0][0], 8)
        self.assertEqual(self.persisted[0][1]["refresh_token"], "new-rt")
        self.assertEqual(self.refreshed, [8])

    def test_resend_uses_same_session(self):
        started = self.manager.start(9, "+447456344799")

        resent = self.manager.resend(9, started["session_id"])

        self.assertEqual(resent["status"], "code_sent")
        self.assertEqual(resent["message"], "验证码已重新发送")

    def test_expired_session_rejects_commands(self):
        broker = InteractivePhoneVerificationBroker(
            account_id=10,
            phone="+447456344799",
            ttl_seconds=0,
            resend_cooldown_seconds=0,
        )

        with self.assertRaisesRegex(ValueError, "已过期"):
            broker.request_command("submit", "654321", timeout=0.1)

    def test_leadbee_session_runs_automatic_flow_without_manual_code(self):
        persisted = []
        refreshed = []

        def automatic_runner(account_id, exchange_code, broker):
            self.assertEqual(account_id, 11)
            self.assertEqual(exchange_code, "bei-sms-DEMO-CODE")
            broker.mark_phone_acquired("+447456344799")
            broker.mark_automatic_sms_sent("+447456344799")
            broker.mark_automatic_code_received()
            broker.mark_phone_verified()
            return {"access_token": "new-at", "refresh_token": "new-rt"}

        manager = ChatGPTPhoneVerificationManager(
            flow_runner=lambda *_args: self.fail("manual runner should not run"),
            automatic_flow_runner=automatic_runner,
            token_persister=lambda account_id, tokens: persisted.append((account_id, tokens)),
            status_refresher=lambda account_id: refreshed.append(account_id),
            start_timeout_seconds=2,
        )

        result = manager.start(11, leadbee_code=" bei-sms-DEMO-CODE ")

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["provider"], "leadbee")
        self.assertTrue(result["automatic"])
        self.assertEqual(result["phone"], "+447456344799")
        self.assertTrue(result["phone_verified"])
        self.assertTrue(result["exchange_code_consumed"])
        self.assertEqual(persisted[0][1]["refresh_token"], "new-rt")
        self.assertEqual(refreshed, [11])

    def test_leadbee_automatic_flows_can_run_in_parallel(self):
        first_started = threading.Event()
        second_started = threading.Event()
        release_first = threading.Event()

        def automatic_runner(account_id, _exchange_code, _broker):
            if account_id == 31:
                first_started.set()
                release_first.wait(timeout=2)
            else:
                second_started.set()
            return {"refresh_token": f"rt-{account_id}"}

        manager = ChatGPTPhoneVerificationManager(
            automatic_flow_runner=automatic_runner,
            token_persister=lambda *_args: None,
            status_refresher=lambda _account_id: None,
            start_timeout_seconds=0.01,
        )

        first = manager.start(31, leadbee_code="bei-sms-FIRST-SERIAL")
        self.assertTrue(first_started.wait(timeout=1))
        second = manager.start(32, leadbee_code="bei-sms-SECOND-SERIAL")
        try:
            self.assertTrue(second_started.wait(timeout=0.5))
        finally:
            release_first.set()

        deadline = time.monotonic() + 2
        while not second_started.is_set() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(second_started.is_set())
        first_status = manager.status(31, first["session_id"])
        while (
            first_status["status"] not in {"completed", "failed", "expired"}
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
            first_status = manager.status(31, first["session_id"])
        self.assertEqual(
            first_status["status"],
            "completed",
        )

    def test_persisted_leadbee_flow_stays_completed_when_status_refresh_has_tls_error(self):
        persisted = []

        def automatic_runner(_account_id, _exchange_code, broker):
            broker.mark_phone_acquired("+447456344799")
            broker.mark_automatic_sms_sent("+447456344799")
            broker.mark_automatic_code_received()
            broker.mark_phone_verified()
            return {"access_token": "new-at", "refresh_token": "new-rt"}

        manager = ChatGPTPhoneVerificationManager(
            automatic_flow_runner=automatic_runner,
            token_persister=lambda account_id, tokens: persisted.append(
                (account_id, tokens)
            ),
            status_refresher=lambda _account_id: (_ for _ in ()).throw(
                RuntimeError("curl: (35) TLS connect error")
            ),
            start_timeout_seconds=2,
        )

        result = manager.start(16, leadbee_code="bei-sms-TLS-RETRY")
        deadline = time.monotonic() + 2
        while result["status"] not in {"completed", "failed", "expired"}:
            if time.monotonic() >= deadline:
                self.fail("phone verification did not reach a terminal state")
            time.sleep(0.01)
            result = manager.status(16, result["session_id"])

        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["phone_verified"])
        self.assertTrue(result["exchange_code_consumed"])
        self.assertEqual(persisted[0][1]["refresh_token"], "new-rt")
        self.assertIn("状态刷新稍后重试", "\n".join(result["logs"]))

    def test_cancel_during_token_persistence_cannot_mark_consumed_flow_failed(self):
        persister_entered = threading.Event()
        release_persister = threading.Event()

        def automatic_runner(_account_id, _exchange_code, broker):
            broker.mark_phone_acquired("+447456344799")
            broker.mark_automatic_sms_sent("+447456344799")
            broker.mark_automatic_code_received()
            broker.mark_phone_verified()
            return {"access_token": "new-at", "refresh_token": "new-rt"}

        def token_persister(_account_id, _tokens):
            persister_entered.set()
            release_persister.wait(timeout=2)

        manager = ChatGPTPhoneVerificationManager(
            automatic_flow_runner=automatic_runner,
            token_persister=token_persister,
            status_refresher=lambda _account_id: None,
            start_timeout_seconds=0.01,
        )

        started = manager.start(17, leadbee_code="bei-sms-PERSIST-RACE")
        self.assertTrue(persister_entered.wait(timeout=1))
        try:
            cancelled = manager.cancel(17, started["session_id"])
            self.assertEqual(cancelled["status"], "persisting")
            self.assertTrue(cancelled["phone_verified"])
            self.assertTrue(cancelled["exchange_code_consumed"])
        finally:
            release_persister.set()

        completed = manager._get(17, started["session_id"]).wait_until_terminal(1)
        self.assertEqual(completed["status"], "completed")

    def test_expired_deadline_keeps_session_that_is_already_persisting(self):
        persister_entered = threading.Event()
        release_persister = threading.Event()

        def token_persister(_account_id, _tokens):
            persister_entered.set()
            release_persister.wait(timeout=2)

        manager = ChatGPTPhoneVerificationManager(
            automatic_flow_runner=lambda *_args: {"refresh_token": "new-rt"},
            token_persister=token_persister,
            status_refresher=lambda _account_id: None,
            start_timeout_seconds=0.01,
        )

        account_id = 25
        started = manager.start(account_id, leadbee_code="bei-sms-PERSIST-EXPIRY")
        self.assertTrue(persister_entered.wait(timeout=1))
        session_id = started["session_id"]
        broker = manager._sessions[session_id]
        broker.expires_at = time.time() - 1

        try:
            status = manager.status(account_id, session_id)
            self.assertEqual(status["status"], "persisting")
            self.assertEqual(manager._account_sessions[account_id], session_id)
        finally:
            release_persister.set()

        completed = broker.wait_until_terminal(1)
        self.assertEqual(completed["status"], "completed")

    def test_cancel_during_status_refresh_keeps_persisted_flow_completed(self):
        refresher_entered = threading.Event()
        release_refresher = threading.Event()

        def automatic_runner(_account_id, _exchange_code, broker):
            broker.mark_phone_acquired("+447456344799")
            broker.mark_automatic_sms_sent("+447456344799")
            broker.mark_automatic_code_received()
            broker.mark_phone_verified()
            return {"access_token": "new-at", "refresh_token": "new-rt"}

        def status_refresher(_account_id):
            refresher_entered.set()
            release_refresher.wait(timeout=2)

        manager = ChatGPTPhoneVerificationManager(
            automatic_flow_runner=automatic_runner,
            token_persister=lambda _account_id, _tokens: None,
            status_refresher=status_refresher,
            start_timeout_seconds=0.01,
        )

        started = manager.start(18, leadbee_code="bei-sms-REFRESH-RACE")
        self.assertTrue(refresher_entered.wait(timeout=1))
        try:
            cancelled = manager.cancel(18, started["session_id"])
            self.assertEqual(cancelled["status"], "completed")
        finally:
            release_refresher.set()

        completed = manager.status(18, started["session_id"])
        self.assertEqual(completed["status"], "completed")

    def test_leadbee_rt_without_add_phone_reports_exchange_code_unused(self):
        persisted = []
        refreshed = []

        def automatic_runner(_account_id, _exchange_code, _broker):
            return {"access_token": "new-at", "refresh_token": "new-rt"}

        manager = ChatGPTPhoneVerificationManager(
            automatic_flow_runner=automatic_runner,
            token_persister=lambda account_id, tokens: persisted.append((account_id, tokens)),
            status_refresher=lambda account_id: refreshed.append(account_id),
            start_timeout_seconds=2,
        )

        result = manager.start(12, leadbee_code="bei-sms-UNUSED-CODE")

        self.assertEqual(result["status"], "completed")
        self.assertFalse(result["phone_verified"])
        self.assertFalse(result["exchange_code_consumed"])
        self.assertEqual(result["phone"], "")
        self.assertIn("未要求新增手机号", result["message"])
        self.assertIn("兑换码未使用", result["message"])
        self.assertFalse(persisted[0][1]["_phone_verified"])
        self.assertFalse(persisted[0][1]["_exchange_code_consumed"])
        self.assertEqual(refreshed, [12])

    def test_leadbee_start_requires_exchange_code(self):
        with self.assertRaisesRegex(ValueError, "兑换码"):
            self.manager.start(12, leadbee_code="   ")

    def test_leadbee_runner_error_redacts_exchange_code_from_api_snapshot(self):
        exchange_code = "bei-sms-SECRET-CODE"

        def automatic_runner(_account_id, received_code, _broker):
            raise RuntimeError(f"provider rejected {received_code}")

        manager = ChatGPTPhoneVerificationManager(
            automatic_flow_runner=automatic_runner,
            token_persister=lambda *_args: None,
            status_refresher=lambda _account_id: None,
            start_timeout_seconds=1,
        )

        result = manager.start(22, leadbee_code=exchange_code)

        self.assertEqual(result["status"], "failed")
        self.assertNotIn(exchange_code, result["message"])
        self.assertNotIn(exchange_code, "\n".join(result["logs"]))
        self.assertIn("卡密已隐藏", result["message"])

    def test_cancelled_automatic_flow_never_persists_late_tokens(self):
        runner_started = threading.Event()
        release_runner = threading.Event()
        persisted = threading.Event()

        def automatic_runner(_account_id, _exchange_code, _broker):
            runner_started.set()
            release_runner.wait(timeout=1)
            return {"access_token": "late-at", "refresh_token": "late-rt"}

        manager = ChatGPTPhoneVerificationManager(
            automatic_flow_runner=automatic_runner,
            token_persister=lambda *_args: persisted.set(),
            status_refresher=lambda _account_id: None,
            start_timeout_seconds=0.01,
        )

        started = manager.start(13, leadbee_code="bei-sms-CANCEL-CODE")
        self.assertTrue(runner_started.wait(timeout=1))

        cancelled = manager.cancel(13, started["session_id"])
        release_runner.set()

        self.assertEqual(cancelled["status"], "failed")
        self.assertIn("取消", cancelled["message"])
        self.assertFalse(persisted.wait(timeout=0.2))

    def test_cancel_waits_for_automatic_provider_cleanup_and_restoration_callback(self):
        runner_started = threading.Event()
        cleanup_started = threading.Event()
        allow_restoration = threading.Event()
        cancel_returned = threading.Event()
        cancel_result = {}

        def automatic_runner(_account_id, _exchange_code, broker):
            runner_started.set()
            self.assertTrue(broker.wait_for_cancellation(timeout=1))
            cleanup_started.set()
            self.assertTrue(allow_restoration.wait(timeout=1))
            broker.mark_exchange_code_restored()
            raise RuntimeError("手机验证已取消")

        manager = ChatGPTPhoneVerificationManager(
            automatic_flow_runner=automatic_runner,
            token_persister=lambda *_args: None,
            status_refresher=lambda _account_id: None,
            start_timeout_seconds=0.01,
            command_timeout_seconds=1,
        )
        started = manager.start(27, leadbee_code="bei-sms-CLEANUP-RACE")
        self.assertTrue(runner_started.wait(timeout=1))

        def cancel_session():
            cancel_result.update(manager.cancel(27, started["session_id"]))
            cancel_returned.set()

        cancel_thread = threading.Thread(target=cancel_session)
        cancel_thread.start()
        try:
            self.assertTrue(cleanup_started.wait(timeout=1))
            self.assertFalse(cancel_returned.wait(timeout=0.05))
            allow_restoration.set()
            cancel_thread.join(timeout=1)
        finally:
            allow_restoration.set()
            cancel_thread.join(timeout=1)

        self.assertFalse(cancel_thread.is_alive())
        self.assertTrue(cancel_result["provider_cleanup_settled"])
        self.assertTrue(cancel_result["exchange_code_restoration_confirmed"])
        self.assertFalse(cancel_result["exchange_code_unusable"])

    def test_expired_status_cancels_before_late_tokens_and_retains_snapshot(self):
        runner_started = threading.Event()
        release_runner = threading.Event()
        persisted = threading.Event()
        worker_threads = []
        real_thread_class = threading.Thread

        def automatic_runner(_account_id, _exchange_code, _broker):
            runner_started.set()
            release_runner.wait(timeout=2)
            return {"access_token": "late-at", "refresh_token": "late-rt"}

        def create_worker(*args, **kwargs):
            worker = real_thread_class(*args, **kwargs)
            worker_threads.append(worker)
            return worker

        manager = ChatGPTPhoneVerificationManager(
            automatic_flow_runner=automatic_runner,
            token_persister=lambda *_args: persisted.set(),
            status_refresher=lambda _account_id: None,
            start_timeout_seconds=0.01,
        )

        account_id = 24
        with mock.patch(
            "services.chatgpt_phone_verification.threading.Thread",
            side_effect=create_worker,
        ):
            started = manager.start(account_id, leadbee_code="bei-sms-EXPIRED-CODE")
        self.assertEqual(len(worker_threads), 1)
        worker = worker_threads[0]
        self.assertTrue(runner_started.wait(timeout=1))
        session_id = started["session_id"]
        manager._sessions[session_id].expires_at = time.time() - 1

        try:
            expired = manager.status(account_id, session_id)
            self.assertEqual(expired["status"], "failed")
            self.assertIn("已过期", expired["message"])
            self.assertFalse(expired["provider_cleanup_settled"])
            self.assertIn(session_id, manager._sessions)
            self.assertEqual(manager._account_sessions[account_id], session_id)
        finally:
            release_runner.set()
            worker.join(timeout=1)

        self.assertFalse(worker.is_alive())
        self.assertFalse(persisted.is_set())
        self.assertIn(session_id, manager._sessions)
        self.assertTrue(
            manager._sessions[session_id]
            .snapshot()["provider_cleanup_settled"]
        )
        self.assertNotIn(account_id, manager._account_sessions)

    def test_status_expiry_between_get_and_snapshot_retires_before_late_tokens(self):
        runner_started = threading.Event()
        release_runner = threading.Event()
        persisted = threading.Event()
        worker_threads = []
        real_thread_class = threading.Thread

        def automatic_runner(_account_id, _exchange_code, _broker):
            runner_started.set()
            release_runner.wait(timeout=2)
            return {"access_token": "late-at", "refresh_token": "late-rt"}

        def create_worker(*args, **kwargs):
            worker = real_thread_class(*args, **kwargs)
            worker_threads.append(worker)
            return worker

        manager = ChatGPTPhoneVerificationManager(
            automatic_flow_runner=automatic_runner,
            token_persister=lambda *_args: persisted.set(),
            status_refresher=lambda _account_id: None,
            start_timeout_seconds=0.01,
        )

        account_id = 26
        with mock.patch(
            "services.chatgpt_phone_verification.threading.Thread",
            side_effect=create_worker,
        ):
            started = manager.start(account_id, leadbee_code="bei-sms-TOCTOU-CODE")
        self.assertEqual(len(worker_threads), 1)
        worker = worker_threads[0]
        self.assertTrue(runner_started.wait(timeout=1))
        session_id = started["session_id"]
        broker = manager._sessions[session_id]
        original_snapshot = broker.snapshot
        snapshot_statuses = []

        def snapshot_then_expire():
            snapshot = original_snapshot()
            snapshot_statuses.append(snapshot["status"])
            if len(snapshot_statuses) == 1:
                broker.expires_at = time.time() - 1
            return snapshot

        try:
            with mock.patch.object(broker, "snapshot", side_effect=snapshot_then_expire):
                status = manager.status(account_id, session_id)
                self.assertEqual(status["status"], "failed")
                self.assertIn("已过期", status["message"])
                self.assertFalse(status["provider_cleanup_settled"])
                self.assertIn(session_id, manager._sessions)
        finally:
            release_runner.set()
            worker.join(timeout=1)

        self.assertNotEqual(snapshot_statuses[0], "expired")
        self.assertIn("expired", snapshot_statuses[1:])
        self.assertFalse(worker.is_alive())
        self.assertFalse(persisted.is_set())
        self.assertIn(session_id, manager._sessions)
        self.assertTrue(
            manager._sessions[session_id]
            .snapshot()["provider_cleanup_settled"]
        )
        self.assertNotIn(account_id, manager._account_sessions)

    def test_expired_replaced_session_cannot_persist_over_new_request(self):
        first_runner_started = threading.Event()
        release_first_runner = threading.Event()
        first_persisted = threading.Event()
        persisted_refresh_tokens = []

        def automatic_runner(_account_id, exchange_code, _broker):
            if exchange_code == "bei-sms-OLD-CODE":
                first_runner_started.set()
                release_first_runner.wait(timeout=2)
                return {"access_token": "old-at", "refresh_token": "old-rt"}
            return {"access_token": "new-at", "refresh_token": "new-rt"}

        def token_persister(_account_id, tokens):
            refresh_token = tokens["refresh_token"]
            persisted_refresh_tokens.append(refresh_token)
            if refresh_token == "old-rt":
                first_persisted.set()

        manager = ChatGPTPhoneVerificationManager(
            automatic_flow_runner=automatic_runner,
            token_persister=token_persister,
            status_refresher=lambda _account_id: None,
            ttl_seconds=30,
            start_timeout_seconds=0.01,
        )
        first = manager.start(23, leadbee_code="bei-sms-OLD-CODE")
        self.assertTrue(first_runner_started.wait(timeout=1))
        first_broker = manager._sessions[first["session_id"]]
        first_broker.expires_at = time.time() - 1

        with self.assertRaisesRegex(ValueError, "仍在清理服务端终态"):
            manager.start(23, leadbee_code="bei-sms-NEW-CODE")
        self.assertFalse(first_broker.snapshot()["provider_cleanup_settled"])
        release_first_runner.set()
        cleanup_deadline = time.monotonic() + 1
        while not first_broker.snapshot()["provider_cleanup_settled"]:
            if time.monotonic() >= cleanup_deadline:
                self.fail("expired provider worker did not settle cleanup")
            time.sleep(0.01)

        second = manager.start(23, leadbee_code="bei-sms-NEW-CODE")
        try:
            self.assertIn(second["status"], {"starting", "completed"})
        finally:
            release_first_runner.set()

        deadline = time.monotonic() + 2
        while second["status"] not in {"completed", "failed", "expired"}:
            if time.monotonic() >= deadline:
                self.fail("replacement phone verification did not complete")
            time.sleep(0.01)
            second = manager.status(23, second["session_id"])

        self.assertEqual(second["status"], "completed")
        self.assertFalse(first_persisted.wait(timeout=0.2))
        self.assertEqual(persisted_refresh_tokens, ["new-rt"])

    def test_terminal_sessions_are_pruned_after_the_retention_window(self):
        manager = ChatGPTPhoneVerificationManager(
            automatic_flow_runner=lambda *_args: {"refresh_token": "new-rt"},
            token_persister=lambda *_args: None,
            status_refresher=lambda _account_id: None,
            start_timeout_seconds=1,
            terminal_retention_seconds=0,
        )

        first = manager.start(14, leadbee_code="bei-sms-FIRST-CODE")
        self.assertEqual(first["status"], "completed")
        manager.start(15, leadbee_code="bei-sms-SECOND-CODE")

        with self.assertRaisesRegex(ValueError, "会话不存在"):
            manager.status(14, first["session_id"])


class PhoneVerificationApiTests(unittest.TestCase):
    def test_start_request_rejects_client_order_and_credentials(self):
        from api.chatgpt import PhoneVerificationStartRequest

        for field in (
            "client_order_id",
            "leadbee_api_key",
            "leadbee_api_secret",
        ):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                PhoneVerificationStartRequest(
                    leadbee_api=True,
                    **{field: "fixture-value"},
                )

    def test_chatgpt_phone_routes_are_registered(self):
        from main import app

        paths = set(app.openapi()["paths"])

        self.assertIn("/api/chatgpt/{account_id}/phone-verification/start", paths)
        self.assertIn(
            "/api/chatgpt/{account_id}/phone-verification/{session_id}/submit",
            paths,
        )
        self.assertIn(
            "/api/chatgpt/{account_id}/phone-verification/{session_id}/resend",
            paths,
        )

    def test_start_endpoint_requires_existing_access_token(self):
        from api.chatgpt import PhoneVerificationStartRequest, start_phone_verification

        account = mock.Mock()
        account.platform = "chatgpt"
        account.email = "existing@example.com"
        account.token = ""
        account.get_extra.return_value = {"access_token": "", "refresh_token": ""}
        session = mock.Mock()
        session.get.return_value = account

        with self.assertRaises(HTTPException) as ctx:
            start_phone_verification(
                3,
                PhoneVerificationStartRequest(phone="+447456344799"),
                session,
            )

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("Access Token", ctx.exception.detail)

    def test_start_endpoint_dispatches_manager(self):
        from api.chatgpt import PhoneVerificationStartRequest, start_phone_verification

        account = mock.Mock()
        account.platform = "chatgpt"
        account.email = "existing@example.com"
        account.token = "existing-at"
        account.get_extra.return_value = {
            "access_token": "existing-at",
            "refresh_token": "",
        }
        session = mock.Mock()
        session.get.return_value = account

        with mock.patch(
            "api.chatgpt.phone_verification_manager.start",
            return_value={"session_id": "phone-session", "status": "starting"},
        ) as start_mock:
            result = start_phone_verification(
                4,
                PhoneVerificationStartRequest(phone="+447456344799"),
                session,
            )

        self.assertEqual(result["session_id"], "phone-session")
        start_mock.assert_called_once_with(4, "+447456344799")

    def test_start_endpoint_dispatches_leadbee_exchange_code(self):
        from api.chatgpt import PhoneVerificationStartRequest, start_phone_verification

        account = mock.Mock()
        account.platform = "chatgpt"
        account.email = "existing@example.com"
        account.token = "existing-at"
        account.get_extra.return_value = {
            "access_token": "existing-at",
            "refresh_token": "",
        }
        session = mock.Mock()
        session.get.return_value = account

        with mock.patch(
            "api.chatgpt.phone_verification_manager.start",
            return_value={"session_id": "leadbee-session", "status": "starting"},
        ) as start_mock:
            result = start_phone_verification(
                5,
                PhoneVerificationStartRequest(leadbee_code="bei-sms-DEMO-CODE"),
                session,
            )

        self.assertEqual(result["session_id"], "leadbee-session")
        start_mock.assert_called_once_with(
            5,
            leadbee_code="bei-sms-DEMO-CODE",
        )

    def test_start_endpoint_dispatches_explicit_leadbee_api_mode(self):
        from api.chatgpt import PhoneVerificationStartRequest, start_phone_verification

        account = mock.Mock()
        account.platform = "chatgpt"
        account.email = "existing@example.com"
        account.token = "existing-at"
        account.get_extra.return_value = {
            "access_token": "existing-at",
            "refresh_token": "",
        }
        session = mock.Mock()
        session.get.return_value = account

        with mock.patch(
            "api.chatgpt.phone_verification_manager.start",
            return_value={"session_id": "api-session", "status": "starting"},
        ) as start_mock:
            result = start_phone_verification(
                6,
                PhoneVerificationStartRequest(leadbee_api=True),
                session,
            )

        self.assertEqual(result["session_id"], "api-session")
        start_mock.assert_called_once_with(6, leadbee_api=True)

    def test_start_endpoint_rejects_mixed_leadbee_api_modes(self):
        from api.chatgpt import PhoneVerificationStartRequest, start_phone_verification

        account = mock.Mock()
        account.platform = "chatgpt"
        account.email = "existing@example.com"
        account.token = "existing-at"
        account.get_extra.return_value = {
            "access_token": "existing-at",
            "refresh_token": "",
        }
        session = mock.Mock()
        session.get.return_value = account

        for body in (
            PhoneVerificationStartRequest(
                phone="+447456344799",
                leadbee_api=True,
            ),
            PhoneVerificationStartRequest(
                leadbee_code="bei-sms-DEMO-CODE",
                leadbee_api=True,
            ),
        ):
            with self.subTest(body=body), self.assertRaises(HTTPException) as ctx:
                start_phone_verification(6, body, session)
            self.assertEqual(ctx.exception.status_code, 400)
            self.assertIn("只能选择一种", ctx.exception.detail)


if __name__ == "__main__":
    unittest.main()
