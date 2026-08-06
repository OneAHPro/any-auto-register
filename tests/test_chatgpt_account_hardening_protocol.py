import unittest


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


class ChatGPTAccountHardeningProtocolTests(unittest.TestCase):
    def test_generates_rfc6238_six_digit_totp(self):
        from platforms.chatgpt.account_hardening import generate_totp

        self.assertEqual(
            generate_totp(
                "gezd gnbv-gy3tqojq gezdgnbvgy3tqojq",
                timestamp=59,
            ),
            "287082",
        )

    def test_rejects_invalid_totp_secret(self):
        from platforms.chatgpt.account_hardening import generate_totp

        with self.assertRaisesRegex(ValueError, "TOTP"):
            generate_totp("not-a-base32-secret-0", timestamp=59)

    def test_reads_inventory_with_bearer_account_and_proxy(self):
        from platforms.chatgpt.account_hardening import ChatGPTMFAClient

        transport = FakeTransport(
            [
                FakeResponse(
                    payload={
                        "mfa_enabled_v2": True,
                        "native_default_factor_id": "factor-1",
                        "factors": [
                            {"id": "factor-1", "factor_type": "totp"},
                            {"id": "factor-2", "factor_type": "sms"},
                        ],
                    }
                )
            ]
        )
        client = ChatGPTMFAClient(
            access_token="access-token-secret",
            account_id="account-123",
            proxy="http://proxy.example:8080",
            transport=transport,
        )

        inventory = client.get_inventory()

        self.assertTrue(inventory.enabled)
        self.assertTrue(inventory.has_totp)
        self.assertEqual(inventory.default_factor_id, "factor-1")
        method, url, kwargs = transport.calls[0]
        self.assertEqual(method, "GET")
        self.assertEqual(
            url,
            "https://chatgpt.com/backend-api/accounts/mfa_info",
        )
        self.assertEqual(
            kwargs["headers"]["Authorization"],
            "Bearer access-token-secret",
        )
        self.assertEqual(kwargs["headers"]["OpenAI-Account-ID"], "account-123")
        self.assertEqual(
            kwargs["proxies"],
            {
                "http": "http://proxy.example:8080",
                "https": "http://proxy.example:8080",
            },
        )

    def test_enrolls_and_activates_totp_with_current_contract(self):
        from platforms.chatgpt.account_hardening import ChatGPTMFAClient

        transport = FakeTransport(
            [
                FakeResponse(
                    payload={
                        "session_id": "session-1",
                        "secret": "JBSWY3DPEHPK3PXP",
                    }
                ),
                FakeResponse(payload={"success": True, "factor_id": "factor-1"}),
            ]
        )
        client = ChatGPTMFAClient(
            access_token="access-token",
            transport=transport,
        )

        enrollment = client.start_totp_enrollment()
        activation = client.activate_totp_enrollment(
            enrollment.session_id,
            "654321",
        )

        self.assertEqual(enrollment.session_id, "session-1")
        self.assertEqual(enrollment.secret, "JBSWY3DPEHPK3PXP")
        self.assertTrue(activation)
        first = transport.calls[0]
        self.assertEqual(first[0], "POST")
        self.assertEqual(
            first[1],
            "https://chatgpt.com/backend-api/accounts/mfa/enroll",
        )
        self.assertEqual(
            first[2]["json"],
            {
                "factor_type": "totp",
                "phone_number": None,
                "phone_verification_channel": None,
            },
        )
        second = transport.calls[1]
        self.assertEqual(
            second[1],
            "https://chatgpt.com/backend-api/accounts/mfa/user/activate_enrollment",
        )
        self.assertEqual(
            second[2]["json"],
            {
                "code": "654321",
                "factor_type": "totp",
                "session_id": "session-1",
            },
        )

    def test_parses_otpauth_uri_and_never_leaks_response_secret(self):
        from platforms.chatgpt.account_hardening import (
            ChatGPTMFAClient,
            ChatGPTMFAError,
        )

        uri_transport = FakeTransport(
            [
                FakeResponse(
                    payload={
                        "session_id": "session-2",
                        "otpauth_uri": (
                            "otpauth://totp/OpenAI:user@example.com"
                            "?secret=JBSWY3DPEHPK3PXP&issuer=OpenAI"
                        ),
                    }
                )
            ]
        )
        enrollment = ChatGPTMFAClient(
            access_token="access-token",
            transport=uri_transport,
        ).start_totp_enrollment()
        self.assertEqual(enrollment.secret, "JBSWY3DPEHPK3PXP")

        failing_transport = FakeTransport(
            [
                FakeResponse(
                    status_code=400,
                    payload={"error": "JBSWY3DPEHPK3PXP"},
                    text="secret=JBSWY3DPEHPK3PXP",
                )
            ]
        )
        with self.assertRaises(ChatGPTMFAError) as ctx:
            ChatGPTMFAClient(
                access_token="access-token-secret",
                transport=failing_transport,
            ).start_totp_enrollment()
        rendered = str(ctx.exception)
        self.assertIn("HTTP 400", rendered)
        self.assertNotIn("JBSWY3DPEHPK3PXP", rendered)
        self.assertNotIn("access-token-secret", rendered)

    def test_rejects_incomplete_enrollment_response(self):
        from platforms.chatgpt.account_hardening import (
            ChatGPTMFAClient,
            ChatGPTMFAError,
        )

        transport = FakeTransport([FakeResponse(payload={"session_id": "session-1"})])
        with self.assertRaisesRegex(ChatGPTMFAError, "enrollment response"):
            ChatGPTMFAClient(
                access_token="access-token",
                transport=transport,
            ).start_totp_enrollment()


if __name__ == "__main__":
    unittest.main()
