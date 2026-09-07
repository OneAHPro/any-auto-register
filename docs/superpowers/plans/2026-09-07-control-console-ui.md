# Control Console UI Implementation Plan

> **For agentic workers:** Use subagent-driven-development for independent surfaces with exclusive file ownership. Steps use checkbox syntax.

**Goal:** Rebuild the working console interface around purchased ChatGPT accounts and multiple Codex2API instances, preserving existing typography and operational behavior.

**Architecture:** Keep React/Ant Design and existing API contracts. Introduce one shared page header, layout tokens, and an application shell; redesign independent page bodies and consolidate import/navigation entry points. Record future cost-ledger and scheduling work separately rather than simulating unavailable production data.

**Tech Stack:** React 19, TypeScript, Ant Design 5, React Router 7, Vite 8, Vitest, existing FastAPI APIs.

## 1. Baseline and documentation

- [x] Isolate baseline77f511f in `.workpatch/control-console-ui`.
- [x] Record confirmed requirements in `docs/development/2026-09-control-console.md` and update `PRODUCT.md`/`DESIGN.md`.
- [x] Run existing frontend tests and capture baseline (215 passed).

## 2. Shell and unified supply entry (root)

Files: `frontend/src/App.tsx`, `frontend/src/App.test.tsx`, `frontend/src/theme.ts`, `frontend/src/main.tsx`, new `frontend/src/console.css`, new `frontend/src/components/console/ConsolePageHeader.tsx`, new `frontend/src/pages/Supply.tsx`.

- [x] Update navigation tests first: primary pages are overview/accounts/supply/instances/scheduler/tasks; legacy new-registration path redirects to supply; theme and logout remain usable.
- [x] Implement desktop sidebar/mobile drawer and common page/toolbar/panel styles; preserve typography values and persisted theme.
- [x] Implement supply choices using existing login/import components and mailbox panel, with action query routing for deep links from account pages.
- [x] Verify navigation, keyboard focus, small screen behavior and typography tests.

Shared component contract:

```tsx
type ConsolePageHeaderProps = {
  title: string
  description?: string
  actions?: React.ReactNode
  children?: React.ReactNode
}
```

## 3. Account workspace (exclusive account agent)

Files: `frontend/src/pages/Accounts.tsx`, `frontend/src/pages/Accounts.test.tsx`, `frontend/src/components/accounts/AccountCard.tsx`, matching card tests, new `frontend/src/components/accounts/account-workspace.css`.

- [x] Cover consolidation of login/import entry points and removal of visible new-registration action with a behavioral test.
- [x] Reshape summary, filters, toolbar, independent account cards and details; preserve API data, pagination, selection, cost editor, relogin/phone operations.
- [x] Keep known window labels, all-time billing and missing data states correct; never fabricate daily income or aggregate only visible rows.
- [x] Run account and cost editor regressions.

## 4. Operations pages (exclusive operations agent)

Files: `frontend/src/pages/Dashboard.tsx`, `RunningTasks.tsx`, `TaskHistory.tsx`, `Login.tsx`, corresponding tests, new `frontend/src/pages/operations-workspace.css`.

- [x] Rebuild overview from a new real finance/operations API, with dense daily/cumulative metrics and seven-day history; disclose incomplete values.
- [x] Consolidate running/completed task presentation, retain detail logs and failed-item recovery.
- [x] Align login surface with the new system while preserving its auth flow.
- [x] Run relevant regressions and responsive browser checks.

## 5. Instances and configuration (exclusive management agent)

Files: `frontend/src/pages/Codex2APITargets.tsx`, `Codex2APIScheduler.tsx`, `Settings.tsx`, `Proxies.tsx`, `SmsPool.tsx`, corresponding tests, new `frontend/src/pages/management-workspace.css`.

- [x] Rebuild instance and scheduling management as coherent operational surfaces; retain current plan confirmation until backend automatic execution is implemented.
- [x] Remove unrelated legacy integration/registration settings from the normal interface while retaining connection, recovery, Bark/email, auth, proxy and phone needs.
- [x] Avoid altering persisted hidden values merely by saving visible settings; cover cross-tab edits and actual imported mailbox provider.
- [x] Keep selection, create/edit/health/plan actions and form validation working under current API contracts; add per-instance sale price.

## 6. Integration and evidence

- [x] Run `npm test -- --reporter=dot` and `npm run build` in `frontend`.
- [x] Independently review requirement coverage, then inspect correctness and maintainability.
- [x] Use isolated mock fixtures for browser verification; never run production login or migration actions as visual tests.
- [x] Capture1440/1024/390px and light/dark images for all representative page types; inspect overflow, font loading, controls and errors.
- [x] Fix evidenced issues, repeat affected checks, update development log and retain clear completion status for future backend work.

## 7. User-requested finance expansion

- [x] Add purchase batch/record tables and transactional integration with existing-account login, JSON import, manual cost editing and deletion.
- [x] Add per-instance sale-price API and editor, without inventing unset prices.
- [x] Aggregate real upstream `total_account_billed` and calendar `today.account_billed`; retain target/account snapshots across removal.
- [x] Calculate global fiat values before display rounding; test NULL costs, unknown dates, failed inventory, stale billing and API price updates.
- [x] Re-run full backend suite after fixing deterministic SMS initial-slot ordering; record results in development documentation.
