# ChatGPT Recurring Quota Threshold Alert Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a configurable recurring USD quota alert at the end of every automatic Codex2API cycle and remove all per-account rows from business alert emails.

**Architecture:** Fetch one fresh Codex2API account list after relogin/sync completes, summarize it once, and share that report between the existing relogin-failure alert and a new independent quota-threshold alert. Store only report totals in task metadata. Extend the existing settings/API surface with a Decimal USD threshold whose zero value disables quota alerts.

**Tech Stack:** Python 3.10+, Decimal, FastAPI, SQLModel task metadata, EmailMessage/SMTP, React 19, Ant Design, Vitest, pytest, systemd immutable releases.

---

### Task 1: Extend quota reports and add a fresh remote quota reader

**Files:**
- Modify: `services/chatgpt_codex2api_quota.py`
- Modify: `services/chatgpt_codex2api_health.py`
- Modify: `tests/test_chatgpt_codex2api_quota.py`
- Modify: `tests/test_chatgpt_codex2api_health.py`

- [ ] **Step 1: Write failing tests for remote totals and end-of-cycle reads**

Update the quota aggregation test to require `report.remote_account_count == 4` while preserving `report.account_count == 2` and `$98.85` remaining.

Add a health-service test:

```python
def test_fetch_quota_accounts_reads_latest_rows_without_triggering_probe():
    from services import chatgpt_codex2api_health as health

    with mock.patch.object(
        health.cffi_requests,
        "get",
        return_value=FakeResponse({
            "accounts": [{
                "id": 101,
                "email": "one@example.com",
                "status": "active",
                "usage_percent_7d": 53,
                "billed_7d": 68.26,
                "reset_7d_at": "ignored",
            }],
        }),
    ) as request, mock.patch.object(health.cffi_requests, "post") as post:
        rows = health.fetch_codex2api_quota_accounts(config=BASE_CONFIG)

    assert rows == [{
        "remote_id": 101,
        "email": "one@example.com",
        "remote_status": "active",
        "usage_percent_7d": 53,
        "billed_7d": 68.26,
    }]
    assert request.call_args.args[0].endswith(
        "/api/admin/accounts?channel=codex"
    )
    post.assert_not_called()
```

- [ ] **Step 2: Run focused tests and verify missing fields/functions fail**

Run:

```bash
python -m pytest -q tests/test_chatgpt_codex2api_quota.py \
  tests/test_chatgpt_codex2api_health.py
```

Expected: failures mention `remote_account_count` and `fetch_codex2api_quota_accounts`.

- [ ] **Step 3: Implement the minimal fresh-reader/report change**

Add `remote_account_count: int` to `AvailableQuotaReport`. Count every mapping row supplied to `summarize_available_quota`, including exhausted, invalid and unhealthy rows. Keep `account_count` as the number of valid normal/available rows.

Add this public function to the health service:

```python
def fetch_codex2api_quota_accounts(
    *,
    config: Mapping[str, object] | None = None,
) -> list[dict[str, object]]:
    snapshot = dict(config) if config is not None else _get_config()
    base_url = _text(snapshot.get("codex2api_api_url")).rstrip("/")
    admin_key = _text(snapshot.get("codex2api_admin_key"))
    if not base_url or not admin_key:
        raise Codex2APIHealthError("Codex2API 地址或 Admin Key 未配置")
    payload = _get_json(
        base_url,
        "/api/admin/accounts?channel=codex",
        admin_key,
    )
    rows = payload.get("accounts") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise Codex2APIHealthError("Codex2API 账号清单格式无效")
    return [_quota_record(row) for row in rows if isinstance(row, dict)]
```

Export it in `__all__`. Do not trigger settings/runtime/probe endpoints and do not include reset fields.

- [ ] **Step 4: Run both focused test files and verify green**

Run: `python -m pytest -q tests/test_chatgpt_codex2api_quota.py tests/test_chatgpt_codex2api_health.py`

Expected: all tests pass.

- [ ] **Step 5: Commit the fresh quota reader**

```bash
git add services/chatgpt_codex2api_quota.py services/chatgpt_codex2api_health.py \
  tests/test_chatgpt_codex2api_quota.py tests/test_chatgpt_codex2api_health.py
git commit -m "feat(chatgpt): read final Codex2API quota snapshot"
```

### Task 2: Add the configurable USD threshold to the backend API

**Files:**
- Modify: `api/config.py`
- Modify: `tests/test_chatgpt_auto_relogin.py`

- [ ] **Step 1: Write failing configuration tests**

Require `GET /config` to return:

```python
assert response["chatgpt_auto_relogin_quota_alert_threshold_usd"] == "0.00"
```

Add parameterized valid writes for `0`, `1200`, `1200.5`, `1200.55`, asserting normalized storage as `0.00`, `1200.00`, `1200.50`, `1200.55`.

Add invalid writes for `-0.01`, `10000000.01`, `12.345`, `NaN`, `Infinity`, and non-numeric text. Assert HTTP 400 and that `config_store.set_many` is not called.

- [ ] **Step 2: Run configuration tests and verify the new key fails**

Run: `python -m pytest -q tests/test_chatgpt_auto_relogin.py -k "quota_alert_threshold or public_config"`

Expected: failures show the missing key/default/validation.

- [ ] **Step 3: Implement Decimal validation and normalization**

Add the key to `CONFIG_KEYS`; return `0.00` when missing. Add:

```python
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

QUOTA_ALERT_MIN_USD = Decimal("0.00")
QUOTA_ALERT_MAX_USD = Decimal("10000000.00")
USD_CENT = Decimal("0.01")


def _normalize_quota_alert_threshold(value: object) -> str:
    text = str(value or "").strip() or "0"
    try:
        parsed = Decimal(text)
    except (InvalidOperation, ValueError):
        raise HTTPException(
            status_code=400,
            detail="Codex2API 剩余额度告警阈值必须是有效美元金额",
        ) from None
    if not parsed.is_finite():
        raise HTTPException(
            status_code=400,
            detail="Codex2API 剩余额度告警阈值必须是有效美元金额",
        )
    rounded = parsed.quantize(USD_CENT, rounding=ROUND_HALF_UP)
    if parsed != rounded:
        raise HTTPException(
            status_code=400,
            detail="Codex2API 剩余额度告警阈值最多保留两位小数",
        )
    if not QUOTA_ALERT_MIN_USD <= parsed <= QUOTA_ALERT_MAX_USD:
        raise HTTPException(
            status_code=400,
            detail="Codex2API 剩余额度告警阈值必须在 0.00 到 10000000.00 美元之间",
        )
    return f"{rounded:.2f}"
```

Check `is_finite()` before `quantize()` to avoid Decimal special-value exceptions. Invoke the helper when the key is present, then persist its string result.

- [ ] **Step 4: Run the backend configuration tests**

Run: `python -m pytest -q tests/test_chatgpt_auto_relogin.py`

Expected: all tests pass.

- [ ] **Step 5: Commit the backend setting**

```bash
git add api/config.py tests/test_chatgpt_auto_relogin.py
git commit -m "feat(chatgpt): configure quota alert threshold"
```

### Task 3: Remove account lists and add the recurring quota email

**Files:**
- Modify: `services/chatgpt_auto_relogin_alerts.py`
- Modify: `tests/test_chatgpt_auto_relogin_alerts.py`

- [ ] **Step 1: Write failing email tests for compact subjects and bodies**

Update the existing quota-content test so both plain and HTML bodies exclude `a@example.com`, `b@example.com`, per-account table headers, and `$60.53`, while retaining the aggregate `$98.85` and count 2.

Require the relogin alert subject:

```python
assert message["Subject"] == (
    "$98.85｜正常可用账号 2 个｜ChatGPT 重登失败账号告警"
)
```

Add quota-threshold tests:

```python
def test_quota_threshold_alert_sends_below_threshold_every_call(monkeypatch):
    report = summarize_available_quota(QUOTA_ROWS)
    monkeypatch.setattr(alerts.smtplib, "SMTP", FakeSMTP)
    config = {
        **BASE_CONFIG,
        "chatgpt_auto_relogin_quota_alert_threshold_usd": "120.00",
    }
    first = alerts.send_quota_threshold_alert(
        task_id="task-one",
        quota_report=report,
        quota_eligible_failure_count=0,
        quota_exhausted_failure_count=0,
        relogin_failed_count=0,
        deleted_account_count=0,
        config=config,
    )
    second = alerts.send_quota_threshold_alert(
        task_id="task-two",
        quota_report=report,
        quota_eligible_failure_count=0,
        quota_exhausted_failure_count=0,
        relogin_failed_count=0,
        deleted_account_count=0,
        config=config,
    )
    assert first["sent"] is True
    assert second["sent"] is True
    assert len(FakeSMTP.instances) == 2
    assert FakeSMTP.instances[0].message["Subject"] == (
        "$98.85｜正常可用账号 2 个｜Codex2API 剩余额度不足告警"
    )
```

Add separate zero-disabled, exact-equality and above-threshold tests that patch SMTP with `pytest.fail` and assert reasons `quota_alert_disabled` or `quota_not_below_threshold`.

- [ ] **Step 2: Run the email tests and verify the new behavior fails**

Run: `python -m pytest -q tests/test_chatgpt_auto_relogin_alerts.py`

Expected: failures show old account rows/subject and missing quota-threshold entry point.

- [ ] **Step 3: Refactor email summaries without account rows**

Add `_business_alert_subject(quota_report, title)` and compact plain/HTML summary builders. Remove `html_quota_rows`, the “正常账号明细” section and all loops over `quota_report.accounts` from business messages.

Keep these aggregate fields in both formats:

```text
当前估算剩余额度：$98.85
正常可用账号：2
Codex2API 账号总数：4
```

Use the shared subject helper for `send_auto_relogin_alert` and the new quota alert.

- [ ] **Step 4: Implement the independent recurring quota alert**

Add:

```python
def send_quota_threshold_alert(
    *,
    task_id: str,
    quota_report: AvailableQuotaReport,
    quota_eligible_failure_count: int,
    quota_exhausted_failure_count: int,
    relogin_failed_count: int,
    deleted_account_count: int = 0,
    config: Mapping[str, object] | None = None,
) -> dict[str, object]:
```

Parse the configured threshold as Decimal with `0.00` fallback. Return before SMTP when threshold is zero or current remaining is greater than/equal to threshold. On every below-threshold call, build and send a fresh email; do not read or store prior alert state. Return `threshold_usd` and `estimated_remaining_usd` strings for task metadata/logging.

- [ ] **Step 5: Run all email tests and commit**

Run: `python -m pytest -q tests/test_chatgpt_auto_relogin_alerts.py`

Expected: all tests pass.

```bash
git add services/chatgpt_auto_relogin_alerts.py tests/test_chatgpt_auto_relogin_alerts.py
git commit -m "feat(chatgpt): send compact recurring quota alerts"
```

### Task 4: Wire the final quota read and both alerts into automatic tasks

**Files:**
- Modify: `api/tasks.py`
- Modify: `tests/test_chatgpt_relogin_task.py`

- [ ] **Step 1: Add failing task orchestration tests**

In test setup, patch both `send_auto_relogin_alert` and `send_quota_threshold_alert`.

Add a completed automatic-task test that patches `fetch_codex2api_quota_accounts` with two final rows, verifies it is called after relogin returns, and asserts both alert functions receive the exact same `AvailableQuotaReport` instance. Assert metadata:

```python
assert meta["codex2api_account_count"] == 2
assert meta["available_quota_account_count"] == 2
assert meta["estimated_remaining_usd"] == "98.85"
assert meta["quota_alert_sent"] is True
assert meta["quota_alert_reason"] == "sent"
assert meta["quota_alert_threshold_usd"] == "120.00"
```

Add tests proving:

- stopped/manual tasks never fetch final quota or call quota alerts;
- final quota query failure records `quota_query_failed`, skips `send_quota_threshold_alert`, and still calls the relogin alert with the initial snapshot report;
- when both alert functions return `sent=True`, task logs contain two distinct sent messages;
- a quota email exception does not change terminal task status.

- [ ] **Step 2: Run the new task tests and verify missing orchestration fails**

Run: `python -m pytest -q tests/test_chatgpt_relogin_task.py -k "final_quota or quota_alert"`

Expected: failures show missing fresh fetch, second alert call and metadata.

- [ ] **Step 3: Implement final report creation and metadata**

After the task is terminal `done`, call `fetch_codex2api_quota_accounts()` and `summarize_available_quota()` once. Persist only report totals. If the fetch raises, summarize the initial `remote_quota_accounts` for the relogin subject, set `quota_alert_reason="quota_query_failed"`, and log the sanitized exception type.

Initialize new metadata in `_create_chatgpt_relogin_task_record` and include summary keys in `TASK_SUMMARY_META_KEYS`.

- [ ] **Step 4: Execute both alert paths independently**

Call `send_auto_relogin_alert(..., quota_report=report)` and, only after a successful final fetch, call `send_quota_threshold_alert(...)`. Wrap them in separate exception handlers so one SMTP failure does not suppress the other or modify the already-finished task outcome.

Log exact outcomes for disabled, not-below-threshold, sent, SMTP missing, query failed and send failed.

- [ ] **Step 5: Run all task tests and commit**

Run: `python -m pytest -q tests/test_chatgpt_relogin_task.py`

Expected: all tests pass.

```bash
git add api/tasks.py tests/test_chatgpt_relogin_task.py
git commit -m "feat(chatgpt): evaluate quota alerts after each cycle"
```

### Task 5: Add the USD threshold field to the settings UI

**Files:**
- Modify: `frontend/src/components/settings/ChatGPTAutoReloginSection.tsx`
- Modify: `frontend/src/components/settings/ChatGPTAutoReloginSection.test.tsx`
- Modify: `frontend/src/pages/Settings.tsx`
- Modify: `frontend/src/pages/Settings.test.tsx`

- [ ] **Step 1: Write failing component and settings tests**

Require an input with accessible name `Codex2API 剩余额度告警阈值（美元）`, default `0`, min `0`, max `10000000`, and help text containing `每轮都会发送`.

Update Settings fixtures with `chatgpt_auto_relogin_quota_alert_threshold_usd: "1200.50"` and assert the form loads/submits `1200.5` without integer rounding.

- [ ] **Step 2: Run focused frontend tests and verify failure**

Run:

```bash
cd frontend
npm test -- --run src/components/settings/ChatGPTAutoReloginSection.test.tsx \
  src/pages/Settings.test.tsx
```

Expected: missing input and submitted key assertions fail.

- [ ] **Step 3: Add the field and decimal normalization**

Add this form item below the failure-count threshold:

```tsx
<Form.Item
  name="chatgpt_auto_relogin_quota_alert_threshold_usd"
  label="Codex2API 剩余额度告警阈值（美元）"
  initialValue={0}
  extra="设置为 0 时关闭额度不足告警；每轮自动鉴权结束后，额度低于此值都会发送一封邮件。"
>
  <InputNumber
    aria-label="Codex2API 剩余额度告警阈值（美元）"
    min={0}
    max={10000000}
    precision={2}
    step={0.01}
    prefix="$"
    style={{ width: '100%' }}
  />
</Form.Item>
```

Add a bounded decimal normalization helper in `Settings.tsx`; use it at both config-load normalization and submit normalization. Include the new key in the submitted payload.

- [ ] **Step 4: Run frontend tests and production build**

Run:

```bash
cd frontend
npm test -- --run src/components/settings/ChatGPTAutoReloginSection.test.tsx \
  src/pages/Settings.test.tsx
npm run build
```

Expected: focused tests and TypeScript/Vite production build pass.

- [ ] **Step 5: Commit the frontend setting**

```bash
git add frontend/src/components/settings/ChatGPTAutoReloginSection.tsx \
  frontend/src/components/settings/ChatGPTAutoReloginSection.test.tsx \
  frontend/src/pages/Settings.tsx frontend/src/pages/Settings.test.tsx
git commit -m "feat(settings): configure recurring quota alerts"
```

### Task 6: Verify and deploy the second version

**Files/locations:**
- Local branch: `codex/chatgpt-auto-mfa-hardening`
- Server current release before switch: `/www/any-auto-register/releases/quota-aware-alert-20260807-1415-f2b0fcd`
- New server release: `/www/any-auto-register/releases/quota-threshold-alert-<timestamp>-<commit>`

- [ ] **Step 1: Run focused and complete backend verification**

Run compileall plus quota, health, config, email and task tests. Then run the complete backend suite while documenting the already-isolated historical MailAPI regression separately if it remains unchanged from the feature base.

- [ ] **Step 2: Run frontend verification**

Run the focused component/settings tests, complete frontend tests, and `npm run build`. Verify generated `static` contains the new field label.

- [ ] **Step 3: Inspect the final diff and secrets boundary**

Run `git diff --check`, verify the worktree is clean after commits, confirm production files contain no test credentials, and confirm no new reset-time use or account-row email rendering exists.

- [ ] **Step 4: Create and validate a new immutable release**

Upload tracked files to a new explicit release, copy the freshly built `static`, run server Python compile/import smoke tests, and verify local/server hashes for changed backend files. Keep the first-version release untouched as rollback.

- [ ] **Step 5: Switch and verify only Any Auto Register**

Atomically repoint `/www/any-auto-register/current`, restart only `any-auto-register.service`, wait for HTTP 200, verify zero new error-log bytes, and confirm Codex2API/Postgres/Redis uptime remains unchanged.

- [ ] **Step 6: Validate production settings and alert rendering**

Verify the config API default/round-trip through the backend without overwriting the user-selected final threshold. Use a non-business local message build or SMTP test path to validate the compact subject/body; do not fabricate login failures. Report the deployed commit, release, current threshold and rollback path.
