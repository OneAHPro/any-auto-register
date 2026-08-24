# Unified OAuth Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Recover expired OpenAI authorization sessions across manual login, automatic re-login, and post-MFA phone verification while preserving the provider-start safety gate.

**Architecture:** Make OAuth entry transactions per-attempt instead of shared across retries, restart the Web login state machine on transient 409 responses, and add one fresh credential-login fallback after same-session post-MFA phone preparation fails. Reduce automatic OAuth entry concurrency to three by default and cap it at three.

**Tech Stack:** Python 3.10+, OAuthClient, ChatGPTClient, SQLModel-backed task runtime, unittest/pytest.

---

### Task 1: Add failing OAuth transaction tests

**Files:**
- Modify: `tests/test_chatgpt_phone_flow.py`
- Modify: `tests/test_chatgpt_existing_account_login.py`

- [ ] **Step 1: Add a test proving each OAuth entry retry gets new PKCE/state values.** Mock the first `_submit_authorize_continue` to return a session-invalid 409 and the second to return a valid `FlowState`; assert the two calls receive different `state` and PKCE-derived authorize parameters.
- [ ] **Step 2: Add a test proving Web login restarts after transient 409.** Make the first helper submit return the session-invalid error and the second bootstrap return an authenticated state; assert the second helper/session is used and login succeeds.
- [ ] **Step 3: Run the focused tests and confirm they fail for the current implementation.**

Run: `PYTHONPATH=. .venv/bin/pytest -q tests/test_chatgpt_phone_flow.py tests/test_chatgpt_existing_account_login.py -k 'fresh_pkce or authorize_continue_409 or session_invalid_restart'`

### Task 2: Implement per-attempt OAuth transaction recreation

**Files:**
- Modify: `platforms/chatgpt/oauth_client.py`
- Modify: `platforms/chatgpt/chatgpt_client.py`

- [ ] **Step 1: Move PKCE/state/authorize parameter generation inside each non-prepared OAuth entry attempt.** Preserve the prepared-context path unchanged and keep the existing bounded six-attempt limit.
- [ ] **Step 2: On transient entry failure, clear the failed helper/session state and begin the next attempt with a new session, fingerprint, device id, PKCE verifier, state, and authorize URL.** Keep the final error redacted and stage-specific.
- [ ] **Step 3: In Web login, treat transient `_submit_authorize_continue` errors as bootstrap failures and restart the outer entry loop rather than returning immediately.** Preserve password-reset recovery and existing bounded limits.
- [ ] **Step 4: Run the Task 1 tests and the existing OAuth/Web-login focused tests.**

### Task 3: Add fresh-login fallback after post-MFA phone OAuth failure

**Files:**
- Modify: `platforms/chatgpt/refresh_token_registration_engine.py`
- Modify: `tests/test_chatgpt_existing_account_login.py`
- Modify: `tests/test_chatgpt_login_with_phone.py`

- [ ] **Step 1: Add a failing test where same-session phone preparation exhausts and a fresh Web login with the new managed MFA returns a valid `add_phone` resume context.** Assert the provider is not called before the context is ready.
- [ ] **Step 2: Extend the preparation helper to accept the email adapter and current credentials, then perform one fresh credential login fallback after same-session exhaustion.** Use `prepare_phone_oauth=True`, do not rotate MFA again, validate the returned context, and copy only the safe context/diagnostic fields to the result.
- [ ] **Step 3: Keep exhaustion partial and durable.** Save Access Token/MFA/account state, set `phone_oauth_ready=False`, and prevent LeadBee/SMS startup.
- [ ] **Step 4: Run phone and login focused suites.**

### Task 4: Bound automatic OAuth entry concurrency

**Files:**
- Modify: `services/chatgpt_auto_relogin.py`
- Modify: `tests/test_chatgpt_auto_relogin.py`

- [ ] **Step 1: Add a failing settings test showing the default and upper bound are three.**
- [ ] **Step 2: Set `DEFAULT_CONCURRENCY=3` and `MAX_CONCURRENCY=3`; retain configurable values in the range 1–3.**
- [ ] **Step 3: Run automation settings and task scheduling tests.**

### Task 5: Verify, commit, deploy, and check the 20-account manifest

**Files:**
- No additional production files.

- [ ] **Step 1: Run compile checks, `git diff --check`, focused suites, and the complete test suite.**
- [ ] **Step 2: Review the diff for secret leakage, provider ordering, stale-context reuse, and retry bounds.**
- [ ] **Step 3: Commit and push `main`; create a production SQLite backup, install an immutable release, restart only as needed, and verify HTTP health.**
- [ ] **Step 4: Check the 20-account manifest before and after deployment.** Record per-account status only as `login_ok`, `mfa_ok`, `phone_oauth_ready`, `refresh_token_ok`, `codex2api_ok`, or a redacted stage/error.
