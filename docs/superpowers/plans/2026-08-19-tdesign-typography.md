# TDesign Typography Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bundle the official TDesign number font and apply one TDesign typography stack to the complete Ant Design interface while retaining monospace text where alignment is functional.

**Architecture:** Store `TCloudNumberVF.ttf` under Vite's public assets, register it once in the global stylesheet, and expose a shared TypeScript font-family constant to both light and dark Ant Design themes. CSS provides the same stack for native elements; existing explicit monospace regions remain untouched.

**Tech Stack:** React 19, TypeScript 5.9, Ant Design 5, Vite 8, Vitest 4, CSS `@font-face`.

---

### Task 1: Add the typography contract test

**Files:**
- Create: `frontend/src/typography.test.ts`
- Read: `frontend/src/index.css`
- Read: `frontend/src/theme.ts`

- [ ] **Step 1: Write a failing test for the global and theme contracts**

Create a Vitest test that reads `index.css` and imports both theme objects. Assert that CSS registers `TCloudNumber`, uses `font-display: swap`, points to `/fonts/TCloudNumberVF.ttf`, applies the TDesign font variable to `body`, and preserves a monospace rule for `pre` and `code`. Assert that both Ant Design themes expose the same font stack through `token.fontFamily`.

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
cd frontend && npm test -- src/typography.test.ts
```

Expected: FAIL because `TCloudNumber`, the global font variable, and Ant Design `fontFamily` tokens do not exist yet.

### Task 2: Bundle and wire the official font

**Files:**
- Create: `frontend/public/fonts/TCloudNumberVF.ttf`
- Modify: `frontend/src/index.css`
- Modify: `frontend/src/theme.ts`

- [ ] **Step 1: Add the official TDesign font asset**

Download `TCloudNumber v1.010` from the official Tencent TDesign asset URL referenced by the TDesign repository and extract `TCloudNumberVF.ttf` into `frontend/public/fonts/`. Record its SHA-256 before committing.

- [ ] **Step 2: Register the font and global stack**

At the top of `index.css`, add an `@font-face` named `TCloudNumber` with the local `/fonts/TCloudNumberVF.ttf` source, variable weight range, normal style, and `font-display: swap`. Add `--font-family-tdesign` and `--font-family-mono` variables. Apply the TDesign stack to `body`, native form controls, and inherited interface text. Apply the mono variable to `pre` and `code` without changing existing component-level monospace overrides.

- [ ] **Step 3: Set the Ant Design font token**

Export one `tdesignFontFamily` constant from `theme.ts` and assign it to `token.fontFamily` in both `darkTheme` and `lightTheme`. Keep all existing colors, radii, and component tokens unchanged.

- [ ] **Step 4: Run the focused test and verify GREEN**

Run:

```bash
cd frontend && npm test -- src/typography.test.ts
```

Expected: PASS.

### Task 3: Verify behavior and visual output

**Files:**
- Verify: `frontend/src/index.css`
- Verify: `frontend/src/theme.ts`
- Verify: `frontend/dist/fonts/TCloudNumberVF.ttf`

- [ ] **Step 1: Run full frontend verification**

Run:

```bash
cd frontend
npm test
npm run lint
npm run build
```

Expected: all tests pass, ESLint exits 0, TypeScript/Vite build exits 0, and `dist/fonts/TCloudNumberVF.ttf` exists.

- [ ] **Step 2: Verify the browser-rendered font**

Start the local app, open one representative Chinese dashboard/task page, and inspect computed styles. Confirm body and Ant Design controls resolve to the TDesign stack, numeric glyphs use `TCloudNumber`, Chinese glyphs fall back to the TDesign Chinese system stack, and log/code regions remain monospace. Check one desktop viewport and one mobile viewport.

- [ ] **Step 3: Review the final diff**

Run `git diff --check`, inspect every changed file, and confirm no color, spacing, layout, backend, database, or runtime-account change is included.

### Task 4: Publish and deploy the verified release

**Files:**
- Commit: design, plan, font asset, test, CSS, and theme changes
- Deploy: generated `frontend/dist/` copied to release `static/`

- [ ] **Step 1: Commit and synchronize the repository**

Commit the typography change on the isolated branch, fast-forward `main`, push `origin/main`, and verify local `main` is neither ahead of nor behind the remote.

- [ ] **Step 2: Preflight production**

On `103.144.241.126:55222`, record the current release target, require `any-auto-register.service` to be active, require zero pending/running tasks, and run SQLite `PRAGMA quick_check`. Create and verify an online database backup even though the change is frontend-only.

- [ ] **Step 3: Create and switch an immutable release**

Package the exact verified commit with the freshly built frontend assets under `/www/any-auto-register/releases/<commit>`, preserve shared runtime data, atomically repoint `/www/any-auto-register/current`, and restart only `any-auto-register.service`. Preserve the previous release target for rollback.

- [ ] **Step 4: Verify production**

Require the service to be active, loopback and public endpoints to return HTTP 200, the deployed CSS to reference `TCloudNumberVF.ttf`, the deployed font URL to return HTTP 200, and the database plus backup to report `quick_check=ok`. If any check fails, restore the previous release symlink and restart only `any-auto-register.service`.
