import unittest

from services.chatgpt_account_state import (
    apply_chatgpt_status_policy,
    classify_local_probe_state,
    classify_remote_sync_state,
    is_account_deactivated_message,
)


class DummyAccount:
    def __init__(self, status="registered"):
        self.status = status


class ChatGPTAccountStateTests(unittest.TestCase):
    def test_local_401_marks_invalid(self):
        account = DummyAccount()
        reason = apply_chatgpt_status_policy(
            account,
            local_probe={
                "auth": {
                    "state": "access_token_invalidated",
                    "http_status": 401,
                    "error_code": "token_invalidated",
                    "message": "invalidated",
                }
            },
        )
        self.assertEqual(reason, "auth_401")
        self.assertEqual(account.status, "invalid")

    def test_remote_401_marks_invalid(self):
        self.assertEqual(
            classify_remote_sync_state(
                {
                    "remote_state": "access_token_invalidated",
                    "last_probe_status_code": 401,
                    "last_probe_error_code": "token_invalidated",
                    "last_probe_message": "invalidated",
                }
            ),
            "remote_401",
        )

    def test_payment_and_quota_do_not_mark_invalid(self):
        self.assertEqual(
            classify_local_probe_state(
                {
                    "auth": {"state": "access_token_valid", "http_status": 200},
                    "codex": {"state": "payment_required", "http_status": 403, "message": "payment required"},
                }
            ),
            "",
        )
        self.assertEqual(
            classify_remote_sync_state(
                {
                    "remote_state": "quota_exhausted",
                    "last_probe_status_code": 429,
                    "last_probe_error_code": "",
                    "last_probe_message": "usage limit reached",
                }
            ),
            "",
        )

    def test_deactivated_message_marks_invalid(self):
        self.assertEqual(
            classify_local_probe_state(
                {
                    "auth": {
                        "state": "banned_like",
                        "http_status": 403,
                        "error_code": "account_deactivated",
                        "message": "You do not have an account because it has been deleted or deactivated.",
                    }
                }
            ),
            "auth_deactivated",
        )

    def test_chinese_deleted_or_deactivated_message_is_detected(self):
        self.assertTrue(
            is_account_deactivated_message(
                message=(
                    'OTP 无效: {"错误":{"消息":"你没有账号，'
                    '因为它已被删除或停用。如果您认为这是错误，请通过电话联系我们"}}'
                )
            )
        )

    def test_ordinary_invalid_otp_is_not_treated_as_deactivated(self):
        self.assertFalse(
            is_account_deactivated_message(message="OTP 无效: 验证码错误")
        )

    def test_negated_or_diagnostic_phrases_are_not_deactivation_signals(self):
        for message in (
            "Response did not say deleted or deactivated.",
            "Expected account_deleted but received timeout.",
        ):
            with self.subTest(message=message):
                self.assertFalse(is_account_deactivated_message(message=message))


if __name__ == "__main__":
    unittest.main()
