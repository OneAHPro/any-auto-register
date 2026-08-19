import sys
import types
import unittest
from unittest import mock

smstome_tool_stub = types.ModuleType("smstome_tool")
smstome_tool_stub.PhoneEntry = type("PhoneEntry", (), {})
smstome_tool_stub.get_unused_phone = lambda *args, **kwargs: None
smstome_tool_stub.mark_phone_blacklisted = lambda *args, **kwargs: None
smstome_tool_stub.parse_country_slugs = lambda value: []
smstome_tool_stub.update_global_phone_list = lambda *args, **kwargs: 0
smstome_tool_stub.wait_for_otp = lambda *args, **kwargs: None
sys.modules.setdefault("smstome_tool", smstome_tool_stub)

from platforms.chatgpt.oauth_client import OAuthClient
from platforms.chatgpt.chatgpt_client import ChatGPTClient
from platforms.chatgpt.refresh_token_registration_engine import (
    RefreshTokenRegistrationEngine,
)
from platforms.chatgpt.utils import FlowState
from core import base_mailbox
from core.task_runtime import StopTaskRequested


class DummyEmailService:
    service_type = type("ST", (), {"value": "dummy"})()

    def create_email(self):
        return {"email": "user@example.com", "service_id": "svc-1"}

    def get_verification_code(self, **kwargs):
        return "123456"


class RefreshTokenRegistrationEngineTests(unittest.TestCase):
    def _make_engine(self, **kwargs):
        return RefreshTokenRegistrationEngine(
            email_service=DummyEmailService(),
            proxy_url="http://127.0.0.1:7890",
            callback_logger=lambda msg: None,
            max_retries=1,
            **kwargs,
        )

    def test_oauth_logs_are_sanitized_before_every_engine_sink(self):
        callback_messages = []
        engine = RefreshTokenRegistrationEngine(
            email_service=DummyEmailService(),
            callback_logger=callback_messages.append,
        )
        oauth_client = engine._build_oauth_client()
        sensitive_message = (
            '{"access_token":"source-access-secret"} '
            "Authorization: Bearer source-bearer-secret "
            "authorization_code=source-auth-secret OTP: SOURCE7"
        )

        with mock.patch(
            "platforms.chatgpt.refresh_token_registration_engine.logger.info"
        ) as logger_info:
            oauth_client._log(sensitive_message)

        rendered = "\n".join(engine.logs + callback_messages)
        rendered += "\n" + "\n".join(
            str(call.args[0]) for call in logger_info.call_args_list
        )
        for secret in (
            "source-access-secret",
            "source-bearer-secret",
            "source-auth-secret",
            "SOURCE7",
        ):
            self.assertNotIn(secret, rendered)
        self.assertIn("已隐藏", rendered)

    @mock.patch("platforms.chatgpt.refresh_token_registration_engine.OAuthManager")
    @mock.patch("platforms.chatgpt.refresh_token_registration_engine.OAuthClient")
    @mock.patch("platforms.chatgpt.refresh_token_registration_engine.ChatGPTClient")
    def test_run_hands_registered_session_to_oauth_login(
        self,
        mock_chatgpt_client_cls,
        mock_oauth_client_cls,
        mock_oauth_manager_cls,
    ):
        register_client = mock.Mock()
        register_client.device_id = "device-fixed"
        register_client.ua = "UA"
        register_client.sec_ch_ua = '"Chromium";v="136"'
        register_client.impersonate = "chrome136"
        register_client.session = mock.Mock()
        register_client.session.headers = {"Accept-Language": "en-US"}
        register_client.register_complete_flow.return_value = (
            True,
            "pending_about_you_submission",
        )
        mock_chatgpt_client_cls.return_value = register_client

        oauth_client = mock.Mock()
        oauth_client.config = {}
        oauth_client.login_and_get_tokens.return_value = {
            "access_token": "at",
            "refresh_token": "rt",
            "id_token": "id-token",
            "account_id": "acct-1",
        }
        oauth_client.last_error = ""
        oauth_client.last_workspace_id = "ws-1"
        oauth_client._decode_oauth_session_cookie.return_value = {
            "workspaces": [{"id": "ws-1"}]
        }
        oauth_client._get_cookie_value.return_value = "session-1"
        mock_oauth_client_cls.return_value = oauth_client

        oauth_manager = mock.Mock()
        oauth_manager.extract_account_info.return_value = {
            "email": "user@example.com",
            "account_id": "acct-1",
        }
        mock_oauth_manager_cls.return_value = oauth_manager

        engine = self._make_engine(extra_config={"register_max_retries": 1})
        result = engine.run()

        self.assertTrue(result.success)
        self.assertEqual(result.email, "user@example.com")
        self.assertEqual(result.account_id, "acct-1")
        self.assertEqual(result.workspace_id, "ws-1")
        self.assertEqual(result.refresh_token, "rt")
        self.assertEqual(result.session_token, "session-1")
        self.assertEqual(result.source, "register")

        register_client.register_complete_flow.assert_called_once()
        oauth_client.adopt_browser_context.assert_called_once()
        oauth_client.login_and_get_tokens.assert_called_once()
        login_kwargs = oauth_client.login_and_get_tokens.call_args.kwargs
        self.assertFalse(login_kwargs["allow_phone_verification"])
        self.assertFalse(login_kwargs["force_new_browser"])
        self.assertEqual(login_kwargs["login_source"], "post_register_workspace_continue")

    @mock.patch("platforms.chatgpt.refresh_token_registration_engine.OAuthManager")
    @mock.patch("platforms.chatgpt.refresh_token_registration_engine.OAuthClient")
    @mock.patch("platforms.chatgpt.refresh_token_registration_engine.ChatGPTClient")
    def test_run_switches_to_login_when_register_reports_existing_account(
        self,
        mock_chatgpt_client_cls,
        mock_oauth_client_cls,
        mock_oauth_manager_cls,
    ):
        register_client = mock.Mock()
        register_client.device_id = "device-fixed"
        register_client.ua = "UA"
        register_client.sec_ch_ua = '"Chromium";v="136"'
        register_client.impersonate = "chrome136"
        register_client.register_complete_flow.return_value = (
            False,
            "注册失败: 400 - user_already_exists",
        )
        mock_chatgpt_client_cls.return_value = register_client

        oauth_client = mock.Mock()
        oauth_client.config = {}
        oauth_client.login_and_get_tokens.return_value = {
            "access_token": "at",
            "refresh_token": "rt",
            "id_token": "id-token",
        }
        oauth_client.last_workspace_id = "ws-1"
        oauth_client._decode_oauth_session_cookie.return_value = {
            "workspaces": [{"id": "ws-1"}]
        }
        oauth_client._get_cookie_value.return_value = ""
        mock_oauth_client_cls.return_value = oauth_client

        oauth_manager = mock.Mock()
        oauth_manager.extract_account_info.return_value = {
            "email": "user@example.com",
            "account_id": "acct-existing",
        }
        mock_oauth_manager_cls.return_value = oauth_manager

        engine = self._make_engine()
        result = engine.run()

        self.assertTrue(result.success)
        self.assertEqual(result.source, "login")
        self.assertEqual(result.account_id, "acct-existing")
        register_client.register_complete_flow.assert_called_once()
        login_kwargs = oauth_client.login_and_get_tokens.call_args.kwargs
        self.assertEqual(login_kwargs["login_source"], "existing_account_continue")
        self.assertTrue(login_kwargs["force_new_browser"])
        self.assertEqual(login_kwargs["user_agent"], "UA")

    @mock.patch("platforms.chatgpt.refresh_token_registration_engine.OAuthClient")
    @mock.patch("platforms.chatgpt.refresh_token_registration_engine.ChatGPTClient")
    def test_run_does_not_rotate_email_for_legacy_full_retry_count(
        self,
        mock_chatgpt_client_cls,
        mock_oauth_client_cls,
    ):
        class RotatingEmailService:
            service_type = type("ST", (), {"value": "dummy"})()

            def __init__(self):
                self.index = 0

            def create_email(self):
                self.index += 1
                return {
                    "email": f"user{self.index}@example.com",
                    "service_id": f"svc-{self.index}",
                }

            def get_verification_code(self, **kwargs):
                return "123456"

        register_client = mock.Mock()
        register_client.register_complete_flow.return_value = (
            False,
            "network timeout",
        )
        mock_chatgpt_client_cls.return_value = register_client
        email_service = RotatingEmailService()

        engine = RefreshTokenRegistrationEngine(
            email_service=email_service,
            proxy_url="http://127.0.0.1:7890",
            callback_logger=lambda msg: None,
            max_retries=2,
        )
        result = engine.run()

        self.assertFalse(result.success)
        self.assertEqual(result.email, "user1@example.com")
        self.assertEqual(email_service.index, 1)
        register_client.register_complete_flow.assert_called_once()
        mock_oauth_client_cls.assert_not_called()


class OAuthClientPasswordlessTests(unittest.TestCase):
    def _make_client(self):
        return OAuthClient({}, proxy="http://127.0.0.1:7890", verbose=False)

    def test_submit_signup_register_uses_json_with_authenticated_browser_headers(self):
        client = self._make_client()
        client.session.post = mock.Mock(
            return_value=mock.Mock(status_code=200, url="https://auth.openai.com/api/accounts/user/register")
        )

        with mock.patch(
            "platforms.chatgpt.oauth_client.get_sentinel_token_via_browser",
            return_value="sentinel-demo",
        ), mock.patch(
            "platforms.chatgpt.oauth_client.build_sentinel_token",
            return_value="",
        ):
            ok = client._submit_signup_register(
                "user@example.com",
                "Secret123!",
                "device-fixed",
                user_agent="UA",
                sec_ch_ua='"Chromium";v="136"',
                impersonate="chrome136",
                referer="https://auth.openai.com/create-account/password",
            )

        self.assertTrue(ok)
        kwargs = client.session.post.call_args.kwargs
        self.assertEqual(
            kwargs["json"],
            {"username": "user@example.com", "password": "Secret123!"},
        )
        self.assertNotIn("data", kwargs)
        headers = kwargs["headers"]
        self.assertEqual(headers["Referer"], "https://auth.openai.com/create-account/password")
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(headers["Accept"], "application/json")
        self.assertEqual(headers["openai-sentinel-token"], "sentinel-demo")
        self.assertEqual(headers["Origin"], "https://auth.openai.com")
        self.assertEqual(headers["oai-device-id"], "device-fixed")

    def test_login_and_get_tokens_prefers_passwordless_over_password_verify(self):
        client = self._make_client()
        login_password_state = FlowState(
            page_type="login_password",
            continue_url="https://auth.openai.com/log-in/password",
            current_url="https://auth.openai.com/log-in/password",
        )
        email_otp_state = FlowState(
            page_type="email_otp_verification",
            continue_url="https://auth.openai.com/email-verification",
            current_url="https://auth.openai.com/email-verification",
        )
        consent_state = FlowState(
            page_type="consent",
            continue_url="https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
            current_url="https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
        )

        with mock.patch.object(client, "_bootstrap_oauth_session", return_value="https://auth.openai.com/log-in"), \
            mock.patch.object(client, "_submit_authorize_continue", return_value=login_password_state) as submit_continue, \
            mock.patch.object(client, "_send_passwordless_login_otp", return_value=email_otp_state) as send_passwordless, \
            mock.patch.object(client, "_handle_otp_verification", return_value=consent_state), \
            mock.patch.object(client, "_oauth_submit_workspace_and_org", return_value=("auth-code", None)), \
            mock.patch.object(client, "_exchange_code_for_tokens", return_value={"access_token": "at"}), \
            mock.patch.object(client, "_submit_password_verify") as submit_password:
            tokens = client.login_and_get_tokens(
                "user@example.com",
                "Secret123!",
                "device-fixed",
                user_agent="UA",
                sec_ch_ua='"Chromium";v="136"',
                impersonate="chrome136",
                skymail_client=mock.Mock(),
                prefer_passwordless_login=True,
                allow_phone_verification=False,
            )

        self.assertEqual(tokens["access_token"], "at")
        submit_continue.assert_called_once()
        self.assertEqual(submit_continue.call_args.kwargs["screen_hint"], "login")
        send_passwordless.assert_called_once()
        submit_password.assert_not_called()

    def test_login_and_get_tokens_dispatches_mfa_with_mailbox_and_totp_context(self):
        client = self._make_client()
        mailbox = mock.Mock()
        login_password_state = FlowState(
            page_type="login_password",
            continue_url="https://auth.openai.com/log-in/password",
            current_url="https://auth.openai.com/log-in/password",
        )
        mfa_state = FlowState(
            page_type="mfa_challenge",
            continue_url="https://auth.openai.com/mfa-challenge/factor-1",
            current_url="https://auth.openai.com/mfa-challenge/factor-1",
            payload={
                "factors": [
                    {"id": "factor-1", "factor_type": "totp"},
                ]
            },
        )
        consent_state = FlowState(
            page_type="consent",
            continue_url="https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
            current_url="https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
        )

        with mock.patch.object(client, "_bootstrap_oauth_session", return_value="https://auth.openai.com/log-in"), \
            mock.patch.object(client, "_submit_authorize_continue", return_value=login_password_state), \
            mock.patch.object(client, "_submit_password_verify", return_value=mfa_state) as submit_password, \
            mock.patch.object(client, "_submit_mfa_challenge", return_value=consent_state) as submit_mfa, \
            mock.patch.object(client, "_oauth_submit_workspace_and_org", return_value=("auth-code", None)), \
            mock.patch.object(client, "_exchange_code_for_tokens", return_value={"access_token": "at", "refresh_token": "rt"}), \
            mock.patch.object(client, "_follow_flow_state") as follow_state:
            tokens = client.login_and_get_tokens(
                "user@example.com",
                "Secret123!",
                "device-fixed",
                user_agent="UA",
                sec_ch_ua='"Chromium";v="136"',
                impersonate="chrome136",
                skymail_client=mailbox,
                prefer_passwordless_login=False,
                force_password_login=True,
                totp_secret="JBSWY3DPEHPK3PXP",
                mfa_recovery_code="RECOVERY-CODE",
                allow_phone_verification=False,
            )

        self.assertIsNotNone(tokens)
        self.assertEqual(tokens["refresh_token"], "rt")
        submit_password.assert_called_once()
        submit_mfa.assert_called_once()
        self.assertEqual(submit_mfa.call_args.kwargs["email"], "user@example.com")
        self.assertIs(submit_mfa.call_args.kwargs["skymail_client"], mailbox)
        self.assertEqual(
            submit_mfa.call_args.kwargs["totp_secret"],
            "JBSWY3DPEHPK3PXP",
        )
        self.assertEqual(
            submit_mfa.call_args.kwargs["mfa_recovery_code"],
            "RECOVERY-CODE",
        )
        follow_state.assert_not_called()

    def test_submit_mfa_falls_back_to_recovery_code_when_totp_is_rejected(self):
        client = self._make_client()
        expected_state = FlowState(page_type="consent")
        mailbox = mock.Mock()
        mailbox.supports_totp_code.return_value = False

        def reject_totp(*args, **kwargs):
            del args, kwargs
            client.last_error = (
                "[stage=mfa] ChatGPT MFA 验证失败: "
                '403 - {"error":{"code":"incorrect_code"}}'
            )
            return None

        with mock.patch.object(
            client,
            "_submit_totp_mfa_challenge",
            side_effect=reject_totp,
        ) as submit_totp, mock.patch.object(
            client,
            "_submit_recovery_code_mfa_challenge",
            return_value=expected_state,
        ) as submit_recovery, mock.patch.object(
            client,
            "_submit_email_mfa_challenge",
        ) as submit_email:
            state = client._submit_mfa_challenge(
                FlowState(
                    page_type="mfa_challenge",
                    payload={
                        "factors": [
                            {"id": "totp-1", "factor_type": "totp"},
                            {
                                "id": "recovery-1",
                                "factor_type": "recovery_code",
                            },
                        ]
                    },
                ),
                email="user@example.com",
                skymail_client=mailbox,
                totp_secret="OUTDATED-TOTP-SECRET",
                mfa_recovery_code="RECOVERY-CODE",
                device_id="device-fixed",
            )

        self.assertIs(state, expected_state)
        submit_totp.assert_called_once()
        submit_recovery.assert_called_once()
        self.assertEqual(
            submit_recovery.call_args.kwargs["recovery_code"],
            "RECOVERY-CODE",
        )
        submit_email.assert_not_called()

    def test_submit_recovery_code_mfa_challenge_never_logs_code(self):
        client = self._make_client()
        logs = []
        client._log = logs.append
        issue_response = mock.Mock(
            status_code=200,
            url="https://auth.openai.com/api/accounts/mfa/issue_challenge",
            text="{}",
        )
        verify_response = mock.Mock(
            status_code=200,
            url="https://auth.openai.com/api/accounts/mfa/verify",
            text='{"page":{"type":"consent"}}',
        )
        verify_response.json.return_value = {"page": {"type": "consent"}}
        client.session.post = mock.Mock(
            side_effect=[issue_response, verify_response]
        )
        expected_state = FlowState(page_type="consent")

        with mock.patch.object(
            client,
            "_state_from_payload",
            return_value=expected_state,
        ):
            state = client._submit_recovery_code_mfa_challenge(
                FlowState(
                    page_type="mfa_challenge",
                    continue_url=(
                        "https://auth.openai.com/mfa-challenge/recovery-1"
                    ),
                    payload={
                        "factors": [
                            {
                                "id": "recovery-1",
                                "factor_type": "recovery_code",
                            }
                        ]
                    },
                ),
                factor={"id": "recovery-1", "type": "recovery_code"},
                recovery_code="RECOVERY-CODE-MUST-STAY-SECRET",
                device_id="device-fixed",
                user_agent="UA",
                sec_ch_ua='"Chromium";v="136"',
                impersonate="chrome136",
            )

        self.assertIs(state, expected_state)
        issue_call, verify_call = client.session.post.call_args_list
        self.assertEqual(
            issue_call.kwargs["json"],
            {
                "id": "recovery-1",
                "type": "recovery_code",
                "force_fresh_challenge": False,
            },
        )
        self.assertEqual(
            verify_call.kwargs["json"],
            {
                "id": "recovery-1",
                "type": "recovery_code",
                "code": "RECOVERY-CODE-MUST-STAY-SECRET",
            },
        )
        self.assertNotIn(
            "RECOVERY-CODE-MUST-STAY-SECRET",
            "\n".join(logs),
        )

    def test_submit_mfa_prefers_supplied_totp_over_email_factor(self):
        client = self._make_client()
        expected_state = FlowState(page_type="consent")
        mailbox = mock.Mock()
        mailbox.supports_totp_code.return_value = False

        with mock.patch.object(
            client,
            "_submit_totp_mfa_challenge",
            return_value=expected_state,
        ) as submit_totp, mock.patch.object(
            client,
            "_submit_email_mfa_challenge",
        ) as submit_email:
            state = client._submit_mfa_challenge(
                FlowState(
                    page_type="mfa_challenge",
                    payload={
                        "factors": [
                            {"id": "totp-1", "factor_type": "totp"},
                            {"id": "email-1", "factor_type": "email"},
                        ]
                    },
                ),
                email="user@example.com",
                skymail_client=mailbox,
                totp_secret="JBSWY3DPEHPK3PXP",
                device_id="device-fixed",
            )

        self.assertIs(state, expected_state)
        submit_totp.assert_called_once()
        self.assertEqual(
            submit_totp.call_args.kwargs["totp_secret"],
            "JBSWY3DPEHPK3PXP",
        )
        submit_email.assert_not_called()

    def test_submit_mfa_falls_back_to_email_without_totp_secret(self):
        client = self._make_client()
        expected_state = FlowState(page_type="consent")
        mailbox = mock.Mock()
        mailbox.supports_totp_code.return_value = False

        with mock.patch.object(
            client,
            "_submit_totp_mfa_challenge",
        ) as submit_totp, mock.patch.object(
            client,
            "_submit_email_mfa_challenge",
            return_value=expected_state,
        ) as submit_email:
            state = client._submit_mfa_challenge(
                FlowState(
                    page_type="mfa_challenge",
                    payload={
                        "factors": [
                            {"id": "totp-1", "factor_type": "totp"},
                            {"id": "email-1", "factor_type": "email"},
                        ]
                    },
                ),
                email="user@example.com",
                skymail_client=mailbox,
                totp_secret="",
                device_id="device-fixed",
            )

        self.assertIs(state, expected_state)
        submit_email.assert_called_once()
        submit_totp.assert_not_called()

    def test_submit_mfa_totp_only_without_secret_reports_missing_secret(self):
        client = self._make_client()
        mailbox = mock.Mock()
        mailbox.supports_totp_code.return_value = False

        state = client._submit_mfa_challenge(
            FlowState(
                page_type="mfa_challenge",
                payload={
                    "factors": [
                        {"id": "totp-1", "factor_type": "totp"},
                    ]
                },
            ),
            email="user@example.com",
            skymail_client=mailbox,
            totp_secret="",
            device_id="device-fixed",
        )

        self.assertIsNone(state)
        self.assertIn("缺少 MFA 秘钥", client.last_error)

    def test_submit_totp_mfa_challenge_issues_and_verifies_locally(self):
        client = self._make_client()
        logs = []
        client._log = logs.append
        issue_response = mock.Mock(
            status_code=200,
            url="https://auth.openai.com/api/accounts/mfa/issue_challenge",
            text="{}",
        )
        verify_response = mock.Mock(
            status_code=200,
            url="https://auth.openai.com/api/accounts/mfa/verify",
            text='{"page":{"type":"consent"}}',
        )
        verify_response.json.return_value = {"page": {"type": "consent"}}
        client.session.post = mock.Mock(side_effect=[issue_response, verify_response])
        expected_state = FlowState(page_type="consent")

        with mock.patch(
            "platforms.chatgpt.oauth_client.generate_totp",
            return_value="123456",
        ) as generate, mock.patch.object(
            client,
            "_state_from_payload",
            return_value=expected_state,
        ):
            state = client._submit_totp_mfa_challenge(
                FlowState(
                    page_type="mfa_challenge",
                    continue_url="https://auth.openai.com/mfa-challenge/factor-1",
                    payload={
                        "factors": [
                            {"id": "factor-1", "factor_type": "totp"},
                        ]
                    },
                ),
                totp_secret="JBSWY3DPEHPK3PXP",
                device_id="device-fixed",
                user_agent="UA",
                sec_ch_ua='"Chromium";v="136"',
                impersonate="chrome136",
            )

        self.assertIs(state, expected_state)
        generate.assert_called_once_with("JBSWY3DPEHPK3PXP")
        self.assertEqual(client.session.post.call_count, 2)
        issue_call, verify_call = client.session.post.call_args_list
        self.assertTrue(issue_call.args[0].endswith("/api/accounts/mfa/issue_challenge"))
        self.assertEqual(
            issue_call.kwargs["json"],
            {"id": "factor-1", "type": "totp", "force_fresh_challenge": False},
        )
        self.assertTrue(verify_call.args[0].endswith("/api/accounts/mfa/verify"))
        self.assertEqual(
            verify_call.kwargs["json"],
            {"id": "factor-1", "type": "totp", "code": "123456"},
        )
        rendered_logs = "\n".join(logs)
        self.assertNotIn("123456", rendered_logs)
        self.assertNotIn("JBSWY3DPEHPK3PXP", rendered_logs)

    def test_submit_email_mfa_challenge_reads_second_code_from_mailbox(self):
        client = self._make_client()
        logs = []
        client._log = logs.append
        issue_response = mock.Mock(
            status_code=200,
            url="https://auth.openai.com/api/accounts/mfa/issue_challenge",
            text="{}",
        )
        verify_response = mock.Mock(
            status_code=200,
            url="https://auth.openai.com/api/accounts/mfa/verify",
            text='{"page":{"type":"consent"}}',
        )
        verify_response.json.return_value = {"page": {"type": "consent"}}
        client.session.post = mock.Mock(side_effect=[issue_response, verify_response])
        mailbox = mock.Mock()
        mailbox.wait_for_verification_code.return_value = "654321"
        expected_state = FlowState(page_type="consent")

        with mock.patch.object(
            client,
            "_state_from_payload",
            return_value=expected_state,
        ), mock.patch(
            "platforms.chatgpt.oauth_client.time.time",
            return_value=1000.0,
        ):
            state = client._submit_mfa_challenge(
                FlowState(
                    page_type="mfa_challenge",
                    continue_url="https://auth.openai.com/mfa-challenge/factor-email",
                    payload={
                        "factors": [
                            {"id": "factor-email", "factor_type": "email"},
                        ]
                    },
                ),
                email="user@example.com",
                skymail_client=mailbox,
                totp_secret="",
                device_id="device-fixed",
                user_agent="UA",
                sec_ch_ua='"Chromium";v="136"',
                impersonate="chrome136",
            )

        self.assertIs(state, expected_state)
        mailbox.wait_for_verification_code.assert_called_once_with(
            "user@example.com",
            timeout=120,
            otp_sent_at=1000.0,
        )
        issue_call, verify_call = client.session.post.call_args_list
        self.assertEqual(
            issue_call.kwargs["json"],
            {"id": "factor-email", "type": "email", "force_fresh_challenge": True},
        )
        self.assertEqual(
            verify_call.kwargs["json"],
            {"id": "factor-email", "type": "email", "code": "654321"},
        )
        rendered_logs = "\n".join(logs)
        self.assertNotIn("654321", rendered_logs)

    def test_submit_email_mfa_challenge_reraises_task_interruption(self):
        client = self._make_client()
        issue_response = mock.Mock(
            status_code=200,
            url="https://auth.openai.com/api/accounts/mfa/issue_challenge",
            text="{}",
        )
        client.session.post = mock.Mock(return_value=issue_response)
        interruption = StopTaskRequested()
        mailbox = mock.Mock()
        mailbox.wait_for_verification_code.side_effect = interruption

        with self.assertRaises(StopTaskRequested) as captured:
            client._submit_email_mfa_challenge(
                FlowState(
                    page_type="mfa_challenge",
                    continue_url="https://auth.openai.com/mfa-challenge/factor-email",
                ),
                factor={"id": "factor-email", "type": "email"},
                email="user@example.com",
                skymail_client=mailbox,
                device_id="device-fixed",
                user_agent="UA",
                sec_ch_ua='"Chromium";v="136"',
                impersonate="chrome136",
            )

        self.assertIs(captured.exception, interruption)
        client.session.post.assert_called_once()

    def test_email_otp_reraises_task_interruption_from_mailbox(self):
        client = self._make_client()
        interruption = StopTaskRequested()
        mailbox = mock.Mock()
        mailbox.wait_for_verification_code.side_effect = interruption

        with mock.patch(
            "platforms.chatgpt.oauth_client.build_sentinel_token",
            return_value="",
        ), mock.patch(
            "platforms.chatgpt.oauth_client.get_sentinel_token_via_browser",
            return_value="",
        ), self.assertRaises(StopTaskRequested) as captured:
            client._handle_otp_verification(
                "user@example.com",
                "device-fixed",
                "UA",
                '"Chromium";v="136"',
                "chrome136",
                mailbox,
                FlowState(
                    page_type="email_otp_verification",
                    continue_url="https://auth.openai.com/email-verification",
                    current_url="https://auth.openai.com/email-verification",
                ),
                prefer_passwordless_login=True,
                allow_cached_code_retry=False,
            )

        self.assertIs(captured.exception, interruption)

    def test_email_otp_stops_immediately_on_terminal_mailbox_auth_failure(self):
        client = self._make_client()
        logs = []
        client._log = logs.append
        mailbox = mock.Mock()
        auth_error_type = getattr(
            base_mailbox,
            "MailboxAuthenticationError",
            RuntimeError,
        )
        mailbox.wait_for_verification_code.side_effect = auth_error_type(
            "微软邮箱 IMAP 鉴权失败，请检查邮箱凭据或 IMAP 权限"
        )

        with (
            mock.patch(
                "platforms.chatgpt.oauth_client.build_sentinel_token",
                return_value="",
            ),
            mock.patch(
                "platforms.chatgpt.oauth_client.get_sentinel_token_via_browser",
                return_value="",
            ),
            mock.patch(
                "platforms.chatgpt.oauth_client.time.time",
                return_value=100.0,
            ),
        ):
            result = client._handle_otp_verification(
                "user@example.com",
                "device-fixed",
                "UA",
                '"Chromium";v="136"',
                "chrome136",
                mailbox,
                FlowState(
                    page_type="email_otp_verification",
                    continue_url="https://auth.openai.com/email-verification",
                    current_url="https://auth.openai.com/email-verification",
                ),
                prefer_passwordless_login=True,
                allow_cached_code_retry=False,
            )

        self.assertIsNone(result)
        self.assertIn("IMAP 鉴权失败", client.last_error)
        mailbox.wait_for_verification_code.assert_called_once()
        self.assertNotIn("暂未收到新的 OTP", "\n".join(logs))

    def test_submit_choose_account_session_selects_matching_authenticated_account(self):
        client = self._make_client()
        choose_url = "https://auth.openai.com/choose-an-account"
        page_response = mock.Mock(
            status_code=200,
            url=choose_url,
            text=(
                '<form action="/choose-an-account" method="post">'
                '<input type="hidden" name="intent" value="select">'
                '<button type="submit" name="session_id" value="session-1">'
                '<span>MFA User</span><span>mfa-user@icloud.com</span>'
                "</button></form>"
            ),
            headers={"content-type": "text/html; charset=utf-8"},
        )
        select_response = mock.Mock(
            status_code=200,
            url="https://auth.openai.com/api/accounts/session/select",
            text='{"page":{"type":"add_phone"}}',
        )
        select_response.json.return_value = {
            "page": {
                "type": "add_phone",
                "continue_url": "https://auth.openai.com/add-phone",
            }
        }
        client.session.get = mock.Mock(return_value=page_response)
        client.session.post = mock.Mock(return_value=select_response)

        with mock.patch(
            "platforms.chatgpt.oauth_client.get_sentinel_token_via_browser",
            return_value="sentinel-demo",
        ), mock.patch(
            "platforms.chatgpt.oauth_client.build_sentinel_token",
            return_value="",
        ) as build_sentinel:
            state = client._submit_choose_account_session(
                FlowState(
                    page_type="choose_an_account",
                    continue_url=choose_url,
                    current_url=choose_url,
                ),
                email="mfa-user@icloud.com",
                device_id="device-fixed",
                user_agent="UA",
                sec_ch_ua='"Chromium";v="136"',
                impersonate="chrome136",
            )

        self.assertEqual(state.page_type, "add_phone")
        build_sentinel.assert_not_called()
        client.session.get.assert_called_once()
        client.session.post.assert_called_once()
        post_call = client.session.post.call_args
        self.assertTrue(post_call.args[0].endswith("/api/accounts/session/select"))
        self.assertEqual(post_call.kwargs["json"], {"session_id": "session-1"})
        self.assertEqual(
            post_call.kwargs["headers"]["openai-sentinel-token"],
            "sentinel-demo",
        )

    def test_prepare_phone_transaction_selects_account_before_parking_oauth(self):
        client = self._make_client()
        client.session.cookies.set(
            "login_session",
            "session-cookie",
            domain=".openai.com",
            path="/",
        )
        choose_state = FlowState(
            page_type="choose_an_account",
            continue_url="https://auth.openai.com/choose-an-account",
            current_url="https://auth.openai.com/choose-an-account",
        )
        add_phone_state = FlowState(
            page_type="add_phone",
            continue_url="https://auth.openai.com/add-phone",
            current_url="https://auth.openai.com/api/accounts/session/select",
            source="api",
        )

        with mock.patch.object(
            client,
            "_bootstrap_oauth_session",
            return_value="https://auth.openai.com/choose-an-account",
        ), mock.patch.object(
            client,
            "_state_from_url",
            return_value=choose_state,
        ), mock.patch.object(
            client,
            "_submit_choose_account_session",
            return_value=add_phone_state,
        ) as select_account:
            context = client.prepare_phone_verification_transaction(
                email="mfa-user@icloud.com",
                device_id="device-fixed",
                user_agent="UA",
                sec_ch_ua='"Chromium";v="136"',
                accept_language="en-US,en;q=0.9",
                impersonate="chrome136",
            )

        self.assertIsNotNone(context)
        self.assertIs(context.flow_state, add_phone_state)
        select_account.assert_called_once()
        self.assertEqual(
            select_account.call_args.kwargs["email"],
            "mfa-user@icloud.com",
        )

    def test_login_and_get_tokens_visits_add_phone_continue_url_before_phone_branch(self):
        client = self._make_client()
        add_phone_state = FlowState(
            page_type="add_phone",
            continue_url="https://auth.openai.com/add-phone",
            current_url="https://auth.openai.com/api/accounts/email-otp/validate",
            source="api",
        )
        consent_state = FlowState(
            page_type="consent",
            continue_url="https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
            current_url="https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
        )

        with mock.patch.object(client, "_bootstrap_oauth_session", return_value="https://auth.openai.com/log-in"), \
            mock.patch.object(client, "_submit_authorize_continue", return_value=add_phone_state), \
            mock.patch.object(client, "_follow_flow_state", return_value=(None, consent_state)) as follow_state, \
            mock.patch.object(client, "_oauth_submit_workspace_and_org", return_value=("auth-code", None)), \
            mock.patch.object(client, "_exchange_code_for_tokens", return_value={"access_token": "at"}), \
            mock.patch.object(client, "_handle_add_phone_verification") as handle_phone:
            tokens = client.login_and_get_tokens(
                "user@example.com",
                "Secret123!",
                "device-fixed",
                prefer_passwordless_login=True,
                allow_phone_verification=False,
            )

        self.assertEqual(tokens["access_token"], "at")
        follow_state.assert_called_once()
        handle_phone.assert_not_called()

    def test_login_and_get_tokens_uses_canonical_consent_url_when_state_is_add_phone(self):
        client = self._make_client()
        add_phone_state = FlowState(
            page_type="add_phone",
            continue_url="https://auth.openai.com/add-phone",
            current_url="https://auth.openai.com/add-phone",
        )

        with mock.patch.object(client, "_bootstrap_oauth_session", return_value="https://auth.openai.com/log-in"), \
            mock.patch.object(client, "_submit_authorize_continue", return_value=add_phone_state), \
            mock.patch.object(client, "_state_supports_workspace_resolution", return_value=True), \
            mock.patch.object(client, "_state_requires_navigation", return_value=False), \
            mock.patch.object(client, "_oauth_submit_workspace_and_org", return_value=("auth-code", None)) as submit_workspace, \
            mock.patch.object(client, "_exchange_code_for_tokens", return_value={"access_token": "at"}):
            tokens = client.login_and_get_tokens(
                "user@example.com",
                "Secret123!",
                "device-fixed",
                prefer_passwordless_login=True,
                allow_phone_verification=False,
                skymail_client=mock.Mock(),
            )

        self.assertEqual(tokens["access_token"], "at")
        self.assertEqual(
            submit_workspace.call_args.args[0],
            "https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
        )

    def test_login_and_get_tokens_does_not_restart_when_add_phone_has_no_workspace(self):
        client = self._make_client()
        add_phone_state = FlowState(
            page_type="add_phone",
            continue_url="https://auth.openai.com/add-phone",
            current_url="https://auth.openai.com/add-phone",
        )

        with mock.patch.object(client, "_bootstrap_oauth_session", return_value="https://auth.openai.com/log-in") as bootstrap, \
            mock.patch.object(client, "_submit_authorize_continue", return_value=add_phone_state) as submit_continue, \
            mock.patch.object(client, "_state_supports_workspace_resolution", return_value=False), \
            mock.patch.object(client, "_state_requires_navigation", return_value=False):
            tokens = client.login_and_get_tokens(
                "user@example.com",
                "Secret123!",
                "device-fixed",
                prefer_passwordless_login=True,
                allow_phone_verification=False,
                skymail_client=mock.Mock(),
            )

        self.assertIsNone(tokens)
        self.assertEqual(bootstrap.call_count, 1)
        self.assertEqual(submit_continue.call_count, 1)
        self.assertIn("未获取到 workspace / callback", client.last_error)

    def test_send_passwordless_login_otp_does_not_send_email_field(self):
        client = self._make_client()
        response = mock.Mock()
        response.status_code = 200
        response.url = "https://auth.openai.com/api/accounts/passwordless/send-otp"
        response.json.return_value = {"page": {"type": "email_otp_verification"}}
        client.session.post = mock.Mock(return_value=response)

        expected_state = FlowState(
            page_type="email_otp_verification",
            continue_url="https://auth.openai.com/email-verification",
            current_url="https://auth.openai.com/email-verification",
        )
        with mock.patch.object(
            client,
            "_state_from_payload",
            return_value=expected_state,
        ):
            state = client._send_passwordless_login_otp(
                "user@example.com",
                "device-fixed",
            )

        self.assertEqual(state, expected_state)
        kwargs = client.session.post.call_args.kwargs
        self.assertNotIn("json", kwargs)
        self.assertNotIn("data", kwargs)

    def test_login_and_get_tokens_submits_about_you_when_configured(self):
        client = self._make_client()
        about_you_state = FlowState(
            page_type="about_you",
            continue_url="https://auth.openai.com/about-you",
            current_url="https://auth.openai.com/about-you",
        )
        consent_state = FlowState(
            page_type="consent",
            continue_url="https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
            current_url="https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
        )

        with mock.patch.object(client, "_bootstrap_oauth_session", return_value="https://auth.openai.com/log-in"), \
            mock.patch.object(client, "_submit_authorize_continue", return_value=about_you_state), \
            mock.patch.object(client, "_submit_about_you_create_account", return_value=consent_state) as submit_about_you, \
            mock.patch.object(client, "_oauth_submit_workspace_and_org", return_value=("auth-code", None)), \
            mock.patch.object(client, "_exchange_code_for_tokens", return_value={"access_token": "at"}):
            tokens = client.login_and_get_tokens(
                "user@example.com",
                "Secret123!",
                "device-fixed",
                prefer_passwordless_login=True,
                allow_phone_verification=False,
                complete_about_you_if_needed=True,
                first_name="Ivy",
                last_name="Stone",
                birthdate="1990-01-02",
                skymail_client=mock.Mock(),
            )

        self.assertEqual(tokens["access_token"], "at")
        submit_about_you.assert_called_once()
        self.assertEqual(submit_about_you.call_args.args[0], "Ivy")
        self.assertEqual(submit_about_you.call_args.args[1], "Stone")
        self.assertEqual(submit_about_you.call_args.args[2], "1990-01-02")


class BrowserFallbackTests(unittest.TestCase):
    def test_chatgpt_create_account_headless_mode_does_not_hide_http_challenge(self):
        client = ChatGPTClient(proxy="http://127.0.0.1:7890", verbose=False, browser_mode="headless")
        client._get_sentinel_token = mock.Mock(return_value="sentinel-token")
        client._browser_submit_create_account = mock.Mock(
            return_value=(
                True,
                FlowState(
                    page_type="consent",
                    continue_url="https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
                    current_url="https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
                ),
            )
        )

        response = mock.Mock()
        response.status_code = 403
        response.text = "<!DOCTYPE html>Just a moment..."
        response.url = "https://auth.openai.com/about-you"
        client.session.post = mock.Mock(return_value=response)

        ok, next_state = client.create_account("Ivy", "Stone", "1990-01-02", return_state=True)

        self.assertFalse(ok)
        self.assertIn("HTTP 403", next_state)
        client._browser_submit_create_account.assert_not_called()

    def test_chatgpt_create_account_protocol_mode_skips_browser_fallback(self):
        client = ChatGPTClient(proxy="http://127.0.0.1:7890", verbose=False, browser_mode="protocol")
        client._get_sentinel_token = mock.Mock(return_value="sentinel-token")
        client._browser_submit_create_account = mock.Mock()

        response = mock.Mock()
        response.status_code = 403
        response.text = "<!DOCTYPE html>Just a moment..."
        response.url = "https://auth.openai.com/about-you"
        response.json.side_effect = ValueError("not json")
        client.session.post = mock.Mock(return_value=response)

        ok, detail = client.create_account("Ivy", "Stone", "1990-01-02", return_state=True)

        self.assertFalse(ok)
        self.assertIn("HTTP 403", detail)
        client._browser_submit_create_account.assert_not_called()

    def test_load_workspace_session_data_uses_consent_html_when_cookie_missing(self):
        client = OAuthClient({}, proxy="http://127.0.0.1:7890", verbose=False, browser_mode="headless")
        client._decode_oauth_session_cookie = mock.Mock(return_value=None)
        client._fetch_consent_page_html = mock.Mock(
            return_value='<html data-session="workspaces"></html>'
        )
        client._extract_session_data_from_consent_html = mock.Mock(
            return_value={"workspaces": [{"id": "ws-1"}]}
        )

        session_data = client._load_workspace_session_data(
            "https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
            user_agent="UA",
            impersonate="chrome136",
        )

        self.assertEqual(session_data["workspaces"][0]["id"], "ws-1")
        client._extract_session_data_from_consent_html.assert_called_once_with(
            '<html data-session="workspaces"></html>'
        )

    def test_workspace_submit_returns_consent_state_when_api_follow_has_no_code(self):
        client = OAuthClient({}, proxy="http://127.0.0.1:7890", verbose=False, browser_mode="headless")
        client._load_workspace_session_data = mock.Mock(
            return_value={"workspaces": [{"id": "ws-1"}]}
        )
        client._oauth_follow_for_code = mock.Mock(return_value=(None, "https://auth.openai.com/sign-in-with-chatgpt/codex/consent"))
        client._browser_capture_callback = mock.Mock(
            return_value="http://localhost:1455/auth/callback?code=auth-code&state=demo"
        )

        response = mock.Mock()
        response.status_code = 200
        response.url = "https://auth.openai.com/api/accounts/workspace/select"
        response.text = '{"continue_url":"https://auth.openai.com/sign-in-with-chatgpt/codex/consent"}'
        response.json.return_value = {
            "continue_url": "https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
            "page": {
                "type": "consent",
                "payload": {
                    "url": "https://auth.openai.com/sign-in-with-chatgpt/codex/consent"
                },
            },
            "data": {
                "orgs": [],
            },
        }
        client.session.post = mock.Mock(return_value=response)

        code, state = client._oauth_submit_workspace_and_org(
            "https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
            "device-fixed",
            "UA",
            "chrome136",
        )

        self.assertIsNone(code)
        self.assertEqual(state.page_type, "consent")
        client._browser_capture_callback.assert_not_called()


if __name__ == "__main__":
    unittest.main()
