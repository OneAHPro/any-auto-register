# Quota Alert Notification Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Send and visibly report balance alerts whenever a trustworthy quota amount is below the configured threshold.

**Architecture:** Select the current-window amount when it is fresh and fall back to the fresh complete total-window amount when only the current window is partial. Keep the total-freshness safety gate, send SMTP and Bark independently, and expose balance-alert results separately from relogin-alert results.

**Tech Stack:** Python 3.11, FastAPI, pytest, React, TypeScript, Vitest

---

### Task 1: Recover partial-window alert evaluation

**Files:**
- Modify: `services/chatgpt_auto_relogin_alerts.py`
- Modify: `services/chatgpt_bark_alerts.py`
- Modify: `api/tasks.py`
- Test: `tests/test_chatgpt_auto_relogin_alerts.py`
- Test: `tests/test_chatgpt_bark_alerts.py`
- Test: `tests/test_chatgpt_relogin_task.py`

- [x] Add failing tests with `current_fresh=False`, `total_fresh=True`, and a total below `$200.00`.
- [x] Verify that both channel functions return stale and the task invokes neither channel before the fix.
- [x] Select `current_remaining_usd` when current is fresh; otherwise select `total_remaining_usd`.
- [x] Keep `total_fresh=False` as a hard no-send condition.
- [x] Run both channel and task regression tests.

### Task 2: Separate task-card alert states

**Files:**
- Modify: `frontend/src/pages/RunningTasks.tsx`
- Modify: `frontend/src/pages/RunningTasks.test.tsx`
- Modify: `frontend/src/components/settings/ChatGPTAutoReloginSection.tsx`
- Modify: `frontend/src/components/settings/ChatGPTAutoReloginSection.test.tsx`

- [x] Add failing tests for balance alert sent, balance data pending, and delivery failure states.
- [x] Rename the existing generic mail tags so they explicitly identify the relogin alert.
- [x] Render balance-alert state from `quota_alert_*` and `bark_quota_alert_*` metadata.
- [x] Document the fresh-total fallback in the setting help text.
- [x] Run focused and complete frontend tests, lint, and production build.

### Task 3: Review, publish, and verify production

**Files:**
- Verify all modified files.
- Deploy an immutable release below `/www/any-auto-register/releases/`.

- [x] Run the relevant backend suites and complete frontend suite.
- [x] Review the diff for stale-data sends, duplicate notifications, channel coupling, and credential leakage.
- [ ] Commit and push `codex/fix-quota-alert-notification`.
- [ ] Confirm no active manual tasks, back up SQLite, upload the release, and atomically switch `current`.
- [ ] Restart only `any-auto-register.service`, retaining automatic rollback on failed health checks.
- [ ] Verify loopback/public HTTP 200, SQLite integrity, empty error log, unchanged Codex2API containers, and the next real task's balance-alert metadata.
