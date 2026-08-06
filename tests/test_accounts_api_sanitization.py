import json
import unittest

from api import accounts as accounts_api
from core.db import AccountModel


class AccountApiSanitizationTests(unittest.TestCase):
    def test_hides_hardening_totp_and_pending_enrollment_material(self):
        account = AccountModel(
            platform="chatgpt",
            email="demo@example.com",
            password="chatgpt-password",
            token="access-token",
        )
        account.set_extra(
            {
                "totp_secret": "JBSWY3DPEHPK3PXP",
                "mfa_pending_secret": "PENDINGSECRET2222",
                "mfa_hardening_session_id": "enrollment-session-secret",
                "otpauth_uri": (
                    "otpauth://totp/OpenAI:demo@example.com"
                    "?secret=JBSWY3DPEHPK3PXP&issuer=OpenAI"
                ),
                "recovery_codes": ["RECOVERY-ONE", "RECOVERY-TWO"],
                "mfa_hardening_status": "confirming",
            }
        )

        payload = accounts_api._account_for_response(account)
        serialized = json.dumps(payload, ensure_ascii=False, default=str)
        extra = json.loads(payload["extra_json"])

        for secret in (
            "JBSWY3DPEHPK3PXP",
            "PENDINGSECRET2222",
            "enrollment-session-secret",
            "otpauth://",
            "RECOVERY-ONE",
            "RECOVERY-TWO",
        ):
            self.assertNotIn(secret, serialized)
        self.assertTrue(extra["totp_configured"])
        self.assertTrue(extra["mfa_enrollment_pending"])
        self.assertEqual(extra["mfa_hardening_status"], "confirming")

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
            }
        )

        payload = accounts_api._account_for_response(account)
        serialized = json.dumps(payload, ensure_ascii=False, default=str)
        extra = json.loads(payload["extra_json"])

        self.assertNotIn("mailbox-password-secret", serialized)
        self.assertNotIn("MFASEEDSECRET2222", serialized)
        self.assertNotIn("openai-cookie-secret", serialized)
        self.assertNotIn("oauth-code-verifier-secret", serialized)
        self.assertNotIn("oauth-state-secret", serialized)
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


if __name__ == "__main__":
    unittest.main()
