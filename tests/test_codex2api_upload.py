import base64
import json
import unittest
from types import SimpleNamespace
from unittest import mock

from curl_cffi import requests as cffi_requests

from platforms.chatgpt.codex2api_upload import (
    delete_codex2api_credential,
    upload_to_codex2api,
)


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

    @staticmethod
    def _jwt(payload):
        encoded = base64.urlsafe_b64encode(
            json.dumps(payload).encode("utf-8")
        ).decode("ascii").rstrip("=")
        return f"header.{encoded}.signature"

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
    def test_replace_existing_uses_identity_import_with_both_fresh_tokens(self, post, get_config):
        get_config.side_effect = self._configured
        post.return_value = _Response(
            ValueError("SSE response"),
            text=(
                'data: {"type":"progress","current":1,"total":1}\n\n'
                'data: {"type":"complete","success":0,"updated":1,'
                '"duplicate":0,"failed":0,"total":1}\n\n'
            ),
        )
        list_before = _Response(
            {
                "accounts": [
                    {
                        "id": 7,
                        "name": "demo@example.com",
                        "email": "demo@example.com",
                        "chatgpt_account_id": "workspace-1",
                        "status": "error",
                    }
                ]
            }
        )
        list_after = _Response(
            {
                "accounts": [
                    {
                        "id": 7,
                        "name": "demo@example.com",
                        "email": "demo@example.com",
                        "chatgpt_account_id": "workspace-1",
                        "status": "active",
                    },
                    {
                        "id": 9,
                        "name": "demo@example.com",
                        "email": "demo@example.com",
                        "chatgpt_account_id": "account-object-1",
                        "status": "active",
                    },
                ]
            }
        )
        test_response = _Response(
            ValueError("SSE response"),
            text='data: {"type":"test_complete","success":true}\n\n',
        )
        account = SimpleNamespace(
            email="demo@example.com",
            refresh_token="fresh-rt",
            access_token="fresh-at",
            id_token="fresh-id",
            session_token="fresh-session",
            user_id="user-1",
            account_id="account-object-1",
            workspace_id="workspace-1",
        )

        with mock.patch(
            "platforms.chatgpt.codex2api_upload.cffi_requests.get"
        ) as get, mock.patch(
            "platforms.chatgpt.codex2api_upload.CurlMime",
            create=True,
        ) as curl_mime:
            get.side_effect = [list_before, list_after, test_response]
            ok, message = upload_to_codex2api(account, replace_existing=True)

        self.assertTrue(ok)
        self.assertIn("已覆盖更新", message)
        self.assertEqual(get.call_count, 3)
        self.assertIn("/api/admin/accounts/7/test", get.call_args_list[2].args[0])
        self.assertEqual(
            post.call_args.args[0],
            "http://codex2api.local:8080/api/admin/accounts/import",
        )
        self.assertNotIn("data", post.call_args.kwargs)
        self.assertNotIn("files", post.call_args.kwargs)
        self.assertNotIn("Content-Type", post.call_args.kwargs["headers"])
        mime = curl_mime.return_value
        self.assertIs(post.call_args.kwargs["multipart"], mime)
        self.assertEqual(mime.addpart.call_count, 2)
        format_part, file_part = mime.addpart.call_args_list
        self.assertEqual(format_part.kwargs, {"name": "format", "data": b"json"})
        self.assertEqual(file_part.kwargs["name"], "file")
        self.assertEqual(file_part.kwargs["filename"], "chatgpt-account.json")
        self.assertEqual(file_part.kwargs["content_type"], "application/json")
        payload = __import__("json").loads(file_part.kwargs["data"].decode("utf-8"))
        self.assertEqual(payload["refresh_token"], "fresh-rt")
        self.assertEqual(payload["access_token"], "fresh-at")
        self.assertEqual(payload["id_token"], "fresh-id")
        self.assertEqual(payload["email"], "demo@example.com")
        self.assertEqual(payload["workspace_id"], "workspace-1")
        self.assertEqual(payload["account_id"], "workspace-1")
        mime.close.assert_called_once_with()

    @mock.patch("platforms.chatgpt.codex2api_upload._get_config_value")
    @mock.patch("platforms.chatgpt.codex2api_upload.cffi_requests.post")
    def test_replace_existing_verifies_new_row_and_removes_stale_remote_row(self, post, get_config):
        get_config.side_effect = self._configured
        post.return_value = _Response(
            ValueError("SSE response"),
            text=(
                'data: {"type":"complete","success":1,"updated":0,'
                '"duplicate":0,"failed":0,"total":1}\n\n'
            ),
        )
        old_row = {
            "id": 7,
            "name": "demo@example.com",
            "email": "",
            "chatgpt_account_id": "",
            "status": "error",
            "error_message": "token_invalidated",
        }
        new_row = {
            "id": 8,
            "name": "demo@example.com",
            "email": "demo@example.com",
            "chatgpt_account_id": "workspace-1",
            "status": "active",
        }
        account = SimpleNamespace(
            email="demo@example.com",
            refresh_token="fresh-rt",
            access_token="fresh-at",
            id_token="fresh-id",
            session_token="fresh-session",
            account_id="workspace-1",
            workspace_id="workspace-1",
            user_id="user-1",
        )

        with mock.patch(
            "platforms.chatgpt.codex2api_upload.cffi_requests.get"
        ) as get, mock.patch(
            "platforms.chatgpt.codex2api_upload.cffi_requests.delete"
        ) as delete:
            get.side_effect = [
                _Response({"accounts": [old_row]}),
                _Response({"accounts": [old_row, new_row]}),
                _Response(
                    ValueError("SSE response"),
                    text='data: {"type":"test_complete","success":true}\n\n',
                ),
            ]
            delete.return_value = _Response({"message": "账号已删除"})
            ok, message = upload_to_codex2api(account, replace_existing=True)

        self.assertTrue(ok)
        self.assertIn("替换", message)
        self.assertEqual(
            delete.call_args.args[0],
            "http://codex2api.local:8080/api/admin/accounts/7",
        )
        self.assertIn("/api/admin/accounts/8/test", get.call_args_list[2].args[0])

    @mock.patch("platforms.chatgpt.codex2api_upload._get_config_value")
    @mock.patch("platforms.chatgpt.codex2api_upload.cffi_requests.post")
    def test_replace_existing_matches_remote_chatgpt_user_identity(
        self,
        post,
        get_config,
    ):
        get_config.side_effect = self._configured
        post.return_value = _Response(
            {"success": 0, "updated": 1, "duplicate": 0, "failed": 0}
        )
        before_row = {
            "id": 7,
            "name": "demo@example.com",
            "email": "demo@example.com",
            "chatgpt_account_id": "user-1",
            "status": "error",
        }
        after_row = {**before_row, "status": "active"}
        account = SimpleNamespace(
            email="demo@example.com",
            refresh_token="fresh-rt",
            access_token="fresh-at",
            id_token=self._jwt(
                {
                    "https://api.openai.com/auth": {
                        "chatgpt_account_id": "workspace-1",
                        "chatgpt_user_id": "user-1",
                    }
                }
            ),
            workspace_id="workspace-1",
            account_id="workspace-1",
        )

        with mock.patch(
            "platforms.chatgpt.codex2api_upload.cffi_requests.get"
        ) as get:
            get.side_effect = [
                _Response({"accounts": [before_row]}),
                _Response({"accounts": [after_row]}),
                _Response(
                    ValueError("SSE response"),
                    text='data: {"type":"test_complete","success":true}\n\n',
                ),
            ]
            ok, message = upload_to_codex2api(account, replace_existing=True)

        self.assertTrue(ok)
        self.assertIn("已覆盖更新", message)
        self.assertIn("/api/admin/accounts/7/test", get.call_args_list[2].args[0])

    @mock.patch("platforms.chatgpt.codex2api_upload._get_config_value")
    @mock.patch("platforms.chatgpt.codex2api_upload.cffi_requests.post")
    def test_replace_existing_keeps_authenticated_usage_limited_account(
        self,
        post,
        get_config,
    ):
        get_config.side_effect = self._configured
        post.return_value = _Response(
            {"success": 1, "updated": 0, "duplicate": 0, "failed": 0}
        )
        new_row = {
            "id": 8,
            "name": "demo@example.com",
            "email": "demo@example.com",
            "chatgpt_account_id": "workspace-1",
            "status": "rate_limited",
        }
        usage_event = {
            "type": "error",
            "error": (
                "The usage limit has been reached\n\n"
                '上游事件: {"response":{"error":'
                '{"type":"usage_limit_reached"}}}'
            ),
        }
        account = SimpleNamespace(
            email="demo@example.com",
            refresh_token="fresh-rt",
            access_token="fresh-at",
            workspace_id="workspace-1",
            account_id="workspace-1",
        )

        with mock.patch(
            "platforms.chatgpt.codex2api_upload.cffi_requests.get"
        ) as get, mock.patch(
            "platforms.chatgpt.codex2api_upload.cffi_requests.delete"
        ) as delete:
            get.side_effect = [
                _Response({"accounts": []}),
                _Response({"accounts": [new_row]}),
                _Response(
                    ValueError("SSE response"),
                    text=f"data: {json.dumps(usage_event)}\n\n",
                ),
            ]
            ok, message = upload_to_codex2api(account, replace_existing=True)

        self.assertTrue(ok)
        self.assertIn("用量已达上限", message)
        delete.assert_not_called()

    @mock.patch("platforms.chatgpt.codex2api_upload._get_config_value")
    @mock.patch("platforms.chatgpt.codex2api_upload.cffi_requests.post")
    def test_replace_existing_prioritizes_invalid_token_over_usage_text(
        self,
        post,
        get_config,
    ):
        get_config.side_effect = self._configured
        post.return_value = _Response(
            {"success": 1, "updated": 0, "duplicate": 0, "failed": 0}
        )
        new_row = {
            "id": 8,
            "name": "demo@example.com",
            "email": "demo@example.com",
            "chatgpt_account_id": "workspace-1",
        }
        account = SimpleNamespace(
            email="demo@example.com",
            refresh_token="fresh-rt",
            access_token="fresh-at",
            workspace_id="workspace-1",
            account_id="workspace-1",
        )

        with mock.patch(
            "platforms.chatgpt.codex2api_upload.cffi_requests.get"
        ) as get, mock.patch(
            "platforms.chatgpt.codex2api_upload.cffi_requests.delete"
        ) as delete:
            get.side_effect = [
                _Response({"accounts": []}),
                _Response({"accounts": [new_row]}),
                _Response(
                    {
                        "success": False,
                        "error": {
                            "code": "token_invalidated",
                            "message": "The usage limit has been reached",
                        },
                    }
                ),
            ]
            delete.return_value = _Response({"message": "账号已删除"})
            ok, message = upload_to_codex2api(account, replace_existing=True)

        self.assertFalse(ok)
        self.assertIn("token_invalidated", message)
        self.assertEqual(
            delete.call_args.args[0],
            "http://codex2api.local:8080/api/admin/accounts/8",
        )

    @mock.patch("platforms.chatgpt.codex2api_upload._get_config_value")
    @mock.patch("platforms.chatgpt.codex2api_upload.cffi_requests.post")
    def test_replace_existing_without_identity_rejects_ambiguous_updated_rows(
        self, post, get_config
    ):
        get_config.side_effect = self._configured
        post.return_value = _Response(
            {"success": 0, "updated": 1, "duplicate": 0, "failed": 0}
        )
        rows = [
            {
                "id": 7,
                "name": "demo@example.com",
                "email": "demo@example.com",
                "chatgpt_account_id": "workspace-a",
            },
            {
                "id": 9,
                "name": "demo@example.com",
                "email": "demo@example.com",
                "chatgpt_account_id": "workspace-b",
            },
        ]

        with mock.patch(
            "platforms.chatgpt.codex2api_upload.cffi_requests.get"
        ) as get, mock.patch(
            "platforms.chatgpt.codex2api_upload.cffi_requests.delete"
        ) as delete:
            get.side_effect = [
                _Response({"accounts": rows}),
                _Response({"accounts": rows}),
            ]
            ok, message = upload_to_codex2api(
                self._account(refresh_token="fresh-rt", access_token="fresh-at"),
                replace_existing=True,
            )

        self.assertFalse(ok)
        self.assertIn("身份", message)
        self.assertEqual(get.call_count, 2)
        delete.assert_not_called()

    @mock.patch("platforms.chatgpt.codex2api_upload._get_config_value")
    @mock.patch("platforms.chatgpt.codex2api_upload.cffi_requests.post")
    def test_replace_existing_rejects_ambiguous_same_workspace_updated_rows(
        self, post, get_config
    ):
        get_config.side_effect = self._configured
        post.return_value = _Response(
            {"success": 0, "updated": 1, "duplicate": 0, "failed": 0}
        )
        before_rows = [
            {
                "id": 7,
                "name": "demo@example.com",
                "email": "demo@example.com",
                "chatgpt_account_id": "workspace-1",
                "status": "error",
            },
            {
                "id": 9,
                "name": "demo@example.com",
                "email": "demo@example.com",
                "chatgpt_account_id": "workspace-1",
                "status": "active",
            },
        ]
        after_rows = [
            {**before_rows[0], "status": "active"},
            before_rows[1],
        ]
        account = SimpleNamespace(
            email="demo@example.com",
            refresh_token="fresh-rt",
            access_token="fresh-at",
            workspace_id="workspace-1",
        )

        with mock.patch(
            "platforms.chatgpt.codex2api_upload.cffi_requests.get"
        ) as get, mock.patch(
            "platforms.chatgpt.codex2api_upload.cffi_requests.delete"
        ) as delete:
            get.side_effect = [
                _Response({"accounts": before_rows}),
                _Response({"accounts": after_rows}),
            ]
            ok, message = upload_to_codex2api(account, replace_existing=True)

        self.assertFalse(ok)
        self.assertIn("身份不唯一", message)
        self.assertEqual(get.call_count, 2)
        delete.assert_not_called()

    @mock.patch("platforms.chatgpt.codex2api_upload._get_config_value")
    @mock.patch("platforms.chatgpt.codex2api_upload.cffi_requests.post")
    def test_replace_existing_without_identity_verifies_one_new_row_without_cleanup(
        self, post, get_config
    ):
        get_config.side_effect = self._configured
        post.return_value = _Response(
            {"success": 1, "updated": 0, "duplicate": 0, "failed": 0}
        )
        old_row = {
            "id": 7,
            "name": "demo@example.com",
            "email": "demo@example.com",
            "chatgpt_account_id": "",
            "status": "error",
            "error_message": "token_invalidated",
        }
        new_row = {
            "id": 8,
            "name": "demo@example.com",
            "email": "demo@example.com",
            "chatgpt_account_id": "",
            "status": "active",
        }

        with mock.patch(
            "platforms.chatgpt.codex2api_upload.cffi_requests.get"
        ) as get, mock.patch(
            "platforms.chatgpt.codex2api_upload.cffi_requests.delete"
        ) as delete:
            get.side_effect = [
                _Response({"accounts": [old_row]}),
                _Response({"accounts": [old_row, new_row]}),
                _Response(
                    ValueError("SSE response"),
                    text='data: {"type":"test_complete","success":true}\n\n',
                ),
            ]
            ok, message = upload_to_codex2api(
                self._account(refresh_token="fresh-rt", access_token="fresh-at"),
                replace_existing=True,
            )

        self.assertTrue(ok)
        self.assertIn("已新增并验证", message)
        self.assertIn("/api/admin/accounts/8/test", get.call_args_list[2].args[0])
        delete.assert_not_called()

    @mock.patch("platforms.chatgpt.codex2api_upload._get_config_value")
    @mock.patch("platforms.chatgpt.codex2api_upload.cffi_requests.post")
    def test_replace_existing_preserves_identityless_row_without_invalid_token_evidence(
        self, post, get_config
    ):
        get_config.side_effect = self._configured
        post.return_value = _Response(
            {"success": 1, "updated": 0, "duplicate": 0, "failed": 0}
        )
        old_row = {
            "id": 7,
            "name": "demo@example.com",
            "email": "demo@example.com",
            "chatgpt_account_id": "",
            "status": "active",
        }
        new_row = {
            "id": 8,
            "name": "demo@example.com",
            "email": "demo@example.com",
            "chatgpt_account_id": "workspace-1",
            "status": "active",
        }
        account = SimpleNamespace(
            email="demo@example.com",
            refresh_token="fresh-rt",
            access_token="fresh-at",
            account_id="account-object-1",
            workspace_id="workspace-1",
        )

        with mock.patch(
            "platforms.chatgpt.codex2api_upload.cffi_requests.get"
        ) as get, mock.patch(
            "platforms.chatgpt.codex2api_upload.cffi_requests.delete"
        ) as delete:
            get.side_effect = [
                _Response({"accounts": [old_row]}),
                _Response({"accounts": [old_row, new_row]}),
                _Response(
                    ValueError("SSE response"),
                    text='data: {"type":"test_complete","success":true}\n\n',
                ),
            ]
            ok, _message = upload_to_codex2api(account, replace_existing=True)

        self.assertTrue(ok)
        delete.assert_not_called()

    @mock.patch("platforms.chatgpt.codex2api_upload._get_config_value")
    @mock.patch("platforms.chatgpt.codex2api_upload.cffi_requests.post")
    def test_replace_existing_rejects_unverified_new_row(self, post, get_config):
        get_config.side_effect = self._configured
        post.return_value = _Response(
            ValueError("SSE response"),
            text=(
                'data: {"type":"complete","success":1,"updated":0,'
                '"duplicate":0,"failed":0,"total":1}\n\n'
            ),
        )
        old_row = {"id": 7, "name": "demo@example.com", "email": ""}
        new_row = {
            "id": 8,
            "name": "demo@example.com",
            "email": "demo@example.com",
            "chatgpt_account_id": "workspace-1",
        }
        account = SimpleNamespace(
            email="demo@example.com",
            refresh_token="fresh-rt",
            access_token="fresh-at",
            id_token="fresh-id",
            account_id="workspace-1",
            workspace_id="workspace-1",
        )

        with mock.patch(
            "platforms.chatgpt.codex2api_upload.cffi_requests.get"
        ) as get, mock.patch(
            "platforms.chatgpt.codex2api_upload.cffi_requests.delete"
        ) as delete:
            get.side_effect = [
                _Response({"accounts": [old_row]}),
                _Response({"accounts": [old_row, new_row]}),
                _Response(
                    ValueError("SSE response"),
                    text=(
                        'data: {"type":"error",'
                        '"error":"token_invalidated"}\n\n'
                    ),
                ),
            ]
            delete.return_value = _Response({"message": "账号已删除"})
            ok, message = upload_to_codex2api(account, replace_existing=True)

        self.assertFalse(ok)
        self.assertIn("token_invalidated", message)
        self.assertEqual(
            delete.call_args.args[0],
            "http://codex2api.local:8080/api/admin/accounts/8",
        )

    @mock.patch("platforms.chatgpt.codex2api_upload._get_config_value")
    @mock.patch("platforms.chatgpt.codex2api_upload.cffi_requests.post")
    def test_replace_existing_reports_failed_cleanup_after_verification_failure(
        self, post, get_config
    ):
        get_config.side_effect = self._configured
        post.return_value = _Response(
            {"success": 1, "updated": 0, "duplicate": 0, "failed": 0}
        )
        new_row = {
            "id": 8,
            "name": "demo@example.com",
            "email": "demo@example.com",
            "chatgpt_account_id": "workspace-1",
        }
        account = SimpleNamespace(
            email="demo@example.com",
            refresh_token="fresh-rt",
            access_token="fresh-at",
            workspace_id="workspace-1",
        )

        with mock.patch(
            "platforms.chatgpt.codex2api_upload.cffi_requests.get"
        ) as get, mock.patch(
            "platforms.chatgpt.codex2api_upload.cffi_requests.delete"
        ) as delete:
            get.side_effect = [
                _Response({"accounts": []}),
                _Response({"accounts": [new_row]}),
                _Response(
                    ValueError("SSE response"),
                    text=(
                        'data: {"type":"error",'
                        '"error":"token_invalidated"}\n\n'
                    ),
                ),
            ]
            delete.return_value = _Response(
                {"message": "delete rejected"}, status_code=500
            )
            ok, message = upload_to_codex2api(account, replace_existing=True)

        self.assertFalse(ok)
        self.assertIn("token_invalidated", message)
        self.assertIn("清理远端旧账号失败", message)

    @mock.patch("platforms.chatgpt.codex2api_upload._get_config_value")
    @mock.patch("platforms.chatgpt.codex2api_upload.cffi_requests.post")
    def test_replace_existing_does_not_treat_failed_import_as_success(self, post, get_config):
        get_config.side_effect = self._configured
        post.return_value = _Response(
            {
                "success": 0,
                "updated": 0,
                "duplicate": 0,
                "failed": 1,
                "message": "identity update failed",
            }
        )

        with mock.patch(
            "platforms.chatgpt.codex2api_upload.cffi_requests.get",
            return_value=_Response({"accounts": []}),
        ):
            ok, message = upload_to_codex2api(
                self._account(refresh_token="fresh-rt", access_token="fresh-at"),
                replace_existing=True,
            )

        self.assertFalse(ok)
        self.assertIn("identity update failed", message)

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
    def test_http_200_failure_detail_is_bounded(self, post, get_config):
        get_config.side_effect = self._configured
        post.return_value = _Response(
            {"success": 0, "failed": 1, "message": "x" * 1000}
        )

        ok, message = upload_to_codex2api(self._account())

        self.assertFalse(ok)
        self.assertLessEqual(len(message), 200)

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
    def test_missing_endpoint_has_bounded_redacted_target(self, post, get_config):
        get_config.side_effect = lambda key, default="": {
            "codex2api_api_url": (
                "http://admin-secret@codex2api.local:8080/base"
                f"?key=admin-secret&padding={'x' * 1000}#private"
            ),
            "codex2api_admin_key": "admin-secret",
        }.get(key, default)
        post.return_value = _Response({"error": "not found"}, status_code=404)

        ok, message = upload_to_codex2api(self._account())

        self.assertFalse(ok)
        self.assertIn("codex2api.local", message)
        self.assertNotIn("admin-secret", message)
        self.assertNotIn("@", message)
        self.assertNotIn("?", message)
        self.assertNotIn("#", message)
        self.assertLessEqual(len(message), 260)

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
    def test_non_json_error_redacts_long_secret_before_truncating(self, post, get_config):
        long_admin_key = "secret-" + "x" * 500
        get_config.side_effect = lambda key, default="": {
            "codex2api_api_url": "http://codex2api.local:8080",
            "codex2api_admin_key": long_admin_key,
        }.get(key, default)
        post.return_value = _Response(
            ValueError("not json"),
            status_code=500,
            text=f"rejected {long_admin_key}",
        )

        ok, message = upload_to_codex2api(self._account())

        self.assertFalse(ok)
        self.assertNotIn(long_admin_key[:200], message)
        self.assertLessEqual(len(message), 260)

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
    def test_transport_error_detail_is_bounded(self, post, get_config):
        get_config.side_effect = self._configured
        post.side_effect = RuntimeError("x" * 1000)

        with self.assertLogs(
            "platforms.chatgpt.codex2api_upload",
            level="ERROR",
        ) as captured:
            ok, message = upload_to_codex2api(self._account())

        self.assertFalse(ok)
        self.assertLessEqual(len(message), 260)
        self.assertLessEqual(len("\n".join(captured.output)), 320)

    @mock.patch("platforms.chatgpt.codex2api_upload._get_config_value")
    @mock.patch("platforms.chatgpt.codex2api_upload.cffi_requests.post")
    def test_tls_handshake_error_is_retried_with_the_same_upload(self, post, get_config):
        get_config.side_effect = self._configured
        post.side_effect = [
            cffi_requests.exceptions.SSLError("TLS connect error", 35),
            _Response({"success": 1, "failed": 0}),
        ]

        ok, message = upload_to_codex2api(self._account())

        self.assertTrue(ok)
        self.assertIn("上传成功", message)
        self.assertEqual(post.call_count, 2)
        first_call, second_call = post.call_args_list
        self.assertEqual(first_call.args, second_call.args)
        self.assertEqual(first_call.kwargs, second_call.kwargs)

    @mock.patch("platforms.chatgpt.codex2api_upload._get_config_value")
    @mock.patch("platforms.chatgpt.codex2api_upload.cffi_requests.post")
    def test_replace_existing_tls_retry_rebuilds_and_closes_multipart(
        self,
        post,
        get_config,
    ):
        get_config.side_effect = self._configured
        post.side_effect = [
            cffi_requests.exceptions.SSLError("TLS connect error", 35),
            _Response(
                {
                    "success": 0,
                    "updated": 0,
                    "duplicate": 0,
                    "failed": 1,
                    "message": "stop after upload",
                }
            ),
        ]
        first_mime = mock.Mock()
        second_mime = mock.Mock()

        with mock.patch(
            "platforms.chatgpt.codex2api_upload.cffi_requests.get",
            return_value=_Response({"accounts": []}),
        ), mock.patch(
            "platforms.chatgpt.codex2api_upload.CurlMime",
            side_effect=[first_mime, second_mime],
        ) as curl_mime:
            ok, message = upload_to_codex2api(
                self._account(refresh_token="fresh-rt", access_token="fresh-at"),
                replace_existing=True,
            )

        self.assertFalse(ok)
        self.assertIn("stop after upload", message)
        self.assertEqual(post.call_count, 2)
        self.assertIs(post.call_args_list[0].kwargs["multipart"], first_mime)
        self.assertIs(post.call_args_list[1].kwargs["multipart"], second_mime)
        self.assertEqual(curl_mime.call_count, 2)
        first_mime.close.assert_called_once_with()
        second_mime.close.assert_called_once_with()

    @mock.patch("platforms.chatgpt.codex2api_upload._get_config_value")
    @mock.patch("platforms.chatgpt.codex2api_upload.cffi_requests.post")
    def test_non_handshake_ssl_error_is_not_retried(self, post, get_config):
        get_config.side_effect = self._configured
        post.side_effect = cffi_requests.exceptions.SSLError(
            "certificate verify failed",
            60,
        )

        with self.assertLogs(
            "platforms.chatgpt.codex2api_upload",
            level="ERROR",
        ):
            ok, message = upload_to_codex2api(self._account())

        self.assertFalse(ok)
        self.assertIn("上传异常", message)
        post.assert_called_once()

    @mock.patch("platforms.chatgpt.codex2api_upload._get_config_value")
    @mock.patch("platforms.chatgpt.codex2api_upload.cffi_requests.post")
    def test_malformed_success_response_is_failure(self, post, get_config):
        get_config.side_effect = self._configured
        post.return_value = _Response(ValueError("not json"), text="not-json")

        ok, message = upload_to_codex2api(self._account())

        self.assertFalse(ok)
        self.assertIn("无法解析", message)


class Codex2APICredentialDeletionTests(unittest.TestCase):
    @staticmethod
    def _configured(key, default=""):
        return {
            "codex2api_api_url": "http://codex2api.local:8080/",
            "codex2api_admin_key": "admin-super-secret",
        }.get(key, default)

    @staticmethod
    def _jwt(payload):
        encoded = base64.urlsafe_b64encode(
            json.dumps(payload).encode("utf-8")
        ).decode("ascii").rstrip("=")
        return f"header.{encoded}.signature"

    def _delete(self, rows, *, identity=None, delete_response=None):
        with mock.patch(
            "platforms.chatgpt.codex2api_upload._get_config_value",
            side_effect=self._configured,
        ), mock.patch(
            "platforms.chatgpt.codex2api_upload.cffi_requests.get",
            return_value=_Response({"accounts": rows}),
        ) as get, mock.patch(
            "platforms.chatgpt.codex2api_upload.cffi_requests.delete",
            return_value=delete_response or _Response({}, status_code=204),
        ) as delete:
            result = delete_codex2api_credential(
                email=" Demo@Example.com ",
                identity=identity,
            )
        return result, get, delete

    def test_deletes_unique_exact_email_stable_identity_match(self):
        rows = [
            {
                "id": 6,
                "email": "demo@example.com",
                "workspaceId": "workspace-other",
            },
            {
                "id": 7,
                "name": "DEMO@example.com",
                "chatgptAccountId": "workspace-1",
            },
        ]

        result, get, delete = self._delete(
            rows,
            identity={"workspace_id": "workspace-1"},
        )

        self.assertEqual(
            result,
            {"status": "deleted", "remote_id": 7, "message": ""},
        )
        self.assertTrue(
            get.call_args.args[0].endswith("/api/admin/accounts?channel=codex")
        )
        self.assertTrue(
            delete.call_args.args[0].endswith("/api/admin/accounts/7")
        )

    def test_deletes_unique_legacy_exact_email_without_identity(self):
        result, _get, delete = self._delete(
            [{"id": 11, "name": "demo@example.com"}],
            identity={"workspace_id": "workspace-1"},
        )

        self.assertEqual(result["status"], "deleted")
        self.assertEqual(result["remote_id"], 11)
        delete.assert_called_once()

    def test_identity_can_match_access_jwt_auth_namespace(self):
        token = self._jwt(
            {
                "https://api.openai.com/auth": {
                    "chatgpt_user_id": "user-stable-1"
                }
            }
        )
        rows = [
            {
                "id": 12,
                "email": "demo@example.com",
                "chatgpt_user_id": "user-stable-1",
            },
            {
                "id": 13,
                "email": "demo@example.com",
                "chatgpt_user_id": "other-user",
            },
        ]

        result, _get, delete = self._delete(
            rows,
            identity={"accessToken": token},
        )

        self.assertEqual(result["status"], "deleted")
        self.assertEqual(result["remote_id"], 12)
        delete.assert_called_once()

    def test_multiple_remaining_candidates_are_ambiguous_without_delete(self):
        result, _get, delete = self._delete(
            [
                {"id": 21, "email": "demo@example.com"},
                {"id": 22, "name": "demo@example.com"},
            ],
        )

        self.assertEqual(
            result,
            {
                "status": "ambiguous",
                "remote_id": None,
                "message": "Codex2API 对应认证不唯一，已停止删除",
            },
        )
        delete.assert_not_called()

    def test_no_exact_email_candidate_is_already_absent(self):
        result, _get, delete = self._delete(
            [{"id": 31, "email": "someone@example.com"}],
        )

        self.assertEqual(
            result,
            {"status": "already_absent", "remote_id": None, "message": ""},
        )
        delete.assert_not_called()

    def test_delete_404_is_idempotent_already_absent(self):
        result, _get, delete = self._delete(
            [{"id": 41, "email": "demo@example.com"}],
            delete_response=_Response({}, status_code=404),
        )

        self.assertEqual(
            result,
            {"status": "already_absent", "remote_id": 41, "message": ""},
        )
        delete.assert_called_once()

    def test_missing_configuration_skips_network(self):
        for values in (
            {"codex2api_api_url": "", "codex2api_admin_key": "key"},
            {"codex2api_api_url": "http://codex.local", "codex2api_admin_key": ""},
        ):
            with self.subTest(values=values), mock.patch(
                "platforms.chatgpt.codex2api_upload._get_config_value",
                side_effect=lambda key, default="": values.get(key, default),
            ), mock.patch(
                "platforms.chatgpt.codex2api_upload.cffi_requests.get"
            ) as get, mock.patch(
                "platforms.chatgpt.codex2api_upload.cffi_requests.delete"
            ) as delete:
                result = delete_codex2api_credential(email="demo@example.com")

            self.assertEqual(result["status"], "config_missing")
            self.assertIsNone(result["remote_id"])
            self.assertLessEqual(len(result["message"]), 200)
            get.assert_not_called()
            delete.assert_not_called()

    def test_list_failures_are_classified_and_redacted(self):
        secret_token = "at-private-prefix-value"
        cases = [
            (_Response({"error": "admin-super-secret"}, status_code=401), "unauthorized"),
            (_Response({"error": secret_token}, status_code=503), "failed"),
            (_Response(ValueError(secret_token), status_code=200), "failed"),
            (_Response({"accounts": {"token": secret_token}}, status_code=200), "failed"),
        ]
        for response, expected in cases:
            with self.subTest(expected=expected), mock.patch(
                "platforms.chatgpt.codex2api_upload._get_config_value",
                side_effect=self._configured,
            ), mock.patch(
                "platforms.chatgpt.codex2api_upload.cffi_requests.get",
                return_value=response,
            ), mock.patch(
                "platforms.chatgpt.codex2api_upload.cffi_requests.delete"
            ) as delete:
                result = delete_codex2api_credential(
                    email="demo@example.com",
                    identity={
                        "access_token": secret_token,
                        "refresh_token": "rt-private-prefix-value",
                        "id_token": "id-private-prefix-value",
                    },
                )

            self.assertEqual(result["status"], expected)
            self.assertLessEqual(len(result["message"]), 200)
            serialized = json.dumps(result, ensure_ascii=False)
            self.assertNotIn("admin-super-secret", serialized)
            self.assertNotIn("private-prefix", serialized)
            delete.assert_not_called()

    def test_transport_and_delete_failures_are_bounded_and_redacted(self):
        with mock.patch(
            "platforms.chatgpt.codex2api_upload._get_config_value",
            side_effect=self._configured,
        ), mock.patch(
            "platforms.chatgpt.codex2api_upload.cffi_requests.get",
            side_effect=TimeoutError("admin-super-secret timed out"),
        ):
            unavailable = delete_codex2api_credential(
                email="demo@example.com",
                identity={"access_token": "at-secret-value"},
            )
        self.assertEqual(unavailable["status"], "unavailable")

        result, _get, _delete = self._delete(
            [{"id": 51, "email": "demo@example.com"}],
            delete_response=_Response(
                {"error": "admin-super-secret at-secret-value"},
                status_code=500,
            ),
        )
        self.assertEqual(result["status"], "failed")
        for value in (unavailable, result):
            self.assertLessEqual(len(value["message"]), 200)
            serialized = json.dumps(value, ensure_ascii=False)
            self.assertNotIn("admin-super-secret", serialized)
            self.assertNotIn("at-secret-value", serialized)


if __name__ == "__main__":
    unittest.main()
