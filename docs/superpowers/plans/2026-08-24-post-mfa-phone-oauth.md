# Post-MFA Phone OAuth Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild and validate phone OAuth immediately after managed MFA rotation so transient OpenAI session states do not become LeadBee task failures.

**Architecture:** Add one bounded same-session preparation helper to `RefreshTokenRegistrationEngine`. The access-token path skips the disposable pre-rotation transaction, rotates MFA, then calls the helper and persists its validated resume context. Existing phone-worker recovery remains a fallback.

**Tech Stack:** Python 3.10/3.11, unittest, pytest, SQLModel, existing OpenAI OAuth client.

---

### Task 1: Lock the post-rotation behavior with failing tests

**Files:**
- Modify: `tests/test_chatgpt_existing_account_login.py`

- [ ] Add a test where access-token login rotates MFA, the first fresh phone helper returns `None`, the second returns an `add_phone` resume context, and assert that the current implementation fails to make those preparation calls.
- [ ] Add a test where all bounded attempts return `None` and assert that result metadata reports `phone_oauth_ready=False` without invoking any phone provider.
- [ ] Run `PYTHONPATH=. .venv/bin/pytest -q tests/test_chatgpt_existing_account_login.py -k post_rotation_phone_oauth` and verify the success-path test fails because no post-rotation helper is called.

### Task 2: Rebuild the transaction in the authenticated session

**Files:**
- Modify: `platforms/chatgpt/refresh_token_registration_engine.py`

- [ ] Add `_prepare_phone_oauth_after_mfa_rotation(chatgpt_client, email)` that creates a fresh OAuth helper per attempt, adopts the authenticated browser context, calls `prepare_phone_verification_transaction`, records only safe diagnostics, and returns the first validated context.
- [ ] Pass `prepare_phone_oauth=False` to the initial web login only when managed rotation is planned.
- [ ] Replace the unconditional post-rotation context clearing with the new bounded same-session preparation.
- [ ] Run the focused tests and verify both success and exhaustion cases pass.

### Task 3: Verify phone-stage integration and recovery

**Files:**
- Test: `tests/test_chatgpt_phone_verification.py`
- Test: `tests/test_chatgpt_login_with_phone.py`

- [ ] Verify a ready post-rotation context is consumed without invoking `_rebuild_phone_oauth_context_with_fresh_login`.
- [ ] Verify exhausted preparation keeps `provider_started=false`, creates no LeadBee order, and returns `OPENAI_OAUTH_CONTEXT_NOT_READY` with bounded diagnostics.
- [ ] Run `PYTHONPATH=. .venv/bin/pytest -q tests/test_chatgpt_phone_verification.py tests/test_chatgpt_login_with_phone.py`.

### Task 4: Complete, review, deploy, and recover

**Files:**
- No additional production files expected.

- [ ] Run compile checks, `git diff --check`, targeted suites, and `PYTHONPATH=. .venv/bin/pytest -q`.
- [ ] Request independent review focused on session identity, provider-start ordering, secret leakage, and bounded retry behavior.
- [ ] Commit and push to `origin/main` after review is clean.
- [ ] Back up production SQLite, deploy an immutable release, and verify HTTP health.
- [ ] Retry partial accounts 1489/1490 and the latest retryable bindings. Confirm phone completion, Refresh Token persistence, Codex2API upload, database integrity, and one successful automatic monitor cycle.
