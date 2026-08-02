# ChatGPT Auto Relogin Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run a persisted, non-overlapping full ChatGPT relogin and Codex2API replacement task every 30 minutes with manual tasks taking priority and no-account auto-pause.

**Architecture:** A focused automation service owns schedule state and eligibility, the existing Scheduler calls its non-blocking tick, and a shared task gate prevents automatic work from overlapping foreground ChatGPT tasks. Manual and automatic callers share one relogin enqueue function. React settings expose configuration and read-only runtime status.

**Tech Stack:** Python 3.12, FastAPI, SQLModel/SQLite, threading, React 19, TypeScript, Ant Design, pytest/unittest, Vitest.

---

### Task 1: Persisted automation configuration and status API

**Files:**
- Create: `services/chatgpt_auto_relogin.py`
- Create: `api/automations.py`
- Modify: `api/config.py`
- Modify: `main.py`
- Test: `tests/test_chatgpt_auto_relogin.py`

- [ ] **Step 1: Write failing tests** for defaults (`enabled=False`, interval `30`, concurrency `10`), interval validation `20..1440`, concurrency validation `1..10`, and the status response shape.
- [ ] **Step 2: Run** `pytest -q tests/test_chatgpt_auto_relogin.py` and verify failure because the service/router do not exist.
- [ ] **Step 3: Implement** `AutoReloginSettings`, strict normalizers, internal state keys, `get_status()`, and `GET /automations/chatgpt-relogin`; add `chatgpt_auto_relogin_enabled`, `chatgpt_auto_relogin_interval_minutes`, and `chatgpt_auto_relogin_concurrency` to the config whitelist and validate them in `PUT /config`.
- [ ] **Step 4: Run** `pytest -q tests/test_chatgpt_auto_relogin.py` and verify all Task 1 tests pass.

### Task 2: Eligibility and no-account pause/resume

**Files:**
- Modify: `services/chatgpt_relogin.py`
- Modify: `services/chatgpt_auto_relogin.py`
- Test: `tests/test_chatgpt_auto_relogin.py`
- Test: `tests/test_chatgpt_relogin.py`

- [ ] **Step 1: Write failing tests** proving password/MFA, saved mailbox context, and Outlook fallback accounts are eligible; accounts without any usable login path are excluded; zero eligible accounts returns `paused_no_accounts`; adding one eligible account schedules the first run for `now + interval`.
- [ ] **Step 2: Run** `pytest -q tests/test_chatgpt_auto_relogin.py tests/test_chatgpt_relogin.py` and verify the new assertions fail for missing eligibility APIs.
- [ ] **Step 3: Implement** a side-effect-free `is_saved_chatgpt_account_relogin_eligible()`/`list_relogin_eligible_account_ids()` using the same credential rules as `_load_saved_account`, and use it in automation status/tick without logging credentials.
- [ ] **Step 4: Re-run** the two test files and verify they pass.

### Task 3: Shared enqueue path and scheduled task metadata

**Files:**
- Modify: `api/tasks.py`
- Modify: `services/chatgpt_auto_relogin.py`
- Test: `tests/test_chatgpt_relogin_task.py`
- Test: `tests/test_chatgpt_auto_relogin.py`

- [ ] **Step 1: Write failing tests** for `enqueue_chatgpt_relogin_task(account_ids, concurrency, source, automation)` used by both the API and scheduler; assert automatic tasks use `source="schedule"`, `meta.mode="relogin"`, `meta.automation=True`, and concurrency `10`.
- [ ] **Step 2: Run** `pytest -q tests/test_chatgpt_relogin_task.py tests/test_chatgpt_auto_relogin.py` and verify the public enqueue contract is missing.
- [ ] **Step 3: Extract** the existing task creation/background launch into the shared enqueue function while preserving the manual route response; make the automation service inject this function so service tests do not import FastAPI route state.
- [ ] **Step 4: Re-run** the tests and verify manual behavior and automatic metadata both pass.

### Task 4: Foreground priority and non-overlap gate

**Files:**
- Create: `core/chatgpt_task_gate.py`
- Modify: `api/tasks.py`
- Modify: `services/chatgpt_auto_relogin.py`
- Test: `tests/test_chatgpt_task_gate.py`
- Test: `tests/test_chatgpt_auto_relogin.py`

- [ ] **Step 1: Write failing concurrency tests** showing: auto enters only with no foreground work; a foreground waiter requests the auto stop callback; foreground execution waits until auto releases; multiple foreground tasks may coexist; a waiting foreground task prevents a new auto task.
- [ ] **Step 2: Run** `pytest -q tests/test_chatgpt_task_gate.py` and verify failure because the gate is absent.
- [ ] **Step 3: Implement** a `threading.Condition`-based gate with `try_enter_automation`, `leave_automation`, `enter_foreground`, and `leave_foreground`; wrap scheduled relogin execution and ChatGPT register/login/manual-relogin background execution. Append a visible waiting log before a foreground task blocks.
- [ ] **Step 4: Run** the gate and task tests, verify no deadlock, and verify stop controls remain responsive.

### Task 5: Scheduler integration and persisted cadence

**Files:**
- Modify: `core/scheduler.py`
- Modify: `services/chatgpt_auto_relogin.py`
- Test: `tests/test_chatgpt_auto_relogin.py`
- Test: `tests/test_scheduler.py`

- [ ] **Step 1: Write failing clock-controlled tests** for startup delay, execution at 30 minutes, no duplicate overdue catch-up, active-task deferral, state persistence across a new service instance, and automatic resume after the account pool changes from zero to nonzero.
- [ ] **Step 2: Run** `pytest -q tests/test_chatgpt_auto_relogin.py tests/test_scheduler.py` and verify the scheduler never invokes the automation tick.
- [ ] **Step 3: Implement** a lightweight `tick(now)` call in the existing minute loop. Launch relogin work on a daemon thread, persist last/next timestamps, and keep the scheduler thread free for trial/CPA maintenance.
- [ ] **Step 4: Re-run** scheduler and automation tests and verify deterministic passing output.

### Task 6: Codex2API settings UI and automation status

**Files:**
- Create: `frontend/src/components/settings/ChatGPTAutoReloginSection.tsx`
- Create: `frontend/src/components/settings/ChatGPTAutoReloginSection.test.tsx`
- Modify: `frontend/src/pages/Settings.tsx`

- [ ] **Step 1: Write failing Vitest tests** for disabled/default state, interval min 20/default 30, concurrency max/default 10, PUT payload, and `paused_no_accounts` text.
- [ ] **Step 2: Run** `npm test -- --run src/components/settings/ChatGPTAutoReloginSection.test.tsx` from `frontend/` and verify the component is missing.
- [ ] **Step 3: Implement** the Ant Design card with Switch/InputNumber fields, status polling via `apiFetch('/automations/chatgpt-relogin')`, and integration into the Codex2API settings tab without changing Accounts selection behavior.
- [ ] **Step 4: Re-run** the component tests and existing Settings/Accounts tests.

### Task 7: Regression, image build, and deployment verification

**Files:**
- Modify only if tests identify a defect in the files above.

- [ ] **Step 1: Run backend regression:** `pytest -q`.
- [ ] **Step 2: Run frontend regression/build:** `npm test -- --run && npm run build` from `frontend/`.
- [ ] **Step 3: Build the Docker image** with the existing local Compose profile and recreate the single app container without deleting the named database volume.
- [ ] **Step 4: Verify** `/api/automations/chatgpt-relogin`, `/api/config`, `/api/sms-pool/stats`, the Settings page, container health, database integrity, and zero active tasks before handoff.
