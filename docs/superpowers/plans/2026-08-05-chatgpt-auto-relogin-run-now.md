# ChatGPT Auto Relogin Run-Now Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an immediate automatic-authentication trigger beside the countdown and confirm missing Codex2API credentials through one full login before deleting or restoring an account.

**Architecture:** A new service transition and POST endpoint enqueue the same `automation=true` task used by the scheduler, under the existing state lock. Remote credential absence becomes an explicit `remote_missing` health state that routes through the existing full-login, local-removal, token-persistence, Codex2API-sync, alert, and completion-based scheduling paths.

**Tech Stack:** Python 3.11, FastAPI, SQLModel, pytest, React 19, TypeScript, Ant Design, Vitest, Testing Library, Vite.

---

### Task 1: Atomic run-now state transition

**Files:**
- Modify: `tests/test_chatgpt_auto_relogin.py`
- Modify: `services/chatgpt_auto_relogin.py`

- [ ] **Step 1: Write the failing service tests**

Add tests that call the wished-for `trigger_chatgpt_auto_relogin_now()` API directly:

```python
def test_run_now_enqueues_immediately_and_tracks_the_automation_task():
    service = _service_module()
    store = _enabled_store(
        chatgpt_auto_relogin_status_state="idle",
        chatgpt_auto_relogin_status_next_run_at="2026-08-02T12:30:00Z",
    )
    now = datetime(2026, 8, 2, 12, 5, tzinfo=timezone.utc)
    enqueues = []

    result = service.trigger_chatgpt_auto_relogin_now(
        store=store,
        now=now,
        list_eligible=lambda: [3, 1, 2, 1],
        try_enqueue=lambda ids, concurrency: (
            enqueues.append((list(ids), concurrency)) or _accepted("task-now")
        ),
    )

    assert enqueues == [([1, 2, 3], 10)]
    assert result["accepted"] is True
    assert result["task_id"] == "task-now"
    assert result["status"]["state"] == "running"
    assert result["status"]["active_task_id"] == "task-now"
    assert result["status"]["last_started_at"] == "2026-08-02T12:05:00Z"
    assert result["status"]["next_run_at"] is None
```

Add independent tests for disabled configuration, zero eligible accounts, an already persisted active task, a busy enqueue decision, an enqueue exception, two concurrent calls creating only one task, and a terminal observation proving the next deadline equals `completed_at + interval`.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
/Users/xuann/Documents/Codex/2026-07-28/docker/outputs/any-auto-register/.venv/bin/python -m pytest tests/test_chatgpt_auto_relogin.py -k "run_now" -q
```

Expected: failures report that `trigger_chatgpt_auto_relogin_now` does not exist.

- [ ] **Step 3: Implement the minimal locked transition**

Add `trigger_chatgpt_auto_relogin_now()` to `services/chatgpt_auto_relogin.py`. Under `_STATUS_TRANSITION_LOCK`, read one snapshot, reject disabled/no-account/active states, call the injected or default scheduled enqueue function, preserve the existing deadline on rejection, and use `_persist_tick_transition()` on success:

```python
return {
    "accepted": True,
    "task_id": task_id,
    "reason": "enqueued",
    "status": _persist_tick_transition(
        resolved_store,
        snapshot,
        state="running",
        reason="task_running",
        eligible_accounts=len(eligible_ids),
        active_task_id=task_id,
        last_task_id=task_id,
        last_started_at=_utc_iso(wall_now),
        next_run_at=None,
        scheduled_interval_minutes=settings.interval_minutes,
    ),
}
```

Rejected results return `accepted=False`, `task_id=None`, a stable machine reason, and the unchanged or intentionally paused public status.

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run the same pytest command and require all selected tests to pass.

- [ ] **Step 5: Commit the service transition**

```bash
git add services/chatgpt_auto_relogin.py tests/test_chatgpt_auto_relogin.py
git commit -m "feat: trigger automatic relogin immediately"
```

### Task 2: Run-now HTTP endpoint

**Files:**
- Modify: `tests/test_chatgpt_auto_relogin.py`
- Modify: `api/automations.py`

- [ ] **Step 1: Write failing route tests**

Test `POST /api/automations/chatgpt-relogin/run-now` with the transition monkeypatched to return accepted, disabled, no-account, busy, and enqueue-failed results. Require `200` for accepted, `409` for user-state conflicts, `503` for enqueue failure, and require the route in the main OpenAPI document.

- [ ] **Step 2: Verify the route tests fail because POST is absent**

```bash
/Users/xuann/Documents/Codex/2026-07-28/docker/outputs/any-auto-register/.venv/bin/python -m pytest tests/test_chatgpt_auto_relogin.py -k "run_now_endpoint or main_includes_automations" -q
```

- [ ] **Step 3: Implement the route and bounded Chinese error mapping**

Import `HTTPException` and `trigger_chatgpt_auto_relogin_now`. Return the accepted result unchanged. Map `disabled_by_config`, `no_eligible_accounts`, `task_busy`, and `foreground_busy` to explicit Chinese 409 details; map `enqueue_failed` to 503. Do not expose exception text or credentials.

- [ ] **Step 4: Verify route and complete backend automation tests**

```bash
/Users/xuann/Documents/Codex/2026-07-28/docker/outputs/any-auto-register/.venv/bin/python -m pytest tests/test_chatgpt_auto_relogin.py -q
```

- [ ] **Step 5: Commit the route**

```bash
git add api/automations.py tests/test_chatgpt_auto_relogin.py
git commit -m "feat: expose immediate automatic relogin endpoint"
```

### Task 3: Confirm missing remote credentials with a full login

**Files:**
- Modify: `tests/test_chatgpt_codex2api_health.py`
- Modify: `tests/test_chatgpt_relogin_task.py`
- Modify: `services/chatgpt_codex2api_health.py`
- Modify: `api/tasks.py`

- [ ] **Step 1: Write failing health and task tests**

Change the remote-absent health expectation from `missing` to `remote_missing`, while retaining `missing` for a concurrently absent local account. Add automatic-task tests with a `remote_missing` snapshot:

```python
health = {
    451: {
        "account_id": 451,
        "email": "remote-deleted@example.com",
        "state": "remote_missing",
        "message": "Codex2API 未找到同邮箱账号，将执行一次完整登录确认",
    }
}
```

One test returns a successful full-login result and requires success plus a `relogin_chatgpt_account()` call. One returns `account_removed=True` and requires `relogin_failed_count=1`, `deleted_account_count=1`, zero generic errors, and a removed TaskLog. One returns a transient failure and requires the local result to remain a generic error with `relogin_failed_count=1`, `deleted_account_count=0`. A `state=missing` test requires no full-login call.

- [ ] **Step 2: Verify RED**

```bash
/Users/xuann/Documents/Codex/2026-07-28/docker/outputs/any-auto-register/.venv/bin/python -m pytest tests/test_chatgpt_codex2api_health.py tests/test_chatgpt_relogin_task.py -k "remote_missing or remote_absent" -q
```

Expected: the health state remains `missing` and automatic tasks do not call full login.

- [ ] **Step 3: Emit `remote_missing` only for an existing local account without a remote match**

In `inspect_codex2api_account_health()`, retain local-record absence as `missing`. Change only the `not matches` branch to `state="remote_missing"` and message `Codex2API 未找到同邮箱账号，将执行一次完整登录确认`.

- [ ] **Step 4: Route `remote_missing` through the existing full-login path**

In the automatic `_do_one()` branch of `api/tasks.py`, handle `remote_missing` beside `auth_failed`: call `relogin_chatgpt_account()` with the frozen deletion switch and task controls, then wrap dictionary results with `mode="full_login"`, `remote_auth_state="remote_missing"`, and the remote status. Do not increment `invalid_rt_count`, because no remote RT failure was observed. Existing `_record_automatic_result()` must continue deriving R and D from the full-login outcome.

- [ ] **Step 5: Verify GREEN and run related removal/alert regressions**

```bash
/Users/xuann/Documents/Codex/2026-07-28/docker/outputs/any-auto-register/.venv/bin/python -m pytest tests/test_chatgpt_codex2api_health.py tests/test_chatgpt_relogin_task.py tests/test_chatgpt_auto_relogin_alerts.py tests/test_chatgpt_account_removal.py -q
```

- [ ] **Step 6: Commit the missing-credential confirmation flow**

```bash
git add services/chatgpt_codex2api_health.py api/tasks.py tests/test_chatgpt_codex2api_health.py tests/test_chatgpt_relogin_task.py
git commit -m "feat: confirm missing Codex2API credentials by login"
```

### Task 4: Immediate-run button beside the countdown

**Files:**
- Modify: `frontend/src/lib/chatgptAutoReloginStatus.ts`
- Modify: `frontend/src/pages/Accounts.test.tsx`
- Modify: `frontend/src/pages/Accounts.tsx`

- [ ] **Step 1: Write failing component tests**

Mock an idle status with `eligible_accounts=64`, assert that `立即执行` follows the countdown, click it, and require:

```typescript
expect(apiFetch).toHaveBeenCalledWith('/automations/chatgpt-relogin/run-now', {
  method: 'POST',
})
```

Keep the POST promise pending to verify the button is loading/disabled and cannot submit twice. Resolve it with a running status and require the button to remain disabled plus the success toast. Add separate disabled-state, running-state, error-toast, and non-ChatGPT visibility tests.

- [ ] **Step 2: Verify RED**

```bash
npm test -- src/pages/Accounts.test.tsx
```

Expected: Testing Library cannot find the `立即执行` button.

- [ ] **Step 3: Implement the typed button flow**

Extend `ChatGPTAutoReloginStatus` with optional `eligible_accounts`, `active_task_id`, `interval_minutes`, and `concurrency`. Add a local primitive loading state, a handler that POSTs without a body, adopts `result.status`, and emits bounded success/error messages. Render an Ant Design small button immediately after the countdown with `ThunderboltOutlined`, loading state, and a derived disabled condition for missing status, disabled automation, running/stopping state, or zero eligible accounts.

- [ ] **Step 4: Verify GREEN, lint, and build**

```bash
npm test -- src/pages/Accounts.test.tsx
npm run lint
npm run build
```

- [ ] **Step 5: Commit the UI**

```bash
git add frontend/src/lib/chatgptAutoReloginStatus.ts frontend/src/pages/Accounts.tsx frontend/src/pages/Accounts.test.tsx
git commit -m "feat: add immediate automatic relogin button"
```

### Task 5: Full verification, review, and production deployment

**Files:**
- Verify all changed files and generated `static/` output

- [ ] **Step 1: Run complete fresh verification**

```bash
/Users/xuann/Documents/Codex/2026-07-28/docker/outputs/any-auto-register/.venv/bin/python -m pytest -q
(cd frontend && npm test)
(cd frontend && npm run build)
git diff --check
git status --short
```

Require zero test failures, successful TypeScript/Vite build, and no unexpected worktree changes.

- [ ] **Step 2: Perform independent spec and code-quality review**

Review exact compliance with the design, concurrency behavior, no-overlap guarantees, error redaction, removal semantics, R/D accounting, frontend request deduplication, and unchanged production configuration values. Fix findings through new failing tests.

- [ ] **Step 3: Push the branch**

```bash
git push -u origin codex/chatgpt-auto-relogin-run-now
```

- [ ] **Step 4: Build and stage the immutable production release**

Build `frontend`, copy its `dist/` into repository `static/`, archive committed backend files plus fresh static assets, upload to `/www/any-auto-register/releases/$(git rev-parse HEAD)`, and run remote `compileall`. Do not edit the production database configuration table.

- [ ] **Step 5: Back up, switch atomically, and health-check**

Create an online SQLite backup under `/www/any-auto-register/shared/backups`, verify source and backup `PRAGMA quick_check=ok`, atomically switch `/www/any-auto-register/current`, restart `any-auto-register.service`, and require local and public `/api/auth/status` HTTP 200. Roll back the symlink and restart the prior release if health does not become 200 within 30 seconds.

- [ ] **Step 6: Verify the production feature without altering user settings**

Read and record the five existing automatic-relogin settings before and after deployment; require exact equality. Authenticate internally, POST run-now once, require exactly one new `source=schedule` task, wait for terminal status, and assert `next_run_at - completed_at` equals the configured interval. For a remote-missing account encountered naturally by the run, verify the logs show one full-login confirmation and either resynchronization or confirmed local removal, never direct deletion from absence alone.
