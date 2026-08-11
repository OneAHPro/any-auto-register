# ChatGPT 登录链路加固 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 MailAPI 假详情导致的验证码超时，并让 OAuth 瞬态失败、任务进度和 Codex2API 同步结果可恢复、可追踪。

**Architecture:** 在现有 MailApiUrlOtpBackend 中增加单邮件页识别和详情回退；在 ChatGPT 任务执行器中对手机 OAuth 未就绪做一次隔离的全新会话重试；把任务进度写入移动到并发完成回调，并让任务终态等待外部同步线程结束，确保同步结果先写入任务日志。WhatsApp/SMS 保持现有换号逻辑。

**Tech Stack:** Python 3、pytest、SQLite/SQLModel、requests/curl_cffi、systemd、Git/SSH。

---

### Task 1: MailAPI 单邮件页面回退与候选过滤

**Files:**
- Modify: `core/base_mailbox.py:4093-4135,4471-4610,4654-4703`
- Test: `tests/test_outlook_mailbox_oauth.py`

- [ ] **Step 1: Write failing tests**

  Add a fixture containing the observed 191006.xyz structure in `tests/test_outlook_mailbox_oauth.py`: a page title containing `Your temporary ChatGPT login code`, `.panel/.meta/.content` blocks, a visible six-digit code, a `时间` row, and three `/cdn-cgi/l/email-protection` links. Assert that the detail candidate list excludes Cloudflare links and that `_parse_mailapi_message()` returns non-empty content, a timestamp, and a stable message id.

- [ ] **Step 2: Run the focused tests and verify RED**

  Run `pytest -q tests/test_outlook_mailbox_oauth.py -k 'single_message_panel or ignores_cloudflare_email_protection or detail_without_code_falls_back'` and confirm the new assertions fail because the current parser returns no timestamp and discovers the Cloudflare path.

- [ ] **Step 3: Implement the smallest parser change**

  Add a single-message HTML parser for `.panel`, `.meta`, `.content` pages; extract `时间` with the existing timestamp parser; derive a SHA-256 message id from subject, timestamp, and visible content; exclude `/cdn-cgi/l/email-protection` and non-mail tracking links from candidate discovery; in `_fetch_mailapi_text()` ignore non-2xx detail responses and return the original page when no detail contains an OTP.

- [ ] **Step 4: Run the focused tests and verify GREEN**

  Run `pytest -q tests/test_outlook_mailbox_oauth.py -k 'single_message_panel or ignores_cloudflare_email_protection or detail_without_code_falls_back'` and confirm all parser, fallback, and freshness assertions pass.

- [ ] **Step 5: Commit the isolated change**

  Run `git add core/base_mailbox.py tests/test_outlook_mailbox_oauth.py && git commit -m "fix(mailapi): parse single-message pages and fall back safely"`.

### Task 2: One-shot OAuth transaction recovery

**Files:**
- Modify: `api/tasks.py:3622-3660` and the existing ChatGPT attempt retry helpers
- Test: `tests/test_chatgpt_login_with_phone.py` or `tests/test_chatgpt_retry_bindings.py`

- [ ] **Step 1: Write a failing test**

  Add a task-level test that supplies an account with a saved Access Token and no `phone_oauth_ready`, makes the first fresh-context attempt return the same state, makes the second return a valid resume context, and asserts LeadBee starts once after the second attempt.

- [ ] **Step 2: Run the focused test and verify RED**

  Run `pytest -q tests/test_chatgpt_login_with_phone.py -k oauth_transaction` and confirm the current implementation exits before LeadBee and reports the existing failure text.

- [ ] **Step 3: Implement bounded recovery**

  Add a one-shot branch that rebuilds the browser/OAuth context for the same mailbox, persists the new context, and rechecks `phone_oauth_ready`; guard it with an attempt-local boolean so no more than one recovery is performed.

- [ ] **Step 4: Run the focused test and verify GREEN**

  Re-run the same command and confirm the second context reaches LeadBee, while a second failure still produces one terminal binding error.

- [ ] **Step 5: Commit the isolated change**

  Run `git add api/tasks.py tests/test_chatgpt_login_with_phone.py tests/test_chatgpt_retry_bindings.py && git commit -m "fix(chatgpt): retry transient phone oauth state once"`.

### Task 3: Progress and synchronization observability

**Files:**
- Modify: `api/tasks.py:2699-2715,3530-3536,4250-4285`
- Modify: `services/external_sync.py:96-126`
- Test: `tests/test_task_snapshot_persistence.py`, `tests/test_external_sync_contribution_mode.py`, and a focused task runtime test

- [ ] **Step 1: Write failing tests**

  Assert that out-of-order futures update progress monotonically by completed count and finish at `total/total`; assert that the task waits for the Codex2API synchronization worker and retains its result in the task log before terminal completion.

- [ ] **Step 2: Run the focused tests and verify RED**

  Run `pytest -q tests/test_task_snapshot_persistence.py tests/test_external_sync_contribution_mode.py -k "progress or codex2api"` and observe the stale progress or missing synchronization assertion.

- [ ] **Step 3: Implement minimal state corrections**

  Remove the attempt-start progress write, update progress in the `as_completed()` loop using the processed counter, force the terminal snapshot to `total/total`, and join external synchronization workers before the task reaches its terminal state. Keep the existing login-success and remote-duplicate semantics.

- [ ] **Step 4: Run focused tests and verify GREEN**

  Re-run the focused command and inspect the persisted task row for monotonic progress and the expected sync result labels.

- [ ] **Step 5: Commit the isolated change**

  Run `git add api/tasks.py services/external_sync.py tests/test_task_snapshot_persistence.py tests/test_external_sync_contribution_mode.py && git commit -m "fix(tasks): make progress and external sync outcomes observable"`.

### Task 4: Log redaction and regression suite

**Files:**
- Modify: `core/base_mailbox.py:4853` and any remaining ChatGPT OTP log paths
- Test: `tests/test_chatgpt_log_sanitizer.py` and the focused MailAPI test

- [ ] **Step 1: Write a failing log assertion**

  Assert that a parsed OTP produces a log line containing the event label but not the six-digit value.

- [ ] **Step 2: Run the test and verify RED**

  Run `pytest -q tests/test_chatgpt_log_sanitizer.py -k otp` and confirm the current MailAPI log contains the raw code.

- [ ] **Step 3: Implement redaction**

  Pass the code through the existing task-secret redaction helper before logging and collapse repeated MailAPI discovery logs per attempt/page signature.

- [ ] **Step 4: Run focused and full tests**

  Run `pytest -q tests/test_chatgpt_log_sanitizer.py tests/test_outlook_mailbox_oauth.py` followed by `pytest -q`.

- [ ] **Step 5: Commit the regression changes**

  Run `git add core/base_mailbox.py tests/test_chatgpt_log_sanitizer.py tests/test_outlook_mailbox_oauth.py && git commit -m "chore(chatgpt): redact otp logs and reduce polling noise"`.

### Task 5: Automation regression coverage

**Files:**
- Modify: `tests/test_chatgpt_auto_relogin_alerts.py` only if a regression assertion is missing
- Modify: `tests/test_chatgpt_auto_relogin.py` only if a scheduler/terminal-state assertion is missing
- Inspect: `api/tasks.py:1450-2428`, `services/chatgpt_relogin.py`, `services/chatgpt_auto_relogin_alerts.py`

- [ ] **Step 1: Run the existing automation tests before release**

  Run `pytest -q tests/test_chatgpt_auto_relogin.py tests/test_chatgpt_auto_relogin_alerts.py tests/test_chatgpt_codex2api_quota.py tests/test_chatgpt_relogin_task.py` and record failures without changing alert semantics.

- [ ] **Step 2: Add only missing regression assertions**

  Cover that a scheduled cycle skips when another ChatGPT task is active, a completed cycle persists `progress=total/total`, quota-exhausted failures do not increment the alert-eligible count, quota threshold alerts send on every below-threshold cycle, and quota query failures skip only the quota alert while preserving task completion.

- [ ] **Step 3: Run the automation tests again**

  Re-run the same command and confirm the scheduler, failure-threshold email, quota email, and task terminal-state assertions pass.

- [ ] **Step 4: Commit automation-only test changes**

  Run `git add tests/test_chatgpt_auto_relogin.py tests/test_chatgpt_auto_relogin_alerts.py tests/test_chatgpt_codex2api_quota.py tests/test_chatgpt_relogin_task.py && git commit -m "test(automation): lock relogin and quota alert behavior"`.

### Task 6: Release, deploy, and verify the four accounts

**Files:**
- Modify: deployment artifact generated from the committed branch; no direct edits on the server

- [ ] **Step 1: Verify repository state**

  Run `git status --short`, `git log --oneline -5`, `pytest -q`, and the project build/deployment asset checks.

- [ ] **Step 2: Push the branch and fast-forward the server release**

  Push the committed branch to the configured repository, transfer the exact commit to the server, create a timestamped release, update `/www/any-auto-register/current`, restart `any-auto-register.service`, and check its status/log for a clean startup.

- [ ] **Step 3: Run the four-account retry**

  Start a task for the four previously stuck accounts, verify each reaches the corrected MailAPI parser, completes OAuth, receives phone verification through the existing flow, saves a Refresh Token, and records Codex2API synchronization.

- [ ] **Step 4: Verify online health**

  Query the service health endpoint, inspect the task row and binding rows, confirm `success=4`, `progress=4/4`, and confirm no raw OTP appears in the new task log.

- [ ] **Step 5: Commit any release metadata only if needed**

  If deployment updates a tracked release pointer or documentation, commit that exact change; otherwise leave application commits unchanged and report the deployed commit SHA.
