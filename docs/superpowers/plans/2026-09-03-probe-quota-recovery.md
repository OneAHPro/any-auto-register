# Probe Quota Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore non-zero, trustworthy automatic-task quota summaries for manually imported Codex2API accounts.

**Architecture:** Keep account health and relogin routing assignment-scoped, while making the final read-only quota inventory target-scoped. Deduplicate target rows by stable email, tolerate known exhausted rows without cost, and reuse only reset-compatible complete observations during transient billing-query gaps.

**Tech Stack:** Python 3.11, FastAPI, SQLModel, pytest, React, TypeScript, Vitest

---

### Task 1: Restore complete target quota inventory

**Files:**
- Modify: `services/chatgpt_codex2api_health.py`
- Test: `tests/test_chatgpt_codex2api_health.py`

- [ ] Add failing tests proving the final reader includes remote-only rows from every enabled target and deduplicates repeated emails with assignment preference.
- [ ] Run the focused tests and confirm they fail because remote-only rows are filtered.
- [ ] Implement target-wide read-only collection and deterministic cross-target deduplication without changing health/relogin routing.
- [ ] Run the focused tests and confirm they pass.

### Task 2: Make quota completeness resilient

**Files:**
- Modify: `services/chatgpt_codex2api_quota.py`
- Modify: `api/tasks.py`
- Test: `tests/test_chatgpt_codex2api_quota.py`
- Test: `tests/test_chatgpt_relogin_task.py`

- [ ] Add failing tests for exhausted rows without `billed_7d`, reset-compatible fallback, reset mismatch rejection, and fallback task metadata.
- [ ] Run the focused tests and confirm the expected failures.
- [ ] Accept missing cost for known exhausted windows and add a same-task stable-window merger.
- [ ] Wire the merger into final quota retries while preserving freshness flags and alert suppression.
- [ ] Run quota and task tests and confirm they pass.

### Task 3: Clarify in-progress task cards

**Files:**
- Modify: `frontend/src/pages/RunningTasks.tsx`
- Test: `frontend/src/pages/RunningTasks.test.tsx`

- [ ] Add a failing test for a terminal task whose quota post-processing metadata is still pending.
- [ ] Render that state as “本次探针额度统计中”.
- [ ] Run the focused frontend test and production build.

### Task 4: Verify, review, commit, push, and deploy

**Files:**
- Verify all modified backend and frontend files.
- Deploy immutable release under `/www/any-auto-register/releases/`.

- [ ] Run the complete relevant backend test suites and frontend test/build commands.
- [ ] Review the final diff for quota scope, secret safety, and multi-target behavior.
- [ ] Commit and push `codex/fix-probe-quota-summary`.
- [ ] Confirm production has no active manual task, back up SQLite, upload the immutable release, and atomically switch `current`.
- [ ] Restart only `any-auto-register.service`; roll back automatically if loopback health fails.
- [ ] Verify service/public HTTP 200, SQLite integrity, unchanged Codex2API/PostgreSQL/Redis containers, and a production read-only quota report containing the manually imported available accounts.
