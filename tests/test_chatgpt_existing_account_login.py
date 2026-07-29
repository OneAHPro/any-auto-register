import unittest
from unittest import mock

from platforms.chatgpt.refresh_token_registration_engine import (
    RefreshTokenRegistrationEngine,
)


class DummyEmailService:
    service_type = type("ServiceType", (), {"value": "microsoft"})()

    def create_email(self):
        return {"email": "existing@example.com", "service_id": "mailbox-1"}

    def get_verification_code(self, **kwargs):
        return "123456"


class ExistingAccountLoginTests(unittest.TestCase):
    def _make_engine(self, *, login_only=True):
        return RefreshTokenRegistrationEngine(
            email_service=DummyEmailService(),
            proxy_url="http://127.0.0.1:7890",
            callback_logger=lambda message: None,
            max_retries=1,
            extra_config={
                "chatgpt_existing_account_login_only": login_only,
            },
        )

    def _successful_oauth_client(self):
        client = mock.Mock()
        client.login_and_get_tokens.return_value = {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "id_token": "id-token",
        }
        client.last_error = ""
        client.last_workspace_id = "workspace-1"
        client._get_cookie_value.return_value = "session-token"
        return client

    def test_login_only_skips_registration_and_saves_both_tokens(self):
        engine = self._make_engine()
        oauth_client = self._successful_oauth_client()
        engine._build_oauth_client = mock.Mock(return_value=oauth_client)
        engine._build_chatgpt_client = mock.Mock()
        engine._extract_account_info = mock.Mock(
            return_value={"email": "existing@example.com", "account_id": "account-1"}
        )

        result = engine.run()

        self.assertTrue(result.success)
        self.assertEqual(result.access_token, "access-token")
        self.assertEqual(result.refresh_token, "refresh-token")
        self.assertEqual(result.source, "login")
        engine._build_chatgpt_client.assert_not_called()
        login_kwargs = oauth_client.login_and_get_tokens.call_args.kwargs
        self.assertEqual(login_kwargs["screen_hint"], "login")
        self.assertTrue(login_kwargs["prefer_passwordless_login"])
        self.assertFalse(login_kwargs["complete_about_you_if_needed"])
        self.assertEqual(login_kwargs["login_source"], "existing_account_login_only")
        self.assertTrue(any("加载邮箱凭据" in line for line in result.logs))
        self.assertFalse(any("成功创建邮箱" in line for line in result.logs))

    def test_login_only_rejects_result_without_refresh_token(self):
        engine = self._make_engine()
        oauth_client = self._successful_oauth_client()
        oauth_client.login_and_get_tokens.return_value = {
            "access_token": "access-token",
            "refresh_token": "",
        }
        engine._build_oauth_client = mock.Mock(return_value=oauth_client)
        engine._build_chatgpt_client = mock.Mock()

        result = engine.run()

        self.assertFalse(result.success)
        self.assertIn("Refresh Token", result.error_message)
        engine._build_chatgpt_client.assert_not_called()

    def test_default_mode_still_enters_registration_state_machine(self):
        engine = self._make_engine(login_only=False)
        register_client = mock.Mock()
        register_client.register_complete_flow.return_value = (False, "fatal")
        engine._build_chatgpt_client = mock.Mock(return_value=register_client)
        engine._build_oauth_client = mock.Mock()

        result = engine.run()

        self.assertFalse(result.success)
        register_client.register_complete_flow.assert_called_once()
        engine._build_oauth_client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
