# Remote Codex2API Accounts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show JSON-imported Codex2API-only accounts in the project account list with fresh quota data and make eligible remote accounts available to pool scheduling.

**Architecture:** Extend the existing target reconciliation to persist remote-only identities, bindings, assignments, and quota snapshots without copying credentials. Merge a sanitized remote account projection into the ChatGPT list endpoint, and make cards render remote-managed rows at a fixed height with explicit data freshness.

**Tech Stack:** FastAPI, SQLModel, pytest, React, TypeScript, Vitest, CSS Grid.

---

### Task 1: Remote projection and reconciliation tests

**Files:**
- Create: `tests/test_remote_codex_accounts.py`
- Modify: `tests/test_chatgpt_account_display.py`

- [ ] **Step 1: Write failing tests** for a remote-only list row, a persisted remote binding/assignment, and `usage_7d_detail.account_billed` display fallback.
- [ ] **Step 2: Run focused pytest and confirm the new behaviors fail because remote-only rows are not projected.

### Task 2: Persist remote-only identities and quota data

**Files:**
- Modify: `services/control_plane_workers.py`
- Modify: `services/chatgpt_codex2api_health.py`
- Modify: `services/chatgpt_account_display.py`

- [ ] **Step 1: Add deterministic remote identity resolution keyed by target and remote ID.
- [ ] **Step 2: Extend reconciliation to create/update remote-only bindings and default-pool assignments while filtering unusable remote states.
- [ ] **Step 3: Add display-only billed and freshness fields without changing scheduler ledger semantics.
- [ ] **Step 4: Run the focused pytest suite and confirm it passes.

### Task 3: Merge remote-only accounts into the API list

**Files:**
- Modify: `api/accounts.py`
- Create: `tests/test_remote_codex_accounts_api.py`

- [ ] **Step 1: Add a failing API test for a combined local plus remote-only response.
- [ ] **Step 2: Implement remote row projection with virtual IDs, source metadata, target/assignment summary, and sanitized `extra_json`.
- [ ] **Step 3: Disable local-only operations for remote-only rows and run the API tests.

### Task 4: Refresh live usage on demand

**Files:**
- Modify: `services/chatgpt_codex2api_health.py`
- Modify: `api/accounts.py`
- Modify: `frontend/src/pages/Accounts.tsx`
- Modify: `tests/test_control_plane_workers.py`
- Modify: `frontend/src/pages/Accounts.test.tsx`

- [ ] **Step 1: Add a failing test for a bounded usage probe before reading the list.
- [ ] **Step 2: Add `refresh_live` query handling and a bounded target probe; keep cached reads for ordinary list calls.
- [ ] **Step 3: Make the page refresh button and 60-second ChatGPT refresh use `refresh_live=1`, and show the latest remote timestamp.
- [ ] **Step 4: Run focused backend/frontend tests.

### Task 5: Equalize card heights and render remote-managed cards

**Files:**
- Modify: `frontend/src/components/accounts/AccountCard.tsx`
- Modify: `frontend/src/index.css`
- Modify: `frontend/src/components/accounts/AccountCard.test.tsx`

- [ ] **Step 1: Add failing tests for the remote-managed badge, hidden credential actions, and fixed-height class.
- [ ] **Step 2: Render source/target metadata, display fallback billing, freshness, and remote-managed actions.
- [ ] **Step 3: Apply fixed card height, stretched grid cells, reserved data slots, and responsive constraints.
- [ ] **Step 4: Run component tests, build, and lint.

### Task 6: Full verification and deployment

**Files:**
- No further source files.

- [ ] **Step 1: Run the complete backend and frontend test suites plus build/lint.
- [ ] **Step 2: Commit the implementation with a focused message.
- [ ] **Step 3: Deploy the commit to the configured server with database backup and atomic service restart.
- [ ] **Step 4: Verify public assets, API response counts, remote-only rows, quota timestamps, service health, and container invariance.
