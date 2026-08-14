import base64
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from curl_cffi import requests as cffi_requests

from platforms.chatgpt.leadbee_open_api import (
    LeadBeeAPIError,
    LeadBeeTransportError,
)
from platforms.chatgpt.oauth_client import OAuthClient
from platforms.chatgpt.phone_service import (
    LeadBeeOpenAPIPhoneService,
    LeadBeePhoneService,
    SMSToMePhoneService,
    create_phone_service,
)
from platforms.chatgpt.utils import FlowState
from services.chatgpt_account_state import ChatGPTAccountDeactivatedError
from services.chatgpt_phone_verification import (
    InteractivePhoneVerificationBroker,
    PhoneVerificationCommand,
)
from smstome_tool import PhoneEntry, parse_country_slugs


class OAuthCookieDecodeTests(unittest.TestCase):
    def test_decode_signed_cookie_payload(self):
        payload = {
            "email": "demo@example.com",
            "phone_number": "+447456344799",
            "phone_verification_channel": "whatsapp",
        }
        encoded = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("utf-8").rstrip("=")
        cookie_value = f"{encoded}.sig-a.sig-b"

        self.assertEqual(OAuthClient._decode_cookie_json_value(cookie_value), payload)

    def test_decode_invalid_cookie_payload(self):
        self.assertIsNone(OAuthClient._decode_cookie_json_value("not-a-valid-cookie"))


class OAuthEntryRetryTests(unittest.TestCase):
    def test_password_verify_raises_typed_signal_for_deactivated_account_response(self):
        client = OAuthClient({}, verbose=False)
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
        client.session.post = mock.Mock(return_value=response)

        with mock.patch(
            "platforms.chatgpt.oauth_client.get_sentinel_token_via_browser",
            return_value="browser-token",
        ), self.assertRaises(ChatGPTAccountDeactivatedError):
            client._submit_password_verify(
                "saved-password",
                "device-id",
                referer="https://auth.openai.com/log-in/password",
            )

    def test_password_verify_raises_for_top_level_deactivation_code(self):
        client = OAuthClient({}, verbose=False)
        response = mock.Mock()
        response.status_code = 403
        response.text = (
            '{"type":"account_deactivated","message":"Account unavailable"}'
        )
        response.json.return_value = {
            "type": "account_deactivated",
            "message": "Account unavailable",
        }
        client.session.post = mock.Mock(return_value=response)

        with mock.patch(
            "platforms.chatgpt.oauth_client.get_sentinel_token_via_browser",
            return_value="browser-token",
        ), self.assertRaises(ChatGPTAccountDeactivatedError):
            client._submit_password_verify(
                "saved-password",
                "device-id",
                referer="https://auth.openai.com/log-in/password",
            )

    def test_password_verify_accepts_canonical_chinese_help_center_message(self):
        client = OAuthClient({}, verbose=False)
        message = (
            "你没有账号，因为它已被删除或停用。如果您认为这是错误，"
            "请通过我们的帮助中心联系我们，地址为 https://help.openai.com。"
        )
        response = mock.Mock()
        response.status_code = 403
        response.text = json.dumps(
            {"错误": {"消息": message}},
            ensure_ascii=False,
        )
        response.json.return_value = {"错误": {"消息": message}}
        client.session.post = mock.Mock(return_value=response)

        with mock.patch(
            "platforms.chatgpt.oauth_client.get_sentinel_token_via_browser",
            return_value="browser-token",
        ), self.assertRaises(ChatGPTAccountDeactivatedError):
            client._submit_password_verify(
                "saved-password",
                "device-id",
                referer="https://auth.openai.com/log-in/password",
            )

    def test_password_verify_does_not_raise_for_diagnostic_deactivation_text(self):
        client = OAuthClient({}, verbose=False)
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
        client.session.post = mock.Mock(return_value=response)

        with mock.patch(
            "platforms.chatgpt.oauth_client.get_sentinel_token_via_browser",
            return_value="browser-token",
        ):
            result = client._submit_password_verify(
                "saved-password",
                "device-id",
                referer="https://auth.openai.com/log-in/password",
            )

        self.assertIsNone(result)
        self.assertIn("密码验证失败: 403", client.last_error)

    def test_password_verify_does_not_raise_for_canonical_prefix_diagnostic(self):
        client = OAuthClient({}, verbose=False)
        response = mock.Mock()
        response.status_code = 403
        response.text = (
            '{"error":{"message":"Account has been deleted or deactivated '
            'in the diagnostic fixture, but this is not an account-state signal."}}'
        )
        response.json.return_value = {
            "error": {
                "message": (
                    "Account has been deleted or deactivated in the diagnostic "
                    "fixture, but this is not an account-state signal."
                ),
            }
        }
        client.session.post = mock.Mock(return_value=response)

        with mock.patch(
            "platforms.chatgpt.oauth_client.get_sentinel_token_via_browser",
            return_value="browser-token",
        ):
            result = client._submit_password_verify(
                "saved-password",
                "device-id",
                referer="https://auth.openai.com/log-in/password",
            )

        self.assertIsNone(result)
        self.assertIn("密码验证失败: 403", client.last_error)

    def test_password_verify_does_not_raise_for_remediation_prefix_diagnostic(self):
        client = OAuthClient({}, verbose=False)
        response = mock.Mock()
        response.status_code = 403
        message = (
            "You do not have an account because it has been deleted or "
            "deactivated. If you believe this was an error, please contact us "
            "through our help center. Diagnostic fixture only; this is not an "
            "account-state signal."
        )
        response.text = json.dumps({"error": {"message": message}})
        response.json.return_value = {"error": {"message": message}}
        client.session.post = mock.Mock(return_value=response)

        with mock.patch(
            "platforms.chatgpt.oauth_client.get_sentinel_token_via_browser",
            return_value="browser-token",
        ):
            result = client._submit_password_verify(
                "saved-password",
                "device-id",
                referer="https://auth.openai.com/log-in/password",
            )

        self.assertIsNone(result)
        self.assertIn("密码验证失败: 403", client.last_error)

    def test_password_verify_does_not_raise_for_deactivation_text_on_non_403(self):
        client = OAuthClient({}, verbose=False)
        response = mock.Mock()
        response.status_code = 502
        response.text = (
            '{"error":{"message":'
            '"You do not have an account because it has been deleted or deactivated."}}'
        )
        response.json.return_value = {
            "error": {
                "message": (
                    "You do not have an account because it has been deleted or "
                    "deactivated."
                ),
            }
        }
        client.session.post = mock.Mock(return_value=response)

        with mock.patch(
            "platforms.chatgpt.oauth_client.get_sentinel_token_via_browser",
            return_value="browser-token",
        ):
            result = client._submit_password_verify(
                "saved-password",
                "device-id",
                referer="https://auth.openai.com/log-in/password",
            )

        self.assertIsNone(result)
        self.assertIn("密码验证失败: 502", client.last_error)

    def test_email_otp_raises_typed_signal_for_deactivated_account_response(self):
        client = OAuthClient(
            {"chatgpt_oauth_otp_wait_seconds": 30},
            verbose=False,
        )
        response = mock.Mock()
        response.status_code = 400
        response.url = "https://auth.openai.com/api/accounts/email-otp/validate"
        response.text = (
            '{"error":{"code":"account_deactivated","message":'
            '"你没有账号，因为它已被删除或停用。"}}'
        )
        response.json.return_value = {
            "error": {
                "code": "account_deactivated",
                "message": "你没有账号，因为它已被删除或停用。",
            }
        }
        client.session.post = mock.Mock(return_value=response)
        email_service = mock.Mock()
        email_service.wait_for_verification_code.return_value = "123456"

        with mock.patch(
            "platforms.chatgpt.oauth_client.build_sentinel_token",
            return_value="http-token",
        ), mock.patch(
            "platforms.chatgpt.oauth_client.get_sentinel_token_via_browser",
            return_value="browser-token",
        ), self.assertRaises(ChatGPTAccountDeactivatedError):
            client._handle_otp_verification(
                "existing@example.com",
                "device-id",
                "Mozilla/5.0",
                '"Chromium";v="136"',
                "chrome136",
                email_service,
                FlowState(
                    page_type="email_otp_verification",
                    current_url="https://auth.openai.com/email-verification",
                ),
                prefer_passwordless_login=True,
            )

        email_service.wait_for_verification_code.assert_called_once()

    def test_phone_transaction_is_prepared_on_a_cloned_post_otp_session(self):
        client = OAuthClient({}, verbose=False)
        original_session = client.session
        original_session.cookies.set(
            "login_session",
            "post-otp-session",
            domain=".openai.com",
            path="/",
        )

        with mock.patch.object(
            client,
            "_bootstrap_oauth_session",
            return_value="https://auth.openai.com/add-phone",
        ) as bootstrap:
            context = client.prepare_phone_verification_transaction(
                device_id="device-id",
                user_agent="UA",
                sec_ch_ua='"Chromium";v="136"',
                accept_language="en-US,en;q=0.9",
                impersonate="chrome136",
            )

        self.assertIsNotNone(context)
        self.assertIsNot(context.session, original_session)
        self.assertIs(client.session, context.session)
        self.assertEqual(context.flow_state.page_type, "add_phone")
        self.assertTrue(context.code_verifier)
        self.assertTrue(context.oauth_state)
        self.assertEqual(context.authorize_params["state"], context.oauth_state)
        self.assertEqual(
            original_session.cookies.get("login_session", domain=".openai.com"),
            "post-otp-session",
        )
        bootstrap.assert_called_once()

    def test_email_otp_prefers_http_pow_before_browser_sentinel(self):
        client = OAuthClient(
            {"chatgpt_oauth_otp_wait_seconds": 30},
            verbose=False,
        )
        response = mock.Mock()
        response.status_code = 200
        response.url = "https://auth.openai.com/api/accounts/email-otp/validate"
        response.text = "{}"
        response.json.return_value = {"continue_url": "https://chatgpt.com/callback"}
        client.session.post = mock.Mock(return_value=response)
        email_service = mock.Mock()
        email_service.wait_for_verification_code.return_value = "123456"
        next_state = FlowState(
            page_type="external_url",
            continue_url="https://chatgpt.com/callback",
        )

        with mock.patch(
            "platforms.chatgpt.oauth_client.build_sentinel_token",
            return_value="http-token",
        ) as http_pow, mock.patch(
            "platforms.chatgpt.oauth_client.get_sentinel_token_via_browser",
            return_value="browser-token",
        ) as browser_sentinel, mock.patch.object(
            client,
            "_state_from_payload",
            return_value=next_state,
        ):
            result = client._handle_otp_verification(
                "existing@example.com",
                "device-id",
                "Mozilla/5.0",
                '"Chromium";v="136"',
                "chrome136",
                email_service,
                FlowState(
                    page_type="email_otp_verification",
                    current_url="https://auth.openai.com/email-verification",
                ),
                prefer_passwordless_login=True,
            )

        self.assertIs(result, next_state)
        http_pow.assert_called_once()
        browser_sentinel.assert_not_called()
        headers = client.session.post.call_args.kwargs["headers"]
        self.assertEqual(headers["openai-sentinel-token"], "http-token")

    def test_stop_after_fresh_email_login_captures_resumable_transaction(self):
        client = OAuthClient({}, verbose=False)
        email_otp_state = FlowState(
            page_type="email_otp_verification",
            current_url="https://auth.openai.com/email-verification",
        )
        add_phone_state = FlowState(
            page_type="add_phone",
            current_url="https://auth.openai.com/add-phone",
        )

        with mock.patch.object(
            client,
            "_bootstrap_oauth_session",
            return_value="https://auth.openai.com/log-in",
        ), mock.patch.object(
            client,
            "_submit_authorize_continue",
            return_value=email_otp_state,
        ), mock.patch.object(
            client,
            "_handle_otp_verification",
            return_value=add_phone_state,
        ), mock.patch.object(
            client,
            "_handle_add_phone_verification",
        ) as handle_phone:
            tokens = client.login_and_get_tokens(
                "existing@example.com",
                "",
                "device-id",
                user_agent="UA",
                sec_ch_ua='"Chromium";v="136"',
                impersonate="chrome136",
                skymail_client=mock.Mock(),
                prefer_passwordless_login=True,
                allow_phone_verification=False,
                stop_after_login=True,
                login_source="prepare_phone_verification",
            )

        self.assertIsNone(tokens)
        handle_phone.assert_not_called()
        context = client.last_prepared_oauth_context
        self.assertIs(context.session, client.session)
        self.assertEqual(context.flow_state.page_type, "add_phone")
        self.assertTrue(context.code_verifier)
        self.assertTrue(context.oauth_state)
        self.assertEqual(context.authorize_params["state"], context.oauth_state)

    def test_stop_after_fresh_email_login_captures_named_codex_consent_transaction(self):
        client = OAuthClient({}, verbose=False)
        email_otp_state = FlowState(
            page_type="email_otp_verification",
            current_url="https://auth.openai.com/email-verification",
        )
        consent_state = FlowState(
            page_type="sign_in_with_chatgpt_codex_consent",
            continue_url="https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
            current_url="https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
            source="api",
        )

        with mock.patch.object(
            client,
            "_bootstrap_oauth_session",
            return_value="https://auth.openai.com/log-in",
        ), mock.patch.object(
            client,
            "_submit_authorize_continue",
            return_value=email_otp_state,
        ), mock.patch.object(
            client,
            "_handle_otp_verification",
            return_value=consent_state,
        ):
            tokens = client.login_and_get_tokens(
                "existing@example.com",
                "",
                "device-id",
                user_agent="UA",
                sec_ch_ua='"Chromium";v="136"',
                impersonate="chrome136",
                skymail_client=mock.Mock(),
                prefer_passwordless_login=True,
                allow_phone_verification=False,
                stop_after_login=True,
                login_source="prepare_phone_verification",
            )

        self.assertIsNone(tokens)
        context = client.last_prepared_oauth_context
        self.assertIsNotNone(context)
        self.assertEqual(
            context.flow_state.page_type,
            "sign_in_with_chatgpt_codex_consent",
        )
        self.assertTrue(context.code_verifier)
        self.assertTrue(context.oauth_state)

    def test_stop_after_login_does_not_claim_success_when_context_capture_fails(self):
        client = OAuthClient({}, verbose=False)
        email_otp_state = FlowState(
            page_type="email_otp_verification",
            current_url="https://auth.openai.com/email-verification",
        )
        unsupported_state = FlowState(
            page_type="unsupported_post_login_state",
            current_url="https://auth.openai.com/unsupported",
            method="POST",
            source="api",
        )

        with mock.patch.object(
            client,
            "_bootstrap_oauth_session",
            return_value="https://auth.openai.com/log-in",
        ), mock.patch.object(
            client,
            "_submit_authorize_continue",
            return_value=email_otp_state,
        ), mock.patch.object(
            client,
            "_handle_otp_verification",
            return_value=unsupported_state,
        ):
            tokens = client.login_and_get_tokens(
                "existing@example.com",
                "",
                "device-id",
                user_agent="UA",
                sec_ch_ua='"Chromium";v="136"',
                impersonate="chrome136",
                skymail_client=mock.Mock(),
                prefer_passwordless_login=True,
                allow_phone_verification=False,
                stop_after_login=True,
                login_source="prepare_phone_verification",
            )

        self.assertIsNone(tokens)
        self.assertIsNone(client.last_prepared_oauth_context)
        self.assertNotIn("登录链路已完成", client.last_error)

    def test_bootstrap_rejects_403_page_even_when_cookie_name_exists(self):
        client = OAuthClient({}, verbose=False)
        response = mock.Mock()
        response.status_code = 403
        response.url = "https://auth.openai.com/api/accounts/authorize"
        response.history = []
        client.session = mock.Mock()
        client.session.cookies = [
            types.SimpleNamespace(
                name="login_session",
                value="stale-session",
                domain=".openai.com",
            )
        ]
        client.session.get.return_value = response

        final_url = client._bootstrap_oauth_session(
            "https://auth.openai.com/oauth/authorize",
            {"state": "demo"},
        )

        self.assertEqual(final_url, "")
        self.assertEqual(client.session.get.call_count, 2)

    def test_login_retries_whole_oauth_entry_after_continue_403(self):
        client = OAuthClient({}, verbose=False)
        client._log = lambda _message: None
        callback_url = "http://localhost:1455/auth/callback?code=auth-code&state=demo"
        callback_state = FlowState(
            page_type="oauth_callback",
            continue_url=callback_url,
            current_url=callback_url,
        )

        def submit_email(*args, **kwargs):
            if submit_email.calls == 0:
                submit_email.calls += 1
                client.last_error = (
                    "[stage=authorize_continue] 提交邮箱失败: 403 - <!DOCTYPE html>"
                )
                return None
            submit_email.calls += 1
            return callback_state

        submit_email.calls = 0

        with mock.patch.object(
            client,
            "_bootstrap_oauth_session",
            return_value="https://auth.openai.com/log-in",
        ) as bootstrap, mock.patch.object(
            client,
            "_submit_authorize_continue",
            side_effect=submit_email,
        ) as submit, mock.patch.object(
            client,
            "_exchange_code_for_tokens",
            return_value={"access_token": "at", "refresh_token": "rt"},
        ), mock.patch.object(
            client,
            "_recreate_session",
        ) as recreate, mock.patch.object(
            client,
            "_ensure_oauth_fingerprint",
            return_value=("UA", '"Chromium";v="136"', "chrome136"),
        ):
            tokens = client.login_and_get_tokens(
                "existing@example.com",
                "",
                "device-id",
                user_agent="UA",
                sec_ch_ua='"Chromium";v="136"',
                impersonate="chrome136",
                prefer_passwordless_login=True,
                allow_phone_verification=True,
                login_source="interactive_phone_verification",
            )

        self.assertEqual(tokens["refresh_token"], "rt")
        self.assertEqual(bootstrap.call_count, 2)
        self.assertEqual(submit.call_count, 2)
        recreate.assert_called_once()

    def test_resumed_authenticated_session_skips_second_email_login(self):
        client = OAuthClient({}, verbose=False)
        callback_url = "http://localhost:1455/auth/callback?code=auth-code&state=demo"
        callback_state = FlowState(
            page_type="oauth_callback",
            continue_url=callback_url,
            current_url=callback_url,
        )

        with mock.patch.object(
            client,
            "_bootstrap_oauth_session",
            return_value="https://auth.openai.com/add-phone",
        ), mock.patch.object(
            client,
            "_submit_authorize_continue",
        ) as submit_email, mock.patch.object(
            client,
            "_handle_add_phone_verification",
            return_value=callback_state,
        ) as handle_phone, mock.patch.object(
            client,
            "_exchange_code_for_tokens",
            return_value={"access_token": "at", "refresh_token": "rt"},
        ):
            tokens = client.login_and_get_tokens(
                "existing@example.com",
                "",
                "device-id",
                user_agent="UA",
                sec_ch_ua='"Chromium";v="136"',
                impersonate="chrome136",
                prefer_passwordless_login=True,
                allow_phone_verification=True,
                force_new_browser=False,
                resume_authenticated_session=True,
                login_source="interactive_phone_verification",
            )

        self.assertEqual(tokens["refresh_token"], "rt")
        submit_email.assert_not_called()
        handle_phone.assert_called_once()

    def test_prepared_phone_transaction_resumes_without_new_oauth_bootstrap(self):
        client = OAuthClient({}, verbose=False)
        callback_url = (
            "http://localhost:1455/auth/callback"
            "?code=auth-code&state=prepared-state"
        )
        callback_state = FlowState(
            page_type="oauth_callback",
            continue_url=callback_url,
            current_url=callback_url,
        )
        resume_context = types.SimpleNamespace(
            session=client.session,
            device_id="device-id",
            user_agent="UA",
            sec_ch_ua='"Chromium";v="136"',
            accept_language="en-US,en;q=0.9",
            impersonate="chrome136",
            code_verifier="prepared-verifier",
            oauth_state="prepared-state",
            authorize_url="https://auth.openai.com/oauth/authorize",
            authorize_params={"state": "prepared-state"},
            flow_state=FlowState(
                page_type="add_phone",
                current_url="https://auth.openai.com/add-phone",
            ),
            referer="https://auth.openai.com/add-phone",
        )

        with mock.patch.object(
            client,
            "_bootstrap_oauth_session",
        ) as bootstrap, mock.patch.object(
            client,
            "_submit_authorize_continue",
        ) as submit_email, mock.patch.object(
            client,
            "_handle_add_phone_verification",
            return_value=callback_state,
        ) as handle_phone, mock.patch.object(
            client,
            "_exchange_code_for_tokens",
            return_value={"access_token": "at", "refresh_token": "rt"},
        ) as exchange:
            tokens = client.login_and_get_tokens(
                "existing@example.com",
                "",
                "device-id",
                user_agent="UA",
                sec_ch_ua='"Chromium";v="136"',
                impersonate="chrome136",
                allow_phone_verification=True,
                prepared_oauth_context=resume_context,
                login_source="interactive_phone_verification",
            )

        self.assertEqual(tokens["refresh_token"], "rt")
        bootstrap.assert_not_called()
        submit_email.assert_not_called()
        handle_phone.assert_called_once()
        self.assertEqual(exchange.call_args.args[1], "prepared-verifier")

    def test_expired_resumed_session_never_falls_back_to_email_login(self):
        client = OAuthClient({}, verbose=False)
        callback_url = "http://localhost:1455/auth/callback?code=auth-code&state=demo"
        callback_state = FlowState(
            page_type="oauth_callback",
            continue_url=callback_url,
            current_url=callback_url,
        )

        with mock.patch.object(
            client,
            "_bootstrap_oauth_session",
            return_value="https://auth.openai.com/log-in",
        ), mock.patch.object(
            client,
            "_submit_authorize_continue",
            return_value=callback_state,
        ) as submit_email, mock.patch.object(
            client,
            "_exchange_code_for_tokens",
            return_value={"access_token": "at", "refresh_token": "rt"},
        ):
            tokens = client.login_and_get_tokens(
                "existing@example.com",
                "",
                "device-id",
                user_agent="UA",
                sec_ch_ua='"Chromium";v="136"',
                impersonate="chrome136",
                prefer_passwordless_login=True,
                allow_phone_verification=True,
                force_new_browser=False,
                resume_authenticated_session=True,
                login_source="interactive_phone_verification",
            )

        self.assertIsNone(tokens)
        submit_email.assert_not_called()
        self.assertIn("登录会话已失效", client.last_error)


class SMSToMeConfigTests(unittest.TestCase):
    def test_parse_country_slugs_accepts_csv_and_iterables(self):
        self.assertEqual(
            parse_country_slugs("united-kingdom, poland;finland"),
            ["united-kingdom", "poland", "finland"],
        )
        self.assertEqual(
            parse_country_slugs(["united-kingdom", "poland", "united_kingdom"]),
            ["united-kingdom", "poland"],
        )

    def test_phone_service_enabled_when_pool_file_exists(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            pool_path = Path(tmp_dir) / "phones.txt"
            pool_path.write_text("+447456344799\tunited-kingdom\thttps://example.com\n", encoding="utf-8")

            service = SMSToMePhoneService({"smstome_global_file": str(pool_path)})
            self.assertTrue(service.enabled)

    def test_phone_service_disabled_for_empty_pool_without_cookie(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            pool_path = Path(tmp_dir) / "phones.txt"
            pool_path.write_text("", encoding="utf-8")

            service = SMSToMePhoneService({"smstome_global_file": str(pool_path)})
            self.assertFalse(service.enabled)

    def test_wait_for_code_forwards_cookie_timeout_and_poll_interval(self):
        entry = PhoneEntry(
            country_slug="united-kingdom",
            phone="+447456344799",
            detail_url="https://example.com/phone/1",
        )
        service = SMSToMePhoneService(
            {
                "smstome_cookie": "cf_clearance=demo",
                "smstome_otp_timeout_seconds": "66",
                "smstome_poll_interval_seconds": "7",
            }
        )

        with mock.patch("platforms.chatgpt.phone_service.wait_for_otp", return_value="123456") as mocked:
            code = service.wait_for_code(entry)

        self.assertEqual(code, "123456")
        mocked.assert_called_once()
        kwargs = mocked.call_args.kwargs
        self.assertEqual(kwargs["cookie_header"], "cf_clearance=demo")
        self.assertEqual(kwargs["timeout"], 66)
        self.assertEqual(kwargs["poll_interval"], 7)
        self.assertFalse(kwargs["raise_on_timeout"])

    def test_ensure_pool_ready_syncs_with_configured_page_limit(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            pool_path = Path(tmp_dir) / "phones.txt"
            service = SMSToMePhoneService(
                {
                    "smstome_cookie": "cf_clearance=demo",
                    "smstome_country_slugs": "united-kingdom",
                    "smstome_global_file": str(pool_path),
                    "smstome_sync_max_pages_per_country": "9",
                }
            )

            with mock.patch("platforms.chatgpt.phone_service.update_global_phone_list", return_value=3) as mocked:
                service.ensure_pool_ready()

        mocked.assert_called_once()
        kwargs = mocked.call_args.kwargs
        self.assertEqual(kwargs["cookie_header"], "cf_clearance=demo")
        self.assertEqual(kwargs["countries"], ["united-kingdom"])
        self.assertEqual(kwargs["output_path"], pool_path)
        self.assertEqual(kwargs["max_pages_per_country"], 9)


class _LeadBeeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class _LeadBeeSession:
    def __init__(self, *payloads):
        self.responses = [_LeadBeeResponse(payload) for payload in payloads]
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if not self.responses:
            raise AssertionError(f"unexpected LeadBee request: {url}")
        return self.responses.pop(0)


def _leadbee_card(**overrides):
    card = {
        "status": "processing",
        "phone": None,
        "sms_code": None,
        "is_terminal": False,
        "can_auto_poll": True,
        "can_refresh": True,
        "can_replace": True,
        "card_type": "platform",
        "has_expiry": True,
        "seconds_remaining": 120,
        "number_queue_seconds": 2,
        "valid_until": None,
    }
    card.update(overrides)
    return card


class _OpenAPIClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class _OpenAPIClient:
    def __init__(
        self,
        *,
        create=(),
        get=(),
        replace=(),
        cancel=(),
    ):
        self.results = {
            "create": list(create),
            "get": list(get),
            "replace": list(replace),
            "cancel": list(cancel),
        }
        self.calls = []

    def _result(self, operation):
        if not self.results[operation]:
            raise AssertionError(f"unexpected {operation} call")
        result = self.results[operation].pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def create_order(
        self,
        client_order_id,
        product_id,
        quantity=1,
        *,
        idempotency_key,
    ):
        self.calls.append(
            (
                "create",
                client_order_id,
                product_id,
                quantity,
                idempotency_key,
            )
        )
        return self._result("create")

    def get_order(self, order_id):
        self.calls.append(("get", order_id))
        return self._result("get")

    def replace_order(self, order_id, *, idempotency_key):
        self.calls.append(("replace", order_id, idempotency_key))
        return self._result("replace")

    def cancel_order(self, order_id, *, idempotency_key):
        self.calls.append(("cancel", order_id, idempotency_key))
        return self._result("cancel")


def _open_api_config(**overrides):
    config = {
        "leadbee_api_enabled": True,
        "leadbee_api_key": "ak_test_phone_fixture",
        "leadbee_api_secret": "secret_test_phone_fixture",
        "leadbee_api_product_id": "product_phone_fixture",
        "leadbee_api_client_order_id": "client_order_phone_fixture",
        "leadbee_phone_timeout_seconds": 30,
        "leadbee_total_timeout_seconds": 60,
        "leadbee_otp_timeout_seconds": 30,
        "leadbee_poll_interval_seconds": 1,
    }
    config.update(overrides)
    return config


def _open_api_service(client, *, clock=None, logs=None, **config):
    resolved_clock = clock or _OpenAPIClock()
    resolved_logs = logs if logs is not None else []
    service = LeadBeeOpenAPIPhoneService(
        _open_api_config(**config),
        log_fn=resolved_logs.append,
        client=client,
        sleep_fn=resolved_clock.sleep,
        monotonic=resolved_clock.monotonic,
    )
    return service, resolved_clock, resolved_logs


class LeadBeeOpenAPIPhoneServiceTests(unittest.TestCase):
    def test_factory_api_flag_is_truthy_and_uses_fixed_client_configuration(self):
        for enabled in (True, 1, "1", "true", "yes", "on", " TRUE "):
            with (
                self.subTest(enabled=enabled),
                mock.patch(
                    "platforms.chatgpt.phone_service.LeadBeeOpenAPIClient"
                ) as client_class,
            ):
                service = create_phone_service(
                    _open_api_config(leadbee_api_enabled=enabled)
                )

                self.assertIsInstance(service, LeadBeeOpenAPIPhoneService)
                self.assertTrue(service.enabled)
                client_class.assert_called_once_with(
                    api_key="ak_test_phone_fixture",
                    api_secret="secret_test_phone_fixture",
                )

    def test_factory_enabled_but_incomplete_returns_disabled_api_service(self):
        service = create_phone_service(
            {
                "leadbee_api_enabled": True,
                "leadbee_code": "bei-sms-LEGACY-FIXTURE",
            }
        )

        self.assertIsInstance(service, LeadBeeOpenAPIPhoneService)
        self.assertFalse(service.enabled)

    def test_factory_disabled_preserves_legacy_and_smstome_selection(self):
        for disabled in (False, 0, "0", "false", "off", ""):
            with self.subTest(disabled=disabled):
                legacy = create_phone_service(
                    {
                        "leadbee_api_enabled": disabled,
                        "leadbee_code": "bei-sms-LEGACY-FIXTURE",
                    }
                )
                fallback = create_phone_service({"leadbee_api_enabled": disabled})

                self.assertIsInstance(legacy, LeadBeePhoneService)
                self.assertIsInstance(fallback, SMSToMePhoneService)

    def test_processing_to_waiting_phone_then_completed_code(self):
        phone = "+12025550123"
        client = _OpenAPIClient(
            create=[
                {
                    "order_id": "order_fixture_001",
                    "status": "PROCESSING",
                    "next_poll_after_seconds": 2,
                }
            ],
            get=[
                {
                    "order_id": "order_fixture_001",
                    "status": "WAITING_CODE",
                    "phone": phone,
                    "next_poll_after_seconds": 3,
                },
                {
                    "order_id": "order_fixture_001",
                    "status": "COMPLETED",
                    "phone": phone,
                    "code": "654321",
                },
            ],
        )
        service, clock, _logs = _open_api_service(client)

        entry = service.acquire_phone()
        code = service.wait_for_code(entry)

        self.assertEqual(entry.country_slug, "leadbee-api")
        self.assertEqual(entry.phone, phone)
        self.assertIn("order_fixture_001", entry.detail_url)
        self.assertNotIn(phone, entry.detail_url)
        self.assertEqual(code, "654321")
        self.assertEqual(clock.sleeps, [2.0, 3.0])
        self.assertEqual(
            [call[0] for call in client.calls],
            ["create", "get", "get"],
        )

    def test_create_retries_reuse_client_reference_body_and_idempotency_key(self):
        client = _OpenAPIClient(
            create=[
                LeadBeeTransportError("fixture transport"),
                LeadBeeAPIError(
                    "fixture rate limit",
                    code="RATE_LIMITED",
                    status_code=429,
                    retry_after=4,
                ),
                {
                    "order_id": "order_fixture_retry",
                    "status": "WAITING_CODE",
                    "phone": "+12025550123",
                },
            ]
        )
        service, clock, _logs = _open_api_service(client)

        service.acquire_phone()

        create_calls = [call for call in client.calls if call[0] == "create"]
        self.assertEqual(len(create_calls), 3)
        self.assertEqual(len(set(create_calls)), 1)
        self.assertEqual(create_calls[0][3], 1)
        self.assertEqual(clock.sleeps, [1.0, 4.0])

    def test_create_retries_plain_503_with_the_same_request_identity(self):
        client = _OpenAPIClient(
            create=[
                LeadBeeAPIError(
                    "fixture unavailable",
                    code="HTTP_ERROR",
                    status_code=503,
                ),
                {
                    "order_id": "order_fixture_503",
                    "status": "WAITING_CODE",
                    "phone": "+12025550123",
                },
            ]
        )
        service, clock, _logs = _open_api_service(client)

        service.acquire_phone()

        create_calls = [call for call in client.calls if call[0] == "create"]
        self.assertEqual(len(create_calls), 2)
        self.assertEqual(create_calls[0], create_calls[1])
        self.assertEqual(clock.sleeps, [1.0])

    def test_nonretryable_create_failure_is_not_posted_again_on_same_service(self):
        client = _OpenAPIClient(
            create=[
                LeadBeeAPIError(
                    "fixture authentication rejected",
                    code="AUTHENTICATION_FAILED",
                    status_code=401,
                )
            ]
        )
        service, _clock, _logs = _open_api_service(client)

        with self.assertRaises(LeadBeeAPIError):
            service.acquire_phone()
        with self.assertRaises(RuntimeError):
            service.acquire_phone()

        self.assertEqual(
            [call[0] for call in client.calls].count("create"),
            1,
        )

    def test_auth_permission_product_and_conflict_errors_override_retryable_http(self):
        cases = (
            ("AUTHENTICATION_FAILED", 429),
            ("SIGNATURE_INVALID", 503),
            ("PERMISSION_DENIED", 429),
            ("IP_NOT_ALLOWED", 503),
            ("PRODUCT_NOT_FOUND", 429),
            ("IDEMPOTENCY_CONFLICT", 503),
        )
        for error_code, status_code in cases:
            with self.subTest(error_code=error_code, status_code=status_code):
                client = _OpenAPIClient(
                    create=[
                        LeadBeeAPIError(
                            "fixture rejected",
                            code=error_code,
                            status_code=status_code,
                            retry_after=1,
                        )
                    ]
                )
                service, clock, _logs = _open_api_service(client)

                with self.assertRaises(LeadBeeAPIError):
                    service.acquire_phone()

                self.assertEqual(
                    [call[0] for call in client.calls].count("create"),
                    1,
                )
                self.assertEqual(clock.sleeps, [])

    def test_create_response_without_order_id_never_starts_a_second_order(self):
        client = _OpenAPIClient(
            create=[
                {
                    "status": "PROCESSING",
                    "next_poll_after_seconds": 1,
                }
            ]
        )
        service, _clock, _logs = _open_api_service(client)

        with self.assertRaisesRegex(RuntimeError, "订单编号"):
            service.acquire_phone()
        with self.assertRaises(RuntimeError):
            service.acquire_phone()

        self.assertEqual(
            [call[0] for call in client.calls].count("create"),
            1,
        )

    def test_replace_retries_then_acquire_polls_same_order_for_different_phone(self):
        old_phone = "+12025550123"
        new_phone = "+14155550199"
        client = _OpenAPIClient(
            create=[
                {
                    "order_id": "order_fixture_replace",
                    "status": "WAITING_CODE",
                    "phone": old_phone,
                }
            ],
            replace=[
                LeadBeeTransportError("fixture transport"),
                {
                    "order_id": "order_fixture_replace",
                    "status": "REPLACING",
                    "next_poll_after_seconds": 2,
                },
            ],
            get=[
                {
                    "order_id": "order_fixture_replace",
                    "status": "WAITING_CODE",
                    "phone": old_phone,
                    "next_poll_after_seconds": 3,
                },
                {
                    "order_id": "order_fixture_replace",
                    "status": "WAITING_CODE",
                    "phone": new_phone,
                },
            ],
        )
        service, clock, _logs = _open_api_service(client)
        first_entry = service.acquire_phone()

        self.assertTrue(
            service.request_replacement(first_entry.phone, reason="openai_rejected")
        )
        replacement_entry = service.acquire_phone()

        self.assertEqual(replacement_entry.phone, new_phone)
        self.assertEqual(service.order_id, "order_fixture_replace")
        self.assertEqual(
            [call[0] for call in client.calls].count("create"),
            1,
        )
        replace_calls = [call for call in client.calls if call[0] == "replace"]
        self.assertEqual(len(replace_calls), 2)
        self.assertEqual(replace_calls[0], replace_calls[1])
        self.assertEqual(clock.sleeps, [1.0, 2.0, 3.0])

    def test_cancel_retries_then_polls_canceling_to_canceled(self):
        client = _OpenAPIClient(
            create=[
                {
                    "order_id": "order_fixture_cancel",
                    "status": "WAITING_CODE",
                    "phone": "+12025550123",
                }
            ],
            cancel=[
                LeadBeeAPIError(
                    "fixture replay unavailable",
                    code="REPLAY_PROTECTION_UNAVAILABLE",
                    status_code=503,
                ),
                {
                    "order_id": "order_fixture_cancel",
                    "status": "CANCELING",
                    "next_poll_after_seconds": 2,
                },
            ],
            get=[
                {
                    "order_id": "order_fixture_cancel",
                    "status": "CANCELED",
                }
            ],
        )
        service, clock, _logs = _open_api_service(client)
        service.acquire_phone()

        self.assertTrue(service.cancel_active())
        self.assertFalse(service.card_at_risk)
        self.assertEqual(service.last_cancel_error, "")
        cancel_calls = [call for call in client.calls if call[0] == "cancel"]
        self.assertEqual(len(cancel_calls), 2)
        self.assertEqual(cancel_calls[0], cancel_calls[1])
        self.assertEqual(clock.sleeps, [1.0, 2.0])

    def test_cancel_without_order_is_safe_and_completed_order_is_not_recanceled(self):
        empty_service, _clock, _logs = _open_api_service(_OpenAPIClient())
        self.assertFalse(empty_service.cancel_active())

        client = _OpenAPIClient(
            create=[
                {
                    "order_id": "order_fixture_completed",
                    "status": "WAITING_CODE",
                    "phone": "+12025550123",
                }
            ],
            get=[
                {
                    "order_id": "order_fixture_completed",
                    "status": "COMPLETED",
                    "code": "654321",
                }
            ],
        )
        service, _clock, _logs = _open_api_service(client)
        entry = service.acquire_phone()
        self.assertEqual(service.wait_for_code(entry), "654321")

        self.assertFalse(service.cancel_active())
        self.assertFalse(service.card_at_risk)
        self.assertNotIn("cancel", [call[0] for call in client.calls])

    def test_expired_and_canceled_are_settled_terminal_states(self):
        for status in ("EXPIRED", "CANCELED"):
            with self.subTest(status=status):
                client = _OpenAPIClient(
                    create=[
                        {
                            "order_id": f"order_fixture_{status.lower()}",
                            "status": status,
                        }
                    ]
                )
                service, _clock, _logs = _open_api_service(client)

                with self.assertRaises(RuntimeError):
                    service.acquire_phone()

                self.assertFalse(service.card_at_risk)
                self.assertEqual(
                    [call[0] for call in client.calls].count("create"),
                    1,
                )

    def test_unknown_manual_review_and_unrecognized_states_are_quarantined(self):
        for status in ("UNKNOWN", "MANUAL_REVIEW", "FUTURE_STATE"):
            with self.subTest(status=status):
                client = _OpenAPIClient(
                    create=[
                        {
                            "order_id": f"order_fixture_{status.lower()}",
                            "status": status,
                        }
                    ]
                )
                service, _clock, _logs = _open_api_service(client)

                with self.assertRaisesRegex(RuntimeError, "隔离"):
                    service.acquire_phone()
                with self.assertRaisesRegex(RuntimeError, "隔离"):
                    service.acquire_phone()

                self.assertEqual(
                    service.order_id,
                    f"order_fixture_{status.lower()}",
                )
                self.assertTrue(service.card_at_risk)
                self.assertEqual(
                    [call[0] for call in client.calls].count("create"),
                    1,
                )

    def test_unrecognized_status_text_is_not_copied_to_logs_or_exceptions(self):
        echoed_status = (
            "ak_test_phone_fixture-secret_test_phone_fixture-+12025550123-654321"
        )
        client = _OpenAPIClient(
            create=[
                {
                    "order_id": "order_fixture_echoed_status",
                    "status": echoed_status,
                }
            ]
        )
        logs = []
        service, _clock, _logs = _open_api_service(client, logs=logs)

        with self.assertRaises(RuntimeError) as caught:
            service.acquire_phone()

        rendered = f"{caught.exception}\n" + "\n".join(logs)
        self.assertNotIn(echoed_status, rendered)
        self.assertNotIn("ak_test_phone_fixture", rendered)
        self.assertNotIn("secret_test_phone_fixture", rendered)
        self.assertNotIn("+12025550123", rendered)
        self.assertNotIn("654321", rendered)
        self.assertIn("UNRECOGNIZED", rendered)

    def test_invalid_phone_is_not_returned_and_invalid_completed_code_is_ignored(self):
        phone = "+12025550123"
        client = _OpenAPIClient(
            create=[
                {
                    "order_id": "order_fixture_validation",
                    "status": "WAITING_CODE",
                    "phone": "202-555-0123",
                    "next_poll_after_seconds": 1,
                }
            ],
            get=[
                {
                    "order_id": "order_fixture_validation",
                    "status": "WAITING_CODE",
                    "phone": phone,
                    "next_poll_after_seconds": 1,
                },
                {
                    "order_id": "order_fixture_validation",
                    "status": "COMPLETED",
                    "code": "12A456",
                },
            ],
        )
        service, _clock, _logs = _open_api_service(client)

        entry = service.acquire_phone()

        self.assertEqual(entry.phone, phone)
        self.assertIsNone(service.wait_for_code(entry))

    def test_logs_exclude_credentials_full_phone_and_verification_code(self):
        phone = "+12025550123"
        code = "654321"
        client = _OpenAPIClient(
            create=[
                {
                    "order_id": "order_fixture_redaction",
                    "status": "WAITING_CODE",
                    "phone": phone,
                    "next_poll_after_seconds": 1,
                }
            ],
            get=[
                {
                    "order_id": "order_fixture_redaction",
                    "status": "COMPLETED",
                    "code": code,
                }
            ],
        )
        logs = []
        service, _clock, _logs = _open_api_service(client, logs=logs)

        entry = service.acquire_phone()
        self.assertEqual(service.wait_for_code(entry), code)

        rendered = "\n".join(logs)
        self.assertNotIn("ak_test_phone_fixture", rendered)
        self.assertNotIn("secret_test_phone_fixture", rendered)
        self.assertNotIn(phone, rendered)
        self.assertNotIn(code, rendered)
        self.assertIn("***", rendered)


class LeadBeePhoneServiceTests(unittest.TestCase):
    def test_factory_prefers_leadbee_when_exchange_code_is_supplied(self):
        service = create_phone_service({"leadbee_code": " bei-sms-DEMO-CODE "})

        self.assertIsInstance(service, LeadBeePhoneService)
        self.assertTrue(service.enabled)
        self.assertEqual(service.max_attempts, 3)

    def test_http_error_redacts_exchange_code_from_response_text(self):
        exchange_code = "bei-sms-HTTP-SECRET-CODE"
        session = mock.Mock()
        session.post.return_value = mock.Mock(
            status_code=502,
            text=f"upstream rejected exchange code {exchange_code}",
        )
        service = LeadBeePhoneService(
            {"leadbee_code": exchange_code},
            session=session,
        )

        with self.assertRaises(RuntimeError) as caught:
            service.acquire_phone()

        error_message = str(caught.exception)
        self.assertNotIn(exchange_code, error_message)
        self.assertIn("[LeadBee兑换码已脱敏]", error_message)

    def test_json_error_redacts_exchange_code_from_message(self):
        exchange_code = "bei-sms-JSON-SECRET-CODE"
        session = _LeadBeeSession(
            {
                "ok": False,
                "message": f"exchange code {exchange_code} is unavailable",
            }
        )
        service = LeadBeePhoneService(
            {"leadbee_code": exchange_code},
            session=session,
        )

        with self.assertRaises(RuntimeError) as caught:
            service.acquire_phone()

        error_message = str(caught.exception)
        self.assertNotIn(exchange_code, error_message)
        self.assertIn("[LeadBee兑换码已脱敏]", error_message)

    def test_provider_exception_redacts_exchange_code(self):
        exchange_code = "bei-sms-PROVIDER-SECRET-CODE"
        session = mock.Mock()
        session.post.side_effect = RuntimeError(
            f"provider rejected exchange code {exchange_code}"
        )
        service = LeadBeePhoneService(
            {"leadbee_code": exchange_code},
            session=session,
        )

        with self.assertRaises(RuntimeError) as caught:
            service.acquire_phone()

        error_message = str(caught.exception)
        self.assertNotIn(exchange_code, error_message)
        self.assertIn("[LeadBee兑换码已脱敏]", error_message)

    def test_acquire_phone_uses_one_session_for_activate_and_poll(self):
        logs = []
        session = _LeadBeeSession(
            {"ok": True, "card": _leadbee_card()},
            {
                "ok": True,
                "card": _leadbee_card(
                    status="number_ready",
                    phone="+447456344799",
                    number_queue_seconds=0,
                ),
            },
        )
        service = LeadBeePhoneService(
            {
                "leadbee_code": "bei-sms-DEMO-CODE",
                "leadbee_poll_interval_seconds": 1,
            },
            log_fn=logs.append,
            session=session,
        )

        with mock.patch("platforms.chatgpt.phone_service.time.sleep"):
            entry = service.acquire_phone()

        self.assertEqual(entry.phone, "+447456344799")
        self.assertEqual(entry.country_slug, "leadbee")
        self.assertEqual(
            [call[0] for call in session.calls],
            [
                "https://sms.leadbee.cn/smsbox/api/activate",
                "https://sms.leadbee.cn/smsbox/api/receive-sms",
            ],
        )
        self.assertEqual(
            [call[1]["json"] for call in session.calls],
            [
                {"code": "bei-sms-DEMO-CODE"},
                {"code": "bei-sms-DEMO-CODE"},
            ],
        )
        self.assertIn("[LeadBee] 正在排队获取号码，请稍候", logs)

    def test_provider_start_is_reported_once_immediately_before_activate(self):
        callback_call_counts = []
        session = _LeadBeeSession(
            {
                "ok": True,
                "card": _leadbee_card(
                    status="number_ready",
                    phone="+447456344799",
                    number_queue_seconds=0,
                ),
            }
        )
        broker = InteractivePhoneVerificationBroker(
            account_id=17,
            provider="leadbee",
            on_provider_start=lambda: callback_call_counts.append(len(session.calls)),
        )
        service = LeadBeePhoneService(
            {
                "leadbee_code": "bei-sms-START-CODE",
                "chatgpt_phone_progress_broker": broker,
            },
            session=session,
        )

        self.assertFalse(broker.snapshot()["provider_started"])
        service.acquire_phone()

        self.assertEqual(callback_call_counts, [0])
        self.assertTrue(broker.snapshot()["provider_started"])

    def test_activate_card_owned_by_another_session_is_not_reported_as_no_inventory(self):
        provider_started = mock.Mock()
        consumed = mock.Mock()
        restored = mock.Mock()
        session = _LeadBeeSession(
            {
                "ok": False,
                "error": "CARD_NOT_IN_SESSION",
                "message": "当前会话无权操作该卡密",
            }
        )
        broker = InteractivePhoneVerificationBroker(
            account_id=17,
            provider="leadbee",
            on_provider_start=provider_started,
            on_exchange_code_consumed=consumed,
            on_exchange_code_restored=restored,
        )
        service = LeadBeePhoneService(
            {
                "leadbee_code": "bei-sms-BUSY-CODE",
                "chatgpt_phone_progress_broker": broker,
            },
            session=session,
        )

        with self.assertRaisesRegex(RuntimeError, "另一会话"):
            service.acquire_phone()

        snapshot = broker.snapshot()
        self.assertEqual(snapshot["provider_error_code"], "CARD_NOT_IN_SESSION")
        self.assertFalse(snapshot["exchange_code_restoration_confirmed"])
        self.assertEqual(snapshot["exchange_code_settlement"], "active_unknown")
        self.assertFalse(snapshot["exchange_code_unusable"])
        provider_started.assert_called_once_with()
        restored.assert_not_called()
        consumed.assert_not_called()

    def test_poll_no_inventory_is_reported_as_released_not_browser_occupied(self):
        restored = mock.Mock()
        broker = InteractivePhoneVerificationBroker(
            account_id=17,
            provider="leadbee",
            on_exchange_code_restored=restored,
        )
        session = _LeadBeeSession(
            {"ok": True, "card": _leadbee_card(status="processing")},
            {
                "ok": False,
                "error": "CARD_NOT_IN_SESSION",
                "message": "当前会话无权操作该卡密",
            },
        )
        service = LeadBeePhoneService(
            {
                "leadbee_code": "bei-sms-POLL-BUSY-CODE",
                "leadbee_poll_interval_seconds": 1,
                "chatgpt_phone_progress_broker": broker,
            },
            session=session,
        )

        with mock.patch("platforms.chatgpt.phone_service.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "暂时无可用号码"):
                service.acquire_phone()

        snapshot = broker.snapshot()
        self.assertEqual(snapshot["provider_error_code"], "CARD_NOT_IN_SESSION")
        self.assertTrue(snapshot["exchange_code_restoration_confirmed"])
        self.assertFalse(snapshot["exchange_code_unusable"])
        self.assertFalse(service.card_at_risk)
        restored.assert_called_once_with()

    def test_terminal_unavailable_card_is_reported_as_restored(self):
        restored = mock.Mock()
        broker = InteractivePhoneVerificationBroker(
            account_id=17,
            provider="leadbee",
            on_exchange_code_restored=restored,
        )
        session = _LeadBeeSession(
            {
                "ok": True,
                "card": _leadbee_card(status="processing"),
            },
            {
                "ok": True,
                "card": _leadbee_card(
                    status="unavailable",
                    is_terminal=True,
                    can_auto_poll=False,
                    can_refresh=False,
                ),
            },
        )
        service = LeadBeePhoneService(
            {
                "leadbee_code": "bei-sms-TERMINAL-NO-INVENTORY",
                "leadbee_poll_interval_seconds": 1,
                "chatgpt_phone_progress_broker": broker,
            },
            session=session,
        )

        with mock.patch("platforms.chatgpt.phone_service.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "暂时无可用号码"):
                service.acquire_phone()

        self.assertFalse(service.card_at_risk)
        self.assertTrue(
            broker.snapshot()["exchange_code_restoration_confirmed"]
        )
        restored.assert_called_once_with()

    def test_phone_timeout_keeps_polling_when_provider_refuses_cancellation(self):
        consumed = mock.Mock()
        restored = mock.Mock()
        broker = InteractivePhoneVerificationBroker(
            account_id=17,
            provider="leadbee",
            on_exchange_code_consumed=consumed,
            on_exchange_code_restored=restored,
        )
        session = _LeadBeeSession(
            {
                "ok": True,
                "card": _leadbee_card(
                    status="processing",
                    number_queue_seconds=180,
                ),
            },
            {
                "ok": False,
                "error": "CANCEL_NOT_ALLOWED",
                "message": "任务当前不能取消",
            },
            {
                "ok": False,
                "error": "CARD_NOT_IN_SESSION",
                "message": "当前会话无权操作该卡密",
            },
        )
        service = LeadBeePhoneService(
            {
                "leadbee_code": "bei-sms-UNCANCELABLE-QUEUE",
                "leadbee_phone_timeout_seconds": 5,
                "leadbee_poll_interval_seconds": 1,
                "chatgpt_phone_progress_broker": broker,
            },
            session=session,
        )

        clock = {"now": 0.0}
        with mock.patch(
            "platforms.chatgpt.phone_service.time.monotonic",
            side_effect=lambda: clock["now"],
        ), mock.patch(
            "platforms.chatgpt.phone_service.time.sleep",
            side_effect=lambda _seconds: clock.update(now=5.0),
        ):
            with self.assertRaisesRegex(RuntimeError, "暂时无可用号码"):
                service.acquire_phone()

        self.assertEqual(
            [call[0] for call in session.calls],
            [
                "https://sms.leadbee.cn/smsbox/api/activate",
                "https://sms.leadbee.cn/smsbox/api/cancel",
                "https://sms.leadbee.cn/smsbox/api/receive-sms",
            ],
        )
        self.assertFalse(service.card_at_risk)
        snapshot = broker.snapshot()
        self.assertEqual(snapshot["provider_error_code"], "CARD_NOT_IN_SESSION")
        self.assertFalse(snapshot["exchange_code_unusable"])
        consumed.assert_not_called()
        restored.assert_called_once_with()

    def test_replacement_timeout_keeps_polling_without_poisoning_card_state(self):
        consumed = mock.Mock()
        broker = InteractivePhoneVerificationBroker(
            account_id=17,
            provider="leadbee",
            on_exchange_code_consumed=consumed,
        )
        first_phone = "+447456344799"
        replacement_phone = "+447911123456"
        session = _LeadBeeSession(
            {
                "ok": True,
                "card": _leadbee_card(
                    status="number_ready",
                    phone=first_phone,
                ),
            },
            {
                "ok": True,
                "card": _leadbee_card(status="processing", phone=None),
            },
            {
                "ok": False,
                "error": "CANCEL_NOT_ALLOWED",
                "message": "任务当前不能取消",
            },
            {
                "ok": True,
                "card": _leadbee_card(
                    status="number_ready",
                    phone=replacement_phone,
                ),
            },
        )
        service = LeadBeePhoneService(
            {
                "leadbee_code": "bei-sms-UNCANCELABLE-REPLACEMENT",
                "leadbee_phone_timeout_seconds": 5,
                "leadbee_total_timeout_seconds": 30,
                "leadbee_poll_interval_seconds": 1,
                "chatgpt_phone_progress_broker": broker,
            },
            session=session,
        )

        clock = {"now": 0.0}
        with mock.patch(
            "platforms.chatgpt.phone_service.time.monotonic",
            side_effect=lambda: clock["now"],
        ), mock.patch(
            "platforms.chatgpt.phone_service.time.sleep",
            side_effect=lambda _seconds: clock.update(now=5.0),
        ):
            first_entry = service.acquire_phone()
            service.request_replacement(first_entry.phone)
            replacement_entry = service.acquire_phone()

        self.assertEqual(replacement_entry.phone, replacement_phone)
        self.assertEqual(
            [call[0] for call in session.calls],
            [
                "https://sms.leadbee.cn/smsbox/api/activate",
                "https://sms.leadbee.cn/smsbox/api/replace-number",
                "https://sms.leadbee.cn/smsbox/api/cancel",
                "https://sms.leadbee.cn/smsbox/api/receive-sms",
            ],
        )
        snapshot = broker.snapshot()
        self.assertEqual(snapshot["provider_error_code"], "")
        self.assertFalse(snapshot["exchange_code_unusable"])
        consumed.assert_not_called()

    def test_total_deadline_quarantines_uncancelable_active_card(self):
        consumed = mock.Mock()
        broker = InteractivePhoneVerificationBroker(
            account_id=17,
            provider="leadbee",
            on_exchange_code_consumed=consumed,
        )
        session = _LeadBeeSession(
            {"ok": True, "card": _leadbee_card(status="processing")},
            {
                "ok": False,
                "error": "CANCEL_NOT_ALLOWED",
                "message": "任务当前不能取消",
            },
        )
        service = LeadBeePhoneService(
            {
                "leadbee_code": "bei-sms-TOTAL-DEADLINE",
                "leadbee_phone_timeout_seconds": 20,
                "leadbee_total_timeout_seconds": 5,
                "leadbee_poll_interval_seconds": 1,
                "chatgpt_phone_progress_broker": broker,
            },
            session=session,
        )

        with mock.patch(
            "platforms.chatgpt.phone_service.time.monotonic",
            side_effect=[0, 5],
        ):
            with self.assertRaisesRegex(RuntimeError, "结算期限"):
                service.acquire_phone()

        snapshot = broker.snapshot()
        self.assertEqual(snapshot["exchange_code_settlement"], "active_unknown")
        self.assertEqual(snapshot["provider_error_code"], "")
        self.assertFalse(snapshot["exchange_code_unusable"])
        self.assertTrue(service.card_at_risk)
        consumed.assert_not_called()

    def test_late_activate_response_cannot_escape_total_deadline(self):
        clock = {"now": 0.0}
        calls = []

        def post(url, **kwargs):
            calls.append((url, kwargs))
            if url.endswith("/api/activate"):
                clock["now"] = 6.0
                return _LeadBeeResponse(
                    {
                        "ok": True,
                        "card": _leadbee_card(
                            status="number_ready",
                            phone="+447456344799",
                        ),
                    }
                )
            if url.endswith("/api/cancel"):
                return _LeadBeeResponse(
                    {
                        "ok": False,
                        "error": "CANCEL_NOT_ALLOWED",
                        "message": "任务当前不能取消",
                    }
                )
            raise AssertionError(f"unexpected LeadBee request: {url}")

        session = mock.Mock()
        session.post.side_effect = post
        broker = InteractivePhoneVerificationBroker(
            account_id=17,
            provider="leadbee",
        )
        service = LeadBeePhoneService(
            {
                "leadbee_code": "bei-sms-LATE-ACTIVATE",
                "leadbee_total_timeout_seconds": 5,
                "chatgpt_phone_progress_broker": broker,
            },
            session=session,
        )

        with mock.patch(
            "platforms.chatgpt.phone_service.time.monotonic",
            side_effect=lambda: clock["now"],
        ):
            with self.assertRaisesRegex(RuntimeError, "结算期限"):
                service.acquire_phone()

        self.assertEqual(
            [url.rsplit("/", 1)[-1] for url, _ in calls],
            ["activate"],
        )
        self.assertEqual(
            broker.snapshot()["exchange_code_settlement"],
            "active_unknown",
        )
        self.assertFalse(broker.snapshot()["exchange_code_unusable"])

    def test_late_unavailable_phone_response_restores_before_deadline_error(self):
        clock = {"now": 0.0}
        calls = []
        restored = mock.Mock()

        def post(url, **kwargs):
            calls.append((url, kwargs))
            if url.endswith("/api/activate"):
                return _LeadBeeResponse(
                    {"ok": True, "card": _leadbee_card(status="processing")}
                )
            if url.endswith("/api/receive-sms"):
                clock["now"] = 6.0
                return _LeadBeeResponse(
                    {
                        "ok": True,
                        "card": _leadbee_card(
                            status="unavailable",
                            is_terminal=True,
                            can_auto_poll=False,
                            can_refresh=False,
                        ),
                    }
                )
            raise AssertionError(f"unexpected LeadBee request: {url}")

        session = mock.Mock()
        session.post.side_effect = post
        broker = InteractivePhoneVerificationBroker(
            account_id=17,
            provider="leadbee",
            on_exchange_code_restored=restored,
        )
        service = LeadBeePhoneService(
            {
                "leadbee_code": "bei-sms-LATE-PHONE-UNAVAILABLE",
                "leadbee_total_timeout_seconds": 5,
                "leadbee_poll_interval_seconds": 1,
                "chatgpt_phone_progress_broker": broker,
            },
            session=session,
        )

        with mock.patch(
            "platforms.chatgpt.phone_service.time.monotonic",
            side_effect=lambda: clock["now"],
        ), mock.patch("platforms.chatgpt.phone_service.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "结算期限.*确认恢复"):
                service.acquire_phone()

        self.assertEqual(
            [url.rsplit("/", 1)[-1] for url, _ in calls],
            ["activate", "receive-sms"],
        )
        snapshot = broker.snapshot()
        self.assertEqual(snapshot["exchange_code_settlement"], "restored")
        self.assertTrue(snapshot["exchange_code_restoration_confirmed"])
        self.assertFalse(snapshot["exchange_code_unusable"])
        restored.assert_called_once_with()

    def test_late_unavailable_sms_response_restores_before_deadline_error(self):
        clock = {"now": 0.0}
        calls = []
        restored = mock.Mock()
        phone = "+447456344799"

        def post(url, **kwargs):
            calls.append((url, kwargs))
            if url.endswith("/api/activate"):
                return _LeadBeeResponse(
                    {
                        "ok": True,
                        "card": _leadbee_card(
                            status="number_ready",
                            phone=phone,
                        ),
                    }
                )
            if url.endswith("/api/receive-sms"):
                clock["now"] = 6.0
                return _LeadBeeResponse(
                    {
                        "ok": True,
                        "card": _leadbee_card(
                            status="unavailable",
                            is_terminal=True,
                            can_auto_poll=False,
                            can_refresh=False,
                        ),
                    }
                )
            raise AssertionError(f"unexpected LeadBee request: {url}")

        session = mock.Mock()
        session.post.side_effect = post
        broker = InteractivePhoneVerificationBroker(
            account_id=17,
            provider="leadbee",
            on_exchange_code_restored=restored,
        )
        service = LeadBeePhoneService(
            {
                "leadbee_code": "bei-sms-LATE-SMS-UNAVAILABLE",
                "leadbee_total_timeout_seconds": 5,
                "leadbee_otp_timeout_seconds": 30,
                "chatgpt_phone_progress_broker": broker,
            },
            session=session,
        )

        with mock.patch(
            "platforms.chatgpt.phone_service.time.monotonic",
            side_effect=lambda: clock["now"],
        ):
            entry = service.acquire_phone()
            with self.assertRaisesRegex(RuntimeError, "结算期限.*确认恢复"):
                service.wait_for_code(entry)

        self.assertEqual(
            [url.rsplit("/", 1)[-1] for url, _ in calls],
            ["activate", "receive-sms"],
        )
        snapshot = broker.snapshot()
        self.assertEqual(snapshot["exchange_code_settlement"], "restored")
        self.assertTrue(snapshot["exchange_code_restoration_confirmed"])
        self.assertFalse(snapshot["exchange_code_unusable"])
        restored.assert_called_once_with()

    def test_late_sms_response_is_not_accepted_after_total_deadline(self):
        clock = {"now": 0.0}
        calls = []
        phone = "+447456344799"

        def post(url, **kwargs):
            calls.append((url, kwargs))
            if url.endswith("/api/activate"):
                return _LeadBeeResponse(
                    {
                        "ok": True,
                        "card": _leadbee_card(
                            status="number_ready",
                            phone=phone,
                        ),
                    }
                )
            if url.endswith("/api/receive-sms"):
                clock["now"] = 6.0
                return _LeadBeeResponse(
                    {
                        "ok": True,
                        "card": _leadbee_card(
                            status="sms_received",
                            phone=phone,
                            sms_code="654321",
                            is_terminal=True,
                            can_auto_poll=False,
                            can_refresh=False,
                        ),
                    }
                )
            if url.endswith("/api/cancel"):
                return _LeadBeeResponse(
                    {
                        "ok": False,
                        "error": "CANCEL_NOT_ALLOWED",
                        "message": "任务当前不能取消",
                    }
                )
            raise AssertionError(f"unexpected LeadBee request: {url}")

        session = mock.Mock()
        session.post.side_effect = post
        broker = InteractivePhoneVerificationBroker(
            account_id=17,
            provider="leadbee",
        )
        service = LeadBeePhoneService(
            {
                "leadbee_code": "bei-sms-LATE-SMS",
                "leadbee_total_timeout_seconds": 5,
                "leadbee_otp_timeout_seconds": 30,
                "chatgpt_phone_progress_broker": broker,
            },
            session=session,
        )

        with mock.patch(
            "platforms.chatgpt.phone_service.time.monotonic",
            side_effect=lambda: clock["now"],
        ):
            entry = service.acquire_phone()
            with self.assertRaisesRegex(RuntimeError, "结算期限"):
                service.wait_for_code(entry)

        self.assertEqual(
            [url.rsplit("/", 1)[-1] for url, _ in calls],
            ["activate", "receive-sms"],
        )
        self.assertEqual(
            broker.snapshot()["exchange_code_settlement"],
            "unusable",
        )
        self.assertTrue(broker.snapshot()["exchange_code_unusable"])

    def test_poll_card_with_error_payload_is_isolated_as_foreign_session(self):
        consumed = mock.Mock()
        restored = mock.Mock()
        broker = InteractivePhoneVerificationBroker(
            account_id=17,
            provider="leadbee",
            on_exchange_code_consumed=consumed,
            on_exchange_code_restored=restored,
        )
        session = _LeadBeeSession(
            {"ok": True, "card": _leadbee_card(status="processing")},
            {
                "ok": False,
                "error": "CARD_NOT_IN_SESSION",
                "message": "当前会话无权操作该卡密",
                # LeadBee includes card when another browser/session still
                # owns the task.  This must not be returned to our pool.
                "card": _leadbee_card(status="processing"),
            },
        )
        service = LeadBeePhoneService(
            {
                "leadbee_code": "bei-sms-POLL-FOREIGN-SESSION",
                "leadbee_poll_interval_seconds": 1,
                "chatgpt_phone_progress_broker": broker,
            },
            session=session,
        )

        with mock.patch("platforms.chatgpt.phone_service.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "另一会话"):
                service.acquire_phone()

        snapshot = broker.snapshot()
        self.assertEqual(snapshot["provider_error_code"], "CARD_NOT_IN_SESSION")
        self.assertFalse(snapshot["exchange_code_restoration_confirmed"])
        self.assertEqual(snapshot["exchange_code_settlement"], "active_unknown")
        self.assertFalse(snapshot["exchange_code_unusable"])
        self.assertTrue(service.card_at_risk)
        restored.assert_not_called()
        consumed.assert_not_called()

    def test_wait_for_code_returns_sms_from_terminal_received_card(self):
        session = _LeadBeeSession(
            {
                "ok": True,
                "card": _leadbee_card(
                    status="number_ready",
                    phone="+447456344799",
                ),
            },
            {
                "ok": True,
                "card": _leadbee_card(
                    status="waiting_sms",
                    phone="+447456344799",
                ),
            },
            {
                "ok": True,
                "card": _leadbee_card(
                    status="sms_received",
                    phone="+447456344799",
                    sms_code="654321",
                    is_terminal=True,
                    can_auto_poll=False,
                    can_refresh=False,
                ),
            },
        )
        service = LeadBeePhoneService(
            {
                "leadbee_code": "bei-sms-DEMO-CODE",
                "leadbee_poll_interval_seconds": 1,
            },
            session=session,
        )
        entry = service.acquire_phone()

        with mock.patch("platforms.chatgpt.phone_service.time.sleep"):
            code = service.wait_for_code(entry)

        self.assertEqual(code, "654321")

    def test_wait_for_code_restores_terminal_unavailable_card(self):
        restored = mock.Mock()
        broker = InteractivePhoneVerificationBroker(
            account_id=17,
            provider="leadbee",
            on_exchange_code_restored=restored,
        )
        session = _LeadBeeSession(
            {
                "ok": True,
                "card": _leadbee_card(
                    status="number_ready",
                    phone="+447456344799",
                ),
            },
            {
                "ok": True,
                "card": _leadbee_card(
                    status="unavailable",
                    phone=None,
                    is_terminal=True,
                    can_auto_poll=False,
                    can_refresh=False,
                ),
            },
        )
        service = LeadBeePhoneService(
            {
                "leadbee_code": "bei-sms-SMS-TERMINAL-UNAVAILABLE",
                "leadbee_poll_interval_seconds": 1,
                "chatgpt_phone_progress_broker": broker,
            },
            session=session,
        )

        entry = service.acquire_phone()
        with mock.patch("platforms.chatgpt.phone_service.time.sleep"):
            code = service.wait_for_code(entry)

        self.assertIsNone(code)
        self.assertFalse(service.card_at_risk)
        self.assertTrue(
            broker.snapshot()["exchange_code_restoration_confirmed"]
        )
        restored.assert_called_once_with()

    def test_rejected_phone_is_replaced_in_same_session_before_next_acquire(self):
        logs = []
        first_phone = "+447456344799"
        replacement_phone = "+447911123456"
        session = _LeadBeeSession(
            {
                "ok": True,
                "card": _leadbee_card(
                    status="number_ready",
                    phone=first_phone,
                    number_queue_seconds=0,
                ),
            },
            {
                "ok": True,
                "message": "已提交换号请求",
                "card": _leadbee_card(
                    status="processing",
                    phone=None,
                    number_queue_seconds=1,
                ),
            },
            {
                "ok": True,
                "card": _leadbee_card(
                    status="number_ready",
                    phone=replacement_phone,
                    number_queue_seconds=0,
                ),
            },
        )
        service = LeadBeePhoneService(
            {
                "leadbee_code": "bei-sms-REPLACE-CODE",
                "leadbee_poll_interval_seconds": 1,
            },
            log_fn=logs.append,
            session=session,
        )

        first_entry = service.acquire_phone()
        service.mark_blacklisted(first_entry.phone)
        with mock.patch("platforms.chatgpt.phone_service.time.sleep"):
            replacement_entry = service.acquire_phone()

        self.assertEqual(first_entry.phone, first_phone)
        self.assertEqual(replacement_entry.phone, replacement_phone)
        self.assertEqual(
            [call[0] for call in session.calls],
            [
                "https://sms.leadbee.cn/smsbox/api/activate",
                "https://sms.leadbee.cn/smsbox/api/replace-number",
                "https://sms.leadbee.cn/smsbox/api/receive-sms",
            ],
        )
        self.assertNotIn(
            "https://sms.leadbee.cn/smsbox/api/cancel",
            [call[0] for call in session.calls],
        )
        self.assertIn("[LeadBee] 当前号码被 OpenAI 拒绝，正在更换号码", logs)
        self.assertIn(f"[LeadBee] 已获取更换后的手机号 {replacement_phone}", logs)

    def test_replace_number_card_not_in_session_is_isolated(self):
        consumed = mock.Mock()
        restored = mock.Mock()
        broker = InteractivePhoneVerificationBroker(
            account_id=17,
            provider="leadbee",
            on_exchange_code_consumed=consumed,
            on_exchange_code_restored=restored,
        )
        session = _LeadBeeSession(
            {
                "ok": True,
                "card": _leadbee_card(
                    status="number_ready",
                    phone="+447456344799",
                    number_queue_seconds=0,
                ),
            },
            {
                "ok": False,
                "error": "CARD_NOT_IN_SESSION",
                "message": "当前会话无权操作该卡密",
            },
        )
        service = LeadBeePhoneService(
            {
                "leadbee_code": "bei-sms-REPLACE-LOST-SESSION",
                "chatgpt_phone_progress_broker": broker,
            },
            session=session,
        )

        entry = service.acquire_phone()
        with self.assertRaisesRegex(RuntimeError, "另一会话"):
            service.request_replacement(entry.phone)

        snapshot = broker.snapshot()
        self.assertEqual(snapshot["provider_error_code"], "CARD_NOT_IN_SESSION")
        self.assertFalse(snapshot["exchange_code_restoration_confirmed"])
        self.assertEqual(snapshot["exchange_code_settlement"], "active_unknown")
        self.assertFalse(snapshot["exchange_code_unusable"])
        self.assertTrue(service.card_at_risk)
        restored.assert_not_called()
        consumed.assert_not_called()

    def test_completed_exchange_code_is_not_reused_for_a_new_login(self):
        consumed = mock.Mock()
        broker = InteractivePhoneVerificationBroker(
            account_id=17,
            provider="leadbee",
            on_exchange_code_consumed=consumed,
        )
        session = _LeadBeeSession(
            {
                "ok": True,
                "restored": True,
                "message": "已恢复该卡密的接码结果",
                "card": _leadbee_card(
                    status="sms_received",
                    phone="+447456344799",
                    sms_code="123456",
                    is_terminal=True,
                    can_auto_poll=False,
                    can_refresh=False,
                ),
            }
        )
        service = LeadBeePhoneService(
            {
                "leadbee_code": "bei-sms-USED-CODE",
                "chatgpt_phone_progress_broker": broker,
            },
            session=session,
        )

        with self.assertRaisesRegex(RuntimeError, "已结束|已使用"):
            service.acquire_phone()

        self.assertEqual(len(session.calls), 1)
        snapshot = broker.snapshot()
        self.assertEqual(snapshot["exchange_code_settlement"], "unusable")
        self.assertTrue(snapshot["exchange_code_unusable"])
        consumed.assert_called_once_with()

    def test_terminal_failed_activation_is_quarantined(self):
        consumed = mock.Mock()
        broker = InteractivePhoneVerificationBroker(
            account_id=17,
            provider="leadbee",
            on_exchange_code_consumed=consumed,
        )
        session = _LeadBeeSession(
            {
                "ok": True,
                "card": _leadbee_card(
                    status="failed",
                    is_terminal=True,
                    can_auto_poll=False,
                    can_refresh=False,
                ),
            }
        )
        service = LeadBeePhoneService(
            {
                "leadbee_code": "bei-sms-TERMINAL-FAILED",
                "chatgpt_phone_progress_broker": broker,
            },
            session=session,
        )

        with self.assertRaisesRegex(RuntimeError, "任务已结束"):
            service.acquire_phone()

        snapshot = broker.snapshot()
        self.assertEqual(snapshot["exchange_code_settlement"], "active_unknown")
        self.assertFalse(snapshot["exchange_code_unusable"])
        consumed.assert_not_called()

    def test_terminal_failed_sms_poll_is_quarantined(self):
        consumed = mock.Mock()
        broker = InteractivePhoneVerificationBroker(
            account_id=17,
            provider="leadbee",
            on_exchange_code_consumed=consumed,
        )
        phone = "+447456344799"
        session = _LeadBeeSession(
            {
                "ok": True,
                "card": _leadbee_card(status="number_ready", phone=phone),
            },
            {
                "ok": True,
                "card": _leadbee_card(
                    status="failed",
                    phone=phone,
                    sms_code="999999",
                    is_terminal=True,
                    can_auto_poll=False,
                    can_refresh=False,
                ),
            },
        )
        service = LeadBeePhoneService(
            {
                "leadbee_code": "bei-sms-SMS-TERMINAL-FAILED",
                "chatgpt_phone_progress_broker": broker,
            },
            session=session,
        )

        entry = service.acquire_phone()
        self.assertIsNone(service.wait_for_code(entry))

        snapshot = broker.snapshot()
        self.assertEqual(snapshot["exchange_code_settlement"], "active_unknown")
        self.assertFalse(snapshot["exchange_code_unusable"])
        consumed.assert_not_called()

    def test_cancellation_after_activation_restores_the_exchange_code(self):
        broker = mock.Mock()
        broker.raise_if_cancelled.side_effect = [
            None,
            RuntimeError("手机验证已取消"),
        ]
        session = _LeadBeeSession(
            {
                "ok": True,
                "card": _leadbee_card(
                    status="number_ready",
                    phone="+447456344799",
                ),
            },
            {
                "ok": True,
                "removed": True,
                "card": _leadbee_card(status="canceled", is_terminal=True),
            },
        )
        service = LeadBeePhoneService(
            {
                "leadbee_code": "bei-sms-CANCEL-CODE",
                "chatgpt_phone_progress_broker": broker,
            },
            session=session,
        )

        with self.assertRaisesRegex(RuntimeError, "已取消"):
            service.acquire_phone()

        self.assertEqual(
            [call[0] for call in session.calls],
            [
                "https://sms.leadbee.cn/smsbox/api/activate",
                "https://sms.leadbee.cn/smsbox/api/cancel",
            ],
        )
        broker.mark_exchange_code_restored.assert_called_once_with()

    def test_confirmed_cancellation_reports_structured_restoration(self):
        restored = mock.Mock()
        broker = InteractivePhoneVerificationBroker(
            account_id=17,
            provider="leadbee",
            on_exchange_code_restored=restored,
        )
        session = _LeadBeeSession(
            {
                "ok": True,
                "card": _leadbee_card(
                    status="number_ready",
                    phone="+447456344799",
                ),
            },
            {
                "ok": True,
                "removed": True,
                "card": _leadbee_card(status="canceled", is_terminal=True),
            },
        )
        service = LeadBeePhoneService(
            {
                "leadbee_code": "bei-sms-RESTORED-CODE",
                "chatgpt_phone_progress_broker": broker,
            },
            session=session,
        )

        service.acquire_phone()
        self.assertTrue(service.cancel_active())

        snapshot = broker.snapshot()
        self.assertTrue(snapshot["exchange_code_restoration_confirmed"])
        restored.assert_called_once_with()

    def test_cancel_requires_explicit_provider_restoration_confirmation(self):
        broker = InteractivePhoneVerificationBroker(
            account_id=17,
            provider="leadbee",
        )
        session = _LeadBeeSession(
            {
                "ok": True,
                "card": _leadbee_card(
                    status="number_ready",
                    phone="+447456344799",
                ),
            },
            {
                "ok": True,
                "removed": False,
                "message": "任务不可取消，仍在排队等待号码",
                "card": _leadbee_card(status="processing"),
            },
        )
        service = LeadBeePhoneService(
            {
                "leadbee_code": "bei-sms-NOT-RESTORED",
                "chatgpt_phone_progress_broker": broker,
            },
            session=session,
        )

        service.acquire_phone()

        self.assertFalse(service.cancel_active())
        self.assertTrue(service.card_at_risk)
        snapshot = broker.snapshot()
        self.assertEqual(snapshot["exchange_code_settlement"], "active_unknown")
        self.assertFalse(snapshot["exchange_code_unusable"])

    def test_cancel_card_not_in_session_is_not_treated_as_restored(self):
        consumed = mock.Mock()
        restored = mock.Mock()
        broker = InteractivePhoneVerificationBroker(
            account_id=17,
            provider="leadbee",
            on_exchange_code_consumed=consumed,
            on_exchange_code_restored=restored,
        )
        broker.raise_if_cancelled = mock.Mock(
            side_effect=[None, RuntimeError("手机验证已取消")]
        )
        session = _LeadBeeSession(
            {
                "ok": True,
                "card": _leadbee_card(status="processing"),
            },
            {
                "ok": False,
                "error": "CARD_NOT_IN_SESSION",
                "message": "当前会话无权操作该卡密",
            },
        )
        service = LeadBeePhoneService(
            {
                "leadbee_code": "bei-sms-CANCEL-LOST-SESSION",
                "chatgpt_phone_progress_broker": broker,
            },
            session=session,
        )

        with self.assertRaisesRegex(RuntimeError, "已取消"):
            service.acquire_phone()

        snapshot = broker.snapshot()
        self.assertEqual(snapshot["provider_error_code"], "CARD_NOT_IN_SESSION")
        self.assertFalse(snapshot["exchange_code_restoration_confirmed"])
        self.assertEqual(snapshot["exchange_code_settlement"], "active_unknown")
        self.assertFalse(snapshot["exchange_code_unusable"])
        self.assertTrue(service.card_at_risk)
        restored.assert_not_called()
        consumed.assert_not_called()

    def test_ambiguous_activate_failure_quarantines_exchange_code(self):
        broker = InteractivePhoneVerificationBroker(
            account_id=18,
            provider="leadbee",
        )
        session = mock.Mock()
        session.post.side_effect = TimeoutError("read timed out")
        service = LeadBeePhoneService(
            {
                "leadbee_code": "bei-sms-AMBIGUOUS",
                "chatgpt_phone_progress_broker": broker,
            },
            session=session,
        )

        with self.assertRaisesRegex(RuntimeError, "激活结果未知.*卡密保持隔离"):
            service.acquire_phone()

        self.assertTrue(service.card_at_risk)
        snapshot = broker.snapshot()
        self.assertEqual(snapshot["exchange_code_settlement"], "active_unknown")
        self.assertFalse(snapshot["exchange_code_unusable"])


class LeadBeeOAuthFlowTests(unittest.TestCase):
    def test_provider_log_redacts_exchange_code_before_api_snapshot(self):
        exchange_code = "bei-sms-OAUTH-SECRET-CODE"
        broker = InteractivePhoneVerificationBroker(
            account_id=17,
            provider="leadbee",
        )
        oauth_logs = []
        client = OAuthClient(
            {
                "chatgpt_leadbee_code": exchange_code,
                "chatgpt_phone_provider": "leadbee",
                "chatgpt_phone_progress_broker": broker,
            },
            verbose=False,
        )
        client._log = oauth_logs.append
        entry = PhoneEntry(
            country_slug="leadbee",
            phone="+447456344799",
            detail_url="https://sms.leadbee.cn/smsbox/",
        )
        next_state = FlowState(
            page_type="phone_otp_verification",
            continue_url="https://auth.openai.com/phone-verification",
        )
        completed_state = FlowState(
            page_type="oauth_callback",
            continue_url="http://localhost:1455/auth/callback?code=done",
        )

        def acquire_phone(service, *, exclude_prefixes=None):
            del exclude_prefixes
            service.log_fn(f"[LeadBee] provider echoed {exchange_code}")
            return entry

        with mock.patch.object(
            LeadBeePhoneService,
            "acquire_phone",
            autospec=True,
            side_effect=acquire_phone,
        ), mock.patch.object(
            LeadBeePhoneService,
            "wait_for_code",
            return_value="654321",
        ), mock.patch.object(
            client,
            "_send_phone_number",
            return_value=(True, next_state, ""),
        ), mock.patch.object(
            client,
            "_decode_oauth_session_cookie",
            return_value={"phone_verification_channel": "sms"},
        ), mock.patch.object(
            client,
            "_validate_phone_otp",
            return_value=(True, completed_state, ""),
        ):
            state = client._handle_add_phone_verification(
                "device-id",
                "Mozilla/5.0",
                None,
                None,
                FlowState(page_type="add_phone"),
            )

        visible_logs = "\n".join(oauth_logs + broker.snapshot()["logs"])
        self.assertEqual(state, completed_state)
        self.assertNotIn(exchange_code, visible_logs)
        self.assertIn("[LeadBee兑换码已脱敏]", visible_logs)

    def test_automatic_leadbee_flow_reports_progress_and_does_not_resend(self):
        client = OAuthClient(
            {
                "leadbee_code": "bei-sms-DEMO-CODE",
                "chatgpt_phone_provider": "leadbee",
            },
            verbose=False,
        )
        client._log = lambda _msg: None
        broker = mock.Mock()
        client.config["chatgpt_phone_progress_broker"] = broker
        entry = PhoneEntry(
            country_slug="leadbee",
            phone="+447456344799",
            detail_url="https://sms.leadbee.cn/smsbox/",
        )
        phone_service = mock.Mock()
        phone_service.enabled = True
        phone_service.provider_name = "LeadBee"
        phone_service.max_attempts = 1
        phone_service.supports_resend = False
        phone_service.acquire_phone.return_value = entry
        phone_service.prefix_hint.return_value = "+447456"
        phone_service.wait_for_code.return_value = "654321"
        next_state = FlowState(
            page_type="phone_otp_verification",
            continue_url="https://auth.openai.com/phone-verification",
        )
        completed_state = FlowState(
            page_type="oauth_callback",
            continue_url="http://localhost:1455/auth/callback?code=done",
        )

        def create_service(_config, log_fn=None):
            self.assertIsNotNone(log_fn)
            log_fn("[LeadBee] 正在排队获取号码，请稍候")
            return phone_service

        with mock.patch(
            "platforms.chatgpt.oauth_client.create_phone_service",
            side_effect=create_service,
        ), mock.patch.object(
            client,
            "_send_phone_number",
            return_value=(True, next_state, ""),
        ), mock.patch.object(
            client,
            "_decode_oauth_session_cookie",
            return_value={"phone_verification_channel": "sms"},
        ), mock.patch.object(
            client,
            "_validate_phone_otp",
            return_value=(True, completed_state, ""),
        ), mock.patch.object(client, "_resend_phone_otp") as resend:
            state = client._handle_add_phone_verification(
                "device-id",
                "Mozilla/5.0",
                None,
                None,
                FlowState(page_type="add_phone"),
            )

        self.assertEqual(state, completed_state)
        broker.mark_phone_acquired.assert_called_once_with(entry.phone)
        broker.mark_automatic_sms_sent.assert_called_once_with(entry.phone)
        broker.mark_automatic_code_received.assert_called_once()
        broker.mark_phone_verified.assert_called_once()
        broker.mark_progress.assert_any_call("[LeadBee] 正在排队获取号码，请稍候")
        resend.assert_not_called()

    def test_sms_delivery_timeout_replaces_number_and_continues_oauth(self):
        first_phone = "+19383086878"
        replacement_phone = "+12025550123"
        session = _LeadBeeSession(
            {
                "ok": True,
                "card": _leadbee_card(
                    status="number_ready",
                    phone=first_phone,
                    number_queue_seconds=0,
                ),
            },
            {
                "ok": True,
                "message": "已提交换号请求",
                "card": _leadbee_card(
                    status="number_ready",
                    phone=replacement_phone,
                    number_queue_seconds=0,
                ),
            },
            {
                "ok": True,
                "card": _leadbee_card(
                    status="sms_received",
                    phone=replacement_phone,
                    sms_code="654321",
                    is_terminal=True,
                    can_auto_poll=False,
                    can_refresh=False,
                ),
            },
        )
        phone_service = LeadBeePhoneService(
            {
                "leadbee_code": "bei-sms-REPLACE-CODE",
                "leadbee_poll_interval_seconds": 1,
                "leadbee_otp_timeout_seconds": 10,
            },
            session=session,
        )
        client = OAuthClient({}, verbose=False)
        client._log = lambda _msg: None
        phone_state = FlowState(
            page_type="phone_otp_verification",
            continue_url="https://auth.openai.com/phone-verification",
        )
        completed_state = FlowState(
            page_type="oauth_callback",
            continue_url="http://localhost:1455/auth/callback?code=done",
        )

        monotonic_values = iter([0.0, 0.0, 0.0, 0.0, 11.0])

        def monotonic_now():
            return next(monotonic_values, 11.0)

        with mock.patch(
            "platforms.chatgpt.oauth_client.create_phone_service",
            return_value=phone_service,
        ), mock.patch.object(
            client,
            "_send_phone_number",
            side_effect=[
                (True, phone_state, ""),
                (True, phone_state, ""),
            ],
        ) as send_phone, mock.patch.object(
            client,
            "_decode_oauth_session_cookie",
            return_value={"phone_verification_channel": "sms"},
        ), mock.patch.object(
            client,
            "_validate_phone_otp",
            return_value=(True, completed_state, ""),
        ), mock.patch(
            "platforms.chatgpt.phone_service.time.monotonic",
            side_effect=monotonic_now,
        ), mock.patch(
            "platforms.chatgpt.phone_service.time.sleep"
        ):
            state = client._handle_add_phone_verification(
                "device-id",
                "Mozilla/5.0",
                None,
                None,
                FlowState(page_type="add_phone"),
            )

        self.assertEqual(state, completed_state)
        self.assertEqual(
            [call.args[0] for call in send_phone.call_args_list],
            [first_phone, replacement_phone],
        )
        self.assertEqual(
            [call[0] for call in session.calls],
            [
                "https://sms.leadbee.cn/smsbox/api/activate",
                "https://sms.leadbee.cn/smsbox/api/replace-number",
                "https://sms.leadbee.cn/smsbox/api/receive-sms",
            ],
        )
        self.assertNotIn(
            "https://sms.leadbee.cn/smsbox/api/cancel",
            [call[0] for call in session.calls],
        )

    def test_similar_phone_rejection_replaces_number_and_continues_oauth(self):
        first_phone = "+19383086878"
        replacement_phone = "+12025550123"
        suspicious_detail = (
            "add-phone/send 失败: 400 - We've detected suspicious behavior "
            "from phone numbers similar to yours. Please try again later."
        )
        session = _LeadBeeSession(
            {
                "ok": True,
                "card": _leadbee_card(
                    status="number_ready",
                    phone=first_phone,
                    number_queue_seconds=0,
                ),
            },
            {
                "ok": True,
                "card": _leadbee_card(
                    status="number_ready",
                    phone=replacement_phone,
                    number_queue_seconds=0,
                ),
            },
            {
                "ok": True,
                "card": _leadbee_card(
                    status="sms_received",
                    phone=replacement_phone,
                    sms_code="654321",
                    is_terminal=True,
                    can_auto_poll=False,
                    can_refresh=False,
                ),
            },
        )
        phone_service = LeadBeePhoneService(
            {
                "leadbee_code": "bei-sms-REPLACE-CODE",
                "leadbee_poll_interval_seconds": 1,
            },
            session=session,
        )
        client = OAuthClient({}, verbose=False)
        client._log = lambda _msg: None
        phone_state = FlowState(
            page_type="phone_otp_verification",
            continue_url="https://auth.openai.com/phone-verification",
        )
        completed_state = FlowState(
            page_type="oauth_callback",
            continue_url="http://localhost:1455/auth/callback?code=done",
        )

        with mock.patch(
            "platforms.chatgpt.oauth_client.create_phone_service",
            return_value=phone_service,
        ), mock.patch.object(
            client,
            "_send_phone_number",
            side_effect=[
                (False, None, suspicious_detail),
                (True, phone_state, ""),
            ],
        ) as send_phone, mock.patch.object(
            client,
            "_decode_oauth_session_cookie",
            return_value={"phone_verification_channel": "sms"},
        ), mock.patch.object(
            client,
            "_validate_phone_otp",
            return_value=(True, completed_state, ""),
        ), mock.patch(
            "platforms.chatgpt.phone_service.time.sleep"
        ):
            state = client._handle_add_phone_verification(
                "device-id",
                "Mozilla/5.0",
                None,
                None,
                FlowState(page_type="add_phone"),
            )

        self.assertEqual(state, completed_state)
        self.assertEqual(
            [call.args[0] for call in send_phone.call_args_list],
            [first_phone, replacement_phone],
        )
        self.assertEqual(
            [call[0] for call in session.calls],
            [
                "https://sms.leadbee.cn/smsbox/api/activate",
                "https://sms.leadbee.cn/smsbox/api/replace-number",
                "https://sms.leadbee.cn/smsbox/api/receive-sms",
            ],
        )

    def test_unhandled_send_failure_preserves_original_error_and_restores_code(self):
        phone = "+12025550123"
        detail = "add-phone/send 失败: 429 - too many verification requests"
        broker = InteractivePhoneVerificationBroker(
            account_id=17,
            provider="leadbee",
        )
        session = _LeadBeeSession(
            {
                "ok": True,
                "card": _leadbee_card(
                    status="number_ready",
                    phone=phone,
                    number_queue_seconds=0,
                ),
            },
            {
                "ok": True,
                "removed": True,
                "card": _leadbee_card(status="canceled", is_terminal=True),
            },
        )
        phone_service = LeadBeePhoneService(
            {
                "leadbee_code": "bei-sms-RESTORE-CODE",
                "chatgpt_phone_progress_broker": broker,
            },
            session=session,
        )
        client = OAuthClient(
            {"chatgpt_phone_progress_broker": broker},
            verbose=False,
        )
        client._log = lambda _msg: None

        with mock.patch(
            "platforms.chatgpt.oauth_client.create_phone_service",
            return_value=phone_service,
        ), mock.patch.object(
            client,
            "_send_phone_number",
            return_value=(False, None, detail),
        ) as send_phone:
            state = client._handle_add_phone_verification(
                "device-id",
                "Mozilla/5.0",
                None,
                None,
                FlowState(page_type="add_phone"),
            )

        self.assertIsNone(state)
        send_phone.assert_called_once()
        self.assertIn(detail, client.last_error)
        self.assertNotIn("LeadBee 换号未成功", client.last_error)
        self.assertIn(detail, "\n".join(broker.snapshot()["logs"]))
        self.assertEqual(
            [call[0] for call in session.calls],
            [
                "https://sms.leadbee.cn/smsbox/api/activate",
                "https://sms.leadbee.cn/smsbox/api/cancel",
            ],
        )

    def test_failed_provider_cancellation_marks_activated_card_as_not_reusable(self):
        phone = "+12025550123"
        detail = "add-phone/send 失败: 429 - too many verification requests"
        session = _LeadBeeSession(
            {
                "ok": True,
                "card": _leadbee_card(
                    status="number_ready",
                    phone=phone,
                    number_queue_seconds=0,
                ),
            },
            {
                "ok": False,
                "error": "TASK_NOT_CANCELLABLE",
                "message": "任务不可取消，仍在排队等待号码",
            },
        )
        phone_service = LeadBeePhoneService(
            {"leadbee_code": "bei-sms-NONCANCEL-CODE"},
            session=session,
        )
        client = OAuthClient({}, verbose=False)
        client._log = lambda _msg: None

        with mock.patch(
            "platforms.chatgpt.oauth_client.create_phone_service",
            return_value=phone_service,
        ), mock.patch.object(
            client,
            "_send_phone_number",
            return_value=(False, None, detail),
        ):
            state = client._handle_add_phone_verification(
                "device-id",
                "Mozilla/5.0",
                None,
                None,
                FlowState(page_type="add_phone"),
            )

        self.assertIsNone(state)
        self.assertIn(detail, client.last_error)
        self.assertIn("任务不可取消", client.last_error)
        self.assertIn("卡密不可复用", client.last_error)
        self.assertEqual(
            [call[0] for call in session.calls],
            [
                "https://sms.leadbee.cn/smsbox/api/activate",
                "https://sms.leadbee.cn/smsbox/api/cancel",
            ],
        )


class OAuthPhoneSendRetryTests(unittest.TestCase):
    def _client(self):
        client = OAuthClient(config={}, verbose=False)
        client._log = mock.Mock()
        return client

    def test_send_phone_number_retries_curl_tls_handshake_error_on_same_session(self):
        client = self._client()
        tls_error = cffi_requests.exceptions.SSLError(
            "TLS connect error: error:00000000:lib(0)::reason(0)",
            35,
        )
        response = mock.Mock(
            status_code=200,
            url="https://auth.openai.com/api/accounts/add-phone/send",
            text="{}",
        )
        response.json.return_value = {"page": {"type": "phone_otp_verification"}}
        client.session.post = mock.Mock(side_effect=[tls_error, response])
        next_state = FlowState(page_type="phone_otp_verification")

        with mock.patch.object(
            client,
            "_state_from_payload",
            return_value=next_state,
        ), mock.patch("platforms.chatgpt.oauth_client.time.sleep"):
            sent, state, detail = client._send_phone_number(
                "+447000000001",
                "device-id",
                "Mozilla/5.0",
                '"Chromium";v="136"',
                "chrome136",
            )

        self.assertTrue(sent)
        self.assertIs(state, next_state)
        self.assertEqual(detail, "")
        self.assertEqual(client.session.post.call_count, 2)
        first_call, second_call = client.session.post.call_args_list
        self.assertEqual(first_call.args, second_call.args)
        self.assertEqual(
            first_call.kwargs["json"],
            second_call.kwargs["json"],
        )

    def test_send_phone_number_does_not_retry_timeout(self):
        client = self._client()
        client.session.post = mock.Mock(
            side_effect=cffi_requests.exceptions.Timeout("request timed out")
        )

        sent, state, detail = client._send_phone_number(
            "+447000000001",
            "device-id",
            "Mozilla/5.0",
            None,
            None,
        )

        self.assertFalse(sent)
        self.assertIsNone(state)
        self.assertIn("timed out", detail)
        client.session.post.assert_called_once()

    def test_send_phone_number_does_not_retry_http_failure(self):
        client = self._client()
        client.session.post = mock.Mock(
            return_value=mock.Mock(
                status_code=500,
                text="temporary upstream failure",
            )
        )

        sent, state, detail = client._send_phone_number(
            "+447000000001",
            "device-id",
            "Mozilla/5.0",
            None,
            None,
        )

        self.assertFalse(sent)
        self.assertIsNone(state)
        self.assertIn("500", detail)
        client.session.post.assert_called_once()


class OAuthPhoneBlacklistTests(unittest.TestCase):
    def test_should_blacklist_explicit_phone_rejection(self):
        state = FlowState(
            page_type="add_phone",
            payload={"error": {"message": "phone number is invalid"}},
        )
        self.assertTrue(
            OAuthClient._should_blacklist_phone_failure(
                "add-phone/send 失败: 400 - phone number is invalid",
                state,
            )
        )

    def test_should_replace_similar_phone_suspicious_rejection(self):
        self.assertTrue(
            OAuthClient._should_blacklist_phone_failure(
                "We've detected suspicious behavior from phone numbers similar "
                "to yours. Please try again later."
            )
        )

    def test_should_replace_phone_number_already_in_use(self):
        self.assertTrue(
            OAuthClient._should_blacklist_phone_failure(
                "add-phone/send 失败: 400 - Phone number already in use. "
                "Please use a different phone number."
            )
        )

    def test_explicit_similar_phone_rejection_wins_over_rate_limit_marker(self):
        self.assertTrue(
            OAuthClient._should_blacklist_phone_failure(
                "Too many phone numbers similar to yours were detected"
            )
        )

    def test_should_not_blacklist_whatsapp_or_delivery_failures(self):
        self.assertFalse(
            OAuthClient._should_blacklist_phone_failure(
                "add_phone 已切到 whatsapp 通道，当前 SMSToMe 仅支持短信接码"
            )
        )
        self.assertFalse(
            OAuthClient._should_blacklist_phone_failure("手机号 +447000000001 未收到短信验证码")
        )

    def test_handle_add_phone_blacklists_explicitly_rejected_number(self):
        client = OAuthClient(config={}, verbose=False)
        client._log = lambda _msg: None
        entry = PhoneEntry(
            country_slug="united-kingdom",
            phone="+447000000001",
            detail_url="https://example.com/phone/1",
        )
        phone_service = mock.Mock()
        phone_service.enabled = True
        phone_service.max_attempts = 1
        phone_service.acquire_phone.return_value = entry
        phone_service.prefix_hint.return_value = "+447000"

        with mock.patch("platforms.chatgpt.oauth_client.create_phone_service", return_value=phone_service):
            with mock.patch.object(
                client,
                "_send_phone_number",
                return_value=(False, None, "add-phone/send 失败: 400 - phone number is invalid"),
            ):
                state = client._handle_add_phone_verification(
                    "device-id",
                    "Mozilla/5.0",
                    None,
                    None,
                    FlowState(page_type="add_phone"),
                )

        self.assertIsNone(state)
        phone_service.mark_blacklisted.assert_called_once_with(entry.phone)
        self.assertIn("add_phone 阶段失败", client.last_error)

    def test_handle_add_phone_does_not_blacklist_whatsapp_channel(self):
        client = OAuthClient(config={}, verbose=False)
        client._log = lambda _msg: None
        entry = PhoneEntry(
            country_slug="united-kingdom",
            phone="+447000000002",
            detail_url="https://example.com/phone/2",
        )
        phone_service = mock.Mock()
        phone_service.enabled = True
        phone_service.max_attempts = 1
        phone_service.acquire_phone.return_value = entry
        phone_service.prefix_hint.return_value = "+447000"

        next_state = FlowState(
            page_type="phone_otp_verification",
            continue_url="https://auth.openai.com/phone-verification",
        )

        with mock.patch("platforms.chatgpt.oauth_client.create_phone_service", return_value=phone_service):
            with mock.patch.object(client, "_send_phone_number", return_value=(True, next_state, "")):
                with mock.patch.object(
                    client,
                    "_decode_oauth_session_cookie",
                    return_value={
                        "phone_verification_channel": "whatsapp",
                        "phone_number": entry.phone,
                    },
                ):
                    state = client._handle_add_phone_verification(
                        "device-id",
                        "Mozilla/5.0",
                        None,
                        None,
                        FlowState(page_type="add_phone"),
                    )

        self.assertIsNone(state)
        phone_service.mark_blacklisted.assert_not_called()
        self.assertIn("whatsapp", client.last_error)

    def test_interactive_phone_flow_supports_resend_and_invalid_code_retry(self):
        broker = mock.Mock()
        broker.wait_for_command.side_effect = [
            PhoneVerificationCommand(id="resend-1", kind="resend", payload=""),
            PhoneVerificationCommand(id="submit-1", kind="submit", payload="111111"),
            PhoneVerificationCommand(id="submit-2", kind="submit", payload="654321"),
        ]
        client = OAuthClient(
            config={
                "chatgpt_phone_number": "+447456344799",
                "chatgpt_interactive_phone_broker": broker,
            },
            verbose=False,
        )
        client._log = lambda _msg: None
        phone_state = FlowState(
            page_type="phone_otp_verification",
            continue_url="https://auth.openai.com/phone-verification",
        )
        completed_state = FlowState(
            page_type="workspace_select",
            continue_url="https://auth.openai.com/workspace",
        )

        with mock.patch.object(
            client,
            "_send_phone_number",
            return_value=(True, phone_state, ""),
        ), mock.patch.object(
            client,
            "_resend_phone_otp",
            return_value=(True, ""),
        ) as resend_mock, mock.patch.object(
            client,
            "_validate_phone_otp",
            side_effect=[
                (False, None, "手机号验证码错误"),
                (True, completed_state, ""),
            ],
        ):
            state = client._handle_add_phone_verification(
                "device-id",
                "Mozilla/5.0",
                None,
                None,
                FlowState(page_type="add_phone"),
            )

        self.assertIs(state, completed_state)
        broker.mark_code_sent.assert_called_once_with("+447456344799")
        broker.mark_phone_verified.assert_called_once()
        resend_mock.assert_called_once()
        self.assertEqual(
            broker.resolve_command.call_args_list,
            [
                mock.call("resend-1", ok=True, message="验证码已重新发送"),
                mock.call("submit-1", ok=False, message="手机号验证码错误"),
                mock.call("submit-2", ok=True, message="手机号验证通过"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
