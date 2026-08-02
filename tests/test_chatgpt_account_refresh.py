import unittest
from unittest import mock


class _Account:
    id = 11
    platform = "chatgpt"
    email = "existing@example.com"
    token = "access-token"
    user_id = "account-1"
    updated_at = None

    def __init__(self):
        self.extra = {
            "access_token": "access-token",
            "refresh_token": "",
            "proxy_used": "http://127.0.0.1:7890",
        }

    def get_extra(self):
        return dict(self.extra)

    def set_extra(self, value):
        self.extra = dict(value)


class _Session:
    def __init__(self, account):
        self.account = account
        self.added = []
        self.committed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def get(self, model, account_id):
        return self.account if account_id == self.account.id else None

    def add(self, account):
        self.added.append(account)

    def commit(self):
        self.committed = True


class ChatGPTAccountRefreshTests(unittest.TestCase):
    def test_refresh_persists_probe_and_applies_status_policy(self):
        from services.chatgpt_account_refresh import refresh_chatgpt_account_by_id

        account = _Account()
        db_session = _Session(account)
        probe = {
            "auth": {"state": "access_token_valid"},
            "subscription": {"plan": "free"},
        }

        with mock.patch(
            "services.chatgpt_account_refresh.Session",
            return_value=db_session,
        ), mock.patch(
            "services.chatgpt_account_refresh.probe_local_chatgpt_status",
            return_value=probe,
        ) as probe_mock, mock.patch(
            "services.chatgpt_account_refresh.apply_chatgpt_status_policy",
        ) as policy_mock:
            result = refresh_chatgpt_account_by_id(11)

        self.assertEqual(result, probe)
        self.assertEqual(account.extra["chatgpt_local"], probe)
        probe_mock.assert_called_once()
        self.assertEqual(
            probe_mock.call_args.kwargs["proxy"],
            "http://127.0.0.1:7890",
        )
        policy_mock.assert_called_once_with(account, local_probe=probe)
        self.assertTrue(db_session.committed)


class LoginTaskWordingTests(unittest.TestCase):
    def test_existing_account_task_uses_login_wording(self):
        from api.tasks import RegisterTaskRequest, _task_action_terms

        request = RegisterTaskRequest(
            platform="chatgpt",
            count=2,
            extra={"chatgpt_existing_account_login_only": True},
        )

        self.assertEqual(_task_action_terms(request), ("登录", "登录"))

    def test_registration_task_keeps_registration_wording(self):
        from api.tasks import RegisterTaskRequest, _task_action_terms

        request = RegisterTaskRequest(platform="chatgpt", count=2, extra={})

        self.assertEqual(_task_action_terms(request), ("注册", "注册"))

    def test_saved_existing_login_triggers_status_refresh(self):
        from api.tasks import RegisterTaskRequest, _refresh_saved_chatgpt_login

        request = RegisterTaskRequest(
            platform="chatgpt",
            extra={"chatgpt_existing_account_login_only": True},
        )
        saved_account = mock.Mock(id=22)

        with mock.patch(
            "services.chatgpt_account_refresh.refresh_chatgpt_account_by_id",
            return_value={"auth": {"state": "access_token_valid"}},
        ) as refresh_mock:
            message = _refresh_saved_chatgpt_login(request, saved_account)

        refresh_mock.assert_called_once_with(22)
        self.assertIn("状态刷新完成", message)


if __name__ == "__main__":
    unittest.main()
