# ChatGPT Quota-Aware Relogin Alert Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Filter automatic relogin alerts by Codex2API 7d usage, suppress exhausted-account failures, and email the estimated remaining USD for every normal Codex2API account.

**Architecture:** Preserve the existing Codex2API wham probe and relogin flow. Add one pure quota-calculation module, carry the 7d percentage/cost fields through the existing health snapshot, count only quota-available final relogin failures in the task, and let the existing SMTP service render the remote-account quota aggregate. The reset timestamp is not read or used.

**Tech Stack:** Python 3.11+, Decimal, FastAPI task runner, curl_cffi, EmailMessage/SMTP, pytest/unittest, systemd release deployment.

---

### Task 1: Add pure Codex2API quota classification and estimation

**Files:**
- Create: `services/chatgpt_codex2api_quota.py`
- Create: `tests/test_chatgpt_codex2api_quota.py`

- [ ] **Step 1: Write failing tests for the two quota states and USD aggregation**

Create tests that require this public interface:

```python
from decimal import Decimal

from services.chatgpt_codex2api_quota import (
    estimate_account_quota,
    summarize_available_quota,
)


def test_estimate_account_quota_marks_one_hundred_percent_exhausted():
    estimate = estimate_account_quota({
        "usage_percent_7d": 100,
        "billed_7d": 81.42,
    })
    assert estimate.state == "exhausted"
    assert estimate.remaining_usd == Decimal("0.00")


def test_estimate_account_quota_calculates_remaining_usd():
    estimate = estimate_account_quota({
        "usage_percent_7d": 68,
        "billed_7d": 81.42,
    })
    assert estimate.state == "available"
    assert estimate.remaining_usd == Decimal("38.32")


def test_estimate_account_quota_rejects_missing_or_zero_percent():
    assert estimate_account_quota({"billed_7d": 10}).state == "invalid"
    assert estimate_account_quota({
        "usage_percent_7d": 0,
        "billed_7d": 0,
    }).state == "invalid"


def test_summarize_available_quota_filters_non_normal_accounts():
    report = summarize_available_quota([
        {
            "email": "a@example.com",
            "remote_status": "active",
            "usage_percent_7d": 53,
            "billed_7d": 68.26,
        },
        {
            "email": "b@example.com",
            "remote_status": "rate_limited",
            "usage_percent_7d": 68,
            "billed_7d": 81.42,
        },
        {
            "email": "full@example.com",
            "remote_status": "rate_limited",
            "usage_percent_7d": 100,
            "billed_7d": 120,
        },
        {
            "email": "bad@example.com",
            "remote_status": "unauthorized",
            "usage_percent_7d": 50,
            "billed_7d": 50,
        },
    ])
    assert report.account_count == 2
    assert report.estimated_remaining_usd == Decimal("98.85")
    assert [row.email for row in report.accounts] == [
        "a@example.com",
        "b@example.com",
    ]
```

- [ ] **Step 2: Run the focused test and verify the missing-module failure**

Run: `pytest -q tests/test_chatgpt_codex2api_quota.py`

Expected: collection fails because `services.chatgpt_codex2api_quota` does not exist.

- [ ] **Step 3: Implement the minimal Decimal-based quota module**

Create immutable `QuotaEstimate`, `AvailableQuotaAccount`, and `AvailableQuotaReport` dataclasses. Parse values with `Decimal(str(value))`, reject booleans/non-finite/negative cost, classify `percent >= 100` as exhausted, reject `percent <= 0`, and calculate:

```python
remaining = (
    billed * (Decimal("100") - percent) / percent
).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
```

`summarize_available_quota()` must accept only remote statuses `active` and `rate_limited`, include only `available` estimates, sort rows by lowercase email, and sum the already rounded per-account amounts into a two-decimal USD total.

- [ ] **Step 4: Run the focused test and verify it passes**

Run: `pytest -q tests/test_chatgpt_codex2api_quota.py`

Expected: all tests pass.

- [ ] **Step 5: Commit the quota calculation unit**

```bash
git add services/chatgpt_codex2api_quota.py tests/test_chatgpt_codex2api_quota.py
git commit -m "feat(chatgpt): estimate Codex2API remaining quota"
```

### Task 2: Carry quota fields through the Codex2API health snapshot

**Files:**
- Modify: `services/chatgpt_codex2api_health.py`
- Modify: `tests/test_chatgpt_codex2api_health.py`

- [ ] **Step 1: Write failing health-snapshot assertions**

Extend the active, rate-limited, and unauthorized fake account rows with `usage_percent_7d` and `billed_7d`. Pass an empty `quota_accounts=[]` collector to `inspect_codex2api_account_health()` and assert:

```python
assert snapshot[1]["usage_percent_7d"] == 53
assert snapshot[1]["billed_7d"] == 68.26
assert snapshot[3]["usage_percent_7d"] == 68
assert snapshot[3]["billed_7d"] == 81.42
assert {row["email"] for row in quota_accounts} == {
    "healthy@example.com",
    "limited@example.com",
    "invalid@example.com",
    "error@example.com",
    "duplicate@example.com",
    "unknown-status@example.com",
}
assert all("reset_7d_at" not in row for row in quota_accounts)
```

The collector must contain every unique remote Codex2API row, including rows that do not map to a local login candidate; duplicate-email rows remain separate by remote ID.

- [ ] **Step 2: Run the focused health test and verify the new assertions fail**

Run: `pytest -q tests/test_chatgpt_codex2api_health.py::test_health_snapshot_matches_accounts_and_only_marks_auth_failures`

Expected: failure because the function has no `quota_accounts` argument or quota fields.

- [ ] **Step 3: Add quota-only row normalization without reset timestamps**

Add:

```python
def _quota_record(row: Mapping[str, object]) -> dict[str, object]:
    return {
        "remote_id": _remote_id(row),
        "email": _remote_email(row),
        "remote_status": _text(row.get("status")).lower(),
        "usage_percent_7d": row.get("usage_percent_7d"),
        "billed_7d": row.get("billed_7d"),
    }
```

Add `quota_accounts: list[dict[str, object]] | None = None` to `inspect_codex2api_account_health`. After validating the remote rows, append `_quota_record(row)` for every dict row. Extend `_record()` with optional `usage_percent_7d` and `billed_7d`; include those exact fields whenever a remote row matched the local account. Do not read or store any reset field.

- [ ] **Step 4: Run all Codex2API health tests**

Run: `pytest -q tests/test_chatgpt_codex2api_health.py`

Expected: all tests pass and the original exact auth-failure record assertion is updated to include its two quota fields.

- [ ] **Step 5: Commit the health snapshot change**

```bash
git add services/chatgpt_codex2api_health.py tests/test_chatgpt_codex2api_health.py
git commit -m "feat(chatgpt): retain Codex2API quota snapshot"
```

### Task 3: Count only quota-available final relogin failures

**Files:**
- Modify: `api/tasks.py`
- Modify: `tests/test_chatgpt_relogin_task.py`

- [ ] **Step 1: Write failing automatic-task tests for exhausted and available failures**

Add one test with six final relogin failures and threshold five: three health rows use 100%, three use 50%. Assert `relogin_failed_count == 6`, `quota_eligible_failure_count == 3`, `quota_exhausted_failure_count == 3`, and the alert call receives the new counters plus the remote quota collector.

Add a second test where all six failures use 100%. Assert the alert service is still called once for terminal bookkeeping but receives `quota_eligible_failure_count=0`; its mocked result is `below_threshold`, so no sent-alert log appears.

Use health rows shaped as:

```python
{
    "account_id": account_id,
    "email": f"account-{account_id}@example.com",
    "state": "auth_failed",
    "remote_status": "unauthorized",
    "usage_percent_7d": percent,
    "billed_7d": 50.0,
}
```

- [ ] **Step 2: Run the two new task tests and verify missing counters fail**

Run: `pytest -q tests/test_chatgpt_relogin_task.py -k "quota_available or quota_exhausted"`

Expected: assertions fail because quota-aware metadata and alert arguments do not exist.

- [ ] **Step 3: Implement quota-aware task counters**

Initialize these metadata fields in `_create_chatgpt_relogin_task_record()` and `_run_chatgpt_relogin_task()`:

```python
"quota_eligible_failure_count": 0,
"quota_exhausted_failure_count": 0,
```

Create `remote_quota_accounts: list[dict[str, object]] = []` and pass it as `quota_accounts=remote_quota_accounts` to the health inspector. Change `_record_automatic_result` to accept `account_id`, call `estimate_account_quota(remote_health.get(account_id) or {})` only for final full-login failures, and increment exactly one of the two quota counters for states `available` and `exhausted`; invalid estimates increment neither. Keep the original overall relogin/deleted counters unchanged.

Pass these arguments to the alert service:

```python
quota_eligible_failure_count=quota_eligible_failure_count,
quota_exhausted_failure_count=quota_exhausted_failure_count,
quota_accounts=remote_quota_accounts,
```

Update task summary and below-threshold logs to name “仍有额度的重登失败数”. Do not include quota rows or secrets in persistent task metadata.

- [ ] **Step 4: Update existing exact mock assertions and run task tests**

Update existing `assert_called_once_with` checks for the alert sender with the three new arguments. Where the health inspector is mocked, capture the optional collector from `kwargs` and populate it only in tests that assert quota email rows; other tests may pass an empty list.

Run: `pytest -q tests/test_chatgpt_relogin_task.py`

Expected: all task tests pass.

- [ ] **Step 5: Commit task-level quota filtering**

```bash
git add api/tasks.py tests/test_chatgpt_relogin_task.py
git commit -m "feat(chatgpt): filter relogin alerts by remaining quota"
```

### Task 4: Render quota-aware threshold and USD summary in the email

**Files:**
- Modify: `services/chatgpt_auto_relogin_alerts.py`
- Modify: `tests/test_chatgpt_auto_relogin_alerts.py`

- [ ] **Step 1: Write failing SMTP tests for threshold filtering and quota content**

Change threshold-focused tests to pass `quota_eligible_failure_count`. Add:

```python
def test_exhausted_failures_never_open_smtp(monkeypatch):
    from services import chatgpt_auto_relogin_alerts as alerts
    monkeypatch.setattr(alerts.smtplib, "SMTP", pytest.fail)
    result = alerts.send_auto_relogin_alert(
        task_id="task-exhausted",
        total_accounts=30,
        successful_accounts=0,
        invalid_rt_count=30,
        relogin_failed_count=30,
        deleted_account_count=30,
        quota_eligible_failure_count=0,
        quota_exhausted_failure_count=30,
        quota_accounts=[],
        config={**BASE_CONFIG, "chatgpt_auto_relogin_alert_threshold": "5"},
    )
    assert result["reason"] == "below_threshold"


def test_available_failure_alert_contains_remaining_usd(monkeypatch):
    from services import chatgpt_auto_relogin_alerts as alerts
    monkeypatch.setattr(alerts.smtplib, "SMTP", FakeSMTP)
    result = alerts.send_auto_relogin_alert(
        task_id="task-quota",
        total_accounts=8,
        successful_accounts=2,
        invalid_rt_count=8,
        relogin_failed_count=8,
        deleted_account_count=3,
        quota_eligible_failure_count=5,
        quota_exhausted_failure_count=3,
        quota_accounts=[
            {"email": "a@example.com", "remote_status": "active", "usage_percent_7d": 53, "billed_7d": 68.26},
            {"email": "b@example.com", "remote_status": "rate_limited", "usage_percent_7d": 68, "billed_7d": 81.42},
        ],
        config={**BASE_CONFIG, "chatgpt_auto_relogin_alert_threshold": "5"},
    )
    assert result["sent"] is True
    message = FakeSMTP.instances[0].message
    plain = message.get_body(preferencelist=("plain",)).get_content()
    html = message.get_body(preferencelist=("html",)).get_content()
    assert "仍有额度的重登失败：5" in plain
    assert "额度已用完的重登失败：3" in plain
    assert "正常可用账号：2" in plain
    assert "估算剩余额度合计：$98.85" in plain
    assert "a@example.com" in html
    assert "$60.53" in html
    assert "重置时间" not in message.as_string()
```

- [ ] **Step 2: Run the alert tests and verify the old signature/threshold fails**

Run: `pytest -q tests/test_chatgpt_auto_relogin_alerts.py`

Expected: failures show missing quota-aware arguments and old relogin-failure threshold behavior.

- [ ] **Step 3: Implement quota-aware message construction**

Extend `send_auto_relogin_alert` and `_build_message` with:

```python
quota_eligible_failure_count: int,
quota_exhausted_failure_count: int,
quota_accounts: Iterable[Mapping[str, object]],
```

Use `quota_eligible_failure_count` for the threshold and subject count. Call `summarize_available_quota(quota_accounts)` only after the threshold is met. Render plain-text rows as:

```text
- a@example.com：使用率 53%，已用 $68.26，估算剩余 $60.53
```

Render an escaped HTML table with columns 账号、7d 使用率、已用成本、估算剩余. Keep overall relogin failure and deleted counts for operational context, but make “仍有额度的重登失败” the explicit trigger metric. Do not include reset fields.

- [ ] **Step 4: Run alert tests and focused backend tests**

Run:

```bash
pytest -q tests/test_chatgpt_auto_relogin_alerts.py \
  tests/test_chatgpt_codex2api_quota.py \
  tests/test_chatgpt_codex2api_health.py \
  tests/test_chatgpt_relogin_task.py
```

Expected: all focused tests pass.

- [ ] **Step 5: Commit the SMTP content change**

```bash
git add services/chatgpt_auto_relogin_alerts.py tests/test_chatgpt_auto_relogin_alerts.py
git commit -m "feat(chatgpt): email remaining Codex2API quota"
```

### Task 5: Run regression verification and prepare the release

**Files:**
- Modify only if a test exposes a regression in the files already listed.

- [ ] **Step 1: Run syntax and focused verification**

Run:

```bash
python -m compileall -q api services tests
pytest -q tests/test_chatgpt_codex2api_quota.py \
  tests/test_chatgpt_codex2api_health.py \
  tests/test_chatgpt_auto_relogin_alerts.py \
  tests/test_chatgpt_relogin_task.py
```

Expected: compile command exits 0 and every focused test passes.

- [ ] **Step 2: Run the complete backend test suite**

Run: `pytest -q`

Expected: suite exits 0 with no failed tests.

- [ ] **Step 3: Inspect the final diff and secrets boundary**

Run:

```bash
git diff --check HEAD~4..HEAD
git status --short
rg -n "reset_5h_at|reset_7d_at|admin-test-secret|smtp-test-credential" \
  services/chatgpt_codex2api_quota.py \
  services/chatgpt_codex2api_health.py \
  services/chatgpt_auto_relogin_alerts.py \
  api/tasks.py
```

Expected: no whitespace errors, clean status, no reset-field usage in the new quota flow, and no literal test credentials in production files.

### Task 6: Deploy the first version to the server and verify it

**Files/locations:**
- Local release source: current worktree commit
- Server release: `/www/any-auto-register/releases/quota-aware-alert-<timestamp>`
- Server symlink: `/www/any-auto-register/current`
- Service: `any-auto-register.service`

- [ ] **Step 1: Capture the current production release and service health**

Run over SSH port 55222 with the configured key:

```bash
readlink -f /www/any-auto-register/current
systemctl is-active any-auto-register.service
curl -fsS http://127.0.0.1:18081/api/automations/chatgpt-relogin
```

Expected: record the previous release path, service is `active`, and the automation endpoint returns JSON.

- [ ] **Step 2: Create a new immutable release from the verified local tree**

Upload tracked files to a new explicit release directory while excluding `.git`, caches, runtime data, `.env`, and local databases. Preserve `/www/any-auto-register/shared` and the previous release. Install Python dependencies only if `requirements.txt` changed.

- [ ] **Step 3: Validate the uploaded release before switching**

Run the server virtualenv Python compile check and the four focused test files against the new release. Verify the release contains the quota module and the committed design/plan.

- [ ] **Step 4: Switch the symlink and restart only Any Auto Register**

Atomically repoint `/www/any-auto-register/current` to the new release, run `systemctl restart any-auto-register.service`, and wait for `systemctl is-active` plus the loopback automation endpoint. Do not restart or modify the Codex2API container.

- [ ] **Step 5: Verify production quota data and rollback readiness**

Read the latest service logs, confirm there are no import/startup errors, and use the existing Codex2API Admin Key from the server environment to read only the field names/counts needed to verify `usage_percent_7d` and `billed_7d`. Record the rollback command that atomically restores the previous release symlink and restarts only `any-auto-register.service`.

- [ ] **Step 6: Report the deployed release and observation window**

Report the local commit, previous and current release paths, focused/full test results, service health, and the next automatic-cycle time. Do not force a synthetic login failure or send a synthetic business alert; the first real threshold event will show the production email format.
