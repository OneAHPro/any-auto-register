# ChatGPT OAuth Session Reuse Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reuse the successful email-login OAuth browser context during phone verification so the normal path does not request a second email OTP.

**Architecture:** A bounded TTL cache owns one-time browser contexts keyed by normalized email. The phone flow adopts a cached Session and fingerprint, starts a fresh PKCE transaction, and skips email submission only when `/oauth/authorize` already lands on an authenticated downstream state; otherwise it falls back to the existing passwordless login.

**Tech Stack:** Python 3.12, curl_cffi, unittest, FastAPI service layer

---

### Task 1: Short-lived browser-context cache

**Files:**
- Create: `platforms/chatgpt/oauth_resume_cache.py`
- Test: `tests/test_chatgpt_oauth_resume.py`

- [ ] Write tests proving email normalization, one-time take, and TTL expiry.
- [ ] Run `python tests/test_chatgpt_oauth_resume.py -q` and confirm the cache tests fail before the module exists.
- [ ] Implement a locked `OAuthResumeContextCache` storing Session and fingerprint fields for 30 minutes.
- [ ] Re-run the focused test and confirm it passes.

### Task 2: Capture the completed email-login context

**Files:**
- Modify: `platforms/chatgpt/refresh_token_registration_engine.py`
- Test: `tests/test_chatgpt_existing_account_login.py`

- [ ] Add a failing test asserting a successful AT-only existing-account login remembers the `ChatGPTClient` Session and fingerprint under the email.
- [ ] Run the focused unittest and verify the expected missing call.
- [ ] Call the cache only after AT extraction succeeds; do not cache failed login attempts.
- [ ] Re-run the focused test.

### Task 3: Resume authenticated OAuth state

**Files:**
- Modify: `platforms/chatgpt/oauth_client.py`
- Test: `tests/test_chatgpt_phone_flow.py`

- [ ] Add a failing test where `/oauth/authorize` lands on `add-phone` with a resumed session and assert `_submit_authorize_continue` is not called.
- [ ] Add `resume_authenticated_session` to `login_and_get_tokens` and recognize only downstream authenticated page types.
- [ ] Preserve the existing retry path when the resumed Session is rejected or lands on a login page.
- [ ] Run the phone-flow test file.

### Task 4: Adopt cached context in phone verification

**Files:**
- Modify: `services/chatgpt_phone_verification.py`
- Test: `tests/test_chatgpt_phone_verification.py`

- [ ] Add failing tests for cache hit and cache miss.
- [ ] On hit, adopt the cached Session/fingerprint and call OAuth with `force_new_browser=False` and `resume_authenticated_session=True`.
- [ ] On miss, keep `force_new_browser=True` and the current passwordless fallback.
- [ ] Surface distinct progress messages through the broker.
- [ ] Run the focused service tests.

### Task 5: Verify and deploy

**Files:**
- Modify: running container `/app/platforms/chatgpt/*` and `/app/services/chatgpt_phone_verification.py`

- [ ] Run all staged-login and phone verification backend unittests under Python 3.12.
- [ ] Run frontend staged-login tests and production build to ensure no UI regression.
- [ ] Run `python -m compileall` and `git diff --check`.
- [ ] Copy changed production files into `any-auto-register`, restart it, and verify health/read-only account state without starting phone verification.
- [ ] Commit the updated container to `any-auto-register-local-app:latest`.
