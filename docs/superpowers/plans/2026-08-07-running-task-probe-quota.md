# Running Task Probe Quota Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Hide internal task IDs from the running-task UI and show the current automatic probe's remaining Codex2API quota.

**Architecture:** Reuse `estimated_remaining_usd` already present in `/tasks/summary` metadata. Add one small formatting helper and conditional card copy in `RunningTasks.tsx`; do not add backend calls or duplicate probe logic.

**Tech Stack:** React 19, TypeScript, Ant Design, Vitest, Testing Library

---

### Task 1: Render probe quota without task IDs

**Files:**
- Modify: `frontend/src/pages/RunningTasks.tsx`
- Test: `frontend/src/pages/RunningTasks.test.tsx`

- [ ] **Step 1: Write failing rendering tests**

Extend the automatic summary fixture with `estimated_remaining_usd: '98.85'`. Assert the completed card shows `剩余可用额度` and `$98.85`, and does not show `task-auto-history`. Open the log drawer and assert the ID remains absent. Add running and invalid-value cases that expect `本次探针额度统计中` and `本次探针额度未生成` respectively. Cover the responsive card regions, wrapping statistics, and shrinkable application content.

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
cd frontend
npm test -- src/pages/RunningTasks.test.tsx
```

Expected: the new quota copy is missing and the task ID is still visible.

- [ ] **Step 3: Add the minimal formatter and rendering branch**

Add optional `estimated_remaining_usd?: string | number` to `TaskSnapshot.meta`. Implement:

```ts
function formatRemainingQuota(value: unknown): string | null {
  if (value === null || value === undefined || value === '') return null
  const amount = Number(value)
  return Number.isFinite(amount) && amount >= 0 ? `$${amount.toFixed(2)}` : null
}
```

Remove the task-ID `<Text code>` from cards and drawer titles. For automatic tasks, render the completed amount, running copy, or unavailable copy according to the design. Keep `task.id` only in React keys, state, API paths, log loading, and deletion. Replace the fixed Row/Col card with responsive CSS Grid regions, let statistics and actions wrap, and keep compact metrics on one line.

- [ ] **Step 4: Run focused and full frontend verification**

Run:

```bash
cd frontend
npm test -- src/pages/RunningTasks.test.tsx
npm test -- --run
npm run build
```

Expected: all tests pass and Vite produces a production bundle.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages/RunningTasks.tsx frontend/src/pages/RunningTasks.test.tsx \
  docs/superpowers/specs/2026-08-07-running-task-probe-quota-design.md \
  docs/superpowers/plans/2026-08-07-running-task-probe-quota.md
git commit -m "feat(tasks): show probe remaining quota"
```

### Task 2: Deploy and verify

**Files:**
- Verify: `static/`
- Server: `/www/any-auto-register/releases/sidebar-quota-20260807-1900`

- [ ] **Step 1: Confirm production is safe to switch**

Require the current service and public endpoint to be healthy, SQLite `PRAGMA quick_check` to be `ok`, and zero `pending` or `running` task rows.

- [ ] **Step 2: Build and stage an immutable release**

Archive the verified Git commit, replace its `static/` directory with the freshly built frontend, upload it, verify the archive hash, and import `main` as the service user with the production environment loaded.

- [ ] **Step 3: Switch atomically with rollback protection**

Atomically repoint `/www/any-auto-register/current`, restart only `any-auto-register.service`, and roll back to the previous release if loopback health does not return HTTP 200.

- [ ] **Step 4: Verify production**

Require service active, loopback/public HTTP 200, zero new error-log bytes, SQLite `quick_check=ok`, and unchanged Codex2API/Postgres/Redis container identities and start times.
