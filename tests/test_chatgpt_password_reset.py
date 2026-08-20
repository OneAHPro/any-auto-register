import types
import unittest
from unittest import mock

from platforms.chatgpt.oauth_client import OAuthClient
from platforms.chatgpt.utils import FlowState


def _response(payload, *, url, status_code=200, text=""):
    return types.SimpleNamespace(
        status_code=status_code,
        url=url,
        text=text,
        json=lambda: payload,
    )


class ChatGPTPasswordResetProtocolTests(unittest.TestCase):
    def setUp(self):
        self.client = OAuthClient({}, verbose=False)
        self.client.session = mock.Mock()

    def test_password_reset_send_otp_enters_email_verification(self):
        self.client.session.post.return_value = _response(
            {
                "page": {
                    "type": "email_otp_verification",
                    "payload": {"url": "/email-verification"},
                }
            },
            url="https://auth.openai.com/api/accounts/password/send-otp",
        )
        state = FlowState(
            page_type="login_password",
            current_url="https://auth.openai.com/log-in/password",
        )

        result = self.client._request_password_reset_otp(
            state,
            device_id="device-id",
            user_agent="ua",
            sec_ch_ua='"Chromium";v="136"',
            impersonate=None,
        )

        self.assertEqual(result.page_type, "email_otp_verification")
        call = self.client.session.post.call_args
        self.assertEqual(
            call.args[0],
            "https://auth.openai.com/api/accounts/password/send-otp",
        )
        self.assertNotIn("password", call.kwargs)
        self.assertEqual(call.kwargs["headers"]["oai-device-id"], "device-id")

    def test_password_reset_send_otp_failure_keeps_response_detail(self):
        self.client.session.post.return_value = _response(
            {},
            url="https://auth.openai.com/api/accounts/password/send-otp",
            status_code=400,
            text='{"error":{"message":"RESET_REQUEST_DETAIL","code":"invalid"}}',
        )
        state = FlowState(
            page_type="login_password",
            current_url="https://auth.openai.com/log-in/password",
        )

        result = self.client._request_password_reset_otp(
            state,
            device_id="device-id",
        )

        self.assertIsNone(result)
        self.assertIn("HTTP 400", self.client.last_error)
        self.assertIn("RESET_REQUEST_DETAIL", self.client.last_error)

    def test_password_reset_submits_new_password_with_sentinel(self):
        self.client.session.post.return_value = _response(
            {
                "page": {
                    "type": "reset_password_success",
                    "payload": {"url": "/reset-password/success"},
                }
            },
            url="https://auth.openai.com/api/accounts/password/reset",
        )
        state = FlowState(
            page_type="reset_password_new_password",
            current_url="https://auth.openai.com/reset-password/new-password",
        )

        with mock.patch(
            "platforms.chatgpt.oauth_client.get_sentinel_token_via_browser",
            return_value="sentinel-token",
        ):
            result = self.client._submit_password_reset_new_password(
                state,
                new_password="Fresh-Password-123!",
                device_id="device-id",
                user_agent="ua",
                sec_ch_ua='"Chromium";v="136"',
                impersonate=None,
            )

        self.assertEqual(result.page_type, "reset_password_success")
        call = self.client.session.post.call_args
        self.assertEqual(
            call.args[0],
            "https://auth.openai.com/api/accounts/password/reset",
        )
        self.assertEqual(call.kwargs["json"], {"password": "Fresh-Password-123!"})
        self.assertEqual(
            call.kwargs["headers"]["openai-sentinel-token"],
            "sentinel-token",
        )

    def test_remote_totp_provider_is_used_without_base32_secret(self):
        issue = _response(
            {
                "page": {
                    "type": "mfa_challenge",
                    "payload": {
                        "factors": [{"id": "factor-1", "factor_type": "totp"}]
                    },
                }
            },
            url="https://auth.openai.com/api/accounts/mfa/issue_challenge",
        )
        verified = _response(
            {"page": {"type": "add_phone", "payload": {"url": "/add-phone"}}},
            url="https://auth.openai.com/api/accounts/mfa/verify",
        )
        self.client.session.post.side_effect = [issue, verified]
        code_provider = mock.Mock(return_value="654321")
        state = FlowState(
            page_type="mfa_challenge",
            current_url="https://auth.openai.com/mfa-challenge/factor-1",
            payload={"factors": [{"id": "factor-1", "factor_type": "totp"}]},
        )

        result = self.client._submit_totp_mfa_challenge(
            state,
            totp_secret="",
            totp_code_provider=code_provider,
            device_id="device-id",
        )

        self.assertEqual(result.page_type, "add_phone")
        code_provider.assert_called_once_with()
        verify_call = self.client.session.post.call_args_list[1]
        self.assertEqual(verify_call.kwargs["json"]["code"], "654321")

    def test_mfa_router_prefers_remote_totp_provider_when_available(self):
        state = FlowState(
            page_type="mfa_challenge",
            current_url="https://auth.openai.com/mfa-challenge/factor-1",
            payload={"factors": [{"id": "factor-1", "factor_type": "totp"}]},
        )
        mailbox = mock.Mock()
        mailbox.get_totp_code.return_value = "654321"
        expected = FlowState(page_type="add_phone")

        with mock.patch.object(
            self.client,
            "_submit_totp_mfa_challenge",
            return_value=expected,
        ) as submit_totp:
            result = self.client._submit_mfa_challenge(
                state,
                email="demo@example.com",
                skymail_client=mailbox,
                totp_secret="",
                device_id="device-id",
            )

        self.assertIs(result, expected)
        provider = submit_totp.call_args.kwargs["totp_code_provider"]
        self.assertEqual(provider(), "654321")
        mailbox.get_totp_code.assert_called_once_with()

    def test_mfa_router_uses_email_when_remote_totp_is_not_configured(self):
        state = FlowState(
            page_type="mfa_challenge",
            payload={
                "factors": [
                    {"id": "factor-totp", "factor_type": "totp"},
                    {"id": "factor-email", "factor_type": "email"},
                ]
            },
        )
        mailbox = mock.Mock()
        mailbox.supports_totp_code.return_value = False
        expected = FlowState(page_type="add_phone")

        with mock.patch.object(
            self.client,
            "_submit_email_mfa_challenge",
            return_value=expected,
        ) as submit_email, mock.patch.object(
            self.client,
            "_submit_totp_mfa_challenge",
        ) as submit_totp:
            result = self.client._submit_mfa_challenge(
                state,
                email="demo@example.com",
                skymail_client=mailbox,
                totp_secret="",
                device_id="device-id",
            )

        self.assertIs(result, expected)
        submit_email.assert_called_once()
        submit_totp.assert_not_called()

    def test_mfa_router_falls_back_to_email_when_supplier_totp_is_rejected(self):
        state = FlowState(
            page_type="mfa_challenge",
            payload={
                "factors": [
                    {"id": "factor-totp", "factor_type": "totp"},
                    {"id": "factor-email", "factor_type": "email"},
                ]
            },
        )
        mailbox = mock.Mock()
        mailbox.supports_totp_code.return_value = True
        expected = FlowState(page_type="add_phone")

        def reject_totp(*args, **kwargs):
            del args, kwargs
            self.client.last_error = (
                "[stage=mfa] ChatGPT MFA 验证失败: "
                "403 - {\"error\":{\"code\":\"incorrect_code\"}}"
            )
            return None

        with mock.patch.object(
            self.client,
            "_submit_totp_mfa_challenge",
            side_effect=reject_totp,
        ) as submit_totp, mock.patch.object(
            self.client,
            "_submit_email_mfa_challenge",
            return_value=expected,
        ) as submit_email:
            result = self.client._submit_mfa_challenge(
                state,
                email="demo@example.com",
                skymail_client=mailbox,
                totp_secret="OLD-SUPPLIER-SECRET",
                device_id="device-id",
            )

        self.assertIs(result, expected)
        submit_totp.assert_called_once()
        submit_email.assert_called_once()

    def test_complete_password_reset_runs_email_otp_then_commits_password(self):
        login_state = FlowState(page_type="login_password")
        otp_state = FlowState(page_type="email_otp_verification")
        new_password_state = FlowState(page_type="reset_password_new_password")
        success_state = FlowState(page_type="reset_password_success")
        mailbox = mock.Mock()
        on_password_reset = mock.Mock()

        with mock.patch.object(
            self.client,
            "_request_password_reset_otp",
            return_value=otp_state,
        ) as send_otp, mock.patch.object(
            self.client,
            "_handle_otp_verification",
            return_value=new_password_state,
        ) as verify_otp, mock.patch.object(
            self.client,
            "_submit_password_reset_new_password",
            return_value=success_state,
        ) as save_password:
            result = self.client._complete_password_reset(
                login_state,
                email="demo@example.com",
                new_password="Fresh-Password-123!",
                skymail_client=mailbox,
                device_id="device-id",
                on_password_reset=on_password_reset,
            )

        self.assertIs(result, success_state)
        send_otp.assert_called_once()
        verify_otp.assert_called_once()
        save_password.assert_called_once()
        on_password_reset.assert_called_once_with("Fresh-Password-123!")

    def test_complete_password_reset_does_not_commit_when_otp_fails(self):
        on_password_reset = mock.Mock()
        with mock.patch.object(
            self.client,
            "_request_password_reset_otp",
            return_value=FlowState(page_type="email_otp_verification"),
        ), mock.patch.object(
            self.client,
            "_handle_otp_verification",
            return_value=None,
        ):
            result = self.client._complete_password_reset(
                FlowState(page_type="login_password"),
                email="demo@example.com",
                new_password="Fresh-Password-123!",
                skymail_client=mock.Mock(),
                device_id="device-id",
                on_password_reset=on_password_reset,
            )

        self.assertIsNone(result)
        on_password_reset.assert_not_called()

    def test_complete_password_reset_fails_when_local_password_commit_returns_false(self):
        on_password_reset = mock.Mock(return_value=False)
        with mock.patch.object(
            self.client,
            "_request_password_reset_otp",
            return_value=FlowState(page_type="email_otp_verification"),
        ), mock.patch.object(
            self.client,
            "_handle_otp_verification",
            return_value=FlowState(page_type="reset_password_new_password"),
        ), mock.patch.object(
            self.client,
            "_submit_password_reset_new_password",
            return_value=FlowState(page_type="reset_password_success"),
        ):
            result = self.client._complete_password_reset(
                FlowState(page_type="login_password"),
                email="demo@example.com",
                new_password="Fresh-Password-123!",
                skymail_client=mock.Mock(),
                device_id="device-id",
                on_password_reset=on_password_reset,
            )

        self.assertIsNone(result)
        self.assertIn("本地凭据保存失败", self.client.last_error)


if __name__ == "__main__":
    unittest.main()
