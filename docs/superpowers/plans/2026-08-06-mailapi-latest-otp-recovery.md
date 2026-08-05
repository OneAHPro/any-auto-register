# MailAPI Latest OTP Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reliably select the latest MailAPI OTP, preserve accounts on mailbox timeout, and test recovery of recently deleted accounts.

**Architecture:** Extend the existing MailAPI backend to enumerate legacy detail pages and extract codes from visible mail content. Keep the OAuth layer responsible for the total wait budget, make each mailbox call respect its requested slice, and treat OTP timeout as retryable rather than destructive.

**Tech Stack:** Python 3.10, FastAPI, SQLModel, pytest/unittest, requests.

---

### Task 1: MailAPI multi-message and semantic extraction

**Files:**
- Modify: `core/base_mailbox.py`
- Test: `tests/test_outlook_mailbox_oauth.py`

- [ ] Add a failing test whose legacy list contains a newer sign-in notification and a verification-code message; assert the backend follows both candidates and returns the OTP detail.
- [ ] Run `pytest -q tests/test_outlook_mailbox_oauth.py -k 'mailapi and (multiple or notification)'` and confirm the new test fails because only one legacy detail URL is returned.
- [ ] Add a failing test whose notification HTML contains a six-digit attribute value but no verification-code semantics; assert no code is returned.
- [ ] Run the focused test and confirm it fails with a false six-digit match.
- [ ] Implement `_find_legacy_mailapi_detail_urls()` returning ordered same-origin candidates, retain the singular wrapper for compatibility, and merge all candidates into `_find_mailapi_detail_urls()`.
- [ ] Make HTML message parsing expose visible card text for code extraction while keeping timestamp and stable message ID parsing.
- [ ] Run `pytest -q tests/test_outlook_mailbox_oauth.py` and confirm all tests pass.

### Task 2: Bounded per-call mailbox waiting

**Files:**
- Modify: `services/chatgpt_relogin.py`
- Test: `tests/test_chatgpt_relogin.py`

- [ ] Add a failing test with a 300-second total budget and a requested 30-second call; assert the mailbox receives no more than 30 seconds across foreground and background phases.
- [ ] Run `pytest -q tests/test_chatgpt_relogin.py -k 'background and timeout'` and confirm the call currently consumes the full remaining budget.
- [ ] Cap each call's phase budget at `min(requested_timeout, total_remaining)` and keep the existing concurrency-slot release around only that slice.
- [ ] Set saved-account OAuth OTP total wait and resend timing from the resolved mailbox timeout so delayed MailAPI delivery is not invalidated by an early resend.
- [ ] Run the focused tests and then `pytest -q tests/test_chatgpt_relogin.py`.

### Task 3: Preserve accounts on OTP timeout

**Files:**
- Modify: `api/tasks.py`
- Modify: `services/chatgpt_relogin.py`
- Test: `tests/test_chatgpt_relogin_task.py`
- Test: `tests/test_chatgpt_relogin.py`

- [ ] Replace the existing automatic-timeout deletion expectation with a failing regression test asserting `account_removed=false`, `deleted_account_count=0`, and a retryable failure log.
- [ ] Run the focused test and confirm the current `remove_on_mailbox_otp_timeout=True` path deletes the account.
- [ ] Disable timeout-driven removal for scheduled automation while retaining explicit deactivation removal.
- [ ] Change the fallback mailbox OTP timeout from 180 to 300 seconds and retain configuration overrides.
- [ ] Run both ChatGPT relogin test files and confirm deactivation deletion tests remain green.

### Task 4: Verification and deployment

**Files:**
- No new production files.

- [ ] Run `pytest -q tests/test_outlook_mailbox_oauth.py tests/test_chatgpt_relogin.py tests/test_chatgpt_relogin_task.py`.
- [ ] Run the broader backend suite excluding only the two documented local browser-installer tests.
- [ ] Build a release directory from the verified commit, copy the current static bundle, switch the `current` symlink, and restart `any-auto-register.service`.
- [ ] Verify `/api/auth/status`, `/running-tasks`, service health, and configured `mailbox_otp_timeout_seconds=300`.

### Task 5: Recent deletion recovery audit

**Files:**
- No repository changes; use one-off read-only/transactional server scripts.

- [ ] Inventory distinct `task_logs.status=removed` rows whose error is mailbox OTP timeout.
- [ ] Match each email to the newest backup or residual credential source without printing passwords, URLs, AT, RT, or OTP values.
- [ ] Run each recoverable account serially through `_login_with_saved_credentials()` in memory with redacted logging and no persistence/removal.
- [ ] Back up the live database before any restoration.
- [ ] Restore only successfully authenticated accounts, preserving mailbox context and fresh AT/RT, then verify default-list visibility and re-login eligibility.
- [ ] Report tested, authenticated, restored, and missing-credential counts separately.
