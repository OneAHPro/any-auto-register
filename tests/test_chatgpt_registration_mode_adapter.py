import unittest
from unittest import mock

from api.tasks import _chatgpt_phone_oauth_is_ready
from platforms.chatgpt.chatgpt_registration_mode_adapter import (
    CHATGPT_REGISTRATION_MODE_ACCESS_TOKEN_ONLY,
    CHATGPT_REGISTRATION_MODE_REFRESH_TOKEN,
    ChatGPTRegistrationContext,
    build_chatgpt_registration_mode_adapter,
    resolve_chatgpt_registration_mode,
)


class ChatGPTRegistrationModeAdapterTests(unittest.TestCase):
    def test_resolve_defaults_to_refresh_token_mode(self):
        self.assertEqual(
            resolve_chatgpt_registration_mode({}),
            CHATGPT_REGISTRATION_MODE_REFRESH_TOKEN,
        )

    def test_resolve_supports_boolean_no_rt_flag(self):
        self.assertEqual(
            resolve_chatgpt_registration_mode(
                {"chatgpt_has_refresh_token_solution": False}
            ),
            CHATGPT_REGISTRATION_MODE_ACCESS_TOKEN_ONLY,
        )

    def test_build_account_marks_selected_mode(self):
        adapter = build_chatgpt_registration_mode_adapter(
            {"chatgpt_registration_mode": "access_token_only"}
        )
        result = type(
            "Result",
            (),
            {
                "email": "demo@example.com",
                "password": "pw",
                "account_id": "acct-demo",
                "access_token": "at-demo",
                "refresh_token": "",
                "id_token": "id-demo",
                "session_token": "session-demo",
                "workspace_id": "ws-demo",
                "source": "register",
            },
        )()

        account = adapter.build_account(result, fallback_password="fallback")

        self.assertEqual(account.email, "demo@example.com")
        self.assertEqual(account.password, "pw")
        self.assertEqual(
            account.extra["chatgpt_registration_mode"],
            CHATGPT_REGISTRATION_MODE_ACCESS_TOKEN_ONLY,
        )
        self.assertFalse(account.extra["chatgpt_has_refresh_token_solution"])

    def test_build_account_preserves_staged_login_metadata(self):
        adapter = build_chatgpt_registration_mode_adapter(
            {"chatgpt_registration_mode": "refresh_token"}
        )
        resume_snapshot = {
            "version": 1,
            "expires_at": 1234567890,
            "cookies": [{"name": "login_session", "value": "cookie"}],
        }
        result = type(
            "Result",
            (),
            {
                "email": "existing@example.com",
                "password": "",
                "account_id": "acct-existing",
                "access_token": "at-existing",
                "refresh_token": "",
                "id_token": "",
                "session_token": "session-existing",
                "workspace_id": "ws-existing",
                "source": "existing_account_web_login",
                "metadata": {
                    "proxy_used": "http://127.0.0.1:7890",
                    "phone_verification_required": True,
                    "mailbox_login_context": {
                        "provider": "microsoft",
                        "email": "existing@example.com",
                        "extra": {"client_id": "mail-client"},
                    },
                    "oauth_resume_context": resume_snapshot,
                },
            },
        )()

        account = adapter.build_account(result, fallback_password="fallback")

        self.assertEqual(account.token, "at-existing")
        self.assertEqual(account.extra["refresh_token"], "")
        self.assertTrue(account.extra["chatgpt_phone_verification_required"])
        self.assertEqual(account.extra["proxy_used"], "http://127.0.0.1:7890")
        self.assertEqual(
            account.extra["mailbox_login_context"]["extra"]["client_id"],
            "mail-client",
        )
        self.assertEqual(account.extra.get("oauth_resume_context"), resume_snapshot)

    def test_build_account_preserves_phone_oauth_prepare_failure_marker(self):
        adapter = build_chatgpt_registration_mode_adapter(
            {"chatgpt_registration_mode": "refresh_token"}
        )
        result = type(
            "Result",
            (),
            {
                "email": "existing@example.com",
                "password": "",
                "account_id": "acct-existing",
                "access_token": "at-existing",
                "refresh_token": "",
                "id_token": "",
                "session_token": "session-existing",
                "workspace_id": "ws-existing",
                "source": "existing_account_web_login",
                "metadata": {
                    "phone_verification_required": True,
                    "phone_oauth_ready": False,
                    "phone_oauth_prepare_error": "OAuth bootstrap failed",
                    "oauth_resume_context": {},
                },
            },
        )()

        account = adapter.build_account(result, fallback_password="fallback")

        self.assertFalse(account.extra["phone_oauth_ready"])
        self.assertEqual(
            account.extra["phone_oauth_prepare_error"],
            "OAuth bootstrap failed",
        )
        self.assertNotIn("oauth_resume_context", account.extra)

    def test_build_account_preserves_post_mfa_rebuild_marker_and_ready_v2(self):
        adapter = build_chatgpt_registration_mode_adapter(
            {"chatgpt_registration_mode": "refresh_token"}
        )
        result = type(
            "Result",
            (),
            {
                "email": "existing@example.com",
                "password": "password",
                "account_id": "acct-existing",
                "access_token": "at-existing",
                "refresh_token": "",
                "id_token": "",
                "session_token": "session-existing",
                "workspace_id": "ws-existing",
                "source": "existing_account_web_login",
                "metadata": {
                    "phone_verification_required": True,
                    "phone_oauth_ready": True,
                    "post_mfa_phone_oauth_rebuild_attempted": True,
                    "oauth_resume_context": {
                        "version": 2,
                        "code_verifier": "verifier",
                        "oauth_state": "state",
                        "flow_state": {"page_type": "add_phone"},
                    },
                },
            },
        )()

        account = adapter.build_account(result, fallback_password="fallback")

        self.assertTrue(account.extra["phone_oauth_ready"])
        self.assertTrue(
            account.extra["post_mfa_phone_oauth_rebuild_attempted"]
        )
        self.assertEqual(account.extra["oauth_resume_context"]["version"], 2)
        self.assertTrue(_chatgpt_phone_oauth_is_ready(account))

    def test_exhausted_post_mfa_metadata_blocks_legacy_browser_fallback(self):
        adapter = build_chatgpt_registration_mode_adapter(
            {"chatgpt_registration_mode": "refresh_token"}
        )
        result = type(
            "Result",
            (),
            {
                "email": "existing@example.com",
                "password": "password",
                "account_id": "acct-existing",
                "access_token": "at-existing",
                "refresh_token": "",
                "id_token": "",
                "session_token": "session-existing",
                "workspace_id": "ws-existing",
                "source": "existing_account_web_login",
                "metadata": {
                    "phone_verification_required": True,
                    "phone_oauth_ready": False,
                    "post_mfa_phone_oauth_rebuild_attempted": True,
                    "oauth_resume_context": {},
                    "oauth_browser_context": {
                        "version": 1,
                        "cookies": [
                            {"name": "login_session", "value": "cookie"}
                        ],
                    },
                },
            },
        )()

        account = adapter.build_account(result, fallback_password="fallback")

        self.assertTrue(
            account.extra["post_mfa_phone_oauth_rebuild_attempted"]
        )
        self.assertFalse(_chatgpt_phone_oauth_is_ready(account))

    def test_access_token_only_adapter_passes_runtime_context_to_engine(self):
        created = {}

        class FakeEngine:
            def __init__(self, **kwargs):
                created["kwargs"] = kwargs
                self.email = None
                self.password = None

            def run(self):
                created["email"] = self.email
                created["password"] = self.password
                return type("Result", (), {"success": True})()

        adapter = build_chatgpt_registration_mode_adapter(
            {"chatgpt_registration_mode": "access_token_only"}
        )
        context = ChatGPTRegistrationContext(
            email_service=object(),
            proxy_url="http://127.0.0.1:7890",
            callback_logger=lambda _msg: None,
            email="demo@example.com",
            password="pw-demo",
            browser_mode="headed",
            max_retries=5,
            extra_config={"register_max_retries": 5},
        )

        with mock.patch(
            "platforms.chatgpt.access_token_only_registration_engine.AccessTokenOnlyRegistrationEngine",
            FakeEngine,
        ):
            adapter.run(context)

        self.assertEqual(created["email"], "demo@example.com")
        self.assertEqual(created["password"], "pw-demo")
        self.assertEqual(created["kwargs"]["browser_mode"], "headed")
        self.assertEqual(created["kwargs"]["max_retries"], 5)


if __name__ == "__main__":
    unittest.main()
