import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from io import StringIO
from unittest import mock

from services.external_sync import sync_account, sync_codex2api_account


class DummyAccount:
    def __init__(self, *, platform="chatgpt", email="demo@example.com", token="at-token", extra=None):
        self.platform = platform
        self.email = email
        self.token = token
        self.extra = dict(extra or {})
        self.id = None

    def get_extra(self):
        return dict(self.extra)


def _config_getter(values: dict[str, str]):
    def _get(key: str, default: str = "") -> str:
        return values.get(key, default)

    return _get


class ExternalSyncContributionModeTests(unittest.TestCase):
    def test_codex2api_upload_runs_inside_shared_mutation_lock(self):
        account = DummyAccount(extra={"refresh_token": "rt-local"})
        cfg = {"codex2api_enabled": "1"}
        events = []

        @contextmanager
        def tracked_lock():
            events.append("enter")
            try:
                yield
            finally:
                events.append("exit")

        def upload(*args, **kwargs):
            events.append("upload")
            return True, "ok"

        with mock.patch(
            "core.config_store.config_store.get",
            side_effect=_config_getter(cfg),
        ), mock.patch(
            "services.external_sync.codex2api_account_mutation_lock",
            side_effect=tracked_lock,
        ), mock.patch(
            "platforms.chatgpt.codex2api_upload.upload_to_codex2api",
            side_effect=upload,
        ), mock.patch(
            "services.external_sync.persist_codex2api_sync_result",
        ):
            result = sync_codex2api_account(account)

        self.assertTrue(result["ok"])
        self.assertEqual(events, ["enter", "upload", "exit"])

    def test_codex2api_sync_reports_status_persistence_failure(self):
        account = DummyAccount()
        cfg = {"codex2api_enabled": "1"}
        with mock.patch(
            "core.config_store.config_store.get",
            side_effect=_config_getter(cfg),
        ), mock.patch(
            "platforms.chatgpt.codex2api_upload.upload_to_codex2api",
            return_value=(True, "远端已更新"),
        ), mock.patch(
            "services.external_sync.persist_codex2api_sync_result",
            side_effect=RuntimeError("database is locked"),
        ):
            result = sync_codex2api_account(account)

        self.assertFalse(result["ok"])
        self.assertIn("同步状态保存失败", result["msg"])

    def test_contribution_enabled_uploads_only_to_contribution_server(self):
        account = DummyAccount()
        cfg = {
            "contribution_enabled": "1",
            "contribution_server_url": "http://contribution.local:7317",
            "contribution_key": "pk-public-1",
            "cpa_api_url": "http://cpa.local",
            "codex_proxy_url": "http://codex.local",
            "sub2api_api_url": "http://sub2api.local",
            "sub2api_api_key": "sub2-key",
        }

        with mock.patch("core.config_store.config_store.get", side_effect=_config_getter(cfg)):
            with mock.patch("services.external_sync.upload_chatgpt_account_to_cpa", return_value=(True, "ok")) as upload_mock:
                with mock.patch("services.external_sync.persist_cpa_sync_result") as persist_mock:
                    result = sync_account(account)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "Contribution")
        self.assertTrue(result[0]["ok"])
        upload_mock.assert_called_once_with(
            account,
            api_url="http://contribution.local:7317",
            api_key="pk-public-1",
        )
        persist_mock.assert_called_once_with(account, True, "ok")

    def test_contribution_enabled_without_server_url_fails_fast(self):
        account = DummyAccount()
        cfg = {
            "contribution_enabled": "true",
            "contribution_server_url": "",
            "contribution_key": "pk-public-1",
        }

        with mock.patch("core.config_store.config_store.get", side_effect=_config_getter(cfg)):
            with mock.patch("services.external_sync.upload_chatgpt_account_to_cpa") as upload_mock:
                with mock.patch("services.external_sync.persist_cpa_sync_result") as persist_mock:
                    result = sync_account(account)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "Contribution")
        self.assertFalse(result[0]["ok"])
        self.assertIn("未配置", result[0]["msg"])
        upload_mock.assert_not_called()
        persist_mock.assert_called_once()

    def test_custom_contribution_upload_sends_token_json_and_top_level_tokens(self):
        account = DummyAccount(
            extra={
                "refreshToken": "rt-camel-case",
                "accessToken": "at-camel-case",
                "idToken": "id-camel-case",
                "clientId": "client-camel-case",
            }
        )
        cfg = {
            "contribution_enabled": "1",
            "contribution_mode": "custom",
            "custom_contribution_url": "http://custom.local:5000",
            "custom_contribution_token": "custom-token",
        }

        response = mock.Mock()
        response.status_code = 200
        response.json.return_value = {"message": "queued"}

        with mock.patch("core.config_store.config_store.get", side_effect=_config_getter(cfg)):
            with mock.patch("platforms.chatgpt.cpa_upload.generate_token_json", return_value={"type": "codex", "email": account.email}):
                with mock.patch("requests.post", return_value=response) as post_mock:
                    with mock.patch("services.external_sync.persist_cpa_sync_result") as persist_mock:
                        result = sync_account(account)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "CustomContribution")
        self.assertTrue(result[0]["ok"])
        persist_mock.assert_called_once_with(account, True, "上传成功: queued")
        post_mock.assert_called_once()

        payload = post_mock.call_args.kwargs["json"]
        self.assertEqual(payload["email"], account.email)
        self.assertEqual(payload["refresh_token"], "rt-camel-case")
        self.assertEqual(payload["access_token"], "at-camel-case")
        self.assertEqual(payload["token_json"]["refresh_token"], "rt-camel-case")
        self.assertEqual(payload["token_json"]["access_token"], "at-camel-case")
        self.assertEqual(payload["token_json"]["id_token"], "id-camel-case")
        self.assertEqual(payload["token_json"]["client_id"], "client-camel-case")

    def test_custom_contribution_does_not_print_token_keys_or_prefixes(self):
        account = DummyAccount(
            extra={
                "refreshToken": "rt-super-secret-prefix-value",
                "accessToken": "at-super-secret-prefix-value",
            }
        )
        cfg = {
            "contribution_enabled": "1",
            "contribution_mode": "custom",
            "custom_contribution_url": "http://custom.local:5000",
            "custom_contribution_token": "custom-token",
        }
        response = mock.Mock(status_code=200)
        response.json.return_value = {"message": "queued"}
        captured_stdout = StringIO()
        captured_stderr = StringIO()

        with mock.patch(
            "core.config_store.config_store.get",
            side_effect=_config_getter(cfg),
        ), mock.patch(
            "platforms.chatgpt.cpa_upload.generate_token_json",
            return_value={"type": "codex", "email": account.email},
        ), mock.patch(
            "requests.post",
            return_value=response,
        ), mock.patch(
            "services.external_sync.persist_cpa_sync_result",
        ), redirect_stdout(captured_stdout), redirect_stderr(captured_stderr):
            result = sync_account(account)

        self.assertTrue(result[0]["ok"])
        output = captured_stdout.getvalue() + captured_stderr.getvalue()
        self.assertNotIn("rt-super-secret", output)
        self.assertNotIn("at-super-secret", output)
        self.assertNotIn("Final token_json keys", output)

    def test_contribution_disabled_keeps_existing_cpa_sync(self):
        account = DummyAccount()
        cfg = {
            "contribution_enabled": "0",
            "cpa_api_url": "http://cpa.local",
        }

        with mock.patch("core.config_store.config_store.get", side_effect=_config_getter(cfg)):
            with mock.patch("services.external_sync.upload_chatgpt_account_to_cpa", return_value=(True, "ok")) as upload_mock:
                with mock.patch("services.external_sync.persist_cpa_sync_result") as persist_mock:
                    result = sync_account(account)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "CPA")
        upload_mock.assert_called_once_with(account)
        persist_mock.assert_called_once_with(account, True, "ok")

    def test_cpa_disabled_skips_auto_upload_but_keeps_configuration(self):
        account = DummyAccount()
        cfg = {
            "contribution_enabled": "0",
            "cpa_enabled": "0",
            "cpa_api_url": "http://cpa.local",
        }

        with mock.patch("core.config_store.config_store.get", side_effect=_config_getter(cfg)):
            with mock.patch("services.external_sync.upload_chatgpt_account_to_cpa") as upload_mock:
                with mock.patch("services.external_sync.persist_cpa_sync_result") as persist_mock:
                    result = sync_account(account)

        self.assertEqual(result, [])
        upload_mock.assert_not_called()
        persist_mock.assert_not_called()

    def test_sub2api_enabled_uploads_and_persists_sync_status(self):
        account = DummyAccount()
        cfg = {
            "contribution_enabled": "0",
            "sub2api_enabled": "1",
            "sub2api_api_url": "http://sub2api.local",
            "sub2api_api_key": "sub2-key",
        }

        with mock.patch("core.config_store.config_store.get", side_effect=_config_getter(cfg)):
            with mock.patch("platforms.chatgpt.sub2api_upload.upload_to_sub2api", return_value=(True, "ok")) as upload_mock:
                with mock.patch("services.external_sync.persist_sub2api_sync_result") as persist_mock:
                    result = sync_account(account)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "Sub2API")
        self.assertTrue(result[0]["ok"])
        upload_mock.assert_called_once()
        persist_mock.assert_called_once_with(account, True, "ok")

    def test_sub2api_disabled_skips_auto_upload_but_keeps_configuration(self):
        account = DummyAccount()
        cfg = {
            "contribution_enabled": "0",
            "sub2api_enabled": "0",
            "sub2api_api_url": "http://sub2api.local",
            "sub2api_api_key": "sub2-key",
        }

        with mock.patch("core.config_store.config_store.get", side_effect=_config_getter(cfg)):
            with mock.patch("platforms.chatgpt.sub2api_upload.upload_to_sub2api") as upload_mock:
                with mock.patch("services.external_sync.persist_sub2api_sync_result") as persist_mock:
                    result = sync_account(account)

        self.assertEqual(result, [])
        upload_mock.assert_not_called()
        persist_mock.assert_not_called()

    def test_codex2api_enabled_uploads_and_persists_sync_status(self):
        account = DummyAccount(extra={"refresh_token": "rt-local"})
        cfg = {
            "contribution_enabled": "0",
            "codex2api_enabled": "1",
            "codex2api_api_url": "http://codex2api.local:8080",
            "codex2api_admin_key": "admin-key",
        }

        with mock.patch(
            "core.config_store.config_store.get",
            side_effect=_config_getter(cfg),
        ):
            with mock.patch(
                "platforms.chatgpt.codex2api_upload.upload_to_codex2api",
                return_value=(True, "ok"),
            ) as upload_mock:
                with mock.patch(
                    "services.external_sync.persist_codex2api_sync_result"
                ) as persist_mock:
                    result = sync_account(account)

        self.assertEqual(
            result,
            [{"name": "Codex2API", "ok": True, "msg": "ok"}],
        )
        upload_mock.assert_called_once()
        uploaded_account = upload_mock.call_args.args[0]
        self.assertEqual(uploaded_account.email, account.email)
        self.assertEqual(uploaded_account.refresh_token, "rt-local")
        persist_mock.assert_called_once_with(account, True, "ok")

    def test_codex2api_disabled_keeps_configuration_without_upload(self):
        account = DummyAccount()
        cfg = {
            "contribution_enabled": "0",
            "codex2api_enabled": "0",
            "codex2api_api_url": "http://codex2api.local:8080",
            "codex2api_admin_key": "admin-key",
        }

        with mock.patch(
            "core.config_store.config_store.get",
            side_effect=_config_getter(cfg),
        ):
            with mock.patch(
                "platforms.chatgpt.codex2api_upload.upload_to_codex2api"
            ) as upload_mock:
                result = sync_account(account)

        self.assertEqual(result, [])
        upload_mock.assert_not_called()

    def test_codex2api_runs_before_contribution_mode_early_return(self):
        account = DummyAccount(extra={"refresh_token": "rt-local"})
        cfg = {
            "contribution_enabled": "1",
            "contribution_server_url": "http://contribution.local:7317",
            "contribution_key": "public-key",
            "codex2api_enabled": "1",
            "codex2api_api_url": "http://codex2api.local:8080",
            "codex2api_admin_key": "admin-key",
        }

        with mock.patch(
            "core.config_store.config_store.get",
            side_effect=_config_getter(cfg),
        ):
            with mock.patch(
                "platforms.chatgpt.codex2api_upload.upload_to_codex2api",
                return_value=(True, "codex-ok"),
            ):
                with mock.patch(
                    "services.external_sync.persist_codex2api_sync_result"
                ):
                    with mock.patch(
                        "services.external_sync.upload_chatgpt_account_to_cpa",
                        return_value=(True, "contribution-ok"),
                    ):
                        with mock.patch(
                            "services.external_sync.persist_cpa_sync_result"
                        ):
                            result = sync_account(account)

        self.assertEqual(
            [item["name"] for item in result],
            ["Codex2API", "Contribution"],
        )


if __name__ == "__main__":
    unittest.main()
