# ChatGPT Staged Account Login Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add batch automatic email-OTP login that saves AT first, followed by interactive per-account phone verification that saves RT.

**Architecture:** The existing task runner remains the batch orchestration surface. A Web-session login method produces AT-only results, while a new in-memory phone verification manager keeps one OAuth flow alive across send/resend/submit HTTP requests and merges RT only after success.

**Tech Stack:** FastAPI, SQLModel, Python threading primitives, React 19, Ant Design, TypeScript, Vitest.

---

### Task 1: AT-only existing-account login

**Files:**
- Modify: `platforms/chatgpt/chatgpt_client.py`
- Modify: `platforms/chatgpt/refresh_token_registration_engine.py`
- Modify: `platforms/chatgpt/chatgpt_registration_mode_adapter.py`
- Modify: `platforms/chatgpt/plugin.py`
- Test: `tests/test_chatgpt_existing_account_login.py`

- [ ] Add failing tests proving the access-token stage succeeds without RT and retains mailbox metadata.
- [ ] Run `python -m unittest tests.test_chatgpt_existing_account_login` and confirm the new tests fail for missing staged-login behavior.
- [ ] Add the Web-session existing-account login state machine and AT-only result mapping.
- [ ] Re-run the focused tests and confirm they pass.

### Task 2: Interactive phone verification backend

**Files:**
- Create: `services/chatgpt_phone_verification.py`
- Modify: `platforms/chatgpt/oauth_client.py`
- Modify: `api/chatgpt.py`
- Modify: `main.py` only if router wiring requires it
- Test: `tests/test_chatgpt_phone_verification.py`
- Test: `tests/test_chatgpt_phone_flow.py`

- [ ] Add failing tests for E.164 validation, single active session, send/resend, retryable invalid OTP, successful RT persistence, and AT preservation on failure.
- [ ] Run the focused tests and confirm failures describe the missing service and broker integration.
- [ ] Implement the broker, TTL session manager, persisted mailbox adapter, OAuthClient interactive add-phone branch, and API endpoints.
- [ ] Re-run focused tests until green.

### Task 3: Automatic status refresh and task wording

**Files:**
- Create: `services/chatgpt_account_refresh.py`
- Modify: `api/tasks.py`
- Modify: `services/chatgpt_phone_verification.py`
- Test: `tests/test_chatgpt_phone_verification.py`
- Test: `tests/test_chatgpt_existing_account_login.py`

- [ ] Add failing tests proving login success triggers a status refresh and login tasks use login wording.
- [ ] Implement shared account refresh and invoke it after both AT and RT persistence.
- [ ] Run focused tests and confirm they pass.

### Task 4: Frontend login task flow

**Files:**
- Create: `frontend/src/lib/chatgptStagedLogin.ts`
- Create: `frontend/src/lib/chatgptStagedLogin.test.ts`
- Modify: `frontend/src/pages/Accounts.tsx`
- Modify: `frontend/package.json`
- Modify: `frontend/package-lock.json`

- [ ] Install Vitest and add the frontend test script.
- [ ] Add failing tests for pool-count parsing, login task payload generation, and phone-action visibility.
- [ ] Implement the top login button/modal, pool count loading, task submission and task log display.
- [ ] Run `npm test -- --run` in `frontend` and confirm green.

### Task 5: Frontend phone verification flow

**Files:**
- Create: `frontend/src/components/ChatGPTPhoneVerificationModal.tsx`
- Create: `frontend/src/components/ChatGPTPhoneVerificationModal.test.tsx`
- Modify: `frontend/src/pages/Accounts.tsx`

- [ ] Add failing component tests for invalid phone, send loading, countdown/resend and successful submit.
- [ ] Implement the modal and place the conditional “接码” button before “详情”.
- [ ] Run frontend tests, lint and build.

### Task 6: Full verification

**Files:**
- Verify all modified files.

- [ ] Run the complete Python unit test suite.
- [ ] Run frontend Vitest, ESLint and production build.
- [ ] Rebuild/restart the local Docker service if required for browser verification.
- [ ] Verify the login modal and conditional phone modal at `http://127.0.0.1:18080/accounts/chatgpt`.
- [ ] Review the diff against every requirement and request code review.
