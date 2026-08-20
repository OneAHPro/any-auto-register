# Password + MFA Relogin Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make email-plus-MailAPI accounts establish and persist a ChatGPT password and managed TOTP, then prefer password plus TOTP on later relogins.

**Architecture:** Keep the existing login engine and password-reset flow. Add narrow classification and credential-normalization helpers in the relogin and Codex2API health services, then verify persistence through existing metadata boundaries.

**Tech Stack:** Python, SQLModel, pytest/unittest, FastAPI service, systemd deployment.

---

### Task 1: Classify explicit remote token invalidation

**Files:**
- Modify: `services/chatgpt_codex2api_health.py`
- Test: `tests/test_chatgpt_codex2api_health.py`

- [x] Add a failing row-level test for `status=error` plus `refresh_token_invalidated`.
- [x] Confirm the test fails as `deferred`.
- [x] Classify only explicit credential markers as `auth_failed`.
- [x] Run the focused health suite.

### Task 2: Normalize legacy MailAPI credentials

**Files:**
- Modify: `services/chatgpt_relogin.py`
- Test: `tests/test_chatgpt_relogin.py`

- [x] Add a failing test showing a top-level saved password is promoted into a legacy `mailapi_url` login context with managed TOTP.
- [x] Add a failing test showing a passwordless legacy record forces the existing password-reset flow.
- [x] Implement minimal normalization and bootstrap selection.
- [x] Verify generated password and TOTP metadata survive persistence.

### Task 3: Regression and deployment

**Files:**
- Verify: `tests/test_chatgpt_codex2api_health.py`
- Verify: `tests/test_chatgpt_relogin.py`
- Verify: `tests/test_chatgpt_relogin_task.py`

- [x] Run focused and full ChatGPT regression suites.
- [x] Review the diff for secret exposure and unrelated edits.
- [ ] Commit and push the branch.
- [ ] Deploy a new immutable release, restart the service, and verify HTTP health.
- [ ] Inspect the next automation classification and login route on production.
