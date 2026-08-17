# ChatGPT TOTP + MailAPI Relogin Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow a saved ChatGPT password + TOTP account to read a required email OTP through its optional MailAPI URL during relogin.

**Architecture:** Preserve the lightweight password/TOTP-only service when no receive URL exists. When a receive URL exists, build the existing AppleMail MailAPI backend and expose all three credentials through `_PersistedEmailService`, reusing its baseline and bounded polling behavior.

**Tech Stack:** Python 3.11, unittest, SQLModel, existing AppleMail/MailAPI mailbox abstraction.

---

### Task 1: Add the relogin regression test

**Files:**
- Modify: `tests/test_chatgpt_relogin.py`

- [ ] Add `test_password_totp_with_mail_url_reads_email_otp_during_relogin`, using a mocked AppleMail mailbox whose baseline is empty and whose `wait_for_code` returns `123456`.
- [ ] Assert that the constructed service exposes `password`, `totp_secret`, and `mail_api_url`, then returns `123456` from `get_verification_code`.
- [ ] Run `.venv/bin/python -m pytest tests/test_chatgpt_relogin.py::ChatGPTReloginTests::test_password_totp_with_mail_url_reads_email_otp_during_relogin -q` and confirm it fails because `_PasswordTotpEmailService` omits `mail_api_url`.

### Task 2: Route hybrid credentials through the persisted mailbox service

**Files:**
- Modify: `services/chatgpt_relogin.py`

- [ ] Preserve optional `mail_api_url` and `pool_file` when recovering password/TOTP credentials.
- [ ] Teach `_PersistedEmailService.create_email()` to expose password, local TOTP secret, and MailAPI URL for `chatgpt_password_totp` accounts.
- [ ] In `_build_email_service`, construct an AppleMail mailbox account and `_PersistedEmailService` only when the recovered record has a receive URL; otherwise retain `_PasswordTotpEmailService`.
- [ ] Bind task control and the configured OTP timeout to the hybrid service exactly as for existing URL credentials.
- [ ] Re-run the focused test and confirm it passes.

### Task 3: Verify, publish, and deploy

**Files:**
- Verify: `tests/test_chatgpt_relogin.py`
- Verify: `tests/test_chatgpt_existing_account_login.py`
- Verify: `tests/test_icloud_mailbox.py`

- [ ] Run the focused regression test and relevant backend test files.
- [ ] Review `git diff --check`, the final diff, and repository status.
- [ ] Commit the implementation and push `main` to `origin`.
- [ ] Deploy the pushed commit with the production compose configuration.
- [ ] Confirm the production container reports the new commit and passes its health check, then run the authorized failed-account relogin and inspect the resulting task log.

