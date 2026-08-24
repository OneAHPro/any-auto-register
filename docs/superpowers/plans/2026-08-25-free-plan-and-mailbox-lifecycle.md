# Free Plan and Mailbox Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop Free accounts after login and make claimed/failed imported mailboxes visible and recoverable.

**Architecture:** Add a lightweight subscription probe to the existing-account engine and a typed Free skip to the plugin/task layer. Extend AppleMail and Outlook lifecycle states and expose them through snapshots to the import UI.

**Tech Stack:** Python, FastAPI, SQLModel, React, Ant Design, pytest, Vitest.

---

### Task 1: Subscription gate

**Files:** `platforms/chatgpt/status_probe.py`, `platforms/chatgpt/refresh_token_registration_engine.py`, `platforms/chatgpt/plugin.py`, `api/tasks.py`, `tests/test_chatgpt_existing_account_login.py`

- [ ] Add a failing test where `free` prevents `_rotate_mfa_after_login`.
- [ ] Implement a backend-me-only subscription probe and typed skipped result.
- [ ] Consume the Free mailbox, avoid account persistence, and count `free_skipped` in the task summary.
- [ ] Run the existing-account and task-control suites.

### Task 2: Durable mailbox states

**Files:** `core/applemail_pool.py`, `core/base_mailbox.py`, `core/db.py`, `main.py`, `tests/test_icloud_mailbox.py`, `tests/test_outlook_mailbox_oauth.py`

- [ ] Add failing tests for visible claimed/failed records and hidden used records.
- [ ] Add failed/discard transitions and make failed records claimable.
- [ ] Persist redacted failure stage/task metadata and recover orphan claims after restart.
- [ ] Run AppleMail, Outlook, retry, and login suites.

### Task 3: Snapshot and UI

**Files:** `services/mail_imports/schemas.py`, `services/mail_imports/providers.py`, `frontend/src/components/settings/MailImportPanel.tsx`, corresponding tests.

- [ ] Add snapshot state/count fields and frontend tests for Available, Processing, and Login Failed.
- [ ] Disable deletion and selection for Processing records and add periodic refresh.
- [ ] Run frontend tests and production build.

### Task 4: Verify and deploy

- [ ] Run backend compile, full pytest, full Vitest, and frontend build.
- [ ] Review secret redaction, claim ownership, Free account cleanup, and provider ordering.
- [ ] Commit, push main, back up production SQLite/frontend, deploy immutable backend/frontend releases, and run real Free/failed lifecycle smoke checks.
