# ChatGPT Bark Critical Alerts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep the existing SMTP alerts and add independent Bark critical/call notifications for both automatic relogin failures and recurring low-quota alerts.

**Architecture:** Add a focused `chatgpt_bark_alerts` service that owns Bark policy, payload construction, transport, and redaction. The existing automatic-task completion hook invokes SMTP and Bark independently and persists channel-specific results. The config API treats the Bark endpoint as a write-only secret and exposes a test route used by the existing React settings card.

**Tech Stack:** Python 3.12, FastAPI, stdlib `urllib.request`/`json`, pytest/unittest.mock, React 19, Ant Design, TypeScript, Vitest/Testing Library.

---

### Task 1: Bark transport and business alert policy

**Files:**
- Create: `services/chatgpt_bark_alerts.py`
- Create: `tests/test_chatgpt_bark_alerts.py`

- [ ] **Step 1: Write failing transport and policy tests**

Create tests with a `FakeResponse` context manager and monkeypatch `urllib.request.urlopen`. Assert the sent request uses JSON `POST`, a 20-second timeout, and exactly these critical fields:

```python
payload = json.loads(request.data.decode("utf-8"))
assert payload["level"] == "critical"
assert payload["call"] == "1"
assert payload["sound"] == "alarm"
assert payload["group"] == "Any Auto Register · Codex"
```

Cover these public calls and outcomes:

```python
send_bark_relogin_alert(
    task_id="task-1",
    quota_report=REPORT,
    quota_eligible_failure_count=5,
    quota_exhausted_failure_count=2,
    relogin_failed_count=7,
    deleted_account_count=3,
    config=CONFIG,
)

send_bark_quota_threshold_alert(
    task_id="task-2",
    quota_report=REPORT,
    config=CONFIG,
)

send_bark_test_notification(config=CONFIG)
```

Expected reasons are `bark_disabled`, `below_threshold`, `quota_alert_disabled`, `quota_not_below_threshold`, `bark_not_configured`, `invalid_bark_endpoint`, `send_failed`, and `sent`. Verify equality does not trigger either threshold, invalid JSON and `code != 200` fail, and logs/results never contain `BARK_DEVICE_SECRET` or the request body.

- [ ] **Step 2: Run the new tests and verify RED**

Run:

```bash
uv run --python 3.12 --with-requirements requirements.txt --with pytest \
  pytest tests/test_chatgpt_bark_alerts.py -q
```

Expected: collection fails because `services.chatgpt_bark_alerts` does not exist.

- [ ] **Step 3: Implement the Bark service**

Implement these constants and public functions:

```python
BARK_TIMEOUT_SECONDS = 20
BARK_GROUP = "Any Auto Register · Codex"
BARK_SOUND = "alarm"
USD_CENT = Decimal("0.01")

def send_bark_relogin_alert(...) -> dict[str, object]: ...
def send_bark_quota_threshold_alert(...) -> dict[str, object]: ...
def send_bark_test_notification(...) -> dict[str, object]: ...
```

Use a private `_send_bark` that validates `urlsplit(endpoint).scheme in {"http", "https"}` and `netloc`, removes trailing `/`, serializes UTF-8 JSON, sends a `urllib.request.Request(..., method="POST", headers={"Content-Type": "application/json"})`, and accepts only HTTP 2xx plus JSON `{"code": 200}`. Catch exceptions, log only `type(exc).__name__`, and return only the error type.

The relogin title must be:

```python
f"${report.estimated_remaining_usd:.2f}｜正常可用账号 {report.account_count} 个｜Codex 重登失败账号告警"
```

The quota title must end with `Codex 剩余额度不足告警`. Bodies include only aggregate metrics and the task ID.

- [ ] **Step 4: Run the Bark unit tests and verify GREEN**

Run the Task 1 command again. Expected: all tests pass.

- [ ] **Step 5: Commit the Bark service**

```bash
git add services/chatgpt_bark_alerts.py tests/test_chatgpt_bark_alerts.py
git commit -m "feat(alerts): add Bark critical notifications"
```

### Task 2: Write-only Bark configuration and test endpoint

**Files:**
- Modify: `api/config.py`
- Create: `tests/test_chatgpt_bark_config.py`

- [ ] **Step 1: Write failing config API tests**

Use a temporary/mocked `config_store` and direct route calls to assert:

```python
assert get_config()["bark_enabled"] == "0"
assert get_config()["bark_endpoint"] == ""
```

Update with `bark_enabled=True` and `bark_endpoint="https://api.day.app/BARK_DEVICE_SECRET"`; verify the store receives `"1"` and the full endpoint. Update again with `bark_endpoint=""`; verify the saved endpoint is preserved. Reject non-HTTP(S) values with HTTP 400 without echoing the value.

Patch `send_bark_test_notification` for `/config/bark/test` and verify unsaved form values override saved values, while an empty form endpoint uses the saved endpoint. Success returns `测试 Bark 强提醒已发送`; disabled, missing, invalid, and transport failures return sanitized 400/502 errors.

- [ ] **Step 2: Run the config tests and verify RED**

```bash
uv run --python 3.12 --with-requirements requirements.txt --with pytest \
  pytest tests/test_chatgpt_bark_config.py -q
```

Expected: failures because Bark keys and route do not exist.

- [ ] **Step 3: Add keys, normalization, redaction, and route**

In `api/config.py`, add:

```python
_BARK_CONFIG_KEYS = {"bark_enabled", "bark_endpoint"}
```

Add both keys to `CONFIG_KEYS`. Default `bark_enabled` to `"0"`; always set the returned `bark_endpoint` to `""`. Normalize `bark_enabled` alongside other booleans. If an update contains an empty `bark_endpoint`, remove it from `safe`; otherwise validate its parsed URL without returning the endpoint in errors.

Add `POST /config/bark/test` using the existing generic test request model. Merge stored and form Bark values, preserve the stored endpoint on an empty override, call `send_bark_test_notification`, and map result reasons to sanitized HTTP responses.

- [ ] **Step 4: Run config tests and existing config regression tests**

```bash
uv run --python 3.12 --with-requirements requirements.txt --with pytest \
  pytest tests/test_chatgpt_bark_config.py \
         tests/test_config_store_env_fallback.py -q
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit the config API**

```bash
git add api/config.py tests/test_chatgpt_bark_config.py
git commit -m "feat(settings): configure Bark critical alerts"
```

### Task 3: Automatic task integration and channel-specific metadata

**Files:**
- Modify: `api/tasks.py`
- Modify: `tests/test_chatgpt_relogin_task.py`

- [ ] **Step 1: Extend task test setup with Bark mocks**

Patch both public Bark functions in the existing test class setup:

```python
self.bark_alert_sender = mock.patch(
    "services.chatgpt_bark_alerts.send_bark_relogin_alert",
    return_value={"sent": False, "reason": "bark_disabled", "threshold": 5},
).start()
self.bark_quota_alert_sender = mock.patch(
    "services.chatgpt_bark_alerts.send_bark_quota_threshold_alert",
    return_value={"sent": False, "reason": "bark_disabled", "threshold_usd": "1200.00"},
).start()
```

Add focused tests proving:

- both Bark calls receive the same final `quota_report` used by email;
- SMTP send failure does not suppress Bark calls;
- Bark failure does not change a `done` task or SMTP results;
- quota query failure suppresses Bark quota alerts but still evaluates Bark relogin alerts using the fallback report;
- stopped tasks call neither Bark function;
- persisted summaries include all Bark result fields.

- [ ] **Step 2: Run focused task tests and verify RED**

```bash
uv run --python 3.12 --with-requirements requirements.txt --with pytest \
  pytest tests/test_chatgpt_relogin_task.py -q
```

Expected: new Bark call and metadata assertions fail.

- [ ] **Step 3: Invoke Bark independently after SMTP**

Extend `TASK_SUMMARY_META_KEYS` with:

```python
"bark_alert_sent",
"bark_alert_reason",
"bark_quota_alert_sent",
"bark_quota_alert_reason",
```

At the completed automatic task hook, call `send_bark_relogin_alert` in its own `try/except` after the SMTP relogin result has been persisted. Store sent, reason, optional threshold, and sanitized error type. Log explicit Bark messages for sent, disabled, below threshold, missing config, invalid config, and send failure.

Inside `if quota_query_succeeded`, repeat the independent flow for `send_bark_quota_threshold_alert`. In the stopped-task branch set both Bark sent flags to false and both reasons to `task_stopped`.

- [ ] **Step 4: Run task and alert regressions**

```bash
uv run --python 3.12 --with-requirements requirements.txt --with pytest \
  pytest tests/test_chatgpt_relogin_task.py \
         tests/test_chatgpt_auto_relogin_alerts.py \
         tests/test_chatgpt_bark_alerts.py -q
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit task integration**

```bash
git add api/tasks.py tests/test_chatgpt_relogin_task.py
git commit -m "feat(chatgpt): dispatch Bark automation alerts"
```

### Task 4: React settings and test notification UX

**Files:**
- Modify: `frontend/src/components/settings/ChatGPTAutoReloginSection.tsx`
- Modify: `frontend/src/components/settings/ChatGPTAutoReloginSection.test.tsx`
- Modify: `frontend/src/pages/Settings.tsx`
- Modify: `frontend/src/pages/Settings.test.tsx`

- [ ] **Step 1: Write failing component and settings tests**

Assert the card contains:

```tsx
screen.getByRole('switch', { name: '启用 Bark 强提醒' })
screen.getByLabelText('Bark 推送地址')
screen.getByRole('button', { name: '发送测试 Bark 通知' })
screen.getByText(/critical.*call=1/i)
```

Click the test button and assert `/config/bark/test` receives only current `bark_enabled` and `bark_endpoint`. Verify success/failure status, retry, and in-flight duplicate-click protection.

In `Settings.test.tsx`, include server values `bark_enabled: "1"` and `bark_endpoint: "server-secret-must-not-return"`; assert the switch hydrates true, the endpoint input is empty, and the save payload contains `bark_enabled: true` plus `bark_endpoint: ""`.

- [ ] **Step 2: Run frontend tests and verify RED**

```bash
cd frontend
npm test -- src/components/settings/ChatGPTAutoReloginSection.test.tsx src/pages/Settings.test.tsx
```

Expected: failures because the Bark controls do not exist.

- [ ] **Step 3: Implement the Bark form and test action**

Rename the divider to `告警通知`, then add:

```tsx
<Form.Item name="bark_enabled" label="启用 Bark 强提醒" valuePropName="checked" initialValue={false}>
  <Switch aria-label="启用 Bark 强提醒" checkedChildren="开启" unCheckedChildren="关闭" />
</Form.Item>
<Form.Item
  name="bark_endpoint"
  label="Bark 推送地址"
  extra="粘贴 Bark App 提供的完整地址；留空保留已保存地址。业务告警固定使用 critical + call=1 强提醒。"
>
  <Input.Password aria-label="Bark 推送地址" autoComplete="new-password" placeholder="https://api.day.app/你的Key" />
</Form.Item>
```

Add independent Bark test state and a `sendBarkTest` handler posting current form values to `/config/bark/test`. Disable/reuse the loading state during an in-flight request and render an accessible success/error result.

In `Settings.tsx`, parse `bark_enabled` during load/save, always clear `data.bark_endpoint` after loading, and include both Bark fields in `form.setFieldsValue` after save with the endpoint reset to `""`.

- [ ] **Step 4: Run focused tests and production build**

```bash
cd frontend
npm test -- src/components/settings/ChatGPTAutoReloginSection.test.tsx src/pages/Settings.test.tsx
npm run build
```

Expected: focused tests and TypeScript/Vite build pass.

- [ ] **Step 5: Commit frontend settings**

```bash
git add frontend/src/components/settings/ChatGPTAutoReloginSection.tsx \
        frontend/src/components/settings/ChatGPTAutoReloginSection.test.tsx \
        frontend/src/pages/Settings.tsx \
        frontend/src/pages/Settings.test.tsx
git commit -m "feat(settings): add Bark critical alert controls"
```

### Task 5: Full verification and deployment readiness

**Files:**
- Verify only; no expected source changes.

- [ ] **Step 1: Compile modified Python modules**

```bash
uv run --python 3.12 --with-requirements requirements.txt \
  python -m py_compile services/chatgpt_bark_alerts.py api/config.py api/tasks.py
```

Expected: exit 0 with no output.

- [ ] **Step 2: Run full backend tests**

```bash
uv run --python 3.12 --with-requirements requirements.txt --with pytest pytest -q
```

Expected: all non-baseline tests pass; any pre-existing flaky failure is isolated and rerun before classification.

- [ ] **Step 3: Run full frontend tests and build**

```bash
cd frontend
npm test
npm run build
```

Expected: all tests pass and production assets build.

- [ ] **Step 4: Inspect secrets and diffs**

```bash
git diff --check HEAD~4..HEAD
rg -n "BARK_DEVICE_SECRET" . -g '!docs/superpowers/plans/**'
git status --short
```

Expected: no whitespace errors, fixture secrets appear only in tests, and the worktree is clean after commits.

- [ ] **Step 5: Prepare deployment without sending a business alert**

Confirm the server has no running automatic task, upload an immutable release, compile/import it offline, atomically switch the Any Auto Register release, and restart only `any-auto-register.service`. Verify HTTP 200, zero new error logs, unchanged Codex2API container start times, and use the Bark test button for the real iPhone delivery check.
