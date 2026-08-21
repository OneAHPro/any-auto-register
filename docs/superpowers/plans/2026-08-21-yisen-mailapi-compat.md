# Yisen MailAPI Compatibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Import `@yisen.uk` `email----password----jwt` rows and retrieve OpenAI email OTPs through Yisen's authenticated mailbox API in production.

**Architecture:** Keep the existing `mailapi_url` account type and add a separate `mailapi_token` persistence field. Detect Yisen rows at the import boundary, propagate the secret through Outlook mailbox lifecycle paths, and add host-scoped authenticated polling plus explicit `results[]` parsing in `MailApiUrlOtpBackend`.

**Tech Stack:** Python 3.11, FastAPI, SQLModel/SQLite, requests, pytest/unittest.

---

### Task 1: Return the existing SMS baseline to green

**Files:**
- Modify: `platforms/chatgpt/refresh_token_registration_engine.py`
- Test: `tests/test_chatgpt_existing_account_login.py`

- [ ] **Step 1: Confirm the committed regression test fails**

Run: `.venv/bin/python -m pytest -q tests/test_chatgpt_existing_account_login.py::ExistingAccountLoginTests::test_sms_pool_request_enables_existing_phone_verification`

Expected: FAIL because `allow_phone_verification` remains false when `chatgpt_existing_account_sms_mode` is `pool`.

- [ ] **Step 2: Implement the missing pool-mode branch**

Update `_existing_account_phone_verification_enabled()` so the existing explicit boolean still wins, while a normalized SMS mode of `pool` also returns true:

```python
if self._as_bool(value):
    return True
sms_mode = str(
    self.extra_config.get("chatgpt_existing_account_sms_mode") or ""
).strip().lower()
return sms_mode == "pool"
```

- [ ] **Step 3: Verify the existing regression test passes**

Run the same test. Expected: PASS.

### Task 2: Parse and persist Yisen credentials

**Files:**
- Modify: `core/db.py`
- Modify: `services/mail_imports/microsoft_import_rules.py`
- Modify: `services/mail_imports/auto_detection.py`
- Modify: `services/mail_imports/providers.py`
- Test: `tests/test_mail_imports_auto_detection.py`
- Test: `tests/test_mail_imports_service.py`

- [ ] **Step 1: Add failing parser and strategy tests**

Cover a credential-safe fixture shaped like:

```python
row = "worker@yisen.uk----login-pass----" + matching_jwt
```

Assert automatic detection selects `microsoft/mailapi_url`, the parser builds the encoded `/api/mails` URL, the token is stored in `mailapi_token`, and no public detection or snapshot payload contains the token. Add mismatched-address and malformed-JWT rejection tests.

- [ ] **Step 2: Run the new tests and verify RED**

Run the exact new test node IDs. Expected: unresolved row, missing attribute, or missing database column failures.

- [ ] **Step 3: Add the native data model and parser**

Add `mailapi_token: str = ""` to `MicrosoftMailImportRecord` and `OutlookAccountModel`; add the SQLite `ALTER TABLE` and null normalization. Decode only the JWT payload segment without signature verification for structural routing, require the claim address to equal the imported email, and build:

```python
https://mail.yisen.uk/api/mails?login=<encoded>&limit=20&offset=0&lite=1
```

Store the JWT separately. Do not return it in `MailImportSnapshotItem` or import `meta`.

- [ ] **Step 4: Verify parser and strategy tests are GREEN**

Run: `.venv/bin/python -m pytest -q tests/test_mail_imports_auto_detection.py tests/test_mail_imports_service.py`

Expected: all pass.

### Task 3: Preserve the token through mailbox lifecycle

**Files:**
- Modify: `core/base_mailbox.py`
- Test: `tests/test_outlook_mailbox_oauth.py`
- Test: `tests/test_chatgpt_plugin.py`

- [ ] **Step 1: Add failing lifecycle tests**

Assert `_pop_account()`/`_mailbox_account_from_payload()` carries `mailapi_token` in `MailboxAccount.extra`, `requeue_account()` writes it back, and `commit_password_reset()` retains it when the claimed row is recreated disabled.

- [ ] **Step 2: Run lifecycle tests and verify RED**

Expected: the token is absent after claim/requeue/commit.

- [ ] **Step 3: Propagate `mailapi_token`**

Copy the field through payload, account extra, requeue update/insert, and password-reset update/insert. Log only `has_mailapi_token=<bool>`.

- [ ] **Step 4: Verify lifecycle tests are GREEN**

Run the new lifecycle node IDs plus `tests/test_chatgpt_plugin.py`.

### Task 4: Authenticate and parse Yisen mail responses

**Files:**
- Modify: `core/base_mailbox.py`
- Test: `tests/test_outlook_mailbox_oauth.py`

- [ ] **Step 1: Add failing request and parsing tests**

Use a synthetic JWT and response:

```json
{"count":1,"results":[{"id":7,"message_id":"mail-7","address":"worker@yisen.uk","metadata":"{\"subject\":\"Your OpenAI verification code\"}","source":"Your verification code is 246810","created_at":"2026-08-21T06:30:00Z"}]}
```

Assert the request uses `Authorization: Bearer <token>`, browser User-Agent,
`Accept: application/json, text/plain, */*`, and `x-lang: zh-CN`; assert parsing
returns code `246810`, timestamp, and stable message identity. Assert 403 logs
contain neither token nor email.

- [ ] **Step 2: Run new request/parser tests and verify RED**

Expected: missing headers and unrecognized `results` payload.

- [ ] **Step 3: Implement host-scoped Yisen polling**

Allow `_request_mailapi()` to receive headers. In `_fetch_mailapi_text()`, when
the host is `mail.yisen.uk`, require `mailapi_token` and pass the authenticated
browser headers. Extend `_parse_mailapi_message()` with a bounded `results`
branch that parses metadata JSON, filters OpenAI/ChatGPT subjects, requires a
six-digit code, sorts by parsed `created_at`, and returns the newest candidate.

- [ ] **Step 4: Verify request/parser tests are GREEN**

Run: `.venv/bin/python -m pytest -q tests/test_outlook_mailbox_oauth.py`

Expected: all pass.

### Task 5: Full verification, repository sync, and production deployment

**Files:**
- Verify all changed files and deployment state.

- [ ] **Step 1: Run focused compatibility suites**

```bash
.venv/bin/python -m pytest -q \
  tests/test_mail_imports_auto_detection.py \
  tests/test_mail_imports_service.py \
  tests/test_outlook_mailbox_oauth.py \
  tests/test_chatgpt_plugin.py \
  tests/test_chatgpt_existing_account_login.py
```

Expected: zero failures.

- [ ] **Step 2: Run the complete suite**

Run: `.venv/bin/python -m pytest -q`

Expected: zero failures, with only the repository's intentional skip.

- [ ] **Step 3: Commit and push safely**

Check `git diff --check`, inspect the secret scan, commit the implementation,
push `codex/yisen-mailapi-compat`, fetch `origin/main`, and fast-forward main
only if it still descends from `32e113f` plus this branch.

- [ ] **Step 4: Deploy atomically**

Wait for active tasks to finish, back up the shared SQLite database, upload the
Git archive into a new release directory, copy the existing non-Git `static/`
assets, preserve shared mail/data links, switch `current`, restart systemd, and
roll back automatically if `http://127.0.0.1:18081/` does not return 200.

- [ ] **Step 5: Import and probe one supplied mailbox**

Submit the ten rows through the production import service with auto-detection,
verify ten enabled `mailapi_url` records and secret presence only as booleans,
then run a read-only `get_current_ids()` probe on one record. Do not start an
account login task unless separately required.

- [ ] **Step 6: Final production checks**

Verify systemd active state, restart count, local/public HTTP 200, SQLite
`quick_check=ok`, deployed source hashes, and absence of recent tracebacks or
credential text.
