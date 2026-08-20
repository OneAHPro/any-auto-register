import types
import unittest
from unittest import mock

from platforms.chatgpt.refresh_token_registration_engine import (
    RefreshTokenRegistrationEngine,
)
from platforms.chatgpt.chatgpt_client import ChatGPTClient
from platforms.chatgpt.mfa_manager import MfaRotationResult
from platforms.chatgpt.oauth_client import OAuthClient
from platforms.chatgpt.utils import FlowState


class DummyEmailService:
    service_type = type("ServiceType", (), {"value": "microsoft"})()

    def create_email(self):
        return {"email": "existing@example.com", "service_id": "mailbox-1"}

    def get_verification_code(self, **kwargs):
        return "123456"

    def get_mailbox_metadata(self):
        return {
            "provider": "microsoft",
            "email": "existing@example.com",
            "account_id": "mailbox-1",
            "extra": {
                "password": "mail-password",
                "client_id": "mail-client",
                "refresh_token": "mail-refresh",
                "account_type": "microsoft_oauth",
            },
        }


class PasswordTotpEmailService:
    service_type = type("ServiceType", (), {"value": "chatgpt_credentials"})()

    def create_email(self):
        return {
            "email": "mfa-user@icloud.com",
            "service_id": "mfa-user@icloud.com",
            "account_type": "chatgpt_password_totp",
            "password": "chatgpt-password",
            "totp_secret": "JBSWY3DPEHPK3PXP",
            "mfa_recovery_code": "RECOVERY-CODE",
        }

    def get_verification_code(self, **kwargs):
        raise AssertionError("ChatGPT password + TOTP login must not read Apple mail")

    def get_mailbox_metadata(self):
        return {
            "provider": "chatgpt_credentials",
            "email": "mfa-user@icloud.com",
            "account_id": "mfa-user@icloud.com",
            "extra": {"account_type": "chatgpt_password_totp"},
        }


class GoogleFederatedEmailService:
    service_type = type("ServiceType", (), {"value": "chatgpt_credentials"})()

    def create_email(self):
        return {
            "email": "worker@custom-google-domain.example",
            "service_id": "worker@custom-google-domain.example",
            "account_type": "chatgpt_google_password",
            "password": "supplier-password",
        }

    def get_verification_code(self, **kwargs):
        raise AssertionError("Google 联邦登录不应读取邮箱验证码")

    def supports_email_verification(self):
        return False

    def get_mailbox_metadata(self):
        return {
            "provider": "chatgpt_credentials",
            "email": "worker@custom-google-domain.example",
            "account_id": "worker@custom-google-domain.example",
            "extra": {
                "account_type": "chatgpt_google_password",
                "password": "supplier-password",
            },
        }

class PasswordTotpWithMailEmailService(PasswordTotpEmailService):
    def __init__(self):
        self.committed_password = ""

    def create_email(self):
        result = super().create_email()
        result["mail_api_url"] = (
            "https://mail.example.test/history?email=mfa-user%40icloud.com"
        )
        return result

    def get_verification_code(self, **kwargs):
        return "123456"

    def commit_password_reset(self, new_password):
        self.committed_password = str(new_password or "")
        return True


class UrlOtpEmailService:
    service_type = type("ServiceType", (), {"value": "chatgpt_credentials"})()

    def __init__(self):
        self.committed_password = ""

    def create_email(self):
        return {
            "email": "url-user@icloud.com",
            "service_id": "url-user@icloud.com",
            "account_type": "chatgpt_password_url_otp",
            "password": "chatgpt-password",
            "mail_api_url": "https://mail.example.test/mail?token=secret",
            "totp_url": "https://2fa.example.test/view?token=secret",
        }

    def get_verification_code(self, **kwargs):
        return "123456"

    def get_totp_code(self):
        return "654321"

    def commit_password_reset(self, new_password):
        self.committed_password = str(new_password or "")
        return True


class PasswordResetUrlEmailService:
    service_type = type("ServiceType", (), {"value": "chatgpt_credentials"})()

    def __init__(self):
        self.committed_password = ""

    def create_email(self):
        return {
            "email": "reset-user@icloud.com",
            "service_id": "reset-user@icloud.com",
            "account_type": "chatgpt_password_reset_url_mail",
            "password": "",
            "mail_api_url": "https://mail.example.test/mail?token=secret",
            "password_reset_required": True,
            "new_password": "Fresh-Password-123!",
        }

    def get_verification_code(self, **kwargs):
        return "123456"

    def commit_password_reset(self, new_password):
        self.committed_password = new_password
        return True


class MailApiOnlyEmailService:
    service_type = type("ServiceType", (), {"value": "microsoft"})()

    def __init__(self):
        self.committed_password = ""

    def create_email(self):
        return {
            "email": "mailapi-only@icloud.com",
            "service_id": "mailapi-only@icloud.com",
            "account_type": "mailapi_url",
            "password": "",
            "mail_api_url": "https://mail.example.test/messages?token=secret",
        }

    def get_verification_code(self, **kwargs):
        return "123456"

    def commit_password_reset(self, new_password):
        self.committed_password = str(new_password or "")
        return True


class ManagedMfaMailApiOnlyEmailService(MailApiOnlyEmailService):
    def create_email(self):
        result = super().create_email()
        # Legacy persisted mailbox adapters expose the saved TOTP, but older
        # rows may omit the explicit chatgpt_mfa_managed marker.
        result["totp_secret"] = "JBSWY3DPEHPK3PXP"
        return result


class ExistingAccountLoginTests(unittest.TestCase):
    def _make_engine(
        self,
        *,
        login_only=True,
        allow_phone_verification=False,
        login_stage="refresh_token",
        email_service=None,
        rotate_mfa=False,
    ):
        return RefreshTokenRegistrationEngine(
            email_service=email_service or DummyEmailService(),
            proxy_url="http://127.0.0.1:7890",
            callback_logger=lambda message: None,
            max_retries=1,
            extra_config={
                "chatgpt_existing_account_login_only": login_only,
                "chatgpt_existing_account_allow_phone_verification": (
                    allow_phone_verification
                ),
                "chatgpt_existing_account_login_stage": login_stage,
                "chatgpt_existing_account_rotate_mfa": rotate_mfa,
            },
        )

    def _successful_oauth_client(self):
        client = mock.Mock()
        client.login_and_get_tokens.return_value = {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "id_token": "id-token",
        }
        client.last_error = ""
        client.last_workspace_id = "workspace-1"
        client._get_cookie_value.return_value = "session-token"
        return client

    def test_login_only_skips_registration_and_saves_both_tokens(self):
        engine = self._make_engine()
        oauth_client = self._successful_oauth_client()
        engine._build_oauth_client = mock.Mock(return_value=oauth_client)
        engine._build_chatgpt_client = mock.Mock()
        engine._extract_account_info = mock.Mock(
            return_value={"email": "existing@example.com", "account_id": "account-1"}
        )

        result = engine.run()

        self.assertTrue(result.success)
        self.assertEqual(result.access_token, "access-token")
        self.assertEqual(result.refresh_token, "refresh-token")
        self.assertEqual(result.source, "login")
        engine._build_chatgpt_client.assert_not_called()
        login_kwargs = oauth_client.login_and_get_tokens.call_args.kwargs
        self.assertEqual(login_kwargs["screen_hint"], "login")
        self.assertTrue(login_kwargs["prefer_passwordless_login"])
        self.assertFalse(login_kwargs["complete_about_you_if_needed"])
        self.assertEqual(login_kwargs["login_source"], "existing_account_login_only")
        self.assertTrue(any("加载邮箱凭据" in line for line in result.logs))
        self.assertFalse(any("成功创建邮箱" in line for line in result.logs))

    def test_refresh_token_stage_rotates_mfa_with_fresh_web_session_before_oauth(self):
        engine = self._make_engine(
            email_service=PasswordTotpEmailService(),
            rotate_mfa=True,
        )
        web_client = mock.Mock()
        web_client.session = object()
        web_client.ua = "web-agent"
        web_client.impersonate = "chrome"
        web_client.login_existing_account_and_get_session.return_value = (
            True,
            {
                "access_token": "web-access-token",
                "account_id": "account-1",
            },
        )
        oauth_client = self._successful_oauth_client()
        engine._build_chatgpt_client = mock.Mock(return_value=web_client)
        engine._build_oauth_client = mock.Mock(return_value=oauth_client)
        rotation = MfaRotationResult(
            totp_secret="NEWSECRET",
            recovery_code="RECOVERY",
            replaced_existing=True,
            mfa_enabled=True,
            rotated_at="2026-08-19T12:00:00+00:00",
        )
        engine._rotate_mfa_after_login = mock.Mock(return_value=rotation)
        engine._extract_account_info = mock.Mock(
            return_value={"email": "existing@example.com", "account_id": "account-1"}
        )

        result = engine.run()

        self.assertTrue(result.success)
        web_call = web_client.login_existing_account_and_get_session.call_args
        self.assertFalse(web_call.kwargs["prepare_phone_oauth"])
        self.assertEqual(
            web_call.kwargs["mfa_recovery_code"],
            "RECOVERY-CODE",
        )
        rotate_call = engine._rotate_mfa_after_login.call_args.kwargs
        self.assertIs(rotate_call["session"], web_client.session)
        self.assertEqual(rotate_call["access_token"], "web-access-token")
        oauth_client.login_and_get_tokens.assert_called_once()
        oauth_client.adopt_browser_context.assert_called_once_with(
            web_client.session,
            device_id=web_client.device_id,
            user_agent=web_client.ua,
            sec_ch_ua=web_client.sec_ch_ua,
            accept_language="",
        )
        oauth_kwargs = oauth_client.login_and_get_tokens.call_args.kwargs
        self.assertFalse(oauth_kwargs["force_new_browser"])
        self.assertTrue(oauth_kwargs["resume_authenticated_session"])

    def test_recovery_mfa_enrollment_is_saved_and_skips_second_rotation(self):
        email_service = PasswordTotpEmailService()
        email_service.commit_mfa_rotation = mock.Mock(return_value=True)
        engine = self._make_engine(
            email_service=email_service,
            rotate_mfa=True,
        )
        web_client = mock.Mock()
        web_client.session = object()
        web_client.ua = "web-agent"
        web_client.impersonate = "chrome"
        web_client.login_existing_account_and_get_session.return_value = (
            True,
            {
                "access_token": "web-access-token",
                "account_id": "account-1",
                "mfa_enrollment": {
                    "totp_secret": "MANDATORY-NEW-SECRET",
                    "recovery_code": "MANDATORY-RECOVERY",
                    "rotated_at": "2026-08-19T12:00:00+00:00",
                },
            },
        )
        oauth_client = self._successful_oauth_client()
        engine._build_chatgpt_client = mock.Mock(return_value=web_client)
        engine._build_oauth_client = mock.Mock(return_value=oauth_client)
        engine._rotate_mfa_after_login = mock.Mock()
        engine._extract_account_info = mock.Mock(
            return_value={"email": "existing@example.com", "account_id": "account-1"}
        )

        result = engine.run()

        self.assertTrue(result.success)
        engine._rotate_mfa_after_login.assert_not_called()
        email_service.commit_mfa_rotation.assert_called_once_with(
            totp_secret="MANDATORY-NEW-SECRET",
            recovery_code="MANDATORY-RECOVERY",
            rotated_at="2026-08-19T12:00:00+00:00",
        )
        oauth_kwargs = oauth_client.login_and_get_tokens.call_args.kwargs
        self.assertEqual(oauth_kwargs["totp_secret"], "MANDATORY-NEW-SECRET")
        self.assertEqual(
            oauth_kwargs["mfa_recovery_code"],
            "MANDATORY-RECOVERY",
        )

    def test_rotated_web_session_falls_back_to_full_oauth_when_resume_is_rejected(self):
        engine = self._make_engine(
            email_service=PasswordTotpEmailService(),
            rotate_mfa=True,
        )
        web_client = mock.Mock()
        web_client.session = object()
        web_client.ua = "web-agent"
        web_client.sec_ch_ua = '"Chromium";v="136"'
        web_client.impersonate = "chrome"
        web_client.device_id = "web-device"
        web_client.login_existing_account_and_get_session.return_value = (
            True,
            {
                "access_token": "web-access-token",
                "account_id": "account-1",
            },
        )
        oauth_client = self._successful_oauth_client()
        successful_tokens = oauth_client.login_and_get_tokens.return_value
        oauth_client.login_and_get_tokens.side_effect = [None, successful_tokens]
        oauth_client.last_error = (
            "OpenAI 登录会话已失效，请先重新执行邮箱登录"
        )
        engine._build_chatgpt_client = mock.Mock(return_value=web_client)
        engine._build_oauth_client = mock.Mock(return_value=oauth_client)
        engine._rotate_mfa_after_login = mock.Mock(
            return_value=MfaRotationResult(
                totp_secret="NEWSECRET",
                recovery_code="RECOVERY",
                replaced_existing=True,
                mfa_enabled=True,
                rotated_at="2026-08-19T12:00:00+00:00",
            )
        )
        engine._extract_account_info = mock.Mock(
            return_value={"email": "existing@example.com", "account_id": "account-1"}
        )

        result = engine.run()

        self.assertTrue(result.success)
        self.assertEqual(oauth_client.login_and_get_tokens.call_count, 2)
        first_kwargs = oauth_client.login_and_get_tokens.call_args_list[0].kwargs
        retry_kwargs = oauth_client.login_and_get_tokens.call_args_list[1].kwargs
        self.assertTrue(first_kwargs["resume_authenticated_session"])
        self.assertFalse(retry_kwargs["resume_authenticated_session"])
        self.assertFalse(retry_kwargs["force_new_browser"])

    def test_recovery_only_enrollment_continues_with_regular_rotation(self):
        engine = self._make_engine(
            email_service=PasswordTotpEmailService(),
            rotate_mfa=True,
        )
        web_client = mock.Mock()
        web_client.session = object()
        web_client.ua = "web-agent"
        web_client.impersonate = "chrome"
        web_client.login_existing_account_and_get_session.return_value = (
            True,
            {
                "access_token": "web-access-token",
                "account_id": "account-1",
                "mfa_enrollment": {
                    "recovery_code": "REFRESHED-RECOVERY",
                },
            },
        )
        oauth_client = self._successful_oauth_client()
        engine._build_chatgpt_client = mock.Mock(return_value=web_client)
        engine._build_oauth_client = mock.Mock(return_value=oauth_client)
        rotation = MfaRotationResult(
            totp_secret="ROTATED-SECRET",
            recovery_code="ROTATED-RECOVERY",
            replaced_existing=True,
            mfa_enabled=True,
            rotated_at="2026-08-19T12:30:00+00:00",
        )
        engine._rotate_mfa_after_login = mock.Mock(return_value=rotation)
        engine._extract_account_info = mock.Mock(
            return_value={"email": "existing@example.com", "account_id": "account-1"}
        )

        result = engine.run()

        self.assertTrue(result.success)
        engine._rotate_mfa_after_login.assert_called_once()

    def test_password_totp_credentials_force_chatgpt_password_login(self):
        engine = self._make_engine(email_service=PasswordTotpEmailService())
        oauth_client = self._successful_oauth_client()
        engine._build_oauth_client = mock.Mock(return_value=oauth_client)
        engine._extract_account_info = mock.Mock(
            return_value={"email": "mfa-user@icloud.com", "account_id": "account-1"}
        )

        result = engine.run()

        self.assertTrue(result.success)
        self.assertEqual(result.password, "chatgpt-password")
        call = oauth_client.login_and_get_tokens.call_args
        self.assertEqual(call.args[1], "chatgpt-password")
        self.assertEqual(
            call.kwargs["totp_secret"],
            "JBSWY3DPEHPK3PXP",
        )
        self.assertEqual(
            call.kwargs["mfa_recovery_code"],
            "RECOVERY-CODE",
        )
        self.assertFalse(call.kwargs["prefer_passwordless_login"])
        self.assertTrue(call.kwargs["force_password_login"])

    def test_google_federated_credentials_force_password_login_without_totp(self):
        engine = self._make_engine(email_service=GoogleFederatedEmailService())
        oauth_client = self._successful_oauth_client()
        engine._build_oauth_client = mock.Mock(return_value=oauth_client)
        engine._extract_account_info = mock.Mock(
            return_value={
                "email": "worker@custom-google-domain.example",
                "account_id": "account-1",
            }
        )

        result = engine.run()

        self.assertTrue(result.success)
        self.assertEqual(result.password, "supplier-password")
        call = oauth_client.login_and_get_tokens.call_args
        self.assertEqual(call.args[1], "supplier-password")
        self.assertEqual(call.kwargs["totp_secret"], "")
        self.assertFalse(call.kwargs["prefer_passwordless_login"])
        self.assertTrue(call.kwargs["force_password_login"])

    def test_url_otp_credentials_force_password_login_and_expose_remote_totp(self):
        engine = self._make_engine(email_service=UrlOtpEmailService())
        oauth_client = self._successful_oauth_client()
        engine._build_oauth_client = mock.Mock(return_value=oauth_client)
        engine._extract_account_info = mock.Mock(
            return_value={"email": "url-user@icloud.com", "account_id": "account-1"}
        )

        result = engine.run()

        self.assertTrue(result.success)
        call = oauth_client.login_and_get_tokens.call_args
        self.assertEqual(call.args[1], "chatgpt-password")
        self.assertFalse(call.kwargs["prefer_passwordless_login"])
        self.assertTrue(call.kwargs["force_password_login"])
        self.assertFalse(call.kwargs["password_reset_required"])
        self.assertEqual(call.kwargs["skymail_client"].get_totp_code(), "654321")

    def test_url_otp_refresh_login_resets_explicitly_rejected_password_once(self):
        email_service = UrlOtpEmailService()
        engine = self._make_engine(email_service=email_service)
        rejected_client = mock.Mock()
        rejected_client.login_and_get_tokens.return_value = None
        rejected_client.last_error = (
            '密码验证失败: 401 - {"error":{"type":"invalid_request_error"}}'
        )
        reset_client = self._successful_oauth_client()
        successful_tokens = reset_client.login_and_get_tokens.return_value

        def finish_reset(*args, **kwargs):
            self.assertTrue(kwargs["password_reset_required"])
            self.assertTrue(kwargs["on_password_reset"](args[1]))
            return successful_tokens

        reset_client.login_and_get_tokens.side_effect = finish_reset
        engine._build_oauth_client = mock.Mock(
            side_effect=[rejected_client, reset_client]
        )
        engine._extract_account_info = mock.Mock(
            return_value={"email": "url-user@icloud.com", "account_id": "account-1"}
        )

        with mock.patch(
            "platforms.chatgpt.refresh_token_registration_engine.generate_random_password",
            return_value="Replacement-Password-2026!",
        ):
            result = engine.run()

        self.assertTrue(result.success)
        self.assertEqual(result.password, "Replacement-Password-2026!")
        self.assertEqual(
            email_service.committed_password,
            "Replacement-Password-2026!",
        )
        self.assertEqual(engine._build_oauth_client.call_count, 2)
        self.assertFalse(
            rejected_client.login_and_get_tokens.call_args.kwargs[
                "password_reset_required"
            ]
        )
        self.assertTrue(any("忘记密码" in line for line in result.logs))

    def test_url_otp_access_login_resets_explicitly_rejected_password_once(self):
        email_service = UrlOtpEmailService()
        engine = self._make_engine(
            email_service=email_service,
            login_stage="access_token",
        )
        rejected_client = mock.Mock()
        rejected_client.login_existing_account_and_get_session.return_value = (
            False,
            '密码验证失败: 401 - {"error":{"type":"invalid_request_error"}}',
        )
        reset_client = mock.Mock()

        def finish_reset(*_args, **kwargs):
            self.assertTrue(kwargs["password_reset_required"])
            self.assertTrue(kwargs["on_password_reset"](kwargs["password"]))
            return True, {
                "access_token": "access-token",
                "session_token": "session-token",
                "account_id": "account-1",
            }

        reset_client.login_existing_account_and_get_session.side_effect = finish_reset
        engine._build_chatgpt_client = mock.Mock(
            side_effect=[rejected_client, reset_client]
        )

        with mock.patch(
            "platforms.chatgpt.refresh_token_registration_engine.generate_random_password",
            return_value="Replacement-Password-2026!",
        ):
            result = engine.run()

        self.assertTrue(result.success)
        self.assertEqual(result.password, "Replacement-Password-2026!")
        self.assertEqual(
            email_service.committed_password,
            "Replacement-Password-2026!",
        )
        self.assertEqual(engine._build_chatgpt_client.call_count, 2)
        self.assertFalse(
            rejected_client.login_existing_account_and_get_session.call_args.kwargs[
                "password_reset_required"
            ]
        )

    def test_mailapi_only_login_409_resets_password_once_and_restarts(self):
        email_service = MailApiOnlyEmailService()
        engine = self._make_engine(email_service=email_service)
        rejected_client = mock.Mock()
        rejected_client.login_and_get_tokens.return_value = None
        rejected_client.last_error = (
            '触发 passwordless OTP 失败: 409 - '
            '{"error":{"message":"Your sign-in session is no longer valid. '
            'Please start over to continue.","code":"invalid_auth_step"}}'
        )
        reset_client = self._successful_oauth_client()
        successful_tokens = reset_client.login_and_get_tokens.return_value

        def finish_reset(*args, **kwargs):
            self.assertEqual(args[1], "Replacement-Password-2026!")
            self.assertTrue(kwargs["password_reset_required"])
            self.assertTrue(kwargs["force_password_login"])
            self.assertFalse(kwargs["prefer_passwordless_login"])
            self.assertTrue(kwargs["on_password_reset"](args[1]))
            return successful_tokens

        reset_client.login_and_get_tokens.side_effect = finish_reset
        engine._build_oauth_client = mock.Mock(
            side_effect=[rejected_client, reset_client]
        )
        engine._extract_account_info = mock.Mock(
            return_value={
                "email": "mailapi-only@icloud.com",
                "account_id": "account-1",
            }
        )

        with mock.patch(
            "platforms.chatgpt.refresh_token_registration_engine.generate_random_password",
            return_value="Replacement-Password-2026!",
        ):
            result = engine.run()

        self.assertTrue(result.success)
        self.assertEqual(result.password, "Replacement-Password-2026!")
        self.assertEqual(
            email_service.committed_password,
            "Replacement-Password-2026!",
        )
        self.assertEqual(engine._build_oauth_client.call_count, 2)
        self.assertTrue(
            any("补充 ChatGPT 密码" in line for line in result.logs)
        )

    def test_mfa_rotation_upgrades_mailapi_only_account_to_password_login(self):
        email_service = MailApiOnlyEmailService()
        engine = self._make_engine(
            email_service=email_service,
            rotate_mfa=True,
        )

        with mock.patch(
            "platforms.chatgpt.refresh_token_registration_engine.generate_random_password",
            return_value="Managed-Password-2026!",
        ):
            created = engine._create_email(existing_account_login_only=True)

        self.assertTrue(created)
        self.assertEqual(engine.password, "Managed-Password-2026!")
        self.assertTrue(engine.password_reset_required)
        self.assertEqual(
            engine.email_info["new_password"],
            "Managed-Password-2026!",
        )
        self.assertTrue(engine.email_info["password_reset_required"])
        self.assertTrue(
            any("仅依赖邮箱验证码" in line for line in engine.logs)
        )

    def test_mailapi_only_account_stays_passwordless_without_mfa_rotation(self):
        engine = self._make_engine(email_service=MailApiOnlyEmailService())

        created = engine._create_email(existing_account_login_only=True)

        self.assertTrue(created)
        self.assertFalse(engine.password)
        self.assertFalse(engine.password_reset_required)

    def test_automatic_relogin_upgrades_managed_mfa_account_to_password(self):
        engine = self._make_engine(
            email_service=ManagedMfaMailApiOnlyEmailService(),
        )

        with mock.patch(
            "platforms.chatgpt.refresh_token_registration_engine.generate_random_password",
            return_value="Managed-Password-2026!",
        ):
            created = engine._create_email(existing_account_login_only=True)

        self.assertTrue(created)
        self.assertEqual(engine.password, "Managed-Password-2026!")
        self.assertEqual(engine.totp_secret, "JBSWY3DPEHPK3PXP")
        self.assertTrue(engine.password_reset_required)

    def test_unconfirmed_password_reset_does_not_expose_generated_password(self):
        email_service = ManagedMfaMailApiOnlyEmailService()
        engine = self._make_engine(email_service=email_service)
        oauth_client = self._successful_oauth_client()
        engine._build_oauth_client = mock.Mock(return_value=oauth_client)
        engine._extract_account_info = mock.Mock(
            return_value={"email": "mailapi-only@icloud.com", "account_id": "account-1"}
        )

        with mock.patch(
            "platforms.chatgpt.refresh_token_registration_engine.generate_random_password",
            return_value="Unconfirmed-Password-2026!",
        ):
            result = engine.run()

        self.assertTrue(result.success)
        self.assertEqual(result.password, "")
        self.assertEqual(email_service.committed_password, "")
        self.assertTrue(engine.password_reset_required)

    def test_unconfirmed_password_reset_does_not_expose_password_in_access_token_stage(self):
        email_service = ManagedMfaMailApiOnlyEmailService()
        engine = self._make_engine(
            email_service=email_service,
            login_stage="access_token",
        )
        chatgpt_client = mock.Mock()
        chatgpt_client.login_existing_account_and_get_session.return_value = (
            True,
            {
                "access_token": "access-token",
                "session_token": "session-token",
                "account_id": "account-1",
            },
        )
        engine._build_chatgpt_client = mock.Mock(return_value=chatgpt_client)

        with mock.patch(
            "platforms.chatgpt.refresh_token_registration_engine.generate_random_password",
            return_value="Unconfirmed-Password-2026!",
        ):
            result = engine.run()

        self.assertTrue(result.success)
        self.assertEqual(result.password, "")
        self.assertEqual(email_service.committed_password, "")
        self.assertTrue(engine.password_reset_required)

    def test_totp_with_mail_access_login_resets_rejected_password_and_keeps_totp(self):
        email_service = PasswordTotpWithMailEmailService()
        engine = self._make_engine(
            email_service=email_service,
            login_stage="access_token",
        )
        rejected_client = mock.Mock()
        rejected_client.login_existing_account_and_get_session.return_value = (
            False,
            '密码验证失败: 401 - {"error":{"type":"invalid_request_error"}}',
        )
        reset_client = mock.Mock()

        def finish_reset(*_args, **kwargs):
            self.assertTrue(kwargs["password_reset_required"])
            self.assertEqual(kwargs["totp_secret"], "JBSWY3DPEHPK3PXP")
            self.assertTrue(kwargs["on_password_reset"](kwargs["password"]))
            return True, {
                "access_token": "access-token",
                "session_token": "session-token",
                "account_id": "account-1",
            }

        reset_client.login_existing_account_and_get_session.side_effect = finish_reset
        engine._build_chatgpt_client = mock.Mock(
            side_effect=[rejected_client, reset_client]
        )

        with mock.patch(
            "platforms.chatgpt.refresh_token_registration_engine.generate_random_password",
            return_value="Replacement-Password-2026!",
        ):
            result = engine.run()

        self.assertTrue(result.success)
        self.assertEqual(
            email_service.committed_password,
            "Replacement-Password-2026!",
        )
        self.assertEqual(engine._build_chatgpt_client.call_count, 2)

    def test_url_otp_transient_login_failure_does_not_reset_password(self):
        email_service = UrlOtpEmailService()
        engine = self._make_engine(email_service=email_service)
        oauth_client = mock.Mock()
        oauth_client.login_and_get_tokens.return_value = None
        oauth_client.last_error = "[stage=password_verify] ReadTimeout"
        engine._build_oauth_client = mock.Mock(return_value=oauth_client)

        result = engine.run()

        self.assertFalse(result.success)
        self.assertEqual(engine._build_oauth_client.call_count, 1)
        self.assertEqual(email_service.committed_password, "")
        self.assertFalse(any("忘记密码" in line for line in result.logs))

    def test_non_401_invalid_credentials_does_not_reset_password(self):
        engine = self._make_engine(
            email_service=PasswordTotpWithMailEmailService(),
        )

        self.assertFalse(
            engine._is_explicit_password_rejection(
                '请求失败: 400 - {"error":{"code":"invalid_credentials"}}'
            )
        )

    def test_reset_url_credentials_pass_generated_password_and_commit_callback(self):
        email_service = PasswordResetUrlEmailService()
        engine = self._make_engine(email_service=email_service)
        oauth_client = self._successful_oauth_client()
        engine._build_oauth_client = mock.Mock(return_value=oauth_client)
        engine._extract_account_info = mock.Mock(
            return_value={"email": "reset-user@icloud.com", "account_id": "account-1"}
        )
        successful_tokens = oauth_client.login_and_get_tokens.return_value

        def finish_reset(*args, **kwargs):
            self.assertTrue(kwargs["on_password_reset"](args[1]))
            return successful_tokens

        oauth_client.login_and_get_tokens.side_effect = finish_reset

        result = engine.run()

        self.assertTrue(result.success)
        self.assertEqual(result.password, "Fresh-Password-123!")
        call = oauth_client.login_and_get_tokens.call_args
        self.assertEqual(call.args[1], "Fresh-Password-123!")
        self.assertTrue(call.kwargs["password_reset_required"])
        self.assertTrue(call.kwargs["force_password_login"])
        self.assertFalse(call.kwargs["prefer_passwordless_login"])
        self.assertEqual(email_service.committed_password, "Fresh-Password-123!")

    def test_password_totp_access_stage_passes_credentials_to_web_login(self):
        engine = self._make_engine(
            login_stage="access_token",
            email_service=PasswordTotpEmailService(),
        )
        chatgpt_client = mock.Mock()
        chatgpt_client.login_existing_account_and_get_session.return_value = (
            True,
            {
                "access_token": "access-token",
                "session_token": "session-token",
                "account_id": "account-1",
                "workspace_id": "workspace-1",
                "user_id": "user-1",
            },
        )
        engine._build_chatgpt_client = mock.Mock(return_value=chatgpt_client)

        result = engine.run()

        self.assertTrue(result.success)
        call = chatgpt_client.login_existing_account_and_get_session.call_args
        self.assertEqual(call.kwargs["password"], "chatgpt-password")
        self.assertEqual(
            call.kwargs["totp_secret"],
            "JBSWY3DPEHPK3PXP",
        )

    def test_password_reset_access_stage_passes_reset_context_to_web_login(self):
        email_service = PasswordResetUrlEmailService()
        engine = self._make_engine(
            login_stage="access_token",
            email_service=email_service,
        )
        chatgpt_client = mock.Mock()
        chatgpt_client.login_existing_account_and_get_session.return_value = (
            True,
            {"access_token": "access-token"},
        )
        engine._build_chatgpt_client = mock.Mock(return_value=chatgpt_client)

        result = engine.run()

        self.assertTrue(result.success)
        call = chatgpt_client.login_existing_account_and_get_session.call_args
        self.assertEqual(call.kwargs["password"], "Fresh-Password-123!")
        self.assertTrue(call.kwargs["password_reset_required"])
        self.assertTrue(callable(call.kwargs["on_password_reset"]))

    def test_login_only_can_enable_existing_phone_verification_service(self):
        engine = self._make_engine(allow_phone_verification=True)
        oauth_client = self._successful_oauth_client()
        engine._build_oauth_client = mock.Mock(return_value=oauth_client)
        engine._extract_account_info = mock.Mock(
            return_value={"email": "existing@example.com", "account_id": "account-1"}
        )

        result = engine.run()

        self.assertTrue(result.success)
        login_kwargs = oauth_client.login_and_get_tokens.call_args.kwargs
        self.assertTrue(login_kwargs["allow_phone_verification"])

    def test_access_token_stage_uses_web_login_and_succeeds_without_refresh_token(self):
        engine = self._make_engine(login_stage="access_token")
        chatgpt_client = mock.Mock()
        chatgpt_client.login_existing_account_and_get_session.return_value = (
            True,
            {
                "access_token": "access-token",
                "session_token": "session-token",
                "account_id": "account-1",
                "workspace_id": "workspace-1",
                "user_id": "user-1",
            },
        )
        engine._build_chatgpt_client = mock.Mock(return_value=chatgpt_client)
        engine._build_oauth_client = mock.Mock()

        result = engine.run()

        self.assertTrue(result.success)
        self.assertEqual(result.access_token, "access-token")
        self.assertEqual(result.refresh_token, "")
        self.assertEqual(result.session_token, "session-token")
        self.assertEqual(result.account_id, "account-1")
        self.assertEqual(result.source, "existing_account_web_login")
        self.assertTrue(result.metadata["phone_verification_required"])
        self.assertEqual(
            result.metadata["mailbox_login_context"]["extra"]["client_id"],
            "mail-client",
        )
        engine._build_oauth_client.assert_not_called()
        call = chatgpt_client.login_existing_account_and_get_session.call_args
        self.assertEqual(call.args[0], "existing@example.com")
        self.assertEqual(call.kwargs["otp_wait_timeout"], 600)
        self.assertTrue(
            any("登录已有 ChatGPT 账号并提取 Access Token" in line for line in result.logs),
            result.logs,
        )

    def test_access_token_stage_caches_authenticated_browser_context(self):
        engine = self._make_engine(login_stage="access_token")
        chatgpt_client = mock.Mock()
        chatgpt_client.session = mock.Mock()
        chatgpt_client.session.headers = {"Accept-Language": "en-US,en;q=0.9"}
        prepared_session = mock.Mock()
        prepared_session.cookies.jar = [
            types.SimpleNamespace(
                name="login_session",
                value="prepared-transaction-cookie",
                domain=".openai.com",
                path="/",
                secure=True,
            )
        ]
        chatgpt_client.device_id = "device-1"
        chatgpt_client.ua = "UA"
        chatgpt_client.sec_ch_ua = '"Chromium";v="136"'
        chatgpt_client.impersonate = "chrome136"
        chatgpt_client.phone_oauth_browser_context = {
            "version": 1,
            "created_at": 100,
            "expires_at": 200,
            "cookies": [{"name": "login_session", "value": "browser-cookie"}],
        }
        chatgpt_client.phone_oauth_resume_context = types.SimpleNamespace(
            session=prepared_session,
            device_id="device-1",
            user_agent="UA",
            sec_ch_ua='"Chromium";v="136"',
            accept_language="en-US,en;q=0.9",
            impersonate="chrome136",
            code_verifier="prepared-verifier",
            oauth_state="prepared-state",
            authorize_url="https://auth.openai.com/oauth/authorize",
            authorize_params={"state": "prepared-state"},
            flow_state=FlowState(
                page_type="add_phone",
                current_url="https://auth.openai.com/add-phone",
            ),
            referer="https://auth.openai.com/add-phone",
        )
        chatgpt_client.login_existing_account_and_get_session.return_value = (
            True,
            {
                "access_token": "access-token",
                "session_token": "session-token",
                "account_id": "account-1",
                "workspace_id": "workspace-1",
            },
        )
        engine._build_chatgpt_client = mock.Mock(return_value=chatgpt_client)

        with mock.patch(
            "platforms.chatgpt.refresh_token_registration_engine.oauth_resume_cache"
        ) as cache:
            result = engine.run()

        self.assertTrue(result.success)
        cache.remember.assert_called_once_with(
            "existing@example.com",
            session=prepared_session,
            device_id="device-1",
            user_agent="UA",
            sec_ch_ua='"Chromium";v="136"',
            accept_language="en-US,en;q=0.9",
            impersonate="chrome136",
            code_verifier="prepared-verifier",
            oauth_state="prepared-state",
            authorize_url="https://auth.openai.com/oauth/authorize",
            authorize_params={"state": "prepared-state"},
            flow_state=chatgpt_client.phone_oauth_resume_context.flow_state,
            referer="https://auth.openai.com/add-phone",
        )
        snapshot = result.metadata.get("oauth_resume_context")
        self.assertIsInstance(snapshot, dict)
        self.assertEqual(snapshot["version"], 2)
        self.assertEqual(snapshot["device_id"], "device-1")
        self.assertEqual(snapshot["cookies"][0]["name"], "login_session")
        self.assertEqual(snapshot["cookies"][0]["value"], "prepared-transaction-cookie")
        self.assertEqual(snapshot["code_verifier"], "prepared-verifier")
        self.assertEqual(snapshot["flow_state"]["page_type"], "add_phone")
        self.assertEqual(result.metadata["oauth_browser_context"]["version"], 1)

    def test_access_token_stage_persists_browser_snapshot_when_prepare_fails(self):
        engine = self._make_engine(login_stage="access_token")
        chatgpt_client = mock.Mock()
        chatgpt_client.phone_oauth_resume_context = None
        chatgpt_client.phone_oauth_resume_error = "OAuth bootstrap failed"
        chatgpt_client.phone_oauth_prepare_diagnostic = {
            "stage": "phone_oauth_prepare",
            "attempt": 3,
            "page_type": "log_in",
            "http_status": 200,
            "recovery_status": "deferred",
        }
        chatgpt_client.phone_oauth_browser_context = {
            "version": 1,
            "created_at": 100,
            "expires_at": 200,
            "device_id": "device-1",
            "cookies": [
                {
                    "name": "login_session",
                    "value": "authenticated-browser-cookie",
                    "domain": ".openai.com",
                }
            ],
        }
        chatgpt_client.login_existing_account_and_get_session.return_value = (
            True,
            {
                "access_token": "access-token",
                "session_token": "session-token",
                "account_id": "account-1",
                "workspace_id": "workspace-1",
            },
        )
        engine._build_chatgpt_client = mock.Mock(return_value=chatgpt_client)

        with mock.patch(
            "platforms.chatgpt.refresh_token_registration_engine.oauth_resume_cache"
        ) as cache:
            result = engine.run()

        self.assertTrue(result.success)
        self.assertFalse(result.metadata["phone_oauth_ready"])
        self.assertEqual(result.metadata["oauth_browser_context"]["version"], 1)
        self.assertEqual(
            result.metadata["phone_oauth_prepare_diagnostic"]["recovery_status"],
            "deferred",
        )
        cache.take.assert_called_once_with("existing@example.com")
        cache.remember.assert_not_called()

    def test_existing_account_chatgpt_client_logs_login_chain(self):
        engine = self._make_engine(login_stage="access_token")
        messages = []
        engine.callback_logger = messages.append

        client = engine._build_chatgpt_client()
        client._log("访问 ChatGPT 首页...")

        self.assertTrue(any("[登录链路] 访问 ChatGPT 首页" in line for line in messages))
        self.assertFalse(any("[注册链路]" in line for line in messages))

    def test_registration_chatgpt_client_keeps_registration_chain_log(self):
        engine = self._make_engine(login_only=False)
        messages = []
        engine.callback_logger = messages.append

        client = engine._build_chatgpt_client()
        client._log("访问 ChatGPT 首页...")

        self.assertTrue(any("[注册链路] 访问 ChatGPT 首页" in line for line in messages))

    def test_authorize_logs_sanitized_login_session_diagnostics(self):
        client = ChatGPTClient(verbose=False)
        logs = []
        client._log = logs.append
        response = mock.Mock()
        response.status_code = 200
        response.url = "https://auth.openai.com/api/accounts/authorize?state=secret"
        response.history = [mock.Mock(), mock.Mock()]
        response.headers = {"content-type": "text/html; charset=utf-8"}
        client.session.get = mock.Mock(return_value=response)
        client._get_cookie_value = mock.Mock(return_value="")

        final_url = client.authorize("https://chatgpt.com/api/auth/signin/openai")

        self.assertEqual(final_url, str(response.url))
        diagnostic = next(line for line in logs if "authorize 会话诊断" in line)
        self.assertIn("status=200", diagnostic)
        self.assertIn("redirects=2", diagnostic)
        self.assertIn("login_session=未获取", diagnostic)
        self.assertIn("final_page=/api/accounts/authorize", diagnostic)
        self.assertIn("content_type=text/html", diagnostic)
        self.assertNotIn("state=secret", diagnostic)

    def test_oauth_cookie_lookup_supports_name_only_cookie_container(self):
        class NameOnlyCookies:
            def __iter__(self):
                return iter(["login_session"])

            def get(self, name):
                if name == "login_session":
                    return "session-value"
                return None

        client = OAuthClient({}, verbose=False)
        client.session = mock.Mock()
        client.session.cookies = NameOnlyCookies()

        value = client._get_cookie_value("login_session")

        self.assertEqual(value, "session-value")

    def test_access_token_stage_reports_web_login_error(self):
        engine = self._make_engine(login_stage="access_token")
        chatgpt_client = mock.Mock()
        chatgpt_client.login_existing_account_and_get_session.return_value = (
            False,
            "邮箱验证码已过期",
        )
        engine._build_chatgpt_client = mock.Mock(return_value=chatgpt_client)

        result = engine.run()

        self.assertFalse(result.success)
        self.assertIn("邮箱验证码已过期", result.error_message)

    def test_web_login_submits_email_when_authorize_stops_on_api_endpoint(self):
        client = ChatGPTClient(verbose=False)
        client.visit_homepage = mock.Mock(return_value=True)
        client.get_csrf_token = mock.Mock(return_value="csrf-token")
        client.signin = mock.Mock(return_value="https://auth.openai.com/authorize-start")
        authorize_endpoint = "https://auth.openai.com/api/accounts/authorize?state=demo"
        client.authorize = mock.Mock(return_value=authorize_endpoint)
        client.last_authorize_status = 200
        client._get_cookie_value = mock.Mock(return_value="login-session")
        client.fetch_chatgpt_session = mock.Mock(
            return_value=(
                True,
                {
                    "accessToken": "access-token",
                    "sessionToken": "session-token",
                    "account": {"id": "account-1"},
                    "user": {"id": "user-1"},
                },
            )
        )
        client.get_next_auth_session_token = mock.Mock(return_value="session-token")
        next_state = FlowState(
            page_type="add_phone",
            current_url="https://auth.openai.com/add-phone",
        )

        with mock.patch.object(
            OAuthClient,
            "_submit_authorize_continue",
            return_value=next_state,
        ) as submit_email, mock.patch.object(
            OAuthClient,
            "prepare_phone_verification_transaction",
            return_value=None,
        ) as prepare_phone:
            ok, result = client.login_existing_account_and_get_session(
                "existing@example.com",
                mock.Mock(),
            )

        self.assertTrue(ok)
        self.assertEqual(result["access_token"], "access-token")
        submit_email.assert_called_once()
        self.assertEqual(submit_email.call_args.args[0], "existing@example.com")
        self.assertEqual(submit_email.call_args.kwargs["screen_hint"], "login")
        prepare_phone.assert_called_once()
        self.assertIn("首轮", client.phone_oauth_resume_error)

    def test_web_login_bootstraps_oauth_session_after_authorize_403(self):
        client = ChatGPTClient(verbose=False)
        client.visit_homepage = mock.Mock(return_value=True)
        client.get_csrf_token = mock.Mock(return_value="csrf-token")
        authorize_endpoint = (
            "https://auth.openai.com/api/accounts/authorize"
            "?client_id=app-demo&state=demo&screen_hint=login"
        )
        client.signin = mock.Mock(return_value=authorize_endpoint)
        client.authorize = mock.Mock(return_value=authorize_endpoint)
        client.last_authorize_status = 403
        client._get_cookie_value = mock.Mock(return_value="")
        client.fetch_chatgpt_session = mock.Mock(
            return_value=(
                True,
                {
                    "accessToken": "access-token",
                    "sessionToken": "session-token",
                    "account": {"id": "account-1"},
                    "user": {"id": "user-1"},
                },
            )
        )
        client.get_next_auth_session_token = mock.Mock(return_value="session-token")
        next_state = FlowState(
            page_type="add_phone",
            current_url="https://auth.openai.com/add-phone",
        )

        with mock.patch.object(
            OAuthClient,
            "_bootstrap_oauth_session",
            return_value="https://auth.openai.com/log-in",
        ) as bootstrap, mock.patch.object(
            OAuthClient,
            "_get_cookie_value",
            return_value="login-session",
        ) as cookie_check, mock.patch.object(
            OAuthClient,
            "_submit_authorize_continue",
            return_value=next_state,
        ) as submit_email, mock.patch.object(
            OAuthClient,
            "prepare_phone_verification_transaction",
            return_value=None,
        ):
            ok, result = client.login_existing_account_and_get_session(
                "existing@example.com",
                mock.Mock(),
            )

        self.assertTrue(ok)
        self.assertEqual(result["access_token"], "access-token")
        bootstrap.assert_called_once()
        self.assertEqual(
            bootstrap.call_args.args[0],
            "https://auth.openai.com/api/accounts/authorize",
        )
        self.assertEqual(bootstrap.call_args.args[1]["client_id"], "app-demo")
        self.assertEqual(bootstrap.call_args.args[1]["state"], "demo")
        cookie_check.assert_called_with("login_session")
        submit_email.assert_called_once()
        self.assertEqual(
            submit_email.call_args.args[2],
            "https://auth.openai.com/log-in",
        )

    def test_web_login_follows_external_callback_before_fetching_session(self):
        client = ChatGPTClient(verbose=False)
        client.visit_homepage = mock.Mock(return_value=True)
        client.get_csrf_token = mock.Mock(return_value="csrf-token")
        client.signin = mock.Mock(return_value="https://auth.openai.com/email-verification")
        client.authorize = mock.Mock(return_value="https://auth.openai.com/email-verification")
        client.last_authorize_status = 200
        client._get_cookie_value = mock.Mock(return_value="login-session")
        events = []
        callback_url = "https://chatgpt.com/api/auth/callback/openai?code=demo"
        external_state = FlowState(
            page_type="external_url",
            continue_url=callback_url,
            current_url=callback_url,
            method="GET",
        )
        callback_state = FlowState(
            page_type="chatgpt_home",
            current_url="https://chatgpt.com/",
            method="GET",
        )

        def follow_callback(*args, **kwargs):
            events.append("follow_callback")
            return True, callback_state

        def fetch_session(*args, **kwargs):
            self.assertEqual(events, ["follow_callback"])
            return (
                True,
                {
                    "accessToken": "access-token",
                    "sessionToken": "session-token",
                    "account": {"id": "account-1"},
                    "user": {"id": "user-1"},
                },
            )

        client._follow_flow_state = mock.Mock(side_effect=follow_callback)
        client.fetch_chatgpt_session = mock.Mock(side_effect=fetch_session)
        client.get_next_auth_session_token = mock.Mock(return_value="session-token")

        with mock.patch.object(
            OAuthClient,
            "_handle_otp_verification",
            return_value=external_state,
        ), mock.patch.object(
            OAuthClient,
            "prepare_phone_verification_transaction",
            return_value=None,
        ):
            ok, result = client.login_existing_account_and_get_session(
                "existing@example.com",
                mock.Mock(),
            )

        self.assertTrue(ok)
        self.assertEqual(result["access_token"], "access-token")
        client._follow_flow_state.assert_called_once()
        self.assertEqual(
            client._follow_flow_state.call_args.args[0].continue_url,
            callback_url,
        )

    def test_web_login_clones_phone_oauth_after_first_email_otp(self):
        client = ChatGPTClient(verbose=False)
        client.visit_homepage = mock.Mock(return_value=True)
        client.get_csrf_token = mock.Mock(return_value="csrf-token")
        client.signin = mock.Mock(return_value="https://auth.openai.com/email-verification")
        client.authorize = mock.Mock(return_value="https://auth.openai.com/email-verification")
        client.last_authorize_status = 200
        client._get_cookie_value = mock.Mock(return_value="login-session")
        events = []
        callback_url = "https://chatgpt.com/api/auth/callback/openai?code=demo"
        external_state = FlowState(
            page_type="external_url",
            continue_url=callback_url,
            current_url=callback_url,
            method="GET",
        )
        callback_state = FlowState(
            page_type="chatgpt_home",
            current_url="https://chatgpt.com/",
            method="GET",
        )
        prepared_context = types.SimpleNamespace(
            session=object(),
            code_verifier="prepared-verifier",
            oauth_state="prepared-state",
            flow_state=FlowState(page_type="add_phone"),
        )

        def prepare_phone_transaction(*args, **kwargs):
            events.append("prepare_cloned_phone_transaction")
            return prepared_context

        def follow_callback(*args, **kwargs):
            events.append("follow_chatgpt_callback")
            return True, callback_state

        client._follow_flow_state = mock.Mock(side_effect=follow_callback)
        def fetch_session(*args, **kwargs):
            events.append("fetch_access_token")
            return (
                True,
                {
                    "accessToken": "access-token",
                    "sessionToken": "session-token",
                    "account": {"id": "account-1"},
                    "user": {"id": "user-1"},
                },
            )

        client.fetch_chatgpt_session = mock.Mock(side_effect=fetch_session)
        client.get_next_auth_session_token = mock.Mock(return_value="session-token")

        with mock.patch.object(
            OAuthClient,
            "_handle_otp_verification",
            return_value=external_state,
        ), mock.patch.object(
            OAuthClient,
            "prepare_phone_verification_transaction",
            side_effect=prepare_phone_transaction,
        ) as clone_prepare, mock.patch.object(
            ChatGPTClient,
            "_prepare_phone_oauth_with_fresh_login",
            create=True,
        ) as fresh_prepare:
            ok, result = client.login_existing_account_and_get_session(
                "existing@example.com",
                mock.Mock(),
            )

        self.assertTrue(ok)
        self.assertEqual(result["access_token"], "access-token")
        clone_prepare.assert_called_once()
        fresh_prepare.assert_not_called()
        self.assertEqual(
            events,
            [
                "prepare_cloned_phone_transaction",
                "follow_chatgpt_callback",
                "fetch_access_token",
            ],
        )

    def test_web_login_retries_phone_oauth_prepare_with_fresh_helper_without_resending_otp(self):
        client = ChatGPTClient(verbose=False)
        client.visit_homepage = mock.Mock(return_value=True)
        client.get_csrf_token = mock.Mock(return_value="csrf-token")
        client.signin = mock.Mock(
            return_value="https://auth.openai.com/email-verification"
        )
        client.authorize = mock.Mock(
            return_value="https://auth.openai.com/email-verification"
        )
        client.last_authorize_status = 200
        client._get_cookie_value = mock.Mock(return_value="login-session")
        external_state = FlowState(
            page_type="external_url",
            continue_url="https://chatgpt.com/api/auth/callback/openai?code=demo",
            current_url="https://chatgpt.com/api/auth/callback/openai?code=demo",
            method="GET",
        )
        client._follow_flow_state = mock.Mock(
            return_value=(
                True,
                FlowState(page_type="chatgpt_home", current_url="https://chatgpt.com/"),
            )
        )
        client.fetch_chatgpt_session = mock.Mock(
            return_value=(
                True,
                {
                    "accessToken": "access-token",
                    "sessionToken": "session-token",
                    "account": {"id": "account-1"},
                    "user": {"id": "user-1"},
                },
            )
        )
        client.get_next_auth_session_token = mock.Mock(return_value="session-token")
        mailbox = mock.Mock()
        prepared_context = types.SimpleNamespace(
            session=object(),
            code_verifier="prepared-verifier",
            oauth_state="prepared-state",
            flow_state=FlowState(page_type="add_phone"),
        )
        prepare_helpers = []

        def prepare_phone_transaction(helper, *args, **kwargs):
            prepare_helpers.append(helper)
            if len(prepare_helpers) == 1:
                helper.last_error = "OAuth bootstrap failed"
                helper.last_state = FlowState(
                    page_type="log_in",
                    current_url="https://auth.openai.com/log-in?state=must-not-leak",
                )
                helper.last_http_status = 200
                return None
            return prepared_context

        with mock.patch.object(
            OAuthClient,
            "_handle_otp_verification",
            return_value=external_state,
        ) as submit_email_otp, mock.patch.object(
            OAuthClient,
            "prepare_phone_verification_transaction",
            autospec=True,
            side_effect=prepare_phone_transaction,
        ) as prepare_phone, mock.patch(
            "platforms.chatgpt.chatgpt_client.time.sleep"
        ) as sleep:
            ok, result = client.login_existing_account_and_get_session(
                "existing@example.com",
                mailbox,
            )

        self.assertTrue(ok)
        self.assertEqual(result["access_token"], "access-token")
        submit_email_otp.assert_called_once()
        self.assertEqual(prepare_phone.call_count, 2)
        self.assertEqual(len(prepare_helpers), 2)
        self.assertIsNot(prepare_helpers[0], prepare_helpers[1])
        self.assertIs(client.phone_oauth_resume_context, prepared_context)
        sleep.assert_called_once_with(0.5)
        self.assertEqual(
            client.phone_oauth_prepare_diagnostic,
            {
                "stage": "phone_oauth_prepare",
                "attempt": 2,
                "page_type": "add_phone",
                "http_status": 0,
                "recovery_status": "recovered",
            },
        )
        self.assertNotIn(
            "must-not-leak",
            str(client.phone_oauth_prepare_diagnostic),
        )

    def test_web_login_defers_phone_oauth_clone_until_email_mfa_passes(self):
        client = ChatGPTClient(verbose=False)
        client.visit_homepage = mock.Mock(return_value=True)
        client.get_csrf_token = mock.Mock(return_value="csrf-token")
        client.signin = mock.Mock(
            return_value="https://auth.openai.com/email-verification"
        )
        client.authorize = mock.Mock(
            return_value="https://auth.openai.com/email-verification"
        )
        client.last_authorize_status = 200
        client._get_cookie_value = mock.Mock(return_value="login-session")
        events = []
        mfa_state = FlowState(
            page_type="mfa_challenge",
            continue_url="https://auth.openai.com/mfa-challenge/factor-email",
            payload={
                "factors": [
                    {"id": "factor-email", "factor_type": "email"},
                ]
            },
        )
        callback_url = "https://chatgpt.com/api/auth/callback/openai?code=demo"
        external_state = FlowState(
            page_type="external_url",
            continue_url=callback_url,
            current_url=callback_url,
            method="GET",
        )
        callback_state = FlowState(
            page_type="chatgpt_home",
            current_url="https://chatgpt.com/",
            method="GET",
        )
        prepared_context = types.SimpleNamespace(
            session=object(),
            code_verifier="prepared-verifier",
            oauth_state="prepared-state",
            flow_state=FlowState(page_type="add_phone"),
        )

        def submit_email_otp(*args, **kwargs):
            events.append("email_otp")
            return mfa_state

        def submit_email_mfa(*args, **kwargs):
            self.assertEqual(events, ["email_otp"])
            events.append("email_mfa")
            return external_state

        def prepare_phone_transaction(*args, **kwargs):
            events.append("prepare_cloned_phone_transaction")
            return prepared_context

        def follow_callback(*args, **kwargs):
            events.append("follow_chatgpt_callback")
            return True, callback_state

        def fetch_session(*args, **kwargs):
            events.append("fetch_access_token")
            return (
                True,
                {
                    "accessToken": "access-token",
                    "sessionToken": "session-token",
                    "account": {"id": "account-1"},
                    "user": {"id": "user-1"},
                },
            )

        client._follow_flow_state = mock.Mock(side_effect=follow_callback)
        client.fetch_chatgpt_session = mock.Mock(side_effect=fetch_session)
        client.get_next_auth_session_token = mock.Mock(return_value="session-token")

        with mock.patch.object(
            OAuthClient,
            "_handle_otp_verification",
            side_effect=submit_email_otp,
        ), mock.patch.object(
            OAuthClient,
            "_submit_mfa_challenge",
            side_effect=submit_email_mfa,
        ) as submit_mfa, mock.patch.object(
            OAuthClient,
            "prepare_phone_verification_transaction",
            side_effect=prepare_phone_transaction,
        ) as clone_prepare:
            ok, result = client.login_existing_account_and_get_session(
                "existing@example.com",
                mock.Mock(),
            )

        self.assertTrue(ok)
        self.assertEqual(result["access_token"], "access-token")
        submit_mfa.assert_called_once()
        clone_prepare.assert_called_once()
        self.assertEqual(
            events,
            [
                "email_otp",
                "email_mfa",
                "prepare_cloned_phone_transaction",
                "follow_chatgpt_callback",
                "fetch_access_token",
            ],
        )

    def test_web_login_uses_password_and_totp_without_reading_apple_mail(self):
        client = ChatGPTClient(verbose=False)
        client.visit_homepage = mock.Mock(return_value=True)
        client.get_csrf_token = mock.Mock(return_value="csrf-token")
        client.signin = mock.Mock(return_value="https://auth.openai.com/log-in/password")
        client.authorize = mock.Mock(return_value="https://auth.openai.com/log-in/password")
        client.last_authorize_status = 200
        client._get_cookie_value = mock.Mock(return_value="login-session")
        client.fetch_chatgpt_session = mock.Mock(
            return_value=(
                True,
                {
                    "accessToken": "access-token",
                    "sessionToken": "session-token",
                    "account": {"id": "account-1"},
                    "user": {"id": "user-1"},
                },
            )
        )
        client.get_next_auth_session_token = mock.Mock(return_value="session-token")
        mfa_state = FlowState(
            page_type="mfa_challenge",
            continue_url="https://auth.openai.com/mfa-challenge/factor-1",
            payload={
                "factors": [{"id": "factor-1", "factor_type": "totp"}],
            },
        )
        add_phone_state = FlowState(
            page_type="add_phone",
            current_url="https://auth.openai.com/add-phone",
        )
        prepared_context = types.SimpleNamespace(
            code_verifier="prepared-verifier",
            oauth_state="prepared-state",
            flow_state=add_phone_state,
        )
        mailbox = mock.Mock()

        with mock.patch.object(
            OAuthClient,
            "_submit_password_verify",
            return_value=mfa_state,
        ) as submit_password, mock.patch.object(
            OAuthClient,
            "_submit_mfa_challenge",
            return_value=add_phone_state,
        ) as submit_mfa, mock.patch.object(
            OAuthClient,
            "_send_passwordless_login_otp",
        ) as passwordless, mock.patch.object(
            OAuthClient,
            "prepare_phone_verification_transaction",
            return_value=prepared_context,
        ) as prepare_phone:
            ok, result = client.login_existing_account_and_get_session(
                "mfa-user@icloud.com",
                mailbox,
                password="chatgpt-password",
                totp_secret="JBSWY3DPEHPK3PXP",
                mfa_recovery_code="RECOVERY-CODE",
            )

        self.assertTrue(ok)
        self.assertEqual(result["access_token"], "access-token")
        submit_password.assert_called_once()
        self.assertEqual(submit_password.call_args.args[0], "chatgpt-password")
        submit_mfa.assert_called_once()
        self.assertEqual(
            submit_mfa.call_args.kwargs["totp_secret"],
            "JBSWY3DPEHPK3PXP",
        )
        self.assertEqual(
            submit_mfa.call_args.kwargs["mfa_recovery_code"],
            "RECOVERY-CODE",
        )
        passwordless.assert_not_called()
        prepare_phone.assert_called_once()
        mailbox.wait_for_verification_code.assert_not_called()
        self.assertIs(client.phone_oauth_resume_context, prepared_context)

    def test_web_login_resets_password_then_restarts_login_once(self):
        client = ChatGPTClient(verbose=False)
        client.visit_homepage = mock.Mock(return_value=True)
        client.get_csrf_token = mock.Mock(return_value="csrf-token")
        client.signin = mock.Mock(
            side_effect=[
                "https://auth.openai.com/log-in/password",
                "https://auth.openai.com/add-phone",
            ]
        )
        client.authorize = mock.Mock(
            side_effect=[
                "https://auth.openai.com/log-in/password",
                "https://auth.openai.com/add-phone",
            ]
        )
        client.last_authorize_status = 200
        client._get_cookie_value = mock.Mock(return_value="login-session")
        client.fetch_chatgpt_session = mock.Mock(
            return_value=(True, {"accessToken": "access-token"})
        )
        client.get_next_auth_session_token = mock.Mock(return_value="session-token")
        reset_success = FlowState(page_type="reset_password_success")
        commit = mock.Mock(return_value=True)

        with mock.patch.object(
            OAuthClient,
            "_complete_password_reset",
            return_value=reset_success,
        ) as complete_reset, mock.patch.object(
            OAuthClient,
            "prepare_phone_verification_transaction",
            return_value=None,
        ):
            ok, result = client.login_existing_account_and_get_session(
                "reset-user@icloud.com",
                mock.Mock(),
                password="Fresh-Password-123!",
                password_reset_required=True,
                on_password_reset=commit,
            )

        self.assertTrue(ok)
        self.assertEqual(result["access_token"], "access-token")
        self.assertEqual(client.signin.call_count, 2)
        complete_reset.assert_called_once()
        self.assertEqual(
            complete_reset.call_args.kwargs["new_password"],
            "Fresh-Password-123!",
        )
        self.assertIs(complete_reset.call_args.kwargs["on_password_reset"], commit)

    def test_web_login_retries_transient_homepage_failure(self):
        client = ChatGPTClient(verbose=False)
        client.visit_homepage = mock.Mock(side_effect=[False, True])
        client._reset_session = mock.Mock()
        client.get_csrf_token = mock.Mock(return_value="csrf-token")
        client.signin = mock.Mock(return_value="https://auth.openai.com/authorize-start")
        client.authorize = mock.Mock(return_value="https://auth.openai.com/add-phone")
        client.last_authorize_status = 200
        client._get_cookie_value = mock.Mock(return_value="login-session")
        client.fetch_chatgpt_session = mock.Mock(
            return_value=(
                True,
                {
                    "accessToken": "access-token",
                    "account": {"id": "account-1"},
                    "user": {"id": "user-1"},
                },
            )
        )
        client.get_next_auth_session_token = mock.Mock(return_value="session-token")

        with mock.patch.object(
            OAuthClient,
            "prepare_phone_verification_transaction",
            return_value=None,
        ):
            ok, result = client.login_existing_account_and_get_session(
                "existing@example.com",
                mock.Mock(),
            )

        self.assertTrue(ok)
        self.assertEqual(result["access_token"], "access-token")
        self.assertEqual(client.visit_homepage.call_count, 2)
        client._reset_session.assert_called_once()

    def test_web_login_keeps_retrying_after_three_transient_entry_failures(self):
        client = ChatGPTClient(verbose=False)
        client.visit_homepage = mock.Mock(side_effect=[False, False, False, True])
        client._reset_session = mock.Mock()
        client.get_csrf_token = mock.Mock(return_value="csrf-token")
        client.signin = mock.Mock(return_value="https://auth.openai.com/authorize-start")
        client.authorize = mock.Mock(return_value="https://auth.openai.com/add-phone")
        client.last_authorize_status = 200
        client._get_cookie_value = mock.Mock(return_value="login-session")
        client.fetch_chatgpt_session = mock.Mock(
            return_value=(
                True,
                {
                    "accessToken": "access-token",
                    "account": {"id": "account-1"},
                    "user": {"id": "user-1"},
                },
            )
        )
        client.get_next_auth_session_token = mock.Mock(return_value="session-token")

        with mock.patch.object(
            OAuthClient,
            "prepare_phone_verification_transaction",
            return_value=None,
        ):
            ok, result = client.login_existing_account_and_get_session(
                "existing@example.com",
                mock.Mock(),
            )

        self.assertTrue(ok)
        self.assertEqual(result["access_token"], "access-token")
        self.assertEqual(client.visit_homepage.call_count, 4)
        self.assertEqual(client._reset_session.call_count, 3)

    def test_login_only_rejects_result_without_refresh_token(self):
        engine = self._make_engine()
        oauth_client = self._successful_oauth_client()
        oauth_client.login_and_get_tokens.return_value = {
            "access_token": "access-token",
            "refresh_token": "",
        }
        engine._build_oauth_client = mock.Mock(return_value=oauth_client)
        engine._build_chatgpt_client = mock.Mock()

        result = engine.run()

        self.assertFalse(result.success)
        self.assertIn("Refresh Token", result.error_message)
        engine._build_chatgpt_client.assert_not_called()

    def test_default_mode_still_enters_registration_state_machine(self):
        engine = self._make_engine(login_only=False)
        register_client = mock.Mock()
        register_client.register_complete_flow.return_value = (False, "fatal")
        engine._build_chatgpt_client = mock.Mock(return_value=register_client)
        engine._build_oauth_client = mock.Mock()

        result = engine.run()

        self.assertFalse(result.success)
        register_client.register_complete_flow.assert_called_once()
        engine._build_oauth_client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
