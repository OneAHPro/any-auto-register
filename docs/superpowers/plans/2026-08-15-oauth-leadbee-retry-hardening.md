# OAuth and LeadBee Retry Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Accept three/four-dash mail imports, recover phone OAuth without another email OTP, and make LeadBee failures observable and safely retryable.

**Architecture:** A shared delimiter parser removes duplicated string splitting. Authenticated browser snapshots and prepared OAuth transactions remain separate, allowing a one-time PKCE fallback without mailbox work. Provider diagnostics travel through the broker into existing task/binding JSON, while retries preserve order identity and billing safety.

**Tech Stack:** Python 3.11, FastAPI, SQLModel, curl_cffi, requests, unittest/pytest, React 19, TypeScript, Vitest.

---

### Task 1: Shared mail delimiter

**Files:**
- Create: `core/mail_import_delimiters.py`
- Modify: `services/mail_imports/auto_detection.py`
- Modify: `services/mail_imports/microsoft_import_rules.py`
- Modify: `core/applemail_pool.py`
- Modify: `services/mail_imports/providers.py`
- Modify: `frontend/src/components/settings/MailImportPanel.tsx`
- Test: `tests/test_mail_imports_auto_detection.py`
- Test: `tests/test_mail_imports_service.py`
- Test: `frontend/src/components/settings/MailImportPanel.test.tsx`

- [x] Add RED tests for three/four-dash mixed rows and embedded one/two-dash values.
- [x] Run the three focused test files and confirm failures are caused by four-dash-only parsing.
- [x] Implement `split_mail_import_fields()` and `mail_import_row_pattern()` around `(?<!-)-{3,4}(?!-)`.
- [x] Replace duplicated Python split/regex logic and update UI help text.
- [x] Run focused tests and retain existing Tab/JSON/four-dash behavior.

### Task 2: OAuth prebuild recovery

**Files:**
- Modify: `platforms/chatgpt/chatgpt_client.py`
- Modify: `platforms/chatgpt/oauth_client.py`
- Modify: `platforms/chatgpt/oauth_resume_cache.py`
- Modify: `platforms/chatgpt/refresh_token_registration_engine.py`
- Modify: `platforms/chatgpt/chatgpt_registration_mode_adapter.py`
- Modify: `services/chatgpt_phone_verification.py`
- Modify: `api/accounts.py`
- Modify: `api/tasks.py`
- Test: `tests/test_chatgpt_existing_account_login.py`
- Test: `tests/test_chatgpt_phone_flow.py`
- Test: `tests/test_chatgpt_login_with_phone.py`
- Test: `tests/test_accounts_api_sanitization.py`

- [x] Add RED tests proving `page=log_in` is retried after backoff, browser fallback skips email submit, and 14 concurrent contexts do not share state/session.
- [x] Persist a v1 authenticated browser snapshot independently of the v2 prepared transaction.
- [x] Stage prebuild attempts around Access Token completion and publish safe recovered/deferred diagnostics.
- [x] Restore the v1 snapshot and prepare one fresh PKCE transaction before the phone provider starts.
- [x] Remove task-level mailbox requeue as the normal OAuth prebuild fallback.
- [x] Sanitize both snapshot types from account API responses and run focused tests.

### Task 3: Safe provider diagnostics and request retry

**Files:**
- Modify: `platforms/chatgpt/leadbee_open_api.py`
- Modify: `platforms/chatgpt/phone_service.py`
- Modify: `platforms/chatgpt/oauth_client.py`
- Modify: `services/chatgpt_phone_verification.py`
- Modify: `api/tasks.py`
- Test: `tests/test_leadbee_open_api.py`
- Test: `tests/test_chatgpt_phone_flow.py`
- Test: `tests/test_chatgpt_phone_verification.py`
- Test: `tests/test_chatgpt_retry_bindings.py`

- [x] Add RED tests for GET 500/502/504, Retry-After, OpenAI send transport/429/5xx, invalid-phone replacement, malformed create reconciliation, and diagnostic redaction.
- [x] Add `GET /orders` and exact `client_order_id` reconciliation to the signed client.
- [x] Bound LeadBee retries to three attempts with exponential backoff and stable request identity.
- [x] Bound OpenAI send retries on the same phone/session and replace only explicit phone rejection.
- [x] Add broker whitelist diagnostics and persist only those fields in task/binding details.
- [x] Run focused provider and sanitization tests.

### Task 4: One safe account retry

**Files:**
- Modify: `api/tasks.py`
- Test: `tests/test_chatgpt_login_with_phone.py`
- Test: `tests/test_chatgpt_retry_bindings.py`

- [x] Add RED tests for one retry after `CANCELED/RELEASED`, no retry for `CAPTURED`/ambiguous state, and ref persistence before provider start.
- [x] Gate retry on the safe diagnostic tuple and `phone_verified=false`.
- [x] Persist a fresh `aar_<uuid>` before capacity/OAuth/provider execution.
- [x] Limit automatic account retry to one and preserve the manual retry binding on final failure.
- [x] Run task routing and retry-binding tests.

### Task 5: Verification, commit, deploy, live regression

**Files:**
- Verify all modified files and deployment scripts/process only.

- [x] Run the focused backend suite from the task prompt.
- [x] Run `PYTHONPATH="$PWD" /tmp/any-auto-register-py311/bin/python -m pytest -q`.
- [x] Run frontend tests, `npm run build`, compileall, diff check and credential scan.
- [ ] Commit on `codex/oauth-leadbee-retry-hardening`, push, and fast-forward `main` only after clean verification.
- [ ] Confirm production has no pending/running task, back up the database, publish an immutable release, atomically switch, restart and health-check.
- [ ] Retry the two failed bindings from `task_3e91c78a80654eb8a7dd53b1affef754`; verify old/new order settlement and zero leaked secrets.
- [ ] Roll back the symlink and service if health, logs, billing, or controlled regression fail.
