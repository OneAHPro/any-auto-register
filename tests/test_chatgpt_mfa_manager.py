import types
import unittest
from unittest import mock

from sqlmodel import SQLModel, create_engine

from core.db import (
    finalize_chatgpt_mfa_rotation,
    load_chatgpt_mfa_rotation,
    mark_chatgpt_mfa_rotation_activated,
    stage_chatgpt_mfa_rotation,
    update_chatgpt_mfa_rotation_recovery_code,
)

from platforms.chatgpt.mfa_manager import (
    ChatGPTMfaManager,
    MfaRotationError,
    MfaRotationResult,
)
from platforms.chatgpt.refresh_token_registration_engine import (
    EmailServiceAdapter,
    RefreshTokenRegistrationEngine,
)


def _response(payload, *, status_code=200, text=""):
    return types.SimpleNamespace(
        status_code=status_code,
        text=text,
        json=lambda: payload,
    )


class ChatGPTMfaManagerTests(unittest.TestCase):
    def _manager(self, session, logs, **kwargs):
        return ChatGPTMfaManager(
            session=session,
            access_token="fixture-access-token",
            account_id="account-1",
            user_agent="fixture-agent",
            log_fn=logs.append,
            **kwargs,
        )

    def test_enrolls_totp_and_recovery_code_when_account_has_no_mfa(self):
        session = mock.Mock()
        session.get.side_effect = [
            _response({"mfa_enabled": False, "factors": []}),
            _response({"mfa_enabled": True, "factors": [{"type": "totp"}]}),
        ]
        session.post.side_effect = [
            _response({"state_token": "state-token"}),
            _response({"secret": "NEWBASE32SECRET"}),
            _response({"status": "success"}),
            _response({"secret": "RECOVERY-CODE"}),
            _response({"status": "success"}),
        ]
        logs = []

        with mock.patch(
            "platforms.chatgpt.mfa_manager.generate_totp",
            return_value="654321",
        ):
            result = self._manager(session, logs).rotate()

        self.assertEqual(result.totp_secret, "NEWBASE32SECRET")
        self.assertEqual(result.recovery_code, "RECOVERY-CODE")
        self.assertFalse(result.replaced_existing)
        self.assertTrue(result.mfa_enabled)
        self.assertEqual(
            session.post.call_args_list[0].args[0],
            "https://chatgpt.com/backend-api/accounts/mfa/user/request_mfa_token_in_house",
        )
        self.assertEqual(
            session.post.call_args_list[1].kwargs["json"],
            {"token": "state-token", "factor_type": "totp"},
        )
        self.assertEqual(
            session.post.call_args_list[2].kwargs["json"],
            {
                "code": "654321",
                "token": "state-token",
                "factor_type": "totp",
                "origin_app_name": "ChatGPT",
            },
        )
        self.assertNotIn("NEWBASE32SECRET", "\n".join(logs))
        self.assertNotIn("RECOVERY-CODE", "\n".join(logs))
        self.assertNotIn("654321", "\n".join(logs))

    def test_disables_existing_default_factor_before_enrolling_new_totp(self):
        session = mock.Mock()
        session.get.side_effect = [
            _response({
                "mfa_enabled": True,
                "native_default_factor_id": "old-factor",
                "factors": [{"id": "old-factor", "type": "totp"}],
            }),
            _response({"mfa_enabled": True, "factors": [{"type": "totp"}]}),
        ]
        session.post.side_effect = [
            _response({"ok": True}),
            _response({"state_token": "state-token"}),
            _response({"secret": "ROTATEDSECRET"}),
            _response({"status": "success"}),
            _response({"secret": "ROTATED-RECOVERY"}),
            _response({"status": "success"}),
        ]

        with mock.patch(
            "platforms.chatgpt.mfa_manager.generate_totp",
            return_value="123456",
        ):
            result = self._manager(
                session,
                [],
                can_recover_by_email=True,
            ).rotate()

        self.assertTrue(result.replaced_existing)
        self.assertEqual(
            session.post.call_args_list[0].args[0],
            "https://chatgpt.com/backend-api/accounts/mfa/user/disable_in_house",
        )
        self.assertEqual(
            session.post.call_args_list[0].kwargs["json"],
            {"factor_id": "old-factor"},
        )

    def test_refuses_destructive_replacement_without_email_recovery(self):
        session = mock.Mock()
        session.get.return_value = _response({
            "mfa_enabled": True,
            "factors": {
                "passkey": [{"id": "passkey-factor"}],
                "totp": [{"id": "totp-factor"}],
            },
        })

        with self.assertRaises(MfaRotationError) as captured:
            self._manager(session, []).rotate()

        self.assertIn("缺少可用的邮箱验证码恢复渠道", str(captured.exception))
        session.post.assert_not_called()

    def test_dict_factor_shape_replaces_totp_instead_of_default_passkey(self):
        session = mock.Mock()
        session.get.side_effect = [
            _response({
                "mfa_enabled": True,
                "native_default_factor_id": "passkey-factor",
                "factors": {
                    "passkey": [{"id": "passkey-factor"}],
                    "totp": [{"id": "totp-factor"}],
                },
            }),
            _response({"mfa_enabled": True, "factors": {"totp": [{}]}}),
        ]
        session.post.side_effect = [
            _response({"ok": True}),
            _response({"state_token": "state-token"}),
            _response({"secret": "ROTATEDSECRET"}),
            _response({"status": "success"}),
            _response({}, status_code=404),
        ]

        with mock.patch(
            "platforms.chatgpt.mfa_manager.generate_totp",
            return_value="123456",
        ):
            self._manager(
                session,
                [],
                can_recover_by_email=True,
            ).rotate()

        self.assertEqual(
            session.post.call_args_list[0].kwargs["json"],
            {"factor_id": "totp-factor"},
        )

    def test_existing_totp_can_be_replaced_with_local_totp_recovery(self):
        session = mock.Mock()
        session.get.side_effect = [
            _response({
                "mfa_enabled": True,
                "factors": {"totp": [{"id": "old-totp"}]},
            }),
            _response({"mfa_enabled": True, "factors": {"totp": [{}]}}),
        ]
        session.post.side_effect = [
            _response({"ok": True}),
            _response({"state_token": "state-token"}),
            _response({"secret": "NEWSECRET"}),
            _response({"status": "success"}),
            _response({}, status_code=404),
        ]

        with mock.patch(
            "platforms.chatgpt.mfa_manager.generate_totp",
            return_value="123456",
        ):
            result = self._manager(
                session,
                [],
                can_recover_by_existing_totp=True,
            ).rotate()

        self.assertTrue(result.replaced_existing)
        self.assertEqual(
            session.post.call_args_list[0].kwargs["json"],
            {"factor_id": "old-totp"},
        )

    def test_explicit_force_replaces_existing_totp_without_email_receiver(self):
        session = mock.Mock()
        session.get.side_effect = [
            _response({
                "mfa_enabled": True,
                "factors": {"totp": [{"id": "old-totp"}]},
            }),
            _response({"mfa_enabled": True, "factors": {"totp": [{}]}}),
        ]
        session.post.side_effect = [
            _response({"ok": True}),
            _response({"state_token": "state-token"}),
            _response({"secret": "NEWSECRET"}),
            _response({"status": "success"}),
            _response({}, status_code=404),
        ]

        with mock.patch(
            "platforms.chatgpt.mfa_manager.generate_totp",
            return_value="123456",
        ):
            result = self._manager(
                session,
                [],
                allow_unrecoverable_replacement=True,
            ).rotate()

        self.assertTrue(result.replaced_existing)
        self.assertEqual(
            session.post.call_args_list[0].kwargs["json"],
            {"factor_id": "old-totp"},
        )

    def test_keeps_activated_secret_when_final_status_is_eventually_consistent(self):
        session = mock.Mock()
        session.get.side_effect = [
            _response({"mfa_enabled": False, "factors": []}),
            _response({"mfa_enabled": False, "factors": []}),
        ]
        session.post.side_effect = [
            _response({"state_token": "state-token"}),
            _response({"secret": "ACTIVATEDSECRET"}),
            _response({"status": "success"}),
            _response({}, status_code=404),
        ]
        logs = []

        with mock.patch(
            "platforms.chatgpt.mfa_manager.generate_totp",
            return_value="123456",
        ):
            result = self._manager(session, logs).rotate()

        self.assertEqual(result.totp_secret, "ACTIVATEDSECRET")
        self.assertTrue(result.mfa_enabled)
        self.assertIn("状态复核尚未同步", "\n".join(logs))

    def test_falls_back_to_in_house_enrollment_protocol(self):
        session = mock.Mock()
        session.get.side_effect = [
            _response({"mfa_enabled": False, "factors": []}),
            _response({"mfa_enabled": True, "factors": {"totp": [{}]}}),
        ]
        session.post.side_effect = [
            _response({"error": {"code": "route_not_found"}}, status_code=404),
            _response({
                "secret": "INHOUSESECRET",
                "session_id": "enrollment-session",
            }),
            _response({"success": True}),
        ]

        with mock.patch(
            "platforms.chatgpt.mfa_manager.generate_totp",
            return_value="123456",
        ):
            result = self._manager(session, []).rotate()

        self.assertEqual(result.totp_secret, "INHOUSESECRET")
        self.assertEqual(result.recovery_code, "")
        self.assertEqual(
            session.post.call_args_list[1].args[0],
            "https://chatgpt.com/backend-api/accounts/mfa/enroll",
        )
        self.assertEqual(
            session.post.call_args_list[2].kwargs["json"],
            {
                "code": "123456",
                "factor_type": "totp",
                "session_id": "enrollment-session",
            },
        )

    def test_activation_failure_raises_redacted_stage_error(self):
        session = mock.Mock()
        session.get.return_value = _response({"mfa_enabled": False, "factors": []})
        session.post.side_effect = [
            _response({"state_token": "state-token"}),
            _response({"secret": "MUST-NOT-LEAK"}),
            _response(
                {
                    "error": {
                        "message": "bad code MUST-NOT-LEAK",
                        "code": "incorrect_code",
                    }
                },
                status_code=403,
                text="MUST-NOT-LEAK",
            ),
        ]
        logs = []

        with mock.patch(
            "platforms.chatgpt.mfa_manager.generate_totp",
            return_value="654321",
        ), self.assertRaises(MfaRotationError) as captured:
            self._manager(session, logs).rotate()

        detail = str(captured.exception)
        self.assertIn("[stage=mfa_rotate]", detail)
        self.assertIn("HTTP 403", detail)
        self.assertIn("incorrect_code", detail)
        self.assertNotIn("MUST-NOT-LEAK", detail)
        self.assertNotIn("654321", detail)
        self.assertNotIn("MUST-NOT-LEAK", "\n".join(logs))


class ChatGPTMfaCredentialPersistenceTests(unittest.TestCase):
    def test_rotation_journal_survives_activation_until_account_is_persisted(self):
        database_engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(database_engine)

        stage_chatgpt_mfa_rotation(
            "demo@example.com",
            "JOURNALSECRET",
            database_engine=database_engine,
        )
        staged = load_chatgpt_mfa_rotation(
            "demo@example.com",
            database_engine=database_engine,
        )
        self.assertEqual(staged["status"], "staged")
        self.assertEqual(staged["totp_secret"], "JOURNALSECRET")

        mark_chatgpt_mfa_rotation_activated(
            "demo@example.com",
            rotated_at="2026-08-19T12:00:00+00:00",
            database_engine=database_engine,
        )
        update_chatgpt_mfa_rotation_recovery_code(
            "demo@example.com",
            "RECOVERY-CODE",
            database_engine=database_engine,
        )
        activated = load_chatgpt_mfa_rotation(
            "demo@example.com",
            database_engine=database_engine,
        )
        self.assertEqual(activated["status"], "activated")
        self.assertEqual(activated["recovery_code"], "RECOVERY-CODE")

        finalize_chatgpt_mfa_rotation(
            "demo@example.com",
            database_engine=database_engine,
        )
        self.assertEqual(
            load_chatgpt_mfa_rotation(
                "demo@example.com",
                database_engine=database_engine,
            ),
            {},
        )

    def test_engine_persists_rotated_credentials_and_uses_new_totp_immediately(self):
        email_service = mock.Mock()
        email_service.service_type.value = "chatgpt_credentials"
        email_service.commit_mfa_rotation.return_value = True
        engine = RefreshTokenRegistrationEngine(email_service)
        engine.email_info = {
            "email": "demo@example.com",
            "account_type": "chatgpt_password_url_otp",
            "password": "password",
            "totp_url": "https://supplier.example/totp",
        }
        adapter = EmailServiceAdapter(email_service, "demo@example.com", lambda _: None)
        result = MfaRotationResult(
            totp_secret="NEWLOCALSECRET",
            recovery_code="NEW-RECOVERY",
            replaced_existing=True,
            mfa_enabled=True,
            rotated_at="2026-08-19T12:00:00+00:00",
        )

        committed = engine._commit_mfa_rotation(adapter, result)

        self.assertTrue(committed)
        self.assertEqual(engine.totp_secret, "NEWLOCALSECRET")
        self.assertEqual(engine.email_info["totp_secret"], "NEWLOCALSECRET")
        self.assertNotIn("totp_url", engine.email_info)
        email_service.commit_mfa_rotation.assert_called_once_with(
            totp_secret="NEWLOCALSECRET",
            recovery_code="NEW-RECOVERY",
            rotated_at="2026-08-19T12:00:00+00:00",
        )

    def test_url_password_credentials_load_a_locally_managed_totp_secret(self):
        email_service = mock.Mock()
        email_service.service_type.value = "chatgpt_credentials"
        email_service.create_email.return_value = {
            "email": "demo@example.com",
            "account_type": "chatgpt_password_url_otp",
            "password": "password",
            "mail_api_url": "https://supplier.example/mail",
            "totp_url": "",
            "totp_secret": "MANAGEDSECRET",
        }
        engine = RefreshTokenRegistrationEngine(email_service)

        self.assertTrue(engine._create_email(existing_account_login_only=True))
        self.assertEqual(engine.totp_secret, "MANAGEDSECRET")


if __name__ == "__main__":
    unittest.main()
