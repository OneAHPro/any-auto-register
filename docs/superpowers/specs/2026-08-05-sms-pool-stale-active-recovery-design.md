# SMS Pool Stale Active Recovery Design

## Goal

Prevent LeadBee SMS pool cards from remaining in **使用中** forever after their
owning registration task has stopped, failed, or disappeared, while continuing
to protect a card during the provider's legitimate processing window.

The production diagnosis on 2026-08-05 found four `sms_pool_items` rows in
`active`. All four had an empty `reserved_task_id`, no `reserved_at`, no
`used_at`, failed attempt bindings, and only stopped task runs. Their
`updated_at` values exactly matched the current service start time. That match
proved startup recovery was refreshing the age of already orphaned rows instead
of making them eligible for later recovery.

## Current behavior and root cause

The pool lifecycle currently has four states:

```text
unused -> reserved -> active -> unused | used
```

- `reserved` means a local task owns the row but has not submitted the card to
  LeadBee.
- `active` means provider work started and the final remote settlement is not
  known locally.
- `used` means the card was consumed or was conservatively made unavailable.
- `unused` means the card can be allocated to a new task.

The state transitions are individually conservative, but three behaviors make
`active` permanent:

1. `SmsPoolService.release_task()` deliberately keeps an `active` row active,
   clears its task owner and reservation time, and refreshes `updated_at`.
2. `SmsPoolService.recover_interrupted()` does the same thing at every service
   start, refreshing `updated_at` again.
3. No scheduler path expires ownerless or terminal-owner `active` rows.

The management UI then amplifies the problem:

- the API combines `active` with `reserved` in the `reserved` count and filter;
- the frontend labels both states **使用中**;
- the status-time column ignores `updated_at`, so an ownerless active row falls
  back to its original `created_at`.

The result is a row that is neither owned by a running task nor available for
reuse, has its age reset after every restart, and looks as though it has been in
active use since its import date.

## Scope

- Preserve the start time of an existing `active` state across task cleanup and
  service recovery.
- Automatically release stale active rows after 30 minutes when no running task
  still owns them.
- Run interrupted-state recovery at application startup and stale-active
  recovery every 600 seconds.
- Keep the strict timeout boundary: a row exactly 30 minutes old remains until
  the next sweep.
- Split `active` from `reserved` in the API and UI.
- Display `active` as **待回收** and use `updated_at` as its status time.
- Preserve the existing provider-settlement callbacks and immediate quarantine
  behavior.

## Non-goals

- Do not add or migrate database columns.
- Do not make a fresh LeadBee activation, receive-SMS, or cancellation request
  from the recovery job.
- Do not release an active row while its owning task is still `pending` or
  `running`.
- Do not alter the handling of confirmed `consumed`, `restored`, or `unusable`
  settlements.
- Do not change card import, card-code display, login concurrency, task control,
  or automatic re-login behavior.
- Do not repair current production rows in this code change; the separate
  operator recovery has already returned them to `unused`.

## Chosen architecture

### Reuse `updated_at` as `active_since`

No schema change is required. While a row's status is `active`, `updated_at`
becomes the immutable start time of that active period:

- `mark_active()` writes `updated_at` when `reserved` transitions to `active`;
- a second provider attempt after `mark_restored()` writes a new active start;
- `release_task()` does not refresh `updated_at` when the row was already
  active;
- `recover_interrupted()` does not refresh `updated_at` for active rows;
- converting an explicitly quarantined `reserved` row to `active` writes the
  quarantine time because that row has no earlier active timestamp;
- leaving `active` for `unused` or `used` writes the terminal transition time.

`reserved_at` continues to mean reservation time. Task cleanup may clear it as
it does today. The active recovery deadline depends only on `updated_at`.

This invariant supports all future rows with the current table. Exact active
start times for legacy rows already touched by the old startup recovery cannot
be reconstructed; their last stored `updated_at` is the conservative fallback.

### Thirty-minute stale-active sweep

Add `SmsPoolService.recover_stale_active()` with these inputs and defaults:

```python
recover_stale_active(
    *,
    now: datetime | None = None,
    stale_seconds: float = 30 * 60,
) -> int
```

The method uses a SQLite `BEGIN IMMEDIATE` transaction and selects only rows
that satisfy all of the following:

1. `status == "active"`;
2. `updated_at < now - 30 minutes`;
3. `reserved_task_id` is empty, or the referenced task is missing, `done`,
   `failed`, or `stopped`.

A task in `pending` or `running` always protects its active rows, even when the
active start is older than 30 minutes. This preserves cards held by a legitimate
long-running batch. A task cannot move from a terminal status back to running,
so terminal-owner recovery is monotonic.

The comparison is strictly `<`. At exactly 30 minutes the row remains active;
the next 10-minute sweep may recover it.

For each eligible row, recovery sets:

```text
status = unused
reserved_task_id = ""
reserved_at = NULL
used_at = NULL
used_by_email = ""
updated_at = recovery time
```

The method returns the number of recovered rows. It never logs card codes,
emails, or task contents.

### Startup and periodic recovery

Application startup keeps the current interrupted-state recovery order:

1. create/migrate database tables;
2. terminalize interrupted attempt bindings;
3. call `recover_interrupted()` so unstarted `reserved` rows become `unused`
   and old-process active owners are cleared;
4. immediately call `recover_stale_active()`.

After startup, the scheduler calls `recover_stale_active(now=wall_now)` every
600 monotonic seconds. Startup initializes the scheduler's last-run marker to
the current monotonic value because `init_db()` has already performed the
immediate sweep.

A recovery exception is isolated from automatic re-login, task-history cleanup,
trial checks, and CPA maintenance. The successful-run marker advances only
after recovery returns, so a failed sweep is retried on the next scheduler
loop.

### API and UI state semantics

`GET /api/sms-pool/stats` changes from a combined count to four independent
counts:

```json
{
  "total": 154,
  "unused": 11,
  "reserved": 0,
  "active": 0,
  "used": 143
}
```

`reserved` counts only `reserved`; `active` counts only `active`. The list API
accepts `status=active`, while `status=reserved` returns only reserved rows.

The frontend presents:

- `unused` as **未使用**;
- `reserved` as **使用中**;
- `active` as **待回收**;
- `used` as **已使用**.

The summary tags show **使用中 N** and **待回收 N** independently. The filter
offers the same four states. The status-time column uses this fallback order:

```text
used_at -> reserved_at -> updated_at -> created_at
```

An ownerless active row therefore shows when its current active period began,
not when the card was imported.

## Provider safety analysis

LeadBee's current integration requires the same in-memory HTTP session and
cookies for activation, phone allocation, SMS polling, and cancellation. The
database stores the card code and base URL but does not store the provider
cookies, remote card identity, or provider session identity. The available
provider operations are mutating operations; there is no supported read-only
status endpoint that can safely reconcile an orphan after process loss.

For that reason, the recovery job is deliberately local and time based. Thirty
minutes is well beyond both relevant local limits:

- the LeadBee provider worker has a hard maximum total deadline of 540 seconds;
- the automatic verification broker has a 600-second TTL.

The timeout is a recovery policy, not a claim that LeadBee positively confirmed
the card's restoration. If a released card is still held remotely, the next
normal use receives the provider conflict, moves the row back to `active`, and
starts a fresh 30-minute quarantine. This is safer than an automated probe that
could itself claim or cancel a remote card.

Persisting encrypted provider cookies and remote task identity could support
true post-restart reconciliation, but that is a separate protocol and data
model change outside this fix.

## Concurrency and failure handling

- All pool state mutations remain under the service `RLock` and a database
  write transaction.
- The stale sweep rechecks state inside the write transaction; it cannot release
  a row concurrently moved to another status.
- Fresh `active` rows are excluded by time before task status is considered.
- `pending` and `running` owner tasks are protected.
- Empty, missing, and terminal owners become recoverable only after the timeout.
- A scheduler failure does not advance its last-success timestamp.
- A later provider callback remains subject to the existing task ownership
  checks. The 30-minute sweep runs only after the local provider and broker
  deadlines have elapsed.
- Logs and API responses never add card codes, emails, or provider payloads as
  part of recovery reporting.

## Test strategy

### Pool service

- Existing active quarantine remains active before 30 minutes.
- `release_task()` preserves the timestamp of an already active row.
- `recover_interrupted()` clears old-process ownership without refreshing the
  active timestamp.
- An ownerless active row older than 30 minutes becomes unused.
- A stale active row whose task is missing or terminal becomes unused.
- A stale active row whose task is pending or running remains active.
- A row exactly on the 30-minute boundary remains active.
- An explicitly quarantined reserved row receives a new active timestamp.
- Recovered rows clear ownership and email metadata.

### Startup and scheduler

- `init_db()` performs interrupted recovery before stale-active recovery.
- The periodic recovery does not run before 600 seconds.
- It runs once at 600 seconds and advances the success marker.
- A failure leaves the marker unchanged and does not prevent other scheduled
  work.

### API and frontend

- Stats report `reserved` and `active` separately.
- `status=reserved` and `status=active` return disjoint rows.
- Active rows render **待回收**, while reserved rows render **使用中**.
- The UI shows the independent active count.
- An active row's status time uses `updated_at` when reservation and use times
  are empty.

### Production verification

- Back up SQLite through its backup API before the first startup on the new
  release and verify the backup with `PRAGMA quick_check`.
- Verify there are no pending/running registration tasks before switching the
  release.
- After startup, verify no active row older than 30 minutes has an empty,
  missing, or terminal owner.
- Confirm recent or running-owner active rows remain protected.
- Confirm the management page shows separate **使用中** and **待回收** counts.
- Confirm automatic re-login settings and the public service remain healthy.
