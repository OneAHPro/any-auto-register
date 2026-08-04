# Task Runs Performance and 12-Hour Retention Design

## Goal

Make the **任务运行** page load promptly and remain responsive while keeping only the task records that are useful for current operations.

The production diagnosis on 2026-08-05 found 539 persisted task runs and an approximately 13.6 MB `GET /api/tasks` response. Persisted logs accounted for about 96% of the payload. The page downloaded and parsed that complete response every 2.5 seconds, rendered every task as an Ant Design card, and allowed polling requests to overlap.

## Scope

- Add a lightweight task-summary endpoint for the task card list.
- Switch the **任务运行** page to the summary endpoint.
- Prevent a new list poll while the previous list request is still in flight.
- Retain terminal task runs for 12 hours after effective completion.
- Delete terminal task runs older than 12 hours every 10 minutes and immediately after service startup.
- Back up the production SQLite database before the first 12-hour purge.
- Preserve full task detail and log streaming for records still inside the retention window.

## Non-goals

- Do not delete accounts, SMS pool items, proxies, configuration, or active tasks.
- Do not change the independent `task_logs` retention policy. Successful task logs remain on the existing 30-day policy and failed task logs on the existing 90-day policy.
- Do not add pagination or virtual scrolling in this change. The 12-hour production population is currently about 69 records, which is small enough once list payloads are summaries.
- Do not change automatic re-login scheduling, concurrency, alerting, or authentication.
- Do not run `VACUUM` during deployment; removing rows immediately reduces query and response work without an exclusive database rewrite.

## Chosen architecture

### Lightweight list contract

Keep `GET /api/tasks` unchanged for compatibility and add `GET /api/tasks/summary` for the management page.

Each summary item contains only:

- `id`, `platform`, `source`, `status`
- `total`, `success`, `registered`, `skipped`
- `error_count`
- `created_at`, `updated_at`
- `meta.automation`
- `meta.invalid_rt_count`
- `meta.relogin_failed_count`
- `meta.deleted_account_count`
- `meta.alert_sent`
- `meta.alert_reason`

The summary query projects only the database columns needed to create those values. It must not load or return `logs_json`, full error text, `control_json`, `cashier_urls_json`, or unrelated metadata. Active tasks sort first, followed by terminal tasks newest first.

`GET /api/tasks/{task_id}` and `GET /api/tasks/{task_id}/logs/stream` remain the full-detail paths used by the log drawer.

### Frontend polling

`RunningTasks` requests `/tasks/summary` on mount and keeps the existing 2.5-second refresh interval. A request-in-flight guard skips interval ticks until the current request settles. The card uses `error_count` instead of downloading `errors` only to read its length.

No log component is mounted until the user opens a task drawer, preserving the current lazy-detail behavior.

### Twelve-hour task-run retention

The retention service treats `done`, `failed`, and `stopped` as terminal. `pending` and `running` are always excluded, regardless of age.

For each terminal row, the effective completion time is:

1. `meta_json.completed_at` when it exists and is a valid epoch timestamp; otherwise
2. `updated_at` for legacy or orphan-finalized rows.

A row is expired only when its effective completion time is strictly earlier than `UTC now - 12 hours`. A row exactly on the boundary remains until the next cleanup cycle.

The default is configurable through `TASK_RUN_RETENTION_HOURS`, with a default of `12`. Existing `TASK_HISTORY_RETENTION_DAYS` and `TASK_HISTORY_FAILURE_RETENTION_DAYS` continue to control only the independent `task_logs` table.

Candidate selection projects only task ID, status, metadata, and update time. Deletion is performed as a bounded SQL delete by candidate IDs so the cleanup process does not load old persisted logs into Python.

The scheduler runs cleanup immediately at startup and then every 600 seconds. Cleanup failures remain isolated from automatic re-login and other scheduler work; a failed cleanup is retried on the next scheduler loop because the successful-run timestamp is not advanced.

## Data safety and deployment

Before the production purge:

1. Confirm the scheduler has no pending or running task.
2. Create a consistent SQLite backup through SQLite's backup API, including live WAL state.
3. Verify the backup with `PRAGMA quick_check`.
4. Stage and preflight the new release.
5. Atomically switch the release and restart only `any-auto-register.service`.

The startup cleanup should remove only expired terminal `task_runs`. The deployment verification prints counts and byte sizes only; it must not print logs, email addresses, credentials, card codes, tokens, or configuration secrets.

The previous release and the pre-purge database backup are retained for rollback.

## Error handling

- An invalid or non-positive `TASK_RUN_RETENTION_HOURS` value falls back to 12 hours.
- Malformed or missing `meta_json.completed_at` falls back to `updated_at`.
- A cleanup exception is logged by type without exposing task contents and does not stop the scheduler.
- The frontend always clears its request-in-flight flag in `finally`, so a failed request cannot permanently stop later refreshes.

## Test strategy

### Backend

- Summary endpoint excludes all heavy/detail fields and returns the exact card contract.
- A record with a large persisted log does not increase summary output or appear in the projected response.
- Summary order is active first and terminal newest first.
- All terminal statuses older than 12 hours are deleted.
- Active rows are preserved even when very old.
- Rows exactly 12 hours old remain.
- Immutable `meta.completed_at` wins over a later `updated_at`.
- Legacy rows without completion metadata use `updated_at`.
- The environment retention value is honored, with invalid values falling back to 12 hours.
- Task-log 30/90-day retention remains unchanged.
- Scheduler startup cleanup and 10-minute cadence are locked by tests.

### Frontend

- The page requests `/tasks/summary` and displays `error_count`.
- A pending summary request prevents an overlapping poll.
- The guard clears after success and failure.
- Existing automatic-authentication counters and lazy log drawer behavior remain intact.
- Automatic-authentication cards render `meta.deleted_account_count` as the
  independent label `已删除账号 N`; the field is a result category rather than
  an error count.

### Production

- Measure authenticated summary response time and bytes without printing its body.
- Confirm only terminal rows older than 12 hours were removed.
- Confirm all active and recent terminal rows remain.
- Confirm the database and backup both pass `PRAGMA quick_check`.
- Confirm the service, public bundle, automatic re-login settings, and recent logs are healthy.
