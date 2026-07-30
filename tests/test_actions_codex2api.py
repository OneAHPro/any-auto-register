import unittest
from unittest import mock

from api.actions import _apply_action_result
from core.db import AccountModel


class Codex2APIActionPersistenceTests(unittest.TestCase):
    def test_manual_action_persists_codex2api_state(self):
        account = AccountModel(
            platform="chatgpt",
            email="demo@example.com",
            password="secret",
            status="registered",
        )
        session = mock.Mock()

        with mock.patch(
            "services.chatgpt_sync.update_account_model_codex2api_sync"
        ) as update_mock:
            _apply_action_result(
                "chatgpt",
                "upload_codex2api",
                account,
                {"ok": True, "data": "uploaded"},
                session,
            )

        update_mock.assert_called_once_with(
            account,
            True,
            "uploaded",
            session=session,
            commit=False,
        )

    def test_failed_action_persists_failure_message(self):
        account = AccountModel(
            platform="chatgpt",
            email="demo@example.com",
            password="secret",
            status="registered",
        )
        session = mock.Mock()

        with mock.patch(
            "services.chatgpt_sync.update_account_model_codex2api_sync"
        ) as update_mock:
            _apply_action_result(
                "chatgpt",
                "upload_codex2api",
                account,
                {"ok": False, "error": "denied"},
                session,
            )

        update_mock.assert_called_once_with(
            account,
            False,
            "denied",
            session=session,
            commit=False,
        )


if __name__ == "__main__":
    unittest.main()
