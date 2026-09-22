import types
import unittest
from unittest import mock

from sqlmodel import SQLModel, create_engine
from sqlalchemy.pool import StaticPool

from core.db import (
    load_chatgpt_mfa_rotation,
    load_chatgpt_device_logout,
    mark_chatgpt_mfa_rotation_activated,
    stage_chatgpt_mfa_rotation,
    finalize_chatgpt_mfa_rotation,
    complete_chatgpt_device_logout,
)
from core.task_runtime import StopTaskRequested
from platforms.chatgpt.mfa_manager import MfaRotationError, MfaRotationResult
from platforms.chatgpt.utils import FlowState
from platforms.chatgpt.refresh_token_registration_engine import (
    EmailServiceAdapter,
    RegistrationResult,
    RefreshTokenRegistrationEngine,
)

EMAIL = "device-logout@example.test"
NEW_SECRET = "JBSWY3DPEHPK3PXP"
ROTATION = MfaRotationResult(
    NEW_SECRET, "RECOVERY", True, True, "2026-09-22T12:00:00+00:00"
)
ENGINE_MODULE = "platforms.chatgpt.refresh_token_registration_engine"


class EmailService:
    service_type = types.SimpleNamespace(value="chatgpt_credentials")

    def get_mailbox_metadata(self):
        return {}

    def supports_email_verification(self):
        return False

    def commit_mfa_rotation(self, **kwargs):
        return True


class DeviceLogoutLoginTests(unittest.TestCase):
    def setUp(self):
        self.db = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        SQLModel.metadata.create_all(self.db)
        patcher = mock.patch("core.db.engine", self.db)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.db.dispose)
        self.engine = RefreshTokenRegistrationEngine(
            email_service=EmailService(),
            max_retries=1,
            extra_config={"chatgpt_existing_account_rotate_mfa": True},
        )
        self.engine.email = EMAIL
        self.engine.password = "fixture-password"
        self.engine.totp_secret = "OLDSECRET"
        self.engine.email_info = {}
        self.engine._apply_subscription_gate = mock.Mock(return_value=True)
        self.adapter = EmailServiceAdapter(
            self.engine.email_service, EMAIL, lambda _: None
        )
        self.events = []
        self.old = self.web_client("old")
        self.fresh = self.web_client("fresh")
        self.engine._build_chatgpt_client = mock.Mock(
            side_effect=[self.old, self.fresh]
        )

        self.manager_patch = mock.patch(ENGINE_MODULE + ".ChatGPTMfaManager")
        self.manager = self.manager_patch.start().return_value
        self.addCleanup(self.manager_patch.stop)

        def rotate():
            kwargs = self.manager_factory.call_args.kwargs
            kwargs["on_secret_enrolled"](NEW_SECRET)
            kwargs["on_secret_activated"](ROTATION.rotated_at)
            kwargs["on_recovery_code"](ROTATION.recovery_code)
            self.events.append("rotate")
            return ROTATION

        self.manager_factory = getattr(
            __import__(ENGINE_MODULE, fromlist=["ChatGPTMfaManager"]),
            "ChatGPTMfaManager",
        )
        self.manager.rotate.side_effect = rotate

        def logout():
            journal = load_chatgpt_mfa_rotation(EMAIL)
            self.assertEqual(journal["status"], "activated")
            self.assertEqual(journal["totp_secret"], NEW_SECRET)
            self.assertEqual(self.engine.totp_secret, NEW_SECRET)
            self.events.append("logout")

        self.manager.logout_all_devices.side_effect = logout

    def web_client(self, label):
        client = mock.Mock()
        client.session = mock.Mock()
        client.device_id = label + "-device"
        client.ua = "UA"
        client.impersonate = "chrome"
        client.phone_oauth_resume_context = None
        client.phone_oauth_browser_context = {"label": label}
        client.phone_oauth_prepare_diagnostic = {}
        client.phone_oauth_resume_error = ""

        def login(*args, **kwargs):
            self.events.append(label + "-login")
            if label == "fresh":
                self.assertEqual(kwargs["totp_secret"], NEW_SECRET)
                self.assertIn("logout", self.events)
                self.assertFalse(kwargs["password_reset_required"])
            return True, {
                "access_token": label + "-access",
                "session_token": label + "-session",
                "account_id": "account-1",
                "workspace_id": "workspace-1",
            }

        client.login_existing_account_and_get_session.side_effect = login
        return client

    def run_access(self):
        return self.engine._login_existing_account_access_token(
            result=RegistrationResult(success=False, email=EMAIL),
            email_adapter=self.adapter,
            otp_wait_seconds=30,
            otp_resend_wait_seconds=30,
        )

    def test_access_path_uses_new_credentials_and_browser_after_durable_rotation(self):
        with mock.patch(ENGINE_MODULE + ".oauth_resume_cache") as cache:
            result = self.run_access()
        self.assertTrue(result.success, result.error_message)
        self.assertEqual(self.events, ["old-login", "rotate", "logout", "fresh-login"])
        self.assertEqual(result.access_token, "fresh-access")
        self.assertEqual(result.session_token, "fresh-session")
        self.assertEqual(result.metadata["oauth_browser_context"], {"label": "fresh"})
        self.assertTrue(result.metadata["mfa_rotation"]["devices_logged_out"])
        cache.take.assert_any_call(EMAIL)

    def test_logout_failure_stops_before_new_login_and_preserves_mfa(self):
        self.manager.logout_all_devices.side_effect = MfaRotationError(
            "[stage=mfa_logout_all] HTTP 401"
        )
        result = self.run_access()
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "mfa_logout_all_failed")
        self.fresh.login_existing_account_and_get_session.assert_not_called()
        self.assertEqual(load_chatgpt_mfa_rotation(EMAIL)["totp_secret"], NEW_SECRET)
        self.assertFalse(result.access_token)

    def test_fresh_login_failure_never_returns_old_credentials(self):
        self.fresh.login_existing_account_and_get_session.side_effect = None
        self.fresh.login_existing_account_and_get_session.return_value = (
            False,
            "fixture login failure",
        )
        result = self.run_access()
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "post_mfa_login_failed")
        self.assertFalse(result.access_token)
        self.assertFalse(result.session_token)
        self.assertEqual(load_chatgpt_mfa_rotation(EMAIL)["totp_secret"], NEW_SECRET)

    def test_failed_local_mfa_commit_prevents_logout(self):
        self.engine.email_service.commit_mfa_rotation = mock.Mock(return_value=False)
        result = self.run_access()
        self.assertFalse(result.success)
        self.manager.logout_all_devices.assert_not_called()
        self.fresh.login_existing_account_and_get_session.assert_not_called()

    def test_stop_during_logout_propagates_without_login(self):
        self.manager.logout_all_devices.side_effect = StopTaskRequested()
        with self.assertRaises(StopTaskRequested):
            self.run_access()
        self.fresh.login_existing_account_and_get_session.assert_not_called()

    def test_missing_fresh_token_stops(self):
        self.fresh.login_existing_account_and_get_session.side_effect = None
        self.fresh.login_existing_account_and_get_session.return_value = (True, {})
        result = self.run_access()
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "post_mfa_login_failed")

    def test_free_plan_exits_before_rotation_and_logout(self):
        self.engine._apply_subscription_gate.return_value = False
        result = self.run_access()
        self.assertFalse(result.success)
        self.manager.rotate.assert_not_called()
        self.manager.logout_all_devices.assert_not_called()

    def oauth_client(self, label):
        client = mock.Mock()
        client.session = mock.Mock()
        client.last_mfa_enrollment = {}
        client.ua = "UA"
        client.impersonate = "chrome"
        client.last_workspace_id = "workspace-1"
        client._get_cookie_value.return_value = label + "-session"

        def login(*args, **kwargs):
            self.events.append(label + "-oauth")
            self.assertEqual(kwargs["totp_secret"], NEW_SECRET)
            self.assertTrue(kwargs["force_new_browser"])
            self.assertFalse(kwargs["resume_authenticated_session"])
            self.assertEqual(kwargs["device_id"], "")
            return {
                "access_token": label + "-access",
                "refresh_token": label + "-refresh",
                "id_token": label + "-id",
            }

        client.login_and_get_tokens.side_effect = login
        return client

    def run_refresh(self):
        self.engine._extract_account_info = mock.Mock(
            return_value={"account_id": "account-1"}
        )
        return self.engine._login_existing_account(
            result=RegistrationResult(success=False, email=EMAIL),
            email_adapter=self.adapter,
            otp_wait_seconds=30,
            otp_resend_wait_seconds=30,
        )

    def test_refresh_path_uses_clean_oauth_and_new_mfa(self):
        fresh = self.oauth_client("fresh")
        self.engine._build_oauth_client = mock.Mock(return_value=fresh)
        result = self.run_refresh()
        self.assertTrue(result.success, result.error_message)
        self.assertEqual(self.events, ["old-login", "rotate", "logout", "fresh-oauth"])
        self.assertEqual(result.access_token, "fresh-access")
        self.assertEqual(result.refresh_token, "fresh-refresh")
        self.assertEqual(result.session_token, "fresh-session")
        fresh.adopt_browser_context.assert_not_called()
        self.assertTrue(result.metadata["mfa_rotation"]["fresh_login_completed"])

    def test_refresh_logout_failure_never_starts_oauth(self):
        self.manager.logout_all_devices.side_effect = MfaRotationError("HTTP 500")
        self.engine._build_oauth_client = mock.Mock()
        result = self.run_refresh()
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "mfa_logout_all_failed")
        self.engine._build_oauth_client.assert_not_called()

    def test_access_path_uses_only_new_phone_transaction(self):
        self.old.phone_oauth_resume_context = types.SimpleNamespace(
            code_verifier="old-verifier"
        )
        self.fresh.phone_oauth_resume_context = types.SimpleNamespace(
            session=self.fresh.session,
            device_id="fresh-device",
            user_agent="UA",
            sec_ch_ua="",
            accept_language="en-US",
            impersonate="chrome",
            code_verifier="fresh-verifier",
            oauth_state="fresh-state",
            authorize_url="https://auth.openai.com/oauth/authorize",
            authorize_params={},
            flow_state=FlowState(page_type="add_phone"),
            referer="",
        )
        with mock.patch(ENGINE_MODULE + ".oauth_resume_cache") as cache, mock.patch(
            ENGINE_MODULE + ".serialize_oauth_resume_context",
            return_value={"code_verifier": "fresh-verifier"},
        ):
            result = self.run_access()
        self.assertTrue(result.success)
        self.assertTrue(result.metadata["phone_oauth_ready"])
        self.assertEqual(
            cache.remember.call_args.kwargs["code_verifier"], "fresh-verifier"
        )
        self.assertIs(cache.remember.call_args.kwargs["session"], self.fresh.session)

    def test_mandatory_web_enrollment_also_logs_out_and_relogs_without_second_rotation(
        self,
    ):
        self.engine.extra_config["chatgpt_existing_account_rotate_mfa"] = False

        def login(*args, **kwargs):
            kwargs["on_mfa_totp_staged"](NEW_SECRET)
            kwargs["on_mfa_totp_activated"](ROTATION.rotated_at)
            kwargs["on_mfa_recovery_code"](ROTATION.recovery_code)
            self.events.append("old-enrollment")
            return True, {
                "access_token": "old-access",
                "mfa_enrollment": {
                    "totp_secret": NEW_SECRET,
                    "recovery_code": ROTATION.recovery_code,
                    "rotated_at": ROTATION.rotated_at,
                },
            }

        self.old.login_existing_account_and_get_session.side_effect = login
        result = self.run_access()
        self.assertTrue(result.success, result.error_message)
        self.assertEqual(self.events, ["old-enrollment", "logout", "fresh-login"])
        self.manager.rotate.assert_not_called()

    def test_retry_with_saved_mfa_retries_logout_without_rotating_again(self):
        self.engine.extra_config[
            "chatgpt_existing_account_skip_managed_mfa_rotation"
        ] = True
        self.engine.email_info = {
            "chatgpt_mfa_managed": True,
            "totp_secret": NEW_SECRET,
        }
        self.engine.totp_secret = NEW_SECRET
        stage_chatgpt_mfa_rotation(EMAIL, NEW_SECRET)
        mark_chatgpt_mfa_rotation_activated(EMAIL, require_device_logout=True)
        self.manager.logout_all_devices.side_effect = lambda: self.events.append(
            "logout"
        )
        result = self.run_access()
        self.assertTrue(result.success)
        self.assertEqual(self.events, ["old-login", "logout", "fresh-login"])
        self.manager.rotate.assert_not_called()

    def test_mandatory_oauth_enrollment_discards_tokens_and_relogs_once(self):
        self.engine.extra_config["chatgpt_existing_account_rotate_mfa"] = False
        first = self.oauth_client("old")
        second = self.oauth_client("fresh")
        first.last_mfa_enrollment = {
            "totp_secret": NEW_SECRET,
            "recovery_code": ROTATION.recovery_code,
            "rotated_at": ROTATION.rotated_at,
        }

        def first_login(*args, **kwargs):
            kwargs["on_mfa_totp_staged"](NEW_SECRET)
            kwargs["on_mfa_totp_activated"](ROTATION.rotated_at)
            kwargs["on_mfa_recovery_code"](ROTATION.recovery_code)
            self.events.append("oauth-enrollment")
            return {"access_token": "old-access", "refresh_token": "old-refresh"}

        first.login_and_get_tokens.side_effect = first_login
        self.engine._build_oauth_client = mock.Mock(side_effect=[first, second])
        result = self.run_refresh()
        self.assertTrue(result.success, result.error_message)
        self.assertEqual(self.events, ["oauth-enrollment", "logout", "fresh-oauth"])
        self.assertEqual(result.refresh_token, "fresh-refresh")
        self.manager.rotate.assert_not_called()
        self.engine._build_chatgpt_client.assert_not_called()

    def test_pending_logout_survives_wal_promotion_with_rotation_switch_off(self):
        stage_chatgpt_mfa_rotation(EMAIL, NEW_SECRET)
        mark_chatgpt_mfa_rotation_activated(EMAIL, require_device_logout=True)
        finalize_chatgpt_mfa_rotation(EMAIL)
        self.assertTrue(load_chatgpt_device_logout(EMAIL))
        self.engine.extra_config["chatgpt_existing_account_rotate_mfa"] = False
        self.engine.totp_secret = NEW_SECRET
        self.manager.logout_all_devices.side_effect = lambda: self.events.append(
            "logout"
        )
        result = self.run_access()
        self.assertTrue(result.success)
        self.assertEqual(self.events, ["old-login", "logout", "fresh-login"])
        self.assertFalse(load_chatgpt_device_logout(EMAIL))
        self.assertTrue(load_chatgpt_device_logout(EMAIL, include_confirmed=True))
        self.manager.rotate.assert_not_called()

    def test_pending_logout_retained_on_failure(self):
        self.manager.logout_all_devices.side_effect = MfaRotationError("timeout")
        result = self.run_access()
        self.assertFalse(result.success)
        self.assertTrue(load_chatgpt_device_logout(EMAIL))

    def test_completed_logout_not_repeated_on_phone_retry(self):
        self.assertTrue(self.run_access().success)
        self.engine.extra_config[
            "chatgpt_existing_account_skip_managed_mfa_rotation"
        ] = True
        self.engine._build_chatgpt_client = mock.Mock(return_value=self.old)
        self.events.clear()
        result = self.run_access()
        self.assertTrue(result.success)
        self.assertEqual(self.events, ["old-login"])
        self.assertFalse(load_chatgpt_device_logout(EMAIL))

    def test_logout_completion_fences_new_mfa_generation(self):
        stage_chatgpt_mfa_rotation(EMAIL, NEW_SECRET)
        mark_chatgpt_mfa_rotation_activated(EMAIL, require_device_logout=True)
        previous = load_chatgpt_device_logout(EMAIL)
        mark_chatgpt_mfa_rotation_activated(EMAIL, require_device_logout=True)
        self.assertFalse(complete_chatgpt_device_logout(EMAIL, previous))
        self.assertTrue(load_chatgpt_device_logout(EMAIL))

    def test_confirmed_logout_survives_failed_login_and_retry_replaces_old_tokens(self):
        self.fresh.login_existing_account_and_get_session.side_effect = None
        self.fresh.login_existing_account_and_get_session.return_value = (
            False,
            "login failed",
        )
        result = self.run_access()
        self.assertFalse(result.success)
        self.assertFalse(load_chatgpt_device_logout(EMAIL))
        generation = load_chatgpt_device_logout(EMAIL, include_confirmed=True)
        self.assertTrue(generation)
        self.engine.extra_config["chatgpt_existing_account_rotate_mfa"] = False
        self.fresh = self.web_client("fresh")
        self.engine._build_chatgpt_client = mock.Mock(return_value=self.fresh)
        result = self.run_access()
        self.assertTrue(result.success)
        self.assertTrue(result.metadata["replace_session_credentials"])
        self.assertEqual(result.metadata["device_logout_generation"], generation)
        self.assertEqual(self.manager.logout_all_devices.call_count, 1)

    def test_configured_auth_database_is_used_for_activation_and_marker(self):
        other = create_engine("sqlite://")
        SQLModel.metadata.create_all(other)
        self.addCleanup(other.dispose)
        self.engine.extra_config["_chatgpt_auth_engine"] = other
        self.manager.logout_all_devices.side_effect = lambda: self.events.append(
            "logout"
        )
        result = self.run_access()
        self.assertTrue(result.success, result.error_message)
        self.assertTrue(
            load_chatgpt_device_logout(
                EMAIL, include_confirmed=True, database_engine=other
            )
        )
        self.assertFalse(
            load_chatgpt_device_logout(
                EMAIL, include_confirmed=True, database_engine=self.db
            )
        )
