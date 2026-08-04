# SMS Pool Full Code Display Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show and copy complete SMS pool card codes on the authenticated management page while preserving masked values for legacy API consumers and task logs.

**Architecture:** Add an explicit full `code` field to the existing authenticated SMS pool list serializer and retain `code_hint` for compatibility. Update only the SMS pool table to render/copy `code`; all task and error log redaction remains unchanged.

**Tech Stack:** Python 3, FastAPI, SQLModel, unittest/pytest, React 19, Ant Design, TypeScript, Vitest/Testing Library.

---

## File map

- `api/sms_pool.py`: serialize the full card code for authenticated list consumers.
- `tests/test_sms_pool.py`: lock full-code API behavior and legacy hint compatibility.
- `frontend/src/pages/SmsPool.tsx`: type, display and copy the full `code` value.
- `frontend/src/pages/SmsPool.test.tsx`: lock full-code display and absence of masked UI text.

### Task 1: Return the complete card code from the list API

**Files:**
- Modify: `tests/test_sms_pool.py:207-225`
- Modify: `api/sms_pool.py:7-31`

- [ ] **Step 1: Replace the old secrecy test with the desired failing API contract**

Rename the test and assert both the new full field and retained hint:

```python
def test_api_list_returns_full_card_code_and_legacy_hint(self):
    with (
        mock.patch("api.sms_pool.engine", self.engine),
        mock.patch("api.sms_pool.sms_pool_service", self.pool),
    ):
        imported = import_sms_pool_items(
            SmsPoolImportRequest(
                content="bei-sms-API-SECRET-0001",
                default_base_url="https://sms.example.com/box",
            )
        )
        response = list_sms_pool_items(status=None, page=1, page_size=50)
        stats = get_sms_pool_stats()

    assert imported["imported"] == 1
    assert stats["unused"] == 1
    assert len(response["items"]) == 1
    assert response["items"][0]["code"] == "bei-sms-API-SECRET-0001"
    assert response["items"][0]["code_hint"] == "bei-****-0001"
```

The equality intentionally reads `response["items"][0]["code"]`; current code must fail with a missing-key mismatch.

- [ ] **Step 2: Run the exact test and verify RED**

Run:

```bash
PYTHONPATH=. .venv/bin/pytest tests/test_sms_pool.py::SmsPoolServiceTests::test_api_list_returns_full_card_code_and_legacy_hint -q
```

Expected: FAIL because the serialized item does not contain `code`.

- [ ] **Step 3: Add the minimal full-code field**

Keep `mask_sms_code` and add one field in `api.sms_pool._serialize_item`:

```python
def _serialize_item(row: SmsPoolItemModel) -> dict:
    return {
        "id": row.id,
        "code": row.code,
        "code_hint": mask_sms_code(row.code),
        "base_url": row.base_url,
        "status": row.status,
        "reserved_task_id": row.reserved_task_id,
        "used_by_email": row.used_by_email,
        "created_at": row.created_at,
        "reserved_at": row.reserved_at,
        "used_at": row.used_at,
        "updated_at": row.updated_at,
    }
```

Do not change `core.sms_pool.mask_sms_code` or any task/log serializer.

- [ ] **Step 4: Run the SMS pool backend suite and verify GREEN**

Run:

```bash
PYTHONPATH=. .venv/bin/pytest tests/test_sms_pool.py -q
```

Expected: all SMS pool tests pass.

- [ ] **Step 5: Commit the API contract**

```bash
git add api/sms_pool.py tests/test_sms_pool.py
git commit -m "feat: expose full SMS pool card codes"
```

### Task 2: Display and copy the complete code

**Files:**
- Modify: `frontend/src/pages/SmsPool.test.tsx:40-100`
- Modify: `frontend/src/pages/SmsPool.tsx:27-149`

- [ ] **Step 1: Change the response fixture to contain distinct full and masked values**

Each fixture item must contain both fields so the test proves which one the page uses:

```typescript
{
  id: 1,
  code: 'bei-sms-FULL-SECRET-0001',
  code_hint: 'bei-****-0001',
  base_url: 'https://sms.example.com/box',
  status: 'unused',
  created_at: '2026-08-01T00:00:00Z',
}
```

Use equivalent unique full values for items 2 and 3.

- [ ] **Step 2: Write the failing full-display test**

Rename the test and assert the complete values are visible while hints are absent:

```typescript
it('shows full cards, receive URLs, counts and usage states', async () => {
  render(<SmsPool />)

  expect(await screen.findByText('SMS接码池')).toBeTruthy()
  expect(await screen.findByText('bei-sms-FULL-SECRET-0001')).toBeTruthy()
  expect(screen.getByText('bei-sms-FULL-SECRET-0002')).toBeTruthy()
  expect(screen.queryByText('bei-****-0001')).toBeNull()
  expect(screen.queryByText('bei-****-0002')).toBeNull()
  expect(screen.getAllByText('https://sms.example.com/box').length).toBeGreaterThan(0)
  expect(screen.getByText('未使用')).toBeTruthy()
  expect(screen.getByText('已使用')).toBeTruthy()
  expect(screen.getByText('使用中')).toBeTruthy()
  expect(screen.getByText('可用 1')).toBeTruthy()
})
```

Update later test waits to use the complete item-1 value.

- [ ] **Step 3: Add a failing copy-source test**

Import `within`, intercept the browser copy command and capture the selected text:

```typescript
afterEach(() => {
  cleanup()
  delete (document as Document & { execCommand?: (command: string) => boolean }).execCommand
})

it('copies the complete card code', async () => {
  let copiedText = ''
  Object.defineProperty(document, 'execCommand', {
    configurable: true,
    value: vi.fn(() => {
      copiedText = document.getSelection()?.toString() || ''
      return true
    }),
  })
  const user = userEvent.setup()
  render(<SmsPool />)

  const code = await screen.findByText('bei-sms-FULL-SECRET-0001')
  const row = code.closest('tr')
  expect(row).toBeTruthy()
  await user.click(within(row as HTMLTableRowElement).getByRole('button', { name: '复制' }))

  expect(copiedText).toBe('bei-sms-FULL-SECRET-0001')
})
```

This verifies Ant Design receives the full string as its explicit copy source, not merely as visible text.

- [ ] **Step 4: Run the component test and verify RED**

Run from `frontend/`:

```bash
npm test -- src/pages/SmsPool.test.tsx
```

Expected: FAIL because the table still reads `code_hint`.

- [ ] **Step 5: Change the typed field and table column**

In `SmsPool.tsx`, require both API fields and render/copy `code`:

```tsx
interface SmsPoolItem {
  id: number
  code: string
  code_hint: string
  base_url: string
  status: SmsPoolStatus
  reserved_task_id?: string
  used_by_email?: string
  created_at?: string
  reserved_at?: string
  used_at?: string
}

{
  title: '接码卡密',
  dataIndex: 'code',
  key: 'code',
  width: 260,
  render: value => {
    const code = String(value || '')
    return (
      <Typography.Text code copyable={code ? { text: code } : false}>
        {code || '-'}
      </Typography.Text>
    )
  },
}
```

The larger width prevents the newly visible value from being unnecessarily cramped. Do not add ellipsis or another reveal interaction.

- [ ] **Step 6: Run focused tests and production build**

Run from `frontend/`:

```bash
npm test -- src/pages/SmsPool.test.tsx
npm run build
```

Expected: the SMS pool tests and production build pass.

- [ ] **Step 7: Commit the UI behavior**

```bash
git add frontend/src/pages/SmsPool.tsx frontend/src/pages/SmsPool.test.tsx
git commit -m "feat: show full SMS pool card codes"
```

### Task 3: Review, verify and deploy

**Files:**
- Verify: all files changed since the production base
- Reference: `docs/superpowers/specs/2026-08-05-sms-pool-full-code-display-design.md`

- [ ] **Step 1: Run focused cross-layer regression**

```bash
PYTHONPATH=. .venv/bin/pytest tests/test_sms_pool.py -q
cd frontend
npm test -- src/pages/SmsPool.test.tsx
```

Expected: all focused backend and frontend tests pass.

- [ ] **Step 2: Perform independent spec and quality reviews**

Review the API contract, authenticated route, legacy hint compatibility, full display/copy source, unchanged task-log redaction and test quality. Resolve every Critical or Important finding and rerun affected tests.

- [ ] **Step 3: Run complete local verification**

```bash
PYTHONPATH=. .venv/bin/pytest -q
cd frontend
npm test
npm run build
```

Expected: full backend, full frontend and production build pass.

- [ ] **Step 4: Push and stage a production release**

Push the verified HEAD to `origin/main`. Build a release from `git archive` plus the freshly generated `static/` assets, upload it under `/www/any-auto-register/releases/<commit>`, compile the changed Python modules and verify the new API serializer with a synthetic `SmsPoolItemModel` without printing any production card values.

- [ ] **Step 5: Switch only while the automation is idle**

Confirm there are no pending/running tasks, atomically point `/www/any-auto-register/current` to the new release and restart only `any-auto-register.service`. Retain the previous release symlink target.

- [ ] **Step 6: Verify production without printing card secrets into logs**

Use an authenticated server-local call or direct serializer assertion to confirm each SMS list item has `code` equal to the stored card value and a separate masked `code_hint`; print only booleans/counts. Verify the public bundle contains the new column contract, the service is active, database `quick_check` is `ok`, automatic relogin remains enabled with interval 10 and threshold 20, and recent service logs have no new errors.
