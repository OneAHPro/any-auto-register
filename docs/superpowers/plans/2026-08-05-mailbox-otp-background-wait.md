# ChatGPT Mailbox OTP Background Wait Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let slow ChatGPT mailbox OTP waits preserve their login session without occupying the configured active-account concurrency slots.

**Architecture:** `RegisterTaskControl` owns an interruptible bounded semaphore and tracks which attempts hold a slot. ChatGPT batch runners use a bounded sleeper-capable executor while the mailbox adapter splits one absolute OTP budget into a 20-second foreground window and a background remainder that temporarily releases the slot.

**Tech Stack:** Python 3.12, `threading`, `concurrent.futures`, `unittest`.

---

### Task 1: Interruptible active-attempt concurrency slots

**Files:**
- Modify: `core/task_runtime.py`
- Test: `tests/test_task_runtime.py`

- [ ] **Step 1: Write the failing tests**

Add tests proving that a second `start_attempt()` blocks at limit 1, pausing the first attempt lets the second begin, finishing the second lets the first resume, and a stop request interrupts a waiter.

```python
control = RegisterTaskControl()
control.configure_active_slots(1)
first = control.start_attempt()
started = threading.Event()

def run_second():
    second = control.start_attempt()
    started.set()
    control.finish_attempt(second)

worker = threading.Thread(target=run_second)
worker.start()
self.assertFalse(started.wait(0.05))
with control.pause_active_slot(first):
    self.assertTrue(started.wait(1))
control.finish_attempt(first)
worker.join(1)
```

- [ ] **Step 2: Run the tests and verify RED**

Run: `.venv/bin/python -m unittest tests.test_task_runtime.RegisterTaskControlTests`

Expected: failure because `configure_active_slots` and `pause_active_slot` do not exist.

- [ ] **Step 3: Implement the minimal slot controller**

Add an optional bounded semaphore, held-attempt tracking, interruptible acquisition, automatic release from `finish_attempt()`, and a `pause_active_slot(attempt_id)` context manager. Existing callers without `configure_active_slots()` remain unchanged.

- [ ] **Step 4: Run the tests and verify GREEN**

Run: `.venv/bin/python -m unittest tests.test_task_runtime.RegisterTaskControlTests`

Expected: all task-control tests pass.

### Task 2: Split mailbox waiting into foreground and background phases

**Files:**
- Modify: `core/base_mailbox.py`
- Modify: `platforms/chatgpt/plugin.py`
- Test: `tests/test_chatgpt_plugin.py`

- [ ] **Step 1: Write the failing tests**

Add a controlled mailbox whose first wait raises `TimeoutError` and whose second wait returns a code. Assert calls use 20 seconds and the remaining total budget, the second call runs while the active slot is paused, and the log announces background waiting without secrets.

```python
code = context.email_service.get_verification_code(timeout=30)
self.assertEqual(code, "654321")
self.assertEqual(mailbox.timeouts, [20, 160])
self.assertEqual(mailbox.background_entries, 1)
```

- [ ] **Step 2: Run the focused test and verify RED**

Run: `.venv/bin/python -m unittest tests.test_chatgpt_plugin.ChatGPTPluginTests.test_slow_mailbox_moves_to_background_after_foreground_window`

Expected: the existing adapter performs only one 30-second wait.

- [ ] **Step 3: Implement one absolute mailbox budget**

Default the existing `mailbox_otp_timeout_seconds` setting to 180 for ChatGPT when blank, normalize the OAuth/register OTP wait keys to the same value, account for baseline elapsed time, poll for at most 20 foreground seconds, then call the remaining wait inside `BaseMailbox.pause_active_slot_for_mailbox_wait()`.

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run: `.venv/bin/python -m unittest tests.test_chatgpt_plugin`

Expected: plugin tests pass, including existing configured-timeout behavior.

### Task 3: Allow waiting accounts without raising active concurrency

**Files:**
- Modify: `api/tasks.py`
- Test: `tests/test_chatgpt_relogin_task.py`
- Test: `tests/test_register_task_controls.py`

- [ ] **Step 1: Write failing scheduler tests**

For ChatGPT tasks with concurrency 1, block the first account inside `pause_active_slot()` and assert the second account starts before the first finishes. Also assert non-ChatGPT tasks retain their original executor width and the sleeper-capable ChatGPT executor never exceeds 64 threads.

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `.venv/bin/python -m unittest tests.test_chatgpt_relogin_task tests.test_register_task_controls`

Expected: the second account does not start while the first owns the sole current executor worker.

- [ ] **Step 3: Implement bounded sleeper-capable executors**

Configure the task control with the user's concurrency value. For ChatGPT only, use `min(total, 64)` executor threads while the task-control semaphore continues to enforce the configured active concurrency; change relogin submission/replenishment limits to the executor width.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `.venv/bin/python -m unittest tests.test_chatgpt_relogin_task tests.test_register_task_controls`

Expected: scheduler and task-stop tests pass.

### Task 4: Regression, security, and deployment verification

**Files:**
- Modify: `docs/superpowers/plans/2026-08-05-mailbox-otp-background-wait.md`

- [ ] **Step 1: Run the complete affected suite**

Run: `.venv/bin/python -m unittest tests.test_task_runtime tests.test_mailbox_task_control tests.test_chatgpt_plugin tests.test_chatgpt_register tests.test_chatgpt_relogin_task tests.test_register_task_controls`

Expected: all tests pass.

- [ ] **Step 2: Scan the diff and logs for secrets**

Run: `git diff --check && git diff --stat && rg -n "mailapi_url|验证码: \{code\}|尝试 OTP: \{code\}" core platforms api`

Expected: no whitespace errors and no new raw URL/code logging.

- [ ] **Step 3: Verify production is idle and deploy atomically**

Check `task_runs` for `running` or `pending` rows. When zero, create a release from the verified commit, switch `/www/any-auto-register/current`, restart `any-auto-register.service`, and verify `/api/health` plus the deployed commit marker. Keep release `523e96f` as rollback.

