# Mail Import Automatic Detection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make mailbox import detect Microsoft, MailAPI, AppleMail, iCloud MFA, and URL credential formats automatically while preserving every existing manually selected import path.

**Architecture:** Add a pure backend detector that classifies and safely groups input before any write, expose its result through a read-only detection endpoint, and add an auto-import orchestrator that delegates to the two existing strategies. The React panel uses that endpoint for live feedback but submits `type=auto` so the backend remains authoritative.

**Tech Stack:** Python 3.11, FastAPI, Pydantic, SQLModel, pytest/unittest, React 19, TypeScript, Ant Design, Vitest, Testing Library.

---

### Task 1: Define automatic detection schemas and pure classifier

**Files:**
- Create: `services/mail_imports/auto_detection.py`
- Modify: `services/mail_imports/schemas.py`
- Modify: `services/mail_imports/__init__.py`
- Test: `tests/test_mail_imports_auto_detection.py`

- [ ] **Step 1: Write failing classifier tests**

Cover these exact cases in `tests/test_mail_imports_auto_detection.py`:

```python
def test_detects_microsoft_mailapi_and_apple_rows_in_one_payload():
    result = detect_mail_import_content(
        "oauth@outlook.com----password----client-id----refresh-token\n"
        "mailapi@hotmail.com----https://mail.example.test/inbox/token\n"
        "mfa@gmail.com----chatgpt-password----JBSWY3DPEHPK3PXP"
    )
    assert [row.provider for row in result.rows] == [
        "microsoft", "microsoft", "applemail"
    ]
    assert result.microsoft_count == 2
    assert result.applemail_count == 1
    assert result.unresolved_count == 0


def test_unknown_four_part_oauth_requires_manual_provider():
    result = detect_mail_import_content(
        "user@custom.example----password----client-id----refresh-token"
    )
    assert result.unresolved_count == 1
    assert result.rows[0].provider is None
    assert "手动指定" in result.rows[0].message
```

Also test Apple URL credentials, forgot-password URL rows, iCloud domains, JSON input, supplier headings, duplicate email detection, and URL/token redaction in errors.

- [ ] **Step 2: Run the detector tests and verify RED**

Run:

```bash
uv run --python 3.11 --with pytest --with-requirements requirements.txt \
  python -m pytest tests/test_mail_imports_auto_detection.py -q
```

Expected: collection fails because `services.mail_imports.auto_detection` does not exist.

- [ ] **Step 3: Add schema types**

In `services/mail_imports/schemas.py`, keep snapshot/delete provider types unchanged and add:

```python
MailImportStoredProviderType = Literal["applemail", "microsoft"]
MailImportExecuteProviderType = Literal["auto", "applemail", "microsoft"]


class MailImportDetectRequest(BaseModel):
    content: str


class MailImportDetectedRow(BaseModel):
    line_number: int
    email: str = ""
    provider: MailImportStoredProviderType | None = None
    account_type: MailImportAccountType | None = None
    resolved: bool = False
    message: str = ""


class MailImportDetectionResponse(BaseModel):
    total: int
    microsoft_count: int
    applemail_count: int
    unresolved_count: int
    rows: list[MailImportDetectedRow] = Field(default_factory=list)
```

Change only `MailImportExecuteRequest.type` to `MailImportExecuteProviderType`. Keep `MailImportSnapshotRequest`, `MailImportDeleteRequest`, `MailImportBatchDeleteRequest`, `MailImportSnapshot`, and strategy descriptors restricted to stored provider types.

- [ ] **Step 4: Implement the pure detector**

Create `services/mail_imports/auto_detection.py` with these public boundaries:

```python
@dataclass(frozen=True)
class DetectedMailImportRow:
    line_number: int
    raw: str
    email: str
    provider: str | None
    account_type: str | None
    message: str = ""


@dataclass(frozen=True)
class DetectedMailImportBatch:
    rows: tuple[DetectedMailImportRow, ...]

    def rows_for(self, provider: str) -> list[DetectedMailImportRow]:
        return [row for row in self.rows if row.provider == provider]

    def to_response(self) -> MailImportDetectionResponse:
        return MailImportDetectionResponse(
            total=len(self.rows),
            microsoft_count=len(self.rows_for("microsoft")),
            applemail_count=len(self.rows_for("applemail")),
            unresolved_count=sum(row.provider is None for row in self.rows),
            rows=[row.to_response_item() for row in self.rows],
        )
```

Expose `detect_mail_import_content(content: str) -> DetectedMailImportBatch`. Use the precedence from the design: JSON→AppleMail; two-part HTTP→Microsoft MailAPI; three-part MFA/reset URL→AppleMail; URL credential rows→AppleMail; known Microsoft/iCloud domains resolve four-part OAuth; other four-part OAuth stays unresolved. Never include raw credential fields in `message` or the API response. `DetectedMailImportRow.to_response_item()` must copy only line number, email, provider, account type, resolution flag, and safe message.

- [ ] **Step 5: Run the detector tests and verify GREEN**

Run the Task 1 command. Expected: all tests pass.

- [ ] **Step 6: Commit Task 1**

```bash
git add services/mail_imports/auto_detection.py services/mail_imports/schemas.py \
  services/mail_imports/__init__.py tests/test_mail_imports_auto_detection.py
git commit -m "feat(mail-import): detect mailbox formats automatically"
```

### Task 2: Add read-only detection API and auto-import orchestration

**Files:**
- Create: `services/mail_imports/auto_import.py`
- Modify: `api/mail_imports.py`
- Modify: `services/mail_imports/__init__.py`
- Test: `tests/test_mail_imports_service.py`
- Test: `tests/test_mail_imports_api.py`

- [ ] **Step 1: Write failing orchestration tests**

Add tests proving:

```python
def test_auto_import_delegates_each_group_without_reclassifying_manual_requests():
    request = MailImportExecuteRequest(
        type="auto",
        content=(
            "mailapi@outlook.com----https://mail.example.test/inbox/token\n"
            "mfa@gmail.com----chatgpt-password----JBSWY3DPEHPK3PXP"
        ),
        bind_to_config=True,
    )
    response = AutoMailImportStrategy().execute(request)
    assert response.summary.total == 2
    assert response.summary.success == 2
    assert response.meta["providers"] == ["microsoft", "applemail"]
    assert response.meta["config_binding_preserved"] is True
```

Mock the two stored strategies so this test verifies grouping, per-provider request fields, aggregate errors, mixed-batch config preservation, Microsoft-only alias settings, and Apple-only filename/pool settings. Add a separate regression asserting existing `type="microsoft"` and `type="applemail"` calls still go directly to their current strategies.

- [ ] **Step 2: Write failing API tests**

In `tests/test_mail_imports_api.py`, use `TestClient(app)` to assert:

```python
response = client.post(
    "/api/mail-imports/detect",
    json={"content": "demo@hotmail.com----https://mail.example.test/inbox/token"},
)
assert response.status_code == 200
assert response.json()["microsoft_count"] == 1
assert "token" not in response.text
```

Also assert zero recognized rows returns a bounded 400 on formal import and detection itself remains read-only.

- [ ] **Step 3: Run Task 2 tests and verify RED**

```bash
uv run --python 3.11 --with pytest --with-requirements requirements.txt \
  python -m pytest tests/test_mail_imports_service.py tests/test_mail_imports_api.py -q
```

Expected: failures for the missing auto strategy and `/detect` route.

- [ ] **Step 4: Implement the auto strategy**

Create `services/mail_imports/auto_import.py`:

```python
class AutoMailImportStrategy:
    def __init__(self, microsoft=None, applemail=None):
        self._microsoft = microsoft or MicrosoftMailImportStrategy()
        self._applemail = applemail or AppleMailImportStrategy()
```

Expose `detect(content: str) -> MailImportDetectionResponse` and `execute(request: MailImportExecuteRequest) -> MailImportResponse`. The execute method must reject duplicate/unresolved rows before delegating those rows, pass only each group’s original lines to its stored strategy, call both with `bind_to_config=False` for mixed batches, and preserve the configured provider for mixed results. For a single provider, preserve the current binding behavior. Aggregate summaries and put per-provider summaries and affected providers into `meta`.

- [ ] **Step 5: Wire routes without changing snapshot/delete semantics**

In `api/mail_imports.py` add:

```python
@router.post("/detect")
def detect_mail_import(body: MailImportDetectRequest):
    return auto_mail_import_strategy.detect(body.content)
```

Branch only `execute_mail_import`: `type="auto"` uses the auto strategy; stored types continue through `mail_import_registry`. Keep snapshot, delete, and batch-delete unchanged.

- [ ] **Step 6: Run Task 2 tests and verify GREEN**

Run the Task 2 command. Expected: all tests pass.

- [ ] **Step 7: Commit Task 2**

```bash
git add services/mail_imports/auto_import.py services/mail_imports/__init__.py \
  api/mail_imports.py tests/test_mail_imports_service.py tests/test_mail_imports_api.py
git commit -m "feat(mail-import): route auto-detected mailbox groups"
```

### Task 3: Make the mailbox import panel automatic by default

**Files:**
- Modify: `frontend/src/components/settings/MailImportPanel.tsx`
- Create: `frontend/src/components/settings/MailImportPanel.test.tsx`
- Modify: `frontend/src/pages/Settings.test.tsx`

- [ ] **Step 1: Write failing UI tests**

Render `MailImportPanel` inside `App` and `Form`. Mock provider/snapshot calls, then assert:

```tsx
await user.type(screen.getByRole('textbox'),
  'demo@hotmail.com----https://mail.example.test/inbox/token')

await waitFor(() => expect(apiFetch).toHaveBeenCalledWith(
  '/mail-imports/detect',
  expect.objectContaining({ method: 'POST' }),
))
expect(await screen.findByText('Microsoft 1')).toBeTruthy()

await user.click(screen.getByRole('button', { name: '确认导入' }))
const importCall = vi.mocked(apiFetch).mock.calls.find(
  ([path, options]) => path === '/mail-imports' && options?.method === 'POST'
)
expect(JSON.parse(String(importCall?.[1]?.body))).toMatchObject({ type: 'auto' })
```

Also test stale detection responses cannot replace a newer result, Apple detection reveals filename input, Microsoft detection reveals alias controls, unresolved rows show a manual fallback, and the top provider select still changes snapshots/deletion targets.

- [ ] **Step 2: Run the component test and verify RED**

```bash
cd frontend
pnpm test -- src/components/settings/MailImportPanel.test.tsx
```

Expected: failures because the panel does not call `/mail-imports/detect` and submits the selected snapshot type.

- [ ] **Step 3: Add automatic detection state and debounced requests**

In `MailImportPanel.tsx` add `MailImportDetection`, `detection`, `detecting`, `manualOverride`, and a monotonic request epoch. When trimmed content changes, debounce 350 ms, POST `{content}`, ignore responses from earlier epochs, and clear detection when content becomes empty.

Keep `selectedType` exclusively for snapshots and deletion. `handleImport` must use:

```ts
const apiType = manualOverride ? toImportApiType(selectedType) : 'auto'
const body = {
  type: apiType,
  content: payload,
  enabled: true,
  bind_to_config: true,
  alias_split_enabled: aliasSplitEnabled,
  alias_split_count: aliasSplitCount,
  alias_include_original: aliasIncludeOriginal,
  filename: filename.trim(),
  pool_dir: String(form.getFieldValue('applemail_pool_dir') || 'mail').trim() || 'mail',
}
```

- [ ] **Step 4: Add compact detection feedback and fallback**

Above the text area show an accessible status row with `自动识别`, provider count tags, and a spinner. Show Apple filename when `applemail_count > 0`; show alias controls when `microsoft_count > 0`. If `unresolved_count > 0`, show the safe row messages and a button labeled `按当前邮箱池类型导入`; activating it sets `manualOverride=true`. Changing or clearing content resets the override.

- [ ] **Step 5: Refresh affected snapshots after import**

Read `response.meta.providers`. Choose the last affected provider as the visible snapshot type, load that snapshot, and preserve the existing form binding behavior for single-provider responses. For mixed responses, do not overwrite the current `mail_import_source`.

- [ ] **Step 6: Run UI tests and production build**

```bash
cd frontend
pnpm test -- src/components/settings/MailImportPanel.test.tsx src/pages/Settings.test.tsx
pnpm build
```

Expected: tests pass and TypeScript/Vite build exits 0.

- [ ] **Step 7: Commit Task 3**

```bash
git add frontend/src/components/settings/MailImportPanel.tsx \
  frontend/src/components/settings/MailImportPanel.test.tsx frontend/src/pages/Settings.test.tsx
git commit -m "feat(mail-import): default to automatic type detection"
```

### Task 4: Full compatibility verification, review, repository sync, and production deployment

**Files:**
- Modify after build: `static/index.html`
- Modify after build: `static/assets/*`

- [ ] **Step 1: Run all backend tests**

```bash
uv run --python 3.11 --with pytest --with-requirements requirements.txt \
  python -m pytest -q
```

Expected: the complete suite passes with no new failures.

- [ ] **Step 2: Run all frontend checks**

```bash
cd frontend
pnpm test
pnpm lint
pnpm build
```

Expected: all commands exit 0. Copy the generated `frontend/dist/` files into the tracked `static/` deployment assets using the project’s existing build workflow, then commit those generated asset changes.

- [ ] **Step 3: Request code review and resolve all Critical/Important findings**

Review the diff from `285832c` through the feature HEAD. Require explicit checks for secret redaction, mixed-batch configuration preservation, manual import compatibility, detector race handling, and unchanged snapshot/delete behavior.

- [ ] **Step 4: Push the verified HEAD to `origin/main`**

Fetch `origin/main`, require it to be an ancestor of HEAD, then push the feature branch and `HEAD:main`. Verify both remote references point to the same SHA.

- [ ] **Step 5: Deploy an immutable release**

Confirm zero `running`/`pending` tasks, create and verify a SQLite online backup, archive the exact pushed commit, include the fresh `static/` assets, compile Python modules, and run a pre-switch detection smoke test containing only synthetic credentials. Atomically repoint `/www/any-auto-register/current`, restart `any-auto-register.service`, and roll back to `main-20260810-1317-285832c` if loopback HTTP does not return 200.

- [ ] **Step 6: Verify production**

Require: service active, loopback and public HTTP 200, deployed source hashes match the pushed commit, database `quick_check=ok`, zero active tasks after observation, `/api/mail-imports/detect` returns safe counts without credential values, a synthetic ambiguous OAuth row remains unresolved, and existing snapshots still load for Microsoft and AppleMail.
