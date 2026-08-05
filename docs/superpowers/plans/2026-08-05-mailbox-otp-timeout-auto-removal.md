# Mailbox OTP Timeout Auto-Removal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove scheduled-maintenance accounts whose email OTP login exhausts a wait of at least 180 seconds without obtaining any candidate code.

**Architecture:** Classify the exact exhausted OTP result in `services/chatgpt_relogin.py`, represent it with a typed exception, and handle it inside the existing account-operation lock. `api/tasks.py` first completes and records every probe-only result, then releases login candidates as a separate phase; scheduled login candidates opt into timeout cleanup and consume the existing standard removed-account result.

**Tech Stack:** Python 3.10+, SQLModel/SQLite, FastAPI task runner, `unittest`.

---

### Task 1: Strict exhausted-OTP classification

**Files:**
- Modify: `services/chatgpt_relogin.py`
- Test: `tests/test_chatgpt_relogin.py`

- [ ] **Step 1: Write failing tests** proving an OTP-stage result with zero attempted codes, a 180-second budget, and at least 180 measured seconds raises `ChatGPTMailboxOTPTimeoutError`; prove attempted codes, shorter budgets, shorter elapsed time, and unrelated failures remain ordinary errors.
- [ ] **Step 2: Run focused tests and confirm they fail because the typed exception and classifier do not exist.**
- [ ] **Step 3: Add the typed exception and exact classifier, measure the saved-login attempt with `time.monotonic()`, and raise the typed exception only after the final adapter failure meets every boundary.**
- [ ] **Step 4: Run the focused tests and confirm they pass.**

### Task 2: Remove timed-out automatic-maintenance accounts

**Files:**
- Modify: `services/chatgpt_relogin.py`
- Modify: `api/tasks.py`
- Test: `tests/test_chatgpt_relogin.py`
- Test: `tests/test_chatgpt_relogin_task.py`

- [ ] **Step 1: Write failing tests** proving scheduled automation passes `remove_on_mailbox_otp_timeout=True`, a typed timeout removes the account and returns `account_removed`, and the default/manual path preserves the account.
- [ ] **Step 2: Run focused tests and confirm they fail on the missing flag and removal behavior.**
- [ ] **Step 3: Add the opt-in flag through the public and locked relogin functions, reuse the existing ordered removal service, return `removal_reason=mailbox_otp_timeout`, and make the task removal log generic enough for both deactivation and OTP timeout cleanup.**
- [ ] **Step 4: Run focused and module tests and confirm they pass.**

### Task 3: Enforce probe-before-login dispatch

**Files:**
- Modify: `api/tasks.py`
- Test: `tests/test_chatgpt_relogin_task.py`

- [ ] **Step 1: Write a failing concurrency-one test** with a login candidate before a healthy account in input order; assert the healthy result is logged before the login service is called.
- [ ] **Step 2: Run the focused test and confirm it fails because input ordering starts the login first.**
- [ ] **Step 3: Partition automatic account IDs into probe-only and login-candidate groups, order probe-only IDs first, and keep login-candidate jobs unsubmitted until every probe-only outcome is counted. This boundary must consume neither executor workers nor active login slots.**
- [ ] **Step 4: Run the focused test and the complete relogin-task module.**

### Task 4: Verify and deploy

**Files:**
- Verify all files above plus the existing mailbox/task runtime tests.

- [ ] **Step 1: Run the full affected unittest suite, `py_compile`, and `git diff --check`.**
- [ ] **Step 2: Commit the implementation.**
- [ ] **Step 3: Build a server release from the commit, run the same tests in that release, and compare checksums.**
- [ ] **Step 4: Recheck that no task is running, atomically switch `current`, restart the service with rollback protection, and verify local/public HTTP 200.**
