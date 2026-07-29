# ChatGPT Existing Account Login Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an explicit existing-account login path that skips signup and saves a ChatGPT account only when both Access Token and Refresh Token are obtained.

**Architecture:** A task-scoped `chatgpt_existing_account_login_only` flag is passed through the existing `extra` configuration. `RefreshTokenRegistrationEngine` branches immediately after loading the mailbox, calls the existing passwordless OAuth login client with `screen_hint=login`, validates both tokens, and then reuses the current result/account persistence path.

**Tech Stack:** Python 3.12, unittest, mock, FastAPI task API, Docker Compose.

---

### Task 1: Add existing-account login regression tests

**Files:**
- Create: `tests/test_chatgpt_existing_account_login.py`
- Test: `tests/test_chatgpt_existing_account_login.py`

- [ ] **Step 1: Write the failing login-only tests**

```python
import unittest
from unittest import mock

from platforms.chatgpt.refresh_token_registration_engine import (
    RefreshTokenRegistrationEngine,
)


class DummyEmailService:
    service_type = type("ServiceType", (), {"value": "microsoft"})()

    def create_email(self):
        return {"email": "existing@example.com", "service_id": "mailbox-1"}

    def get_verification_code(self, **kwargs):
        return "123456"


class ExistingAccountLoginTests(unittest.TestCase):
    def _make_engine(self, *, login_only=True):
        return RefreshTokenRegistrationEngine(
            email_service=DummyEmailService(),
            proxy_url="http://127.0.0.1:7890",
            callback_logger=lambda message: None,
            max_retries=1,
            extra_config={
                "chatgpt_existing_account_login_only": login_only,
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
```

- [ ] **Step 2: Run the new tests and verify RED**

Run: `python -m unittest tests.test_chatgpt_existing_account_login`

Expected: the two login-only tests fail because the flag is not implemented; the default-mode test passes.

### Task 2: Implement the login-only OAuth branch

**Files:**
- Modify: `platforms/chatgpt/refresh_token_registration_engine.py:149-650`
- Test: `tests/test_chatgpt_existing_account_login.py`

- [ ] **Step 1: Add flag parsing and mode-specific mailbox logs**

Add a `_existing_account_login_only()` method that accepts boolean and string truthy values. Change `_create_email()` to accept `existing_account_login_only=False`; in login-only mode log `正在加载 ... 邮箱凭据` and `邮箱凭据加载成功`, while retaining current registration wording in default mode.

```python
    def _existing_account_login_only(self) -> bool:
        value = self.extra_config.get("chatgpt_existing_account_login_only", False)
        if isinstance(value, bool):
            return value
        return str(value or "").strip().lower() in {"1", "true", "yes", "on"}
```

- [ ] **Step 2: Add a focused existing-account login helper**

Add `_login_existing_account(...)` that configures OAuth timeouts and calls:

```python
tokens = oauth_client.login_and_get_tokens(
    result.email,
    self.password or "",
    device_id="",
    user_agent=None,
    sec_ch_ua=None,
    impersonate=None,
    skymail_client=email_adapter,
    prefer_passwordless_login=True,
    allow_phone_verification=False,
    force_new_browser=True,
    force_chatgpt_entry=False,
    screen_hint="login",
    force_password_login=False,
    complete_about_you_if_needed=False,
    login_source="existing_account_login_only",
)
```

If the call returns no token dictionary, use `oauth_client.last_error`. If `access_token` or `refresh_token` is empty, return an error naming the missing token. Otherwise call `_populate_result_from_tokens()` with `registration_message="existing_account_login_only"`, `source="login"`, and `register_client=None`.

- [ ] **Step 3: Branch before registration profile generation**

In `run()`, resolve the flag before logging. After mailbox loading and `EmailServiceAdapter` construction, call `_login_existing_account()` and return its result. Do not generate random profile data and do not build `ChatGPTClient` in this branch.

- [ ] **Step 4: Keep saved metadata accurate**

In `_populate_result_from_tokens()`, set `metadata["registration_flow"]` to `"skipped_existing_account_login"` when `registration_message == "existing_account_login_only"`; retain `"chatgpt_client.register_complete_flow"` otherwise.

- [ ] **Step 5: Run tests and verify GREEN**

Run: `python -m unittest tests.test_chatgpt_existing_account_login tests.test_outlook_mailbox_oauth`

Expected: all new login-only tests and all Outlook OAuth tests pass.

### Task 3: Container verification and batch execution

**Files:**
- Verify: `platforms/chatgpt/refresh_token_registration_engine.py`
- Verify: `core/base_mailbox.py`

- [ ] **Step 1: Rebuild the local service**

Run: `docker compose -p any-auto-register-local -f docker-compose.yml -f docker-compose.local.yml up -d --build`

Expected: image builds, `any-auto-register` is recreated, and `http://127.0.0.1:18080/` returns HTTP 200.

- [ ] **Step 2: Run target tests inside the Python 3.12 container**

Run: `docker compose -p any-auto-register-local -f docker-compose.yml -f docker-compose.local.yml exec -T app python -m unittest tests.test_chatgpt_existing_account_login tests.test_outlook_mailbox_oauth`

Expected: all target tests pass.

- [ ] **Step 3: Run one existing mailbox**

POST `/api/tasks/register` with `count=1`, `concurrency=1`, `executor_type=protocol`, and `extra.chatgpt_existing_account_login_only=true`.

Expected: logs contain the existing-account login source, no registration-state-machine entry, OTP succeeds, both tokens are persisted, and the account count increases by one.

- [ ] **Step 4: Run the remaining mailboxes sequentially**

POST a second task with the remaining pool count, `concurrency=1`, and the same login-only flag. Stop on repeated upstream `/error`, Sentinel, or connection failures and re-import only unsuccessful mailboxes before retrying.

- [ ] **Step 5: Verify final state**

Confirm task counters, local ChatGPT account count, mailbox pool count, and token presence using counts/booleans only. Do not print email addresses or token values.
