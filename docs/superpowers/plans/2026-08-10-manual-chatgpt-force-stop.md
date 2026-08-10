# Manual ChatGPT Task Force-Stop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent Sentinel Playwright calls from keeping a manual ChatGPT login task alive indefinitely, and make stop/skip interrupt the exact browser driver owned by the active attempt.

**Architecture:** Add attempt-scoped interrupt callbacks and a thread-local task-attempt context in `core.task_runtime`. Wrap each registration attempt with that context, then let the Sentinel browser helper register its Playwright driver as an interruptible resource. Bound the in-page Sentinel SDK Promise with `Promise.race`; task stop kills only the registered driver and then propagates the existing `StopTaskRequested` checkpoint through normal cleanup.

**Tech Stack:** Python 3.10, FastAPI background tasks, Playwright sync API, `threading`, `unittest`/pytest, SQLite production verification.

---

### Task 1: Attempt-scoped interrupt callbacks

**Files:**
- Modify: `core/task_runtime.py`
- Test: `tests/test_task_runtime.py`

- [ ] **Step 1: Write failing stop and skip callback tests**

Add tests that start one or two attempts, register callbacks, call
`request_stop_once()` or `request_skip_current()`, and assert that:

```python
called: list[int] = []
unregister = control.register_attempt_interrupt(
    attempt_id,
    lambda: called.append(attempt_id),
)
control.request_stop_once()
self.assertEqual(called, [attempt_id])
control.request_stop_once()
self.assertEqual(called, [attempt_id])
unregister()
```

The skip test must also prove callbacks belonging to a finished attempt are not
called later.

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
python3 -m pytest -q \
  tests/test_task_runtime.py::RegisterTaskControlTests::test_stop_interrupts_registered_attempt_resource \
  tests/test_task_runtime.py::RegisterTaskControlTests::test_skip_interrupts_only_live_attempt_resources
```

Expected: both fail because `register_attempt_interrupt` does not exist.

- [ ] **Step 3: Implement callback storage and lock-safe delivery**

Add `register_attempt_interrupt(attempt_id, callback)` returning an idempotent
unregister closure. Store callbacks by attempt, collect them while holding the
control lock, invoke them after releasing the lock, isolate callback exceptions,
invoke immediately when stop/skip is already pending, and clear callbacks in
`finish_attempt()`.

- [ ] **Step 4: Run task runtime tests and verify GREEN**

Run:

```bash
python3 -m pytest -q tests/test_task_runtime.py
```

Expected: all task runtime tests pass.

### Task 2: Thread-local task-attempt context

**Files:**
- Modify: `core/task_runtime.py`
- Modify: `api/tasks.py`
- Test: `tests/test_task_runtime.py`
- Test: `tests/test_register_task_controls.py`

- [ ] **Step 1: Write failing context lifetime tests**

Test that `bind_task_attempt_context(control, attempt_id)` exposes the exact
control/attempt pair inside the context and restores the previous context after
exit. Add a registration control test with a fake ChatGPT platform whose
`register()` registers an interrupt resource, waits until that resource is
interrupted, then raises a Playwright-style error. Start a manual task, call the
real `stop_task()` endpoint, and assert within one second that status is
`stopped`, `active_attempts=0`, and the foreground gate can admit an automation
lease.

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
python3 -m pytest -q \
  tests/test_task_runtime.py -k task_attempt_context \
  tests/test_register_task_controls.py -k exposes_attempt_context
```

Expected: failures because the context API is absent and `_platform.register()`
is not wrapped.

- [ ] **Step 3: Implement and bind the context**

Add an immutable `TaskAttemptContext`, `bind_task_attempt_context()` context
manager, `current_task_attempt_context()`, and
`checkpoint_current_task_attempt()` in `core.task_runtime`. In `_do_one()`, wrap
only this attempt's platform call:

```python
with bind_task_attempt_context(control, attempt_id):
    account = _platform.register(
        email=bound_email or req.email or None,
        password=req.password,
    )
```

- [ ] **Step 4: Run focused registration tests and verify GREEN**

Run:

```bash
python3 -m pytest -q tests/test_task_runtime.py tests/test_register_task_controls.py
```

Expected: all tests pass.

### Task 3: Bound and interrupt Sentinel Playwright

**Files:**
- Modify: `platforms/chatgpt/sentinel_browser.py`
- Create: `tests/test_chatgpt_sentinel_browser.py`

- [ ] **Step 1: Write failing Sentinel helper tests**

Use fake Playwright manager, driver process, browser, context, and page objects.
Assert the page evaluation payload includes a finite `timeoutMs` and the script
contains `Promise.race`. Under `bind_task_attempt_context`, register the fake
driver, request stop from another thread, make the fake evaluate call raise, and
assert the helper propagates `StopTaskRequested`, invokes driver termination once,
and still attempts browser/Playwright cleanup.

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
python3 -m pytest -q tests/test_chatgpt_sentinel_browser.py
```

Expected: failures because the existing page script has no Promise deadline and
the driver is not registered with task control.

- [ ] **Step 3: Implement the two Sentinel protections**

Replace the unbounded SDK call with:

```javascript
await Promise.race([
  window.SentinelSDK.token(flow),
  new Promise((_, reject) => {
    setTimeout(() => reject(new Error('Sentinel SDK token timeout')), timeoutMs)
  }),
])
```

Resolve the current Playwright transport process, register a task interrupt that
terminates only that process, call `checkpoint_current_task_attempt()` after a
browser exception and before returning a token, and catch cleanup errors so they
cannot replace `StopTaskRequested`. Keep the existing `None` return for ordinary
Sentinel failure so HTTP PoW fallback remains available.

- [ ] **Step 4: Run Sentinel and OAuth compatibility tests**

Run:

```bash
python3 -m pytest -q \
  tests/test_chatgpt_sentinel_browser.py \
  tests/test_chatgpt_register.py \
  tests/test_chatgpt_password_reset.py \
  tests/test_chatgpt_phone_flow.py
```

Expected: all tests pass.

### Task 4: Task-control and ChatGPT regression verification

**Files:**
- Test: `tests/test_task_runtime.py`
- Test: `tests/test_register_task_controls.py`
- Test: `tests/test_chatgpt_relogin_task.py`
- Test: `tests/test_chatgpt_phone_verification.py`
- Test: `tests/test_chatgpt_phone_flow.py`
- Test: `tests/test_chatgpt_register.py`

- [ ] **Step 1: Run the full task-control and ChatGPT regression group**

Run:

```bash
python3 -m pytest -q \
  tests/test_task_runtime.py \
  tests/test_register_task_controls.py \
  tests/test_chatgpt_relogin_task.py \
  tests/test_chatgpt_phone_verification.py \
  tests/test_chatgpt_phone_flow.py \
  tests/test_chatgpt_register.py
```

Expected: all tests pass without leaked worker threads.

### Task 5: Full verification, repository sync, and production release

**Files:**
- Modify after build only: ignored deployment assets under `static/`

- [ ] **Step 1: Run complete backend and frontend verification**

Run the full backend suite with the production Python 3.10 environment, then:

```bash
cd frontend
pnpm test
pnpm exec eslint src/components/TaskLogPanel.tsx src/pages/RunningTasks.tsx
pnpm build
```

Expected: full backend and frontend tests pass, changed frontend files have zero
lint errors, and the production bundle builds.

- [ ] **Step 2: Commit and push the verified branch and `main`**

Fetch `origin/main`, require it to be an ancestor of feature HEAD, then push
`codex/manual-chatgpt-force-stop` and `HEAD:main`. Verify both remote refs equal
the local SHA.

- [ ] **Step 3: Build and switch an immutable production release**

Require zero active tasks, create a SQLite online backup with
`PRAGMA quick_check=ok`, archive the pushed commit plus fresh static assets,
compile Python modules, atomically repoint `/www/any-auto-register/current`, and
restart only `any-auto-register.service`. Retain the previous release for rollback.

- [ ] **Step 4: Verify production without real account consumption**

Run a synthetic in-process driver-interrupt smoke test using a disposable child
process. Require the registered stop callback to terminate only that child and
leave the service active. Confirm database and backup integrity, HTTP 200 inside
and outside, zero active manual tasks, automatic relogin enabled, scheduler not
`foreground_busy`, and no new traceback/error log patterns.
