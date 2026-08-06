import unittest

from platforms.chatgpt.log_sanitizer import sanitize_chatgpt_log_message


class ChatGPTLogSanitizerTests(unittest.TestCase):
    def test_redacts_structured_and_free_form_auth_secrets(self):
        secrets = {
            "json-access-secret",
            "json-session-secret",
            "bearer-secret",
            "auth-code-secret",
            "exchange-code-secret",
            "ALPHA9",
            "mail-password-secret",
        }
        message = (
            '{"access_token":"json-access-secret", '
            '"session_token": "json-session-secret"} '
            "Authorization: Bearer bearer-secret "
            "authorization_code=auth-code-secret "
            "code=exchange-code-secret "
            "OTP: ALPHA9 password=mail-password-secret"
        )

        sanitized = sanitize_chatgpt_log_message(message)

        for secret in secrets:
            self.assertNotIn(secret, sanitized)
        self.assertIn("[令牌已隐藏]", sanitized)
        self.assertIn("[授权码已隐藏]", sanitized)
        self.assertIn("[验证码已隐藏]", sanitized)
        self.assertIn("[密码已隐藏]", sanitized)

    def test_redacts_otp_from_source_log_phrases(self):
        messages = (
            "成功获取验证码: ZXCVBN",
            "尝试 OTP: OTP998",
            "检测到近期缓存 OTP，先直接尝试: CACHE7 (3s前)",
            "跳过已尝试验证码: SKIP88",
        )

        for message in messages:
            with self.subTest(message=message):
                sanitized = sanitize_chatgpt_log_message(message)
                self.assertIn("[验证码已隐藏]", sanitized)
                self.assertNotIn(message.rsplit(":", 1)[-1].split()[0], sanitized)

    def test_keeps_non_secret_status_codes_readable(self):
        message = "上游返回 code: token_invalidated status=401"

        self.assertEqual(sanitize_chatgpt_log_message(message), message)


if __name__ == "__main__":
    unittest.main()
