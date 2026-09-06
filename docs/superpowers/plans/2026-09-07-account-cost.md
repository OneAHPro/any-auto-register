# Account Cost Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Let operators set, clear, and view each account's purchase cost in CNY directly from the account card.

**Architecture:** Store optional integer cents in the account's independent `purchase_cost_cents` column, project a two-decimal string at `extra_json.purchase_cost_cny`, and update via `PATCH /accounts/{id}`. Add a third identity-grid cell opening a dedicated cost editor. Read all-time charges separately from Codex2API and compute CNY cost per USD of cumulative charges.

**Tech Stack:** FastAPI, SQLModel, React, Ant Design, Vitest, pytest.

---

### Task 1: Backend cost persistence

**Files:**
- Modify: `api/accounts.py` (`AccountUpdate`, `update_account`)
- Modify: `core/db.py` (`AccountModel`, additive `init_account_pool_schema` migration)
- Test: `tests/test_accounts_purchase_cost.py`

- [x] Write a failing test proving PATCH accepts a non-negative `purchase_cost_cny`, returns it, and explicit `null` clears it.
- [x] Run the focused test and confirm it fails because the update model ignores the field.
- [x] Add `purchase_cost_cny: Optional[Decimal]` to `AccountUpdate`; distinguish omitted from explicit null with `model_fields_set`, validate finite non-negative two-decimal amounts, and write integer cents or NULL to the independent column.
- [x] Verify credential refresh and cost PATCH may overlap without losing the edit or restoring cleared costs.
- [x] Run the focused backend tests and confirm they pass.

### Task 2: Card display and edit interaction

**Files:**
- Modify: `frontend/src/components/accounts/AccountCard.tsx`
- Modify: `frontend/src/pages/Accounts.tsx`
- Modify: `frontend/src/index.css`
- Test: `frontend/src/components/accounts/AccountCard.test.tsx`, `frontend/src/pages/Accounts.test.tsx`

- [x] Add failing tests for `未设置`, formatted `¥12.50`, clicking the cost control, loading the value into the detail form, saving a numeric value, and clearing it.
- [x] Run those tests and confirm the new assertions fail.
- [x] Add the third identity-grid item as a button-like control that stops card activation and opens the cost editor.
- [x] Add a dedicated cost modal with InputNumber, save/cancel/clear actions, pending state and inline errors. Submit only `purchase_cost_cny` in PATCH payloads; update only that value on the matching card, retaining its current quota and status data.
- [x] Add the desktop three-column grid with the existing two-column narrow-screen fallback.
- [x] Run the focused frontend tests and confirm they pass.

### Task 3: All-time billing and derived price

- [x] Add `account_usage_all(remote_id)` to `services/codex2api_target_client.py` using `GET /api/admin/accounts/{remote_id}/usage?days=0`, validate `total_account_billed` and all-time scope.
- [x] Add `services/codex_account_billing.py` to fetch only current-page node/ID keys with four concurrent requests and per-engine cache isolation. Successful data expires after 60 seconds, failures after 15 seconds; manual refresh bypasses cache.
- [x] Attach `chatgpt_display.billing` after pagination in `api/accounts.py`. Transport failures must retain the list and quota values without substituting weekly charges.
- [x] Replace the status metric with purchase cost divided by all-time charges, center all three metrics, and test unavailable, zero and very small values.
- [x] Verify `tests/test_accounts_billing.py`, `tests/test_codex_account_billing.py`, target-client tests, and AccountCard tests.

### Task 4: Full verification and deployment

- [x] Run backend regression, `pnpm exec vitest run`, `pnpm build`, Python compileall, and `git diff --check`.
- [ ] Commit the implementation and push `HEAD:main`.
- [ ] Build an immutable release, back up and quick-check the production SQLite database, atomically switch `/www/any-auto-register/current`, and restart only `any-auto-register.service`.
- [ ] Verify the service, loopback/public HTTP 200 responses, database `quick_check=ok`, and a read-only account API response containing the configured cost field.

Validation: frontend 215 tests passed; final focused backend review 111 tests passed. The full backend run on an isolated, migrated database yielded 1809 passed, 1 skipped and one subprocess failure caused by launching pytest from stdin. That remaining multiprocessing test passed when rerun through the standard pytest executable. Use a normal pytest entry point, and initialize the isolated database in a separate process before running the suite. Browser checks passed for save/reload/clear, same-email account isolation, keyboard input, all-time totals and derived price at 390/768/1440 widths. A read-only production billing check returned all 27 requested remote-account totals in 1.93 seconds and matched the provider's all-time total.
