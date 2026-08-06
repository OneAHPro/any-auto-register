# ChatGPT Account Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Keep execution in the primary session; the operator requested no subagents.

**Goal:** Automatically give every successfully logged-in ChatGPT account a durable password and a locally stored TOTP MFA secret, then apply the same resumable process to every existing server account.

**Architecture:** Add a small OpenAI backend-API adapter and a database-backed hardening state machine. Future login paths invoke the state machine after token persistence; a dedicated task endpoint drives the same state machine for existing accounts with concurrency capped at two. Remote mutations are two-phase: stage the enrollment secret locally, confirm it remotely, then promote it to the normal login context.

**Tech Stack:** Python 3.12, FastAPI, SQLModel/SQLite, curl_cffi, unittest/mock, existing task runtime and ChatGPT OAuth/password-reset services.

---

## Task 1: Add the OpenAI MFA protocol adapter

**Files:**
- Create: `platforms/chatgpt/account_hardening.py`
- Create: `tests/test_chatgpt_account_hardening_protocol.py`

- [ ] Write failing tests for normalized Base32 secrets and RFC 6238 six-digit TOTP generation, including invalid input.
- [ ] Write failing HTTP-adapter tests for these current ChatGPT contracts:

```text
GET  https://chatgpt.com/backend-api/accounts/mfa_info
POST https://chatgpt.com/backend-api/accounts/mfa/enroll
     {"factor_type": "totp", "phone_number": null,
      "phone_verification_channel": null}
POST https://chatgpt.com/backend-api/accounts/mfa/user/activate_enrollment
     {"code": "123456", "factor_type": "totp", "session_id": "..."}
```

- [ ] Cover Bearer authorization, `OpenAI-Account-ID`, proxy forwarding, typed non-2xx failures, response validation, and secret-free exception text.
- [ ] Run `python -m unittest tests.test_chatgpt_account_hardening_protocol -v` and confirm the new tests fail because the module is absent.
- [ ] Implement `generate_totp`, `MFAInventory`, `MFAEnrollment`, `ChatGPTMFAClient.get_inventory`, `start_totp_enrollment`, and `activate_totp_enrollment` with injected transport/time for deterministic tests.
- [ ] Rerun the focused test and confirm it passes.
- [ ] Commit: `feat: add ChatGPT MFA enrollment protocol`

## Task 2: Add guarded hardening persistence

**Files:**
- Modify: `core/db.py`
- Create: `tests/test_chatgpt_account_hardening_persistence.py`

- [ ] Write failing tests for compare-and-swap account updates guarded by ID, platform, normalized email, `created_at`, and the previously read `updated_at`.
- [ ] Cover ownership acquisition, stale-owner rejection, staged `mfa_pending_secret`, promotion to `totp_secret`, and lock release after errors.
- [ ] Assert promotion writes `account_type=chatgpt_password_totp`, `mfa_hardening_status=ready`, and removes the pending secret in one SQL update.
- [ ] Assert valid tokens and the account row survive every hardening failure state.
- [ ] Run `python -m unittest tests.test_chatgpt_account_hardening_persistence -v` and confirm red.
- [ ] Implement `claim_chatgpt_account_hardening`, `update_chatgpt_account_hardening`, and `promote_chatgpt_mfa_secret` using guarded SQL updates.
- [ ] Rerun focused tests and confirm green.
- [ ] Commit: `feat: add guarded account hardening persistence`

## Task 3: Implement classification, password, MFA, and secret recovery

**Files:**
- Create: `services/chatgpt_account_hardening.py`
- Create: `tests/test_chatgpt_account_hardening_service.py`
- Modify: `core/applemail_pool.py`

- [ ] Write failing classification tests for `ready`, `needs_password`, `needs_mfa`, `recoverable_mfa`, `replacement_candidate`, and `missing_mfa_material`.
- [ ] Write failing password tests proving a unique `secrets` password is persisted only after the existing password-reset callback succeeds; mailbox loss records `pending_password` without clearing tokens.
- [ ] Write failing enrollment tests proving the returned secret is staged before remote activation, promoted only after activation, and resumed from `mfa_pending_secret` after a simulated crash.
- [ ] Write failing recovery tests that search, in order, account extras, `mailbox_login_context`, archived credential-pool records, attempt bindings, and operator-supplied SQLite backup paths by normalized email.
- [ ] Validate every candidate with the existing real MFA challenge callback; never accept a candidate by format alone.
- [ ] Cover already-enabled remote MFA with no mailbox/secret: retain the account and tokens and persist `missing_mfa_material`; do not invoke factor deletion.
- [ ] Run `python -m unittest tests.test_chatgpt_account_hardening_service -v` and confirm red.
- [ ] Implement `ChatGPTAccountHardeningService.harden_authenticated_account` and `harden_saved_account`, typed results/counters, candidate discovery, and redacted errors.
- [ ] Rerun the focused tests and confirm green.
- [ ] Commit: `feat: implement ChatGPT account hardening service`

## Task 4: Invoke hardening after every successful login

**Files:**
- Modify: `api/tasks.py`
- Modify: `services/chatgpt_relogin.py`
- Modify: `tests/test_register_task_controls.py`
- Modify: `tests/test_chatgpt_relogin.py`
- Modify: `tests/test_chatgpt_account_persistence.py`

- [ ] Add failing tests showing registration/existing-account login saves tokens first, then hardens the saved account.
- [ ] Add failing relogin tests showing fresh tokens remain committed when hardening reports `pending_password`, `missing_mfa_material`, or a transient error.
- [ ] Assert task details expose only `ready`, `hardening_pending`, or `missing_mfa_material`, without secrets.
- [ ] Assert a `ready` account's next login selects password + local TOTP and does not request mailbox OTP.
- [ ] Run the three focused test modules and confirm red.
- [ ] Add a post-persistence hardening hook to successful registration/login and relogin paths. Hardening exceptions become warnings and never roll back successful token persistence.
- [ ] Rerun the focused tests and confirm green.
- [ ] Commit: `feat: harden accounts after successful login`

## Task 5: Add the resumable existing-account batch task

**Files:**
- Modify: `api/tasks.py`
- Create: `tests/test_chatgpt_account_hardening_task.py`

- [ ] Write failing API/task tests for `POST /tasks/chatgpt-hardening` with `account_ids`, `dry_run`, and `concurrency` fields.
- [ ] Assert omitted IDs select all ChatGPT accounts, concurrency defaults to one and caps at two, and an active ChatGPT mutation task blocks conflicting starts.
- [ ] Assert counters include `total`, `ready_before`, `hardened`, `recovered_secret`, `pending_password`, `missing_mfa_material`, and `failed`.
- [ ] Assert completed account checkpoints survive restart and retry skips `ready` rows.
- [ ] Assert stop cancels undispatched accounts, safely completes/checkpoints an in-flight activation, and returns terminal `stopped`.
- [ ] Run `python -m unittest tests.test_chatgpt_account_hardening_task -v` and confirm red.
- [ ] Implement request model, task creation, sequential/bounded worker loop, progress/counter persistence, dry-run mode, and reuse existing stop control.
- [ ] Rerun the focused tests and confirm green.
- [ ] Commit: `feat: add resumable ChatGPT hardening batch`

## Task 6: Redact all new hardening secrets

**Files:**
- Modify: `api/accounts.py`
- Modify: `platforms/chatgpt/log_sanitizer.py`
- Modify: `tests/test_accounts_api_sanitization.py`
- Modify: `tests/test_chatgpt_log_sanitizer.py`

- [ ] Add failing tests containing `totp_secret`, `mfa_pending_secret`, enrollment `secret`, `otpauth://` URIs, activation codes, recovery codes, and bearer tokens in account extras, exceptions, task logs, and API payloads.
- [ ] Run both focused sanitizer modules and confirm red.
- [ ] Replace secret-valued API fields with boolean readiness metadata, extend structured-key and URI redaction, and sanitize task error snapshots before persistence.
- [ ] Rerun focused tests and confirm green.
- [ ] Commit: `fix: redact ChatGPT hardening secrets`

## Task 7: Run local regression and canary verification

**Files:**
- Modify only files required by failures found in this task.

- [ ] Run focused hardening regression:

```bash
python -m unittest \
  tests.test_chatgpt_account_hardening_protocol \
  tests.test_chatgpt_account_hardening_persistence \
  tests.test_chatgpt_account_hardening_service \
  tests.test_chatgpt_account_hardening_task \
  tests.test_chatgpt_relogin \
  tests.test_register_task_controls \
  tests.test_accounts_api_sanitization \
  tests.test_chatgpt_log_sanitizer -v
```

- [ ] Run `python -m unittest discover -s tests -p 'test_*.py'` and require exit code zero.
- [ ] Run `git diff --check`, inspect `git status --short`, and review every changed diff for secret leakage and accidental factor deletion.
- [ ] Commit any regression-only fixes separately.

## Task 8: Deploy, classify, canary, and batch all server accounts

**Files:**
- No repository edits unless deployment verification exposes a defect.

- [ ] Wait until all current server tasks are terminal.
- [ ] Create a timestamped production SQLite backup, run `PRAGMA integrity_check`, and record the pre-deployment account count.
- [ ] Build a new release from the verified commit, install dependencies, run focused server tests, and atomically switch `/www/any-auto-register/current` while retaining the prior release.
- [ ] Call the hardening task in `dry_run` mode for all ChatGPT accounts and record each classification counter.
- [ ] Select one account with a valid access token and mailbox fallback as the canary; run remote hardening for only that ID.
- [ ] Verify the canary row has a password, `totp_secret`, `account_type=chatgpt_password_totp`, `mfa_hardening_status=ready`, and no pending secret.
- [ ] Perform a second real login for the canary with mailbox access disabled and require password + local TOTP success.
- [ ] Start the all-account batch at concurrency one, monitor to a terminal state, retry transient failures once, and export a final summary of `ready`, `pending_password`, `missing_mfa_material`, and `failed` account IDs.
- [ ] Run post-batch database integrity check and create a second timestamped backup.
- [ ] Confirm scheduled probes prefer local TOTP and no longer wait for mailbox codes on `ready` accounts.
