import unittest
from types import SimpleNamespace
from unittest import mock

from platforms.chatgpt.codex2api_upload import upload_to_codex2api


class _Response:
    def __init__(self, payload=None, *, status_code=200, text=""):
        self._payload = payload
        self.status_code = status_code
        self.text = text

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class Codex2APIUploadTests(unittest.TestCase):
    def _account(self, *, refresh_token="rt-secret", access_token="at-secret"):
        return SimpleNamespace(
            email="demo@example.com",
            refresh_token=refresh_token,
            access_token=access_token,
        )

    @staticmethod
    def _configured(key, default=""):
        return {
            "codex2api_api_url": "http://codex2api.local:8080/",
            "codex2api_admin_key": "admin-secret",
        }.get(key, default)

    @mock.patch("platforms.chatgpt.codex2api_upload._get_config_value")
    @mock.patch("platforms.chatgpt.codex2api_upload.cffi_requests.post")
    def test_refresh_token_takes_precedence(self, post, get_config):
        get_config.side_effect = self._configured
        post.return_value = _Response({"success": 1, "failed": 0})

        ok, message = upload_to_codex2api(self._account())

        self.assertTrue(ok)
        self.assertIn("Refresh Token", message)
        self.assertEqual(
            post.call_args.args[0],
            "http://codex2api.local:8080/api/admin/accounts",
        )
        self.assertEqual(
            post.call_args.kwargs["json"],
            {"name": "demo@example.com", "refresh_token": "rt-secret"},
        )
        self.assertEqual(
            post.call_args.kwargs["headers"]["X-Admin-Key"],
            "admin-secret",
        )

    @mock.patch("platforms.chatgpt.codex2api_upload._get_config_value")
    @mock.patch("platforms.chatgpt.codex2api_upload.cffi_requests.post")
    def test_request_verifies_tls_and_rejects_redirects(self, post, get_config):
        get_config.side_effect = self._configured
        post.return_value = _Response({"success": 1, "failed": 0})

        ok, _message = upload_to_codex2api(self._account())

        self.assertTrue(ok)
        self.assertIs(post.call_args.kwargs["verify"], True)
        self.assertIs(post.call_args.kwargs["allow_redirects"], False)

    @mock.patch("platforms.chatgpt.codex2api_upload._get_config_value")
    @mock.patch("platforms.chatgpt.codex2api_upload.cffi_requests.post")
    def test_access_token_is_used_when_refresh_token_is_missing(self, post, get_config):
        get_config.side_effect = self._configured
        post.return_value = _Response({"updated": 1, "failed": 0})

        ok, message = upload_to_codex2api(
            self._account(refresh_token="")
        )

        self.assertTrue(ok)
        self.assertIn("Access Token", message)
        self.assertEqual(
            post.call_args.args[0],
            "http://codex2api.local:8080/api/admin/accounts/at",
        )
        self.assertEqual(
            post.call_args.kwargs["json"],
            {"name": "demo@example.com", "access_token": "at-secret"},
        )

    @mock.patch("platforms.chatgpt.codex2api_upload._get_config_value")
    @mock.patch("platforms.chatgpt.codex2api_upload.cffi_requests.post")
    def test_success_updated_and_duplicate_are_successful(self, post, get_config):
        get_config.side_effect = self._configured
        cases = (
            ({"success": 1, "failed": 0}, "上传成功"),
            ({"updated": 1, "failed": 0}, "已更新"),
            ({"duplicate": 1, "failed": 0}, "已存在"),
        )

        for payload, expected in cases:
            with self.subTest(payload=payload):
                post.return_value = _Response(payload)
                ok, message = upload_to_codex2api(self._account())
                self.assertTrue(ok)
                self.assertIn(expected, message)

    @mock.patch("platforms.chatgpt.codex2api_upload._get_config_value")
    @mock.patch("platforms.chatgpt.codex2api_upload.cffi_requests.post")
    def test_missing_url_does_not_send(self, post, get_config):
        get_config.side_effect = lambda key, default="": {
            "codex2api_admin_key": "admin-secret",
        }.get(key, default)

        ok, message = upload_to_codex2api(self._account())

        self.assertFalse(ok)
        self.assertIn("API URL", message)
        post.assert_not_called()

    @mock.patch("platforms.chatgpt.codex2api_upload._get_config_value")
    @mock.patch("platforms.chatgpt.codex2api_upload.cffi_requests.post")
    def test_missing_admin_key_does_not_send(self, post, get_config):
        get_config.side_effect = lambda key, default="": {
            "codex2api_api_url": "http://codex2api.local:8080",
        }.get(key, default)

        ok, message = upload_to_codex2api(self._account())

        self.assertFalse(ok)
        self.assertIn("Admin Key", message)
        post.assert_not_called()

    @mock.patch("platforms.chatgpt.codex2api_upload._get_config_value")
    @mock.patch("platforms.chatgpt.codex2api_upload.cffi_requests.post")
    def test_missing_account_tokens_does_not_send(self, post, get_config):
        get_config.side_effect = self._configured

        ok, message = upload_to_codex2api(
            self._account(refresh_token="", access_token="")
        )

        self.assertFalse(ok)
        self.assertIn("Refresh Token", message)
        self.assertIn("Access Token", message)
        post.assert_not_called()

    @mock.patch("platforms.chatgpt.codex2api_upload._get_config_value")
    @mock.patch("platforms.chatgpt.codex2api_upload.cffi_requests.post")
    def test_http_200_with_only_failures_is_failure(self, post, get_config):
        get_config.side_effect = self._configured
        post.return_value = _Response(
            {"success": 0, "duplicate": 0, "failed": 1, "message": "invalid token"}
        )

        ok, message = upload_to_codex2api(self._account())

        self.assertFalse(ok)
        self.assertEqual(message, "invalid token")

    @mock.patch("platforms.chatgpt.codex2api_upload._get_config_value")
    @mock.patch("platforms.chatgpt.codex2api_upload.cffi_requests.post")
    def test_authentication_failure_has_safe_message(self, post, get_config):
        get_config.side_effect = self._configured
        post.return_value = _Response(
            {"error": "admin-secret rejected"},
            status_code=401,
        )

        ok, message = upload_to_codex2api(self._account())

        self.assertFalse(ok)
        self.assertIn("Admin Key", message)
        self.assertNotIn("admin-secret", message)

    @mock.patch("platforms.chatgpt.codex2api_upload._get_config_value")
    @mock.patch("platforms.chatgpt.codex2api_upload.cffi_requests.post")
    def test_error_response_redacts_all_secrets(self, post, get_config):
        get_config.side_effect = self._configured
        post.return_value = _Response(
            {"error": "admin-secret rejected rt-secret and at-secret"},
            status_code=500,
        )

        ok, message = upload_to_codex2api(self._account())

        self.assertFalse(ok)
        self.assertNotIn("admin-secret", message)
        self.assertNotIn("rt-secret", message)
        self.assertNotIn("at-secret", message)

    @mock.patch("platforms.chatgpt.codex2api_upload._get_config_value")
    @mock.patch("platforms.chatgpt.codex2api_upload.cffi_requests.post")
    def test_error_response_detail_is_bounded(self, post, get_config):
        get_config.side_effect = self._configured
        post.return_value = _Response(
            {"error": "x" * 1000},
            status_code=500,
        )

        ok, message = upload_to_codex2api(self._account())

        self.assertFalse(ok)
        self.assertLessEqual(len(message), 260)

    @mock.patch("platforms.chatgpt.codex2api_upload._get_config_value")
    @mock.patch("platforms.chatgpt.codex2api_upload.cffi_requests.post")
    def test_transport_error_redacts_all_secrets(self, post, get_config):
        get_config.side_effect = self._configured
        post.side_effect = RuntimeError(
            "admin-secret failed for rt-secret and at-secret"
        )

        with self.assertLogs(
            "platforms.chatgpt.codex2api_upload",
            level="ERROR",
        ) as captured:
            ok, message = upload_to_codex2api(self._account())

        self.assertFalse(ok)
        self.assertNotIn("admin-secret", message)
        self.assertNotIn("rt-secret", message)
        self.assertNotIn("at-secret", message)
        log_output = "\n".join(captured.output)
        self.assertNotIn("admin-secret", log_output)
        self.assertNotIn("rt-secret", log_output)
        self.assertNotIn("at-secret", log_output)

    @mock.patch("platforms.chatgpt.codex2api_upload._get_config_value")
    @mock.patch("platforms.chatgpt.codex2api_upload.cffi_requests.post")
    def test_malformed_success_response_is_failure(self, post, get_config):
        get_config.side_effect = self._configured
        post.return_value = _Response(ValueError("not json"), text="not-json")

        ok, message = upload_to_codex2api(self._account())

        self.assertFalse(ok)
        self.assertIn("无法解析", message)


if __name__ == "__main__":
    unittest.main()
