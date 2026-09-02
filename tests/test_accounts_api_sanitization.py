import json
import unittest

from api import accounts as accounts_api
from core.db import AccountModel


class AccountApiSanitizationTests(unittest.TestCase):
    def test_hides_mailbox_credentials_and_oauth_cookies(self):
        account = AccountModel(
            platform="chatgpt",
            email="demo@icloud.com",
            password="chatgpt-account-password",
            token="access-token",
        )
        account.set_extra(
            {
                "access_token": "access-token",
                "refresh_token": "",
                "mailbox_login_context": {
                    "provider": "icloud",
                    "email": "demo@icloud.com",
                    "account_id": "demo@icloud.com",
                    "extra": {
                        "password": "mailbox-password-secret",
                        "mfa_secret": "MFASEEDSECRET2222",
                        "mail_api_url": "https://mail.example.test/?token=MAILURLSECRET",
                        "totp_url": "https://totp.example.test/?token=TOTPURLSECRET",
                    },
                },
                "oauth_resume_context": {
                    "version": 2,
                    "created_at": 100,
                    "expires_at": 4102444800,
                    "code_verifier": "oauth-code-verifier-secret",
                    "oauth_state": "oauth-state-secret",
                    "cookies": [
                        {
                            "name": "login_session",
                            "value": "openai-cookie-secret",
                            "domain": "auth.openai.com",
                        }
                    ],
                    "flow_state": {"page_type": "add_phone"},
                },
                "oauth_browser_context": {
                    "version": 1,
                    "created_at": 101,
                    "expires_at": 4102444801,
                    "device_id": "device-secret",
                    "cookies": [
                        {
                            "name": "login_session",
                            "value": "browser-cookie-secret",
                            "domain": "auth.openai.com",
                        }
                    ],
                },
            }
        )

        payload = accounts_api._account_for_response(account, include_credentials=False)
        serialized = json.dumps(payload, ensure_ascii=False, default=str)
        extra = json.loads(payload["extra_json"])

        self.assertNotIn("mailbox-password-secret", serialized)
        self.assertNotIn("MFASEEDSECRET2222", serialized)
        self.assertNotIn("openai-cookie-secret", serialized)
        self.assertNotIn("oauth-code-verifier-secret", serialized)
        self.assertNotIn("oauth-state-secret", serialized)
        self.assertNotIn("browser-cookie-secret", serialized)
        self.assertNotIn("device-secret", serialized)
        self.assertNotIn("MAILURLSECRET", serialized)
        self.assertNotIn("TOTPURLSECRET", serialized)
        self.assertEqual(
            extra["mailbox_login_context"],
            {
                "provider": "icloud",
                "email": "demo@icloud.com",
                "account_id": "demo@icloud.com",
                "configured": True,
            },
        )
        self.assertEqual(
            extra["oauth_resume_context"],
            {
                "version": 2,
                "created_at": 100.0,
                "expires_at": 4102444800.0,
                "ready": True,
                "flow_state": {"page_type": "add_phone"},
            },
        )
        self.assertEqual(
            extra["oauth_browser_context"],
            {
                "version": 1,
                "created_at": 101.0,
                "expires_at": 4102444801.0,
                "ready": True,
            },
        )

    def test_account_response_never_exposes_password_or_tokens(self):
        account = AccountModel(
            platform="chatgpt",
            email="safe@example.com",
            password="password-secret",
            token="access-secret",
            extra_json=(
                '{"refresh_token":"refresh-secret",'
                '"access_token":"access-secret",'
                '"id_token":"id-secret",'
                '"session_token":"session-secret",'
                '"workspace_id":"workspace-1"}'
            ),
        )

        payload = accounts_api._account_for_response(account, include_credentials=False)
        serialized = json.dumps(payload, ensure_ascii=False, default=str)

        self.assertNotIn("password-secret", serialized)
        self.assertNotIn("refresh-secret", serialized)
        self.assertNotIn("access-secret", serialized)
        self.assertNotIn("id-secret", serialized)
        self.assertNotIn("session-secret", serialized)


if __name__ == "__main__":
    unittest.main()
