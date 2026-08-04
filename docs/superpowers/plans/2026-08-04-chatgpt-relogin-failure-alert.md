# ChatGPT Relogin Failure Alert Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Send one optimized Beijing-time email after a completed automatic authentication cycle only when the configurable relogin-failure count reaches its threshold, defaulting to 20.

**Architecture:** Keep the existing configuration key and task completion hook. Narrow the alert service trigger to `relogin_failed_count >= threshold`, pass the task's final success count into a multipart plain-text/HTML message builder, and update the existing settings field without adding storage or API migrations.

**Tech Stack:** Python 3, FastAPI, stdlib `email`/`smtplib`/`zoneinfo`, pytest/unittest.mock, React 19, Ant Design, TypeScript, Vitest/Testing Library.

---

## File map

- `services/chatgpt_auto_relogin_alerts.py`: threshold decision, Beijing-time formatting, multipart alert email and SMTP delivery.
- `tests/test_chatgpt_auto_relogin_alerts.py`: alert boundary, configurable threshold, four metrics, HTML escaping, timezone and SMTP regressions.
- `api/tasks.py`: pass the final success count and write precise task-log messages.
- `tests/test_chatgpt_relogin_task.py`: task-to-alert call contract and log semantics.
- `api/config.py`: public fallback default and validation label.
- `tests/test_chatgpt_auto_relogin.py`: backend configuration default and range regression.
- `frontend/src/components/settings/ChatGPTAutoReloginSection.tsx`: user-facing label, help text and field default.
- `frontend/src/components/settings/ChatGPTAutoReloginSection.test.tsx`: component semantics and default.
- `frontend/src/pages/Settings.tsx`: load/save fallback default.
- `frontend/src/pages/Settings.test.tsx`: saved-value round trip and missing-value fallback.

### Task 1: Alert trigger and multipart message

**Files:**
- Modify: `tests/test_chatgpt_auto_relogin_alerts.py`
- Modify: `services/chatgpt_auto_relogin_alerts.py`

- [ ] **Step 1: Replace the old either-counter tests with failing relogin-only boundary tests**

Set `BASE_CONFIG["chatgpt_auto_relogin_alert_threshold"]` to `"20"`. Add `successful_accounts` to every `send_auto_relogin_alert` call. Replace the old parametrized either-counter case with these exact behavioral cases:

```python
@pytest.mark.parametrize(
    ("threshold", "invalid_rt_count", "relogin_failed_count"),
    [(20, 100, 19), (7, 100, 6)],
)
def test_relogin_failures_below_threshold_do_not_open_smtp(
    monkeypatch, threshold, invalid_rt_count, relogin_failed_count
):
    from services import chatgpt_auto_relogin_alerts as alerts

    monkeypatch.setattr(alerts.smtplib, "SMTP", pytest.fail)
    monkeypatch.setattr(alerts.smtplib, "SMTP_SSL", pytest.fail)
    config = {**BASE_CONFIG, "chatgpt_auto_relogin_alert_threshold": str(threshold)}

    result = alerts.send_auto_relogin_alert(
        task_id="task-below",
        total_accounts=120,
        successful_accounts=20,
        invalid_rt_count=invalid_rt_count,
        relogin_failed_count=relogin_failed_count,
        config=config,
    )

    assert result == {
        "sent": False,
        "reason": "below_threshold",
        "threshold": threshold,
    }


@pytest.mark.parametrize(("threshold", "failed"), [(20, 20), (20, 27), (7, 7)])
def test_reaching_relogin_failure_threshold_sends_one_message(
    monkeypatch, threshold, failed
):
    from services import chatgpt_auto_relogin_alerts as alerts

    monkeypatch.setattr(alerts.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(
        alerts.smtplib,
        "SMTP_SSL",
        lambda *args, **kwargs: pytest.fail("587 should use STARTTLS"),
    )
    config = {**BASE_CONFIG, "chatgpt_auto_relogin_alert_threshold": str(threshold)}

    result = alerts.send_auto_relogin_alert(
        task_id="task-threshold",
        total_accounts=106,
        successful_accounts=78,
        invalid_rt_count=28,
        relogin_failed_count=failed,
        config=config,
    )

    assert result == {"sent": True, "reason": "sent", "threshold": threshold}
    assert len(FakeSMTP.instances) == 1
```

- [ ] **Step 2: Add failing message-content, timezone and escaping assertions**

For the sent message, select both MIME bodies and assert the exact four labels and values, dynamic subject, Beijing-time marker and safe HTML:

```python
message = FakeSMTP.instances[0].message
plain = message.get_body(preferencelist=("plain",)).get_content()
html = message.get_body(preferencelist=("html",)).get_content()

assert message["Subject"] == (
    f"[Any Auto Register] ChatGPT 重登失败账号告警（{failed} 个）"
)
for label, value in (
    ("账号总数", 106),
    ("成功账号", 78),
    ("鉴权失败", 28),
    ("重登失败", failed),
):
    assert f"{label}：{value}" in plain
    assert label in html
    assert f">{value}<" in html
assert "北京时间" in plain
assert "北京时间" in html
assert "鉴权失败数仅用于展示" in plain
assert "smtp-test-credential" not in message.as_string()
```

Add a direct builder test with `task_id='<script>alert("x")</script>'`, monkeypatch `_format_beijing_time` to `"2026-08-04 21:17:45（北京时间）"`, and assert the raw script tag is absent from HTML while escaped text is present.

- [ ] **Step 3: Run the focused alert tests and verify RED**

Run:

```bash
PYTHONPATH=. .venv/bin/pytest tests/test_chatgpt_auto_relogin_alerts.py -q
```

Expected: FAIL because `successful_accounts` is not accepted, invalid-auth-only cycles still trigger, the default is 5, and no HTML alternative exists.

- [ ] **Step 4: Implement the minimal relogin-only trigger and Beijing-time helper**

In `services/chatgpt_auto_relogin_alerts.py`, use:

```python
from datetime import datetime, timezone
from html import escape
from zoneinfo import ZoneInfo

DEFAULT_ALERT_THRESHOLD = 20
BEIJING_TIMEZONE = ZoneInfo("Asia/Shanghai")


def _format_beijing_time(value: datetime | None = None) -> str:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(BEIJING_TIMEZONE).strftime(
        "%Y-%m-%d %H:%M:%S（北京时间）"
    )
```

Change the public function contract and decision to:

```python
def send_auto_relogin_alert(
    *,
    task_id: str,
    total_accounts: int,
    successful_accounts: int,
    invalid_rt_count: int,
    relogin_failed_count: int,
    config: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Send one alert when relogin failures reach the configured threshold."""
    snapshot = dict(config) if config is not None else _get_config()
    threshold = _positive_int(
        snapshot.get("chatgpt_auto_relogin_alert_threshold"),
        DEFAULT_ALERT_THRESHOLD,
    )
    failed_count = _non_negative_int(relogin_failed_count)
    if failed_count < threshold:
        return {"sent": False, "reason": "below_threshold", "threshold": threshold}
```

Sanitize all four counters through `_non_negative_int` before message construction. Use `_format_beijing_time()` in both alert and SMTP-test messages.

- [ ] **Step 5: Build the exact multipart message**

Set a plain-text part containing the four lines in the required order:

```python
message.set_content(
    "ChatGPT 重登失败账号告警\n\n"
    "本轮自动鉴权已完成，重登失败账号数已达到告警阈值。\n\n"
    f"账号总数：{total_accounts}\n"
    f"成功账号：{successful_accounts}\n"
    f"鉴权失败：{invalid_rt_count}\n"
    f"重登失败：{relogin_failed_count}\n\n"
    f"任务 ID：{task_id}\n"
    f"告警阈值：{threshold}\n"
    f"完成时间：{occurred_at}\n\n"
    "说明：鉴权失败数仅用于展示；重登失败数是本邮件的触发依据。"
    "两项为过程指标，可能包含同一账号，四项统计不应相加核对总数。\n"
    "请在 Any Auto Register 的“任务运行”页面查看本轮详细记录。\n"
)
```

Add one HTML alternative. Escape `task_id` and `occurred_at` with `html.escape`; render a presentation table with four `<td class="metric-cell" width="25%">` cells ordered as total, success, auth failure and relogin failure. Add an email-safe media rule that makes `.metric-cell` block-level and 100% wide below 600px. Include the same threshold, time and metric explanation as the plain-text body.

- [ ] **Step 6: Update existing SMTP regression calls for the new contract**

Every existing `send_auto_relogin_alert` test call gets `successful_accounts=...`. Cases intended to reach SMTP use a relogin failure count equal to their configured threshold; invalid-auth-only values must no longer be used as the trigger. Multipart assertions use `message.get_body(preferencelist=("plain",))` instead of `message.get_content()`.

- [ ] **Step 7: Run focused tests and verify GREEN**

Run:

```bash
PYTHONPATH=. .venv/bin/pytest tests/test_chatgpt_auto_relogin_alerts.py -q
```

Expected: all tests pass.

- [ ] **Step 8: Commit the alert service change**

```bash
git add services/chatgpt_auto_relogin_alerts.py tests/test_chatgpt_auto_relogin_alerts.py
git commit -m "feat: alert on relogin failure threshold"
```

### Task 2: Task completion integration and logs

**Files:**
- Modify: `tests/test_chatgpt_relogin_task.py`
- Modify: `api/tasks.py`

- [ ] **Step 1: Write the failing task contract assertion**

Update `test_automatic_task_records_cycle_counts_and_sends_one_summary_alert` so its expected call includes the final success count and verify the sent log:

```python
self.alert_sender.assert_called_once_with(
    task_id=task_id,
    total_accounts=4,
    successful_accounts=1,
    invalid_rt_count=3,
    relogin_failed_count=1,
)
self.assertTrue(
    any("重登失败告警邮件已发送" in line for line in snapshot["logs"])
)
```

Add an assertion to a below-threshold automatic-task test that its log contains `重登失败数未达到配置阈值`.

- [ ] **Step 2: Run the focused task test and verify RED**

Run:

```bash
PYTHONPATH=. .venv/bin/pytest tests/test_chatgpt_relogin_task.py::ChatGPTReloginTaskTests::test_automatic_task_records_cycle_counts_and_sends_one_summary_alert -q
```

Expected: FAIL because `successful_accounts` is not passed and the old generic log text remains.

- [ ] **Step 3: Pass the final success count and update all alert logs**

In `api/tasks.py`, call:

```python
alert_result = send_auto_relogin_alert(
    task_id=task_id,
    total_accounts=total,
    successful_accounts=success,
    invalid_rt_count=invalid_rt_count,
    relogin_failed_count=relogin_failed_count,
)
```

Use these task-log messages:

```python
if alert_meta["alert_sent"]:
    _log(task_id, "[ALERT] 本轮重登失败告警邮件已发送")
elif alert_reason == "below_threshold":
    _log(task_id, "邮件告警未触发：本轮重登失败数未达到配置阈值")
elif alert_reason == "smtp_not_configured":
    _log(task_id, "[ALERT] 本轮重登失败数已达到阈值，但 SMTP 配置不完整")
else:
    error_type = str(alert_meta.get("alert_error_type") or "UnknownError")
    _log(task_id, f"[ALERT] 重登失败告警邮件发送失败（{error_type}）")
```

- [ ] **Step 4: Run task regressions and verify GREEN**

Run:

```bash
PYTHONPATH=. .venv/bin/pytest tests/test_chatgpt_relogin_task.py tests/test_task_runtime.py -q
```

Expected: all tests pass, including stopped-task suppression and terminal persistence ordering.

- [ ] **Step 5: Commit the task integration**

```bash
git add api/tasks.py tests/test_chatgpt_relogin_task.py
git commit -m "feat: report relogin alert cycle metrics"
```

### Task 3: Backend configuration default

**Files:**
- Modify: `tests/test_chatgpt_auto_relogin.py`
- Modify: `api/config.py`

- [ ] **Step 1: Change the public-default test to expect 20**

```python
assert response["chatgpt_auto_relogin_alert_threshold"] == "20"
```

Keep the existing explicit saved value `5` test unchanged to prove saved user configuration is preserved.

- [ ] **Step 2: Run the focused config test and verify RED**

Run:

```bash
PYTHONPATH=. .venv/bin/pytest tests/test_chatgpt_auto_relogin.py::test_public_config_defaults_are_exposed_without_database_writes -q
```

Expected: FAIL with `"5" != "20"`.

- [ ] **Step 3: Update the backend fallback and validation wording**

In `api/config.py`:

```python
if not str(all_cfg.get("chatgpt_auto_relogin_alert_threshold", "") or "").strip():
    all_cfg["chatgpt_auto_relogin_alert_threshold"] = "20"
```

Change only the validation label from `邮件告警阈值` to `重登失败告警阈值`; keep the range 1–10000 and existing configuration key.

- [ ] **Step 4: Run config regressions and verify GREEN**

Run:

```bash
PYTHONPATH=. .venv/bin/pytest tests/test_chatgpt_auto_relogin.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit the backend config change**

```bash
git add api/config.py tests/test_chatgpt_auto_relogin.py
git commit -m "feat: default relogin alert threshold to twenty"
```

### Task 4: Frontend setting semantics and fallback

**Files:**
- Modify: `frontend/src/components/settings/ChatGPTAutoReloginSection.test.tsx`
- Modify: `frontend/src/components/settings/ChatGPTAutoReloginSection.tsx`
- Modify: `frontend/src/pages/Settings.test.tsx`
- Modify: `frontend/src/pages/Settings.tsx`

- [ ] **Step 1: Write failing component expectations**

In the defaults test, query and assert:

```typescript
const threshold = screen.getByRole('spinbutton', {
  name: '重登失败告警阈值（账号数）',
}) as HTMLInputElement
expect(threshold.value).toBe('20')
expect(screen.getByText(/鉴权失败数仅展示，不触发告警/)).toBeTruthy()
```

- [ ] **Step 2: Write a failing Settings fallback test**

Extend the existing missing-config table to include a case with no threshold, then assert:

```typescript
expect((screen.getByRole('spinbutton', {
  name: '重登失败告警阈值（账号数）',
}) as HTMLInputElement).value).toBe('20')
```

Keep the saved-value round-trip fixture at `5` and assert the submitted payload remains `5`, proving user settings are not overwritten.

- [ ] **Step 3: Run focused frontend tests and verify RED**

Run:

```bash
cd frontend
npm test -- src/components/settings/ChatGPTAutoReloginSection.test.tsx src/pages/Settings.test.tsx
```

Expected: FAIL because the old label/help/default and Settings fallbacks still use 5.

- [ ] **Step 4: Update the component label, help and default**

In `ChatGPTAutoReloginSection.tsx`, use:

```tsx
<Form.Item
  name="chatgpt_auto_relogin_alert_threshold"
  label="重登失败告警阈值（账号数）"
  initialValue={20}
  extra="每轮自动鉴权完成后，重登失败账号数达到或超过此值时发送一封提醒；鉴权失败数仅展示，不触发告警。"
>
  <InputNumber
    aria-label="重登失败告警阈值（账号数）"
    min={1}
    max={10000}
    precision={0}
    style={{ width: '100%' }}
  />
</Form.Item>
```

- [ ] **Step 5: Update both Settings numeric fallbacks**

In both load and save calls to `normalizeBoundedInteger` for `chatgpt_auto_relogin_alert_threshold`, replace only the fallback argument `5` with `20`; keep min 1 and max 10000.

- [ ] **Step 6: Run frontend tests, lint and build**

Run:

```bash
cd frontend
npm test -- src/components/settings/ChatGPTAutoReloginSection.test.tsx src/pages/Settings.test.tsx
npm run lint
npm run build
```

Expected: tests, ESLint and production build all pass.

- [ ] **Step 7: Commit the frontend change**

```bash
git add frontend/src/components/settings/ChatGPTAutoReloginSection.tsx \
  frontend/src/components/settings/ChatGPTAutoReloginSection.test.tsx \
  frontend/src/pages/Settings.tsx \
  frontend/src/pages/Settings.test.tsx
git commit -m "feat: clarify relogin failure alert setting"
```

### Task 5: Cross-layer verification, review and production rollout

**Files:**
- Verify: all modified files
- Reference: `docs/superpowers/specs/2026-08-04-chatgpt-relogin-failure-alert-design.md`
- Reference: `docs/superpowers/plans/2026-08-03-no-downtime-server-deployment-plan.md`

- [ ] **Step 1: Run focused cross-layer tests**

```bash
PYTHONPATH=. .venv/bin/pytest \
  tests/test_chatgpt_auto_relogin_alerts.py \
  tests/test_chatgpt_relogin_task.py \
  tests/test_task_runtime.py \
  tests/test_chatgpt_auto_relogin.py -q
cd frontend
npm test -- src/components/settings/ChatGPTAutoReloginSection.test.tsx src/pages/Settings.test.tsx
```

Expected: all focused backend and frontend tests pass.

- [ ] **Step 2: Run full local verification**

```bash
PYTHONPATH=. .venv/bin/pytest -q
cd frontend
npm test
npm run lint
npm run build
```

Expected: complete backend suite, complete frontend suite, ESLint and build pass with no new failures.

- [ ] **Step 3: Perform an independent code review**

Review the complete diff against the spec. Verify relogin-only triggering, inclusive boundary, saved-value compatibility, no credential leakage, four metric order, dynamic subject count, explicit timezone, task status stability and no unrelated refactor. Resolve every finding and rerun affected tests.

- [ ] **Step 4: Commit any review fixes and verify a clean tree**

```bash
git diff --check
git status --short
```

Expected: no whitespace errors and no uncommitted implementation changes.

- [ ] **Step 5: Push and deploy with the existing zero-downtime procedure**

Push `codex/chatgpt-auto-relogin`, deploy the verified commit using the repository's established production deployment path, and wait for the application health check to become healthy before retiring the previous instance.

- [ ] **Step 6: Set the production threshold to 20 without altering other settings**

Read the current production configuration, update only `chatgpt_auto_relogin_alert_threshold` to `20`, and confirm the returned/public value is `"20"`. Preserve the saved SMTP credential by omitting `smtp_password` from the update.

- [ ] **Step 7: Verify production behavior without generating a false business alert**

Check the deployed commit, health endpoint, automatic-relogin status, configured interval/enabled state and recent logs. In the running application environment, call the pure message builder with synthetic counts to verify:

- subject uses the actual relogin-failure count;
- plain and HTML parts contain the four metrics in order;
- time includes `北京时间`;
- no SMTP connection is opened for threshold 20 with 19 failures and a large auth-failure count.

Do not send this synthetic message. Leave the automatic workflow enabled with its existing interval and threshold 20.
