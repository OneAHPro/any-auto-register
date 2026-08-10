import json
import unittest

from services.mail_imports.auto_detection import detect_mail_import_content


class MailImportAutoDetectionTests(unittest.TestCase):
    def test_detects_microsoft_mailapi_url_without_exposing_url(self):
        secret_url = "https://mail.example.test/messages?token=super-secret"

        result = detect_mail_import_content(
            f"worker@outlook.com----{secret_url}"
        )

        self.assertEqual(result.counts, {"microsoft": 1, "applemail": 0, "unresolved": 0})
        self.assertEqual(result.rows[0].provider, "microsoft")
        self.assertEqual(result.rows[0].account_type, "mailapi_url")
        self.assertNotIn(secret_url, json.dumps(result.to_public_dict()))

    def test_detects_chatgpt_password_totp_as_applemail(self):
        secret = "E4MEYUM757WMF6YTEUD43EWRXZK5R7IP"

        result = detect_mail_import_content(
            f"worker@gmail.com----password-value----{secret}"
        )

        self.assertEqual(result.counts, {"microsoft": 0, "applemail": 1, "unresolved": 0})
        self.assertEqual(result.rows[0].provider, "applemail")
        self.assertEqual(result.rows[0].account_type, "chatgpt_password_totp")
        self.assertNotIn(secret, json.dumps(result.to_public_dict()))

    def test_detects_url_credentials_and_reset_mail_as_applemail(self):
        content = "\n".join(
            [
                "one@gmail.com----password----https://mail.test/messages/one----https://totp.test/ABCDEFGHIJKLMNOP",
                "two@gmail.com----忘记密码----https://mail.test/messages/two",
            ]
        )

        result = detect_mail_import_content(content)

        self.assertEqual(result.counts, {"microsoft": 0, "applemail": 2, "unresolved": 0})
        self.assertEqual(
            [row.account_type for row in result.rows],
            ["chatgpt_password_url_otp", "chatgpt_password_reset_url_mail"],
        )

    def test_non_reset_url_credentials_require_both_mail_and_totp_urls(self):
        result = detect_mail_import_content(
            "one@gmail.com----password----https://mail.test/messages/one----not-a-totp-url"
        )

        self.assertFalse(result.can_import)
        self.assertEqual(result.counts["unresolved"], 1)
        self.assertNotIn("https://", result.rows[0].message)

    def test_detects_known_microsoft_oauth_domains(self):
        content = "\n".join(
            [
                "one@outlook.jp----password----client-id-value-1234567890----refresh-token-value-12345678901234567890",
                "two@hotmail.com----password----client-id-value-1234567890----refresh-token-value-12345678901234567890",
            ]
        )

        result = detect_mail_import_content(content)

        self.assertEqual(result.counts, {"microsoft": 2, "applemail": 0, "unresolved": 0})
        self.assertTrue(all(row.account_type == "microsoft_oauth" for row in result.rows))

    def test_detects_icloud_oauth_and_json_as_applemail(self):
        oauth_result = detect_mail_import_content(
            "one@icloud.com----password----client-id-value-1234567890----refresh-token-value-12345678901234567890"
        )
        json_result = detect_mail_import_content(
            json.dumps(
                [
                    {
                        "email": "two@gmail.com",
                        "password": "secret-password",
                        "mfa_secret": "QM5QPLWGNKZYUQDWSCBDJIJUGXEHIQA3",
                    }
                ]
            )
        )

        self.assertEqual(oauth_result.rows[0].provider, "applemail")
        self.assertEqual(oauth_result.rows[0].account_type, "applemail_oauth")
        self.assertEqual(json_result.rows[0].provider, "applemail")
        self.assertEqual(json_result.rows[0].account_type, "chatgpt_password_totp")
        self.assertNotIn("secret-password", json.dumps(json_result.to_public_dict()))

    def test_leaves_ambiguous_four_part_custom_domain_unresolved(self):
        result = detect_mail_import_content(
            "one@example.com----password----client-id-value-1234567890----refresh-token-value-12345678901234567890"
        )

        self.assertEqual(result.counts, {"microsoft": 0, "applemail": 0, "unresolved": 1})
        self.assertIsNone(result.rows[0].provider)
        self.assertIn("手动", result.rows[0].message)

    def test_duplicate_email_is_reported_before_import(self):
        result = detect_mail_import_content(
            "\n".join(
                [
                    "same@outlook.com----https://mail.test/one",
                    "SAME@outlook.com----https://mail.test/two",
                ]
            )
        )

        self.assertTrue(result.has_duplicates)
        self.assertEqual(result.duplicate_emails, ["same@outlook.com"])
        self.assertEqual(result.counts["unresolved"], 2)
        self.assertTrue(all(not row.resolved for row in result.rows))

    def test_mixed_payload_preserves_group_content_for_existing_strategies(self):
        microsoft_line = "one@outlook.com----https://mail.test/messages/one"
        applemail_line = (
            "two@gmail.com----password----QM5QPLWGNKZYUQDWSCBDJIJUGXEHIQA3"
        )

        result = detect_mail_import_content(f"{microsoft_line}\n{applemail_line}")

        self.assertEqual(result.provider_content("microsoft"), microsoft_line)
        self.assertEqual(result.provider_content("applemail"), applemail_line)
        self.assertEqual(result.counts, {"microsoft": 1, "applemail": 1, "unresolved": 0})

    def test_ignores_supplier_instructions_around_email_rows(self):
        account_line = (
            "two@gmail.com----password----QM5QPLWGNKZYUQDWSCBDJIJUGXEHIQA3"
        )

        result = detect_mail_import_content(
            f"=== 使用说明 ===\n请先阅读登录流程\n=== 卡密内容 ===\n{account_line}"
        )

        self.assertTrue(result.can_import)
        self.assertEqual(result.counts, {"microsoft": 0, "applemail": 1, "unresolved": 0})
        self.assertEqual(result.provider_content("applemail"), account_line)

    def test_marks_malformed_credential_line_unresolved_instead_of_dropping_it(self):
        valid_line = "valid@outlook.com----https://mail.test/messages/valid"
        malformed_line = "broken@outlook----https://mail.test/messages/broken"

        result = detect_mail_import_content(
            f"=== 卡密内容 ===\n{valid_line}\n{malformed_line}"
        )

        self.assertFalse(result.can_import)
        self.assertEqual(result.counts, {"microsoft": 1, "applemail": 0, "unresolved": 1})
        self.assertEqual(result.rows[1].line_number, 3)
        self.assertIn("邮箱地址", result.rows[1].message)

    def test_accepts_utf8_bom_before_first_credential_row(self):
        account_line = "one@outlook.com----https://mail.test/messages/one"

        result = detect_mail_import_content(f"\ufeff{account_line}")

        self.assertTrue(result.can_import)
        self.assertEqual(result.counts, {"microsoft": 1, "applemail": 0, "unresolved": 0})
        self.assertEqual(result.provider_content("microsoft"), account_line)


if __name__ == "__main__":
    unittest.main()
