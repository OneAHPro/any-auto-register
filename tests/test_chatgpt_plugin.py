from contextlib import contextmanager
import threading
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from core.applemail_pool import load_applemail_pool_records, load_applemail_pool_snapshot
from core.applemail_pool import save_applemail_pool_json
from core.base_mailbox import MailboxAccount
from core.base_mailbox import AppleMailMailbox
from core.base_platform import Account, RegisterConfig
from platforms.chatgpt.chatgpt_registration_mode_adapter import (
    RefreshTokenChatGPTRegistrationAdapter,
)
from platforms.chatgpt.plugin import ChatGPTPlatform


class _BlankMailbox:
    def get_email(self):
        return MailboxAccount(email="", account_id="blank-mailbox")

    def wait_for_code(self, *args, **kwargs):
        return "123456"


class _TrackingMailbox:
    def __init__(self):
        self.account = MailboxAccount(email="demo@example.com", account_id="tracked-mailbox")
        self.wait_call = None
        self.current_ids_calls = []

    def get_email(self):
        return self.account

    def get_current_ids(self, account):
        self.current_ids_calls.append(account)
        return {"mid-1"}

    def wait_for_code(self, *args, **kwargs):
        self.wait_call = (args, kwargs)
        return "123456"


class _DelayedBaselineMailbox(_TrackingMailbox):
    def __init__(self):
        super().__init__()
        self.baseline_started = threading.Event()
        self.baseline_release = threading.Event()
        self.baseline_finished = threading.Event()

    def get_current_ids(self, account):
        self.current_ids_calls.append(account)
        self.baseline_started.set()
        self.baseline_release.wait(timeout=1)
        self.baseline_finished.set()
        return {"mid-1"}


class _SlowMailbox(_TrackingMailbox):
    def __init__(self):
        super().__init__()
        self.timeouts = []
        self.poll_intervals = []
        self.background_entries = 0

    def wait_for_code(self, *args, **kwargs):
        self.timeouts.append(kwargs.get("timeout"))
        self.poll_intervals.append(kwargs.get("poll_interval"))
        if len(self.timeouts) == 1:
            raise TimeoutError("foreground mailbox wait expired")
        return "123456"

    @contextmanager
    def pause_active_slot_for_mailbox_wait(self):
        self.background_entries += 1
        yield True


class _RepeatedSlowMailbox(_SlowMailbox):
    def wait_for_code(self, *args, **kwargs):
        self.timeouts.append(kwargs.get("timeout"))
        self.poll_intervals.append(kwargs.get("poll_interval"))
        if len(self.timeouts) in {1, 3}:
            raise TimeoutError("mailbox phase expired")
        return "123456" if len(self.timeouts) == 2 else "654321"


class _RequeueMailbox(_TrackingMailbox):
    def __init__(self):
        super().__init__()
        self.requeued = []

    def requeue_account(self, account):
        self.requeued.append(account)


class _SequentialLoginMailbox:
    def __init__(self):
        self.claimed = []

    def get_email(self):
        index = len(self.claimed) + 1
        account = MailboxAccount(
            email=f"login-{index}@example.com",
            account_id=f"login-{index}",
            extra={"provider": "applemail"},
        )
        self.claimed.append(account)
        return account

    def get_current_ids(self, _account):
        return set()


class _MailboxOAuthRetryAdapter(RefreshTokenChatGPTRegistrationAdapter):
    def __init__(self, retry_error: str):
        self.retry_error = retry_error
        self.engine_runs = 0

    def _create_engine(self, context):
        adapter = self

        class _Engine:
            def run(self):
                adapter.engine_runs += 1
                email_info = context.email_service.create_email()
                email = str(email_info.get("email") or "")
                if adapter.engine_runs == 1:
                    return SimpleNamespace(
                        success=False,
                        error_message=adapter.retry_error,
                    )
                return SimpleNamespace(
                    success=True,
                    email=email,
                    password="chatgpt-password",
                    account_id="chatgpt-account",
                    access_token="access-token",
                    refresh_token="",
                    id_token="",
                    session_token="",
                    workspace_id="",
                    source="login",
                    metadata={},
                )

        return _Engine()


class _CredentialMailbox(_TrackingMailbox):
    def __init__(self):
        super().__init__()
        self.account.extra = {
            "provider": "microsoft",
            "password": "mail-password",
            "client_id": "mail-client",
            "refresh_token": "mail-refresh",
            "account_type": "microsoft_oauth",
        }


class _FailingCommitMailbox(_CredentialMailbox):
    def __init__(self):
        super().__init__()
        self.requeued = []

    def mark_account_used(self, _account):
        raise OSError("disk unavailable")

    def requeue_account(self, account):
        self.requeued.append(account)


class _FakeAdapter:
    def run(self, context):
        context.email_service.create_email()
        raise AssertionError("create_email 应该先报错")


class _VerificationAdapter:
    def __init__(self):
        self.run_called = False

    def run(self, context):
        self.run_called = True
        self.extra_config = dict(context.extra_config)
        context.email_service.create_email()
        self.mailbox_metadata = context.email_service.get_mailbox_metadata()
        code = context.email_service.get_verification_code(
            timeout=30,
            otp_sent_at=123.0,
            exclude_codes={"654321"},
        )
        self.last_code = code
        return mock.Mock(success=True)

    def build_account(self, result, fallback_password):
        return {"success": True, "password": fallback_password}


class _RepeatedVerificationAdapter(_VerificationAdapter):
    def run(self, context):
        self.run_called = True
        self.extra_config = dict(context.extra_config)
        context.email_service.create_email()
        self.codes = [
            context.email_service.get_verification_code(timeout=30),
            context.email_service.get_verification_code(timeout=30),
        ]
        return mock.Mock(success=True)


class _AsyncBaselineAdapter(_VerificationAdapter):
    def __init__(self, mailbox):
        super().__init__()
        self.mailbox = mailbox
        self.create_returned_before_baseline = False

    def run(self, context):
        release_timer = threading.Timer(0.2, self.mailbox.baseline_release.set)
        release_timer.start()
        try:
            context.email_service.create_email()
            self.mailbox.baseline_started.wait(timeout=1)
            self.create_returned_before_baseline = not self.mailbox.baseline_finished.is_set()
            self.mailbox.baseline_release.set()
            self.last_code = context.email_service.get_verification_code(timeout=30)
        finally:
            release_timer.cancel()
        return mock.Mock(success=True)


class _FailingAdapter:
    def run(self, context):
        context.email_service.create_email()
        return mock.Mock(success=False, error_message="boom")


class _SuccessfulAccountAdapter:
    def run(self, context):
        context.email_service.create_email()
        return mock.Mock(success=True)

    def build_account(self, result, fallback_password):
        return Account(
            platform="chatgpt",
            email="demo@example.com",
            password=fallback_password,
            token="access-token",
            extra={"access_token": "access-token", "refresh_token": ""},
        )


class _SuccessfulRefreshTokenAccountAdapter(_SuccessfulAccountAdapter):
    def build_account(self, result, fallback_password):
        account = super().build_account(result, fallback_password)
        account.extra["refresh_token"] = "refresh-token"
        return account


class ChatGPTPluginTests(unittest.TestCase):
    def test_existing_login_reuses_first_mailbox_when_adapter_rebuilds_engine(self):
        for retry_error in ("service_abuse_mode", "oauth_token_failed"):
            with self.subTest(retry_error=retry_error):
                mailbox = _SequentialLoginMailbox()
                adapter = _MailboxOAuthRetryAdapter(retry_error)
                platform = ChatGPTPlatform(
                    config=RegisterConfig(
                        extra={
                            "chatgpt_registration_mode": "refresh_token",
                            "chatgpt_existing_account_login_only": True,
                        }
                    ),
                    mailbox=mailbox,
                )

                with mock.patch(
                    "platforms.chatgpt.plugin.build_chatgpt_registration_mode_adapter",
                    return_value=adapter,
                ):
                    account = platform.register()

                self.assertEqual(adapter.engine_runs, 2)
                self.assertEqual(
                    [item.email for item in mailbox.claimed],
                    ["login-1@example.com"],
                )
                self.assertEqual(account.email, "login-1@example.com")

    def test_actions_include_parameterless_codex2api_upload(self):
        platform = ChatGPTPlatform(config=RegisterConfig(extra={}))

        action = next(
            item
            for item in platform.get_platform_actions()
            if item["id"] == "upload_codex2api"
        )

        self.assertEqual(action["label"], "上传 Codex2API")
        self.assertEqual(action["params"], [])

    def test_codex2api_action_ignores_request_target_overrides(self):
        platform = ChatGPTPlatform(config=RegisterConfig(extra={}))
        account = SimpleNamespace(
            email="demo@example.com",
            token="at-local",
            extra={"refresh_token": "rt-local"},
            user_id="account-id",
        )

        with mock.patch(
            "platforms.chatgpt.codex2api_upload.upload_to_codex2api",
            return_value=(True, "uploaded"),
        ) as upload_mock:
            result = platform.execute_action(
                "upload_codex2api",
                account,
                {
                    "api_url": "http://forbidden.example",
                    "api_key": "forbidden-key",
                },
            )

        self.assertEqual(result, {"ok": True, "data": "uploaded"})
        upload_mock.assert_called_once()
        uploaded_account = upload_mock.call_args.args[0]
        self.assertEqual(uploaded_account.email, account.email)
        self.assertEqual(uploaded_account.refresh_token, "rt-local")
        self.assertEqual(uploaded_account.access_token, "at-local")

    def test_codex2api_action_supports_legacy_camel_case_tokens(self):
        platform = ChatGPTPlatform(config=RegisterConfig(extra={}))
        account = SimpleNamespace(
            email="demo@example.com",
            token="",
            extra={
                "refreshToken": "rt-camel",
                "accessToken": "at-camel",
            },
            user_id="account-id",
        )

        with mock.patch(
            "platforms.chatgpt.codex2api_upload.upload_to_codex2api",
            return_value=(True, "uploaded"),
        ) as upload_mock:
            result = platform.execute_action("upload_codex2api", account, {})

        self.assertEqual(result, {"ok": True, "data": "uploaded"})
        uploaded_account = upload_mock.call_args.args[0]
        self.assertEqual(uploaded_account.refresh_token, "rt-camel")
        self.assertEqual(uploaded_account.access_token, "at-camel")

    def test_codex2api_action_ignores_blank_snake_case_tokens(self):
        platform = ChatGPTPlatform(config=RegisterConfig(extra={}))
        account = SimpleNamespace(
            email="demo@example.com",
            token="",
            extra={
                "refresh_token": "   ",
                "refreshToken": "rt-camel",
                "access_token": "\t",
                "accessToken": "at-camel",
            },
            user_id="account-id",
        )

        with mock.patch(
            "platforms.chatgpt.codex2api_upload.upload_to_codex2api",
            return_value=(True, "uploaded"),
        ) as upload_mock:
            result = platform.execute_action("upload_codex2api", account, {})

        self.assertEqual(result, {"ok": True, "data": "uploaded"})
        uploaded_account = upload_mock.call_args.args[0]
        self.assertEqual(uploaded_account.refresh_token, "rt-camel")
        self.assertEqual(uploaded_account.access_token, "at-camel")

    def test_custom_provider_rejects_blank_email(self):
        platform = ChatGPTPlatform(
            config=RegisterConfig(extra={"chatgpt_registration_mode": "refresh_token"}),
            mailbox=_BlankMailbox(),
        )

        with mock.patch(
            "platforms.chatgpt.plugin.build_chatgpt_registration_mode_adapter",
            return_value=_FakeAdapter(),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                platform.register()

        self.assertIn("custom_provider 返回空邮箱地址", str(ctx.exception))

    def test_custom_provider_uses_mailbox_baseline_for_verification_code(self):
        mailbox = _TrackingMailbox()
        platform = ChatGPTPlatform(
            config=RegisterConfig(extra={"chatgpt_registration_mode": "refresh_token"}),
            mailbox=mailbox,
        )
        adapter = _VerificationAdapter()

        with mock.patch(
            "platforms.chatgpt.plugin.build_chatgpt_registration_mode_adapter",
            return_value=adapter,
        ):
            result = platform.register()

        self.assertTrue(adapter.run_called)
        self.assertEqual(adapter.last_code, "123456")
        self.assertEqual(result["success"], True)
        self.assertEqual(mailbox.current_ids_calls, [mailbox.account])
        self.assertEqual(adapter.mailbox_metadata["provider"], "custom_provider")
        self.assertEqual(adapter.mailbox_metadata["email"], "demo@example.com")
        self.assertEqual(adapter.mailbox_metadata["account_id"], "tracked-mailbox")
        self.assertIsNotNone(mailbox.wait_call)
        _, kwargs = mailbox.wait_call
        self.assertEqual(kwargs.get("before_ids"), {"mid-1"})
        self.assertEqual(kwargs.get("otp_sent_at"), 123.0)
        self.assertEqual(kwargs.get("exclude_codes"), {"654321"})

    def test_custom_provider_loads_mailbox_baseline_in_parallel_with_oauth(self):
        mailbox = _DelayedBaselineMailbox()
        platform = ChatGPTPlatform(
            config=RegisterConfig(extra={"chatgpt_registration_mode": "refresh_token"}),
            mailbox=mailbox,
        )
        adapter = _AsyncBaselineAdapter(mailbox)

        with mock.patch(
            "platforms.chatgpt.plugin.build_chatgpt_registration_mode_adapter",
            return_value=adapter,
        ):
            result = platform.register()

        self.assertTrue(adapter.create_returned_before_baseline)
        self.assertEqual(adapter.last_code, "123456")
        self.assertEqual(result["success"], True)
        _, kwargs = mailbox.wait_call
        self.assertEqual(kwargs.get("before_ids"), {"mid-1"})

    def test_custom_provider_records_mailbox_binding_before_login_continues(self):
        mailbox = _TrackingMailbox()
        binding_callback = mock.Mock()
        platform = ChatGPTPlatform(
            config=RegisterConfig(
                extra={
                    "chatgpt_registration_mode": "refresh_token",
                    "_chatgpt_attempt_binding_callback": binding_callback,
                }
            ),
            mailbox=mailbox,
        )
        adapter = _VerificationAdapter()

        with mock.patch(
            "platforms.chatgpt.plugin.build_chatgpt_registration_mode_adapter",
            return_value=adapter,
        ):
            platform.register()

        binding_callback.assert_called_once_with(
            "demo@example.com",
            mailbox.account,
        )

    def test_custom_provider_prefers_configured_mailbox_timeout(self):
        mailbox = _TrackingMailbox()
        platform = ChatGPTPlatform(
            config=RegisterConfig(
                extra={
                    "chatgpt_registration_mode": "refresh_token",
                    "mailbox_otp_timeout_seconds": 90,
                }
            ),
            mailbox=mailbox,
        )
        adapter = _VerificationAdapter()

        with mock.patch(
            "platforms.chatgpt.plugin.build_chatgpt_registration_mode_adapter",
            return_value=adapter,
        ):
            platform.register()

        _, kwargs = mailbox.wait_call
        self.assertEqual(kwargs.get("timeout"), 20)

    def test_slow_mailbox_moves_to_background_after_foreground_window(self):
        mailbox = _SlowMailbox()
        logs = []
        platform = ChatGPTPlatform(
            config=RegisterConfig(
                extra={"chatgpt_registration_mode": "refresh_token"}
            ),
            mailbox=mailbox,
        )
        platform._log_fn = logs.append
        adapter = _VerificationAdapter()

        with mock.patch(
            "platforms.chatgpt.plugin.build_chatgpt_registration_mode_adapter",
            return_value=adapter,
        ):
            result = platform.register()

        self.assertEqual(result["success"], True)
        self.assertEqual(adapter.last_code, "123456")
        self.assertEqual(mailbox.timeouts, [20, 160])
        self.assertEqual(mailbox.poll_intervals, [3, 10])
        self.assertEqual(mailbox.background_entries, 1)
        self.assertEqual(
            adapter.extra_config["mailbox_otp_timeout_seconds"],
            180,
        )
        self.assertEqual(
            adapter.extra_config["chatgpt_oauth_otp_wait_seconds"],
            180,
        )
        self.assertTrue(any("后台等待" in line for line in logs))

    def test_repeated_otp_reads_share_one_total_wait_budget(self):
        mailbox = _RepeatedSlowMailbox()
        platform = ChatGPTPlatform(
            config=RegisterConfig(
                extra={
                    "chatgpt_registration_mode": "refresh_token",
                    "mailbox_otp_timeout_seconds": 60,
                }
            ),
            mailbox=mailbox,
        )
        adapter = _RepeatedVerificationAdapter()

        with mock.patch(
            "platforms.chatgpt.plugin.build_chatgpt_registration_mode_adapter",
            return_value=adapter,
        ):
            platform.register()

        self.assertEqual(adapter.codes, ["123456", "654321"])
        self.assertEqual(mailbox.timeouts, [20, 40, 20, 20])

    def test_custom_provider_does_not_requeue_mailbox_account_on_failure(self):
        mailbox = _RequeueMailbox()
        platform = ChatGPTPlatform(
            config=RegisterConfig(extra={"chatgpt_registration_mode": "refresh_token"}),
            mailbox=mailbox,
        )

        with mock.patch(
            "platforms.chatgpt.plugin.build_chatgpt_registration_mode_adapter",
            return_value=_FailingAdapter(),
        ):
            with self.assertRaises(RuntimeError):
                platform.register()

        self.assertEqual(mailbox.requeued, [])

    def test_existing_account_login_requeues_mailbox_account_on_failure(self):
        mailbox = _RequeueMailbox()
        platform = ChatGPTPlatform(
            config=RegisterConfig(
                extra={
                    "chatgpt_registration_mode": "refresh_token",
                    "chatgpt_existing_account_login_only": True,
                }
            ),
            mailbox=mailbox,
        )

        with mock.patch(
            "platforms.chatgpt.plugin.build_chatgpt_registration_mode_adapter",
            return_value=_FailingAdapter(),
        ):
            with self.assertRaises(RuntimeError):
                platform.register()

        self.assertEqual(mailbox.requeued, [mailbox.account])

    def test_failed_applemail_existing_login_is_returned_to_pool(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            save_applemail_pool_json(
                "retry@icloud.com----chatgpt-password----JBSWY3DPEHPK3PXP",
                pool_dir=tmp_dir,
                filename="retry.json",
            )
            mailbox = AppleMailMailbox(
                pool_dir=tmp_dir,
                pool_file="retry.json",
            )
            platform = ChatGPTPlatform(
                config=RegisterConfig(
                    extra={
                        "chatgpt_registration_mode": "refresh_token",
                        "chatgpt_existing_account_login_only": True,
                    }
                ),
                mailbox=mailbox,
            )

            with mock.patch(
                "platforms.chatgpt.plugin.build_chatgpt_registration_mode_adapter",
                return_value=_FailingAdapter(),
            ):
                with self.assertRaisesRegex(RuntimeError, "boom"):
                    platform.register()

            snapshot = load_applemail_pool_snapshot(
                pool_dir=tmp_dir,
                pool_file="retry.json",
            )
            self.assertEqual(snapshot["count"], 1)
            self.assertEqual(snapshot["items"][0]["email"], "retry@icloud.com")

    def test_totp_only_account_without_secret_is_not_requeued(self):
        mailbox = _RequeueMailbox()
        mailbox.account.extra = {
            "provider": "chatgpt_credentials",
            "account_type": "chatgpt_password_totp",
            "password": "chatgpt-password",
            "totp_secret": "",
        }
        platform = ChatGPTPlatform(
            config=RegisterConfig(
                extra={
                    "chatgpt_registration_mode": "refresh_token",
                    "chatgpt_existing_account_login_only": True,
                }
            ),
            mailbox=mailbox,
        )

        with self.assertRaisesRegex(RuntimeError, "缺少密码或 MFA 秘钥"):
            platform.register()

        self.assertEqual(mailbox.requeued, [])

    def test_existing_mailapi_login_keeps_password_blank_for_email_otp(self):
        mailbox = _TrackingMailbox()
        mailbox.account.extra = {
            "provider": "microsoft",
            "account_type": "mailapi_url",
            "mailapi_url": "https://mail.example.test/messages",
        }
        platform = ChatGPTPlatform(
            config=RegisterConfig(
                extra={
                    "chatgpt_registration_mode": "refresh_token",
                    "chatgpt_existing_account_login_only": True,
                }
            ),
            mailbox=mailbox,
        )
        adapter = mock.Mock()

        def run(context):
            adapter.context = context
            context.email_service.create_email()
            return mock.Mock(success=True)

        adapter.run.side_effect = run
        adapter.build_account.return_value = Account(
            platform="chatgpt",
            email="demo@example.com",
            password="",
            token="access-token",
            extra={"access_token": "access-token", "refresh_token": ""},
        )

        with mock.patch(
            "platforms.chatgpt.plugin.build_chatgpt_registration_mode_adapter",
            return_value=adapter,
        ):
            platform.register()

        self.assertEqual(adapter.context.password, "")
        self.assertEqual(adapter.build_account.call_args.args[1], "")

    def test_successful_chatgpt_flow_persists_consumed_mailbox_credentials(self):
        mailbox = _CredentialMailbox()
        platform = ChatGPTPlatform(
            config=RegisterConfig(extra={"chatgpt_registration_mode": "refresh_token"}),
            mailbox=mailbox,
        )

        with mock.patch(
            "platforms.chatgpt.plugin.build_chatgpt_registration_mode_adapter",
            return_value=_SuccessfulAccountAdapter(),
        ):
            account = platform.register()

        context = account.extra["mailbox_login_context"]
        self.assertEqual(context["provider"], "microsoft")
        self.assertEqual(context["email"], "demo@example.com")
        self.assertEqual(context["extra"]["client_id"], "mail-client")
        self.assertEqual(context["extra"]["refresh_token"], "mail-refresh")

    def test_rt_success_stays_successful_and_claimed_when_pool_commit_io_fails(self):
        mailbox = _FailingCommitMailbox()
        logs = []
        platform = ChatGPTPlatform(
            config=RegisterConfig(
                extra={
                    "chatgpt_registration_mode": "refresh_token",
                    "chatgpt_existing_account_login_only": True,
                }
            ),
            mailbox=mailbox,
        )
        platform._log_fn = logs.append

        with mock.patch(
            "platforms.chatgpt.plugin.build_chatgpt_registration_mode_adapter",
            return_value=_SuccessfulRefreshTokenAccountAdapter(),
        ):
            account = platform.register()

        self.assertEqual(account.extra["refresh_token"], "refresh-token")
        self.assertEqual(mailbox.requeued, [])
        self.assertIn("邮箱池消费状态保存失败", "\n".join(logs))

    def test_three_field_chatgpt_credentials_reach_engine_without_apple_login(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            save_applemail_pool_json(
                "mfa-user@icloud.com----chatgpt-password----JBSWY3DPEHPK3PXP",
                pool_dir=tmp_dir,
                filename="chatgpt_mfa.json",
            )
            mailbox = AppleMailMailbox(
                pool_dir=tmp_dir,
                pool_file="chatgpt_mfa.json",
            )
            mailbox.get_current_ids = mock.Mock(
                side_effect=AssertionError(
                    "ChatGPT password + TOTP credentials must not read Apple mail"
                )
            )
            platform = ChatGPTPlatform(
                config=RegisterConfig(
                    extra={
                        "chatgpt_registration_mode": "refresh_token",
                        "chatgpt_existing_account_login_only": True,
                    }
                ),
                mailbox=mailbox,
            )
            oauth_client = mock.Mock()
            oauth_client.config = {}
            oauth_client.login_and_get_tokens.return_value = {
                "access_token": "access-token",
                "refresh_token": "refresh-token",
                "id_token": "",
                "account_id": "account-1",
            }
            oauth_client.last_workspace_id = "workspace-1"
            oauth_client._get_cookie_value.return_value = "session-token"

            with mock.patch(
                "core.icloud_mail.ICloudMailClient",
            ) as icloud_client_class, mock.patch(
                "platforms.chatgpt.refresh_token_registration_engine."
                "RefreshTokenRegistrationEngine._build_oauth_client",
                return_value=oauth_client,
            ):
                account = platform.register()

            login_call = oauth_client.login_and_get_tokens.call_args
            self.assertEqual(login_call.args[0], "mfa-user@icloud.com")
            self.assertEqual(login_call.args[1], "chatgpt-password")
            self.assertEqual(login_call.kwargs["totp_secret"], "JBSWY3DPEHPK3PXP")
            self.assertTrue(login_call.kwargs["force_password_login"])
            self.assertFalse(login_call.kwargs["prefer_passwordless_login"])
            mailbox.get_current_ids.assert_not_called()
            icloud_client_class.assert_not_called()
            self.assertEqual(account.email, "mfa-user@icloud.com")
            self.assertEqual(account.password, "chatgpt-password")
            self.assertEqual(account.extra["refresh_token"], "refresh-token")
            snapshot = load_applemail_pool_snapshot(
                pool_dir=tmp_dir,
                pool_file="chatgpt_mfa.json",
            )
            self.assertEqual(snapshot["count"], 0)
            _path, records = load_applemail_pool_records(
                pool_dir=tmp_dir,
                pool_file="chatgpt_mfa.json",
            )
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["pool_state"], "used")
            context_extra = account.extra["mailbox_login_context"]["extra"]
            self.assertEqual(context_extra["account_type"], "chatgpt_password_totp")
            self.assertEqual(context_extra["password"], "chatgpt-password")
            self.assertEqual(context_extra["totp_secret"], "JBSWY3DPEHPK3PXP")
            self.assertEqual(
                Path(tmp_dir, "chatgpt_mfa.json").stat().st_mode & 0o777,
                0o600,
            )

    def test_password_reset_flow_persists_self_contained_login_credentials(self):
        class ResetAdapter:
            def run(self, context):
                email_info = context.email_service.create_email()
                self.new_password = "Reset-Password-2026"
                self.assert_reset = email_info["password_reset_required"]
                context.email_service.commit_password_reset(self.new_password)
                return mock.Mock(success=True)

            def build_account(self, result, fallback_password):
                del result, fallback_password
                return Account(
                    platform="chatgpt",
                    email="reset-user@icloud.com",
                    password=self.new_password,
                    token="access-token",
                    extra={
                        "access_token": "access-token",
                        "refresh_token": "refresh-token",
                    },
                )

        with tempfile.TemporaryDirectory() as tmp_dir:
            save_applemail_pool_json(
                "reset-user@icloud.com----登陆请点击忘记密码----"
                "https://mail.example.test/mail?token=MAIL_SECRET",
                pool_dir=tmp_dir,
                filename="reset-user.json",
            )
            mailbox = AppleMailMailbox(
                pool_dir=tmp_dir,
                pool_file="reset-user.json",
            )
            adapter = ResetAdapter()
            platform = ChatGPTPlatform(
                config=RegisterConfig(
                    extra={
                        "chatgpt_registration_mode": "refresh_token",
                        "chatgpt_existing_account_login_only": True,
                    }
                ),
                mailbox=mailbox,
            )

            with mock.patch(
                "platforms.chatgpt.plugin.build_chatgpt_registration_mode_adapter",
                return_value=adapter,
            ):
                account = platform.register()

            self.assertTrue(adapter.assert_reset)
            context_extra = account.extra["mailbox_login_context"]["extra"]
            self.assertEqual(
                context_extra["account_type"],
                "chatgpt_password_reset_url_mail",
            )
            self.assertEqual(context_extra["password"], "Reset-Password-2026")
            self.assertIn("MAIL_SECRET", context_extra["mail_api_url"])
            self.assertFalse(context_extra["password_reset_required"])
            self.assertNotIn("new_password", context_extra)
            self.assertNotIn("_pool_claim_id", context_extra)



if __name__ == "__main__":
    unittest.main()
