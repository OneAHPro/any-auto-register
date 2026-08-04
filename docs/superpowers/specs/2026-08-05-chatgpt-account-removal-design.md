# ChatGPT Account Removal and Codex2API Cleanup Design

## Goal

Represent deactivated ChatGPT accounts as an independent `已删除账号` task
outcome, keep the re-login alert sensitive to bulk account bans, and optionally
remove the corresponding Codex2API credential before deleting a local account.

The setting is available to the operator, defaults to off for new deployments,
and is explicitly enabled on the current production server after deployment.

## Scope

- Add the boolean configuration `codex2api_delete_on_account_remove_enabled`.
- Apply the setting to automatic deactivation cleanup, manual single-account
  deletion, and manual batch deletion.
- Resolve the corresponding Codex2API row by exact email and stable OAuth
  identity, then delete it through the Codex2API admin account API.
- Delete remotely first and locally second when the setting is enabled.
- Preserve the local account and report a real failure when remote cleanup is
  missing configuration, ambiguous, unauthorized, unavailable, or unsuccessful.
- Count a successfully removed deactivated account as `deleted_account_count`,
  not as a generic task error.
- Count every failed full re-login, including a confirmed deactivated/deleted
  account, in `relogin_failed_count` so bulk bans trigger the configured email
  alert.
- Show partial batch-deletion results instead of reporting unconditional
  success.

## Non-goals

- Do not delete incomplete registration rows from Codex2API; those rows have not
  completed the normal upload lifecycle and continue to use the existing
  internal rollback path rather than the explicit account-removal service.
- Do not use the CLIProxy `/v0/management/auth-files` protocol. Codex2API uses
  its own `/api/admin/accounts/{id}` resource.
- Do not guess credential filenames from email addresses.
- Do not make the setting depend on `codex2api_enabled`; turning off future
  automatic uploads must not prevent cleanup of credentials already uploaded.
- Do not expose Admin Keys, access tokens, refresh tokens, remote account
  payloads, or unredacted upstream response bodies in API responses or logs.

## Configuration and UI

The backend configuration key is:

```text
codex2api_delete_on_account_remove_enabled
```

Missing, blank, or unrecognized values resolve to `"0"`. The config API
normalizes accepted truthy values to `"1"` and all other values to `"0"`.

The Codex2API settings tab gains a separate **删除联动** section with a switch
labeled:

```text
删除本地 ChatGPT 账号时，同步删除 Codex2API 认证
```

Its help text explains that it applies to automatic cleanup, manual deletion,
and batch deletion, and that a remote failure preserves the local account. It
is intentionally separate from the upload switch because the two controls have
different lifecycle meanings.

An automatic task freezes the effective switch value in task metadata as
`codex2api_delete_on_account_remove_enabled`, so changing settings during a
running cycle cannot create mixed behavior inside that cycle.

## Codex2API credential resolution

Credential cleanup uses the existing admin protocol:

1. Read `GET /api/admin/accounts?channel=codex` with `X-Admin-Key`.
2. Match the local email case-insensitively against the remote `email` or
   `name` field.
3. Derive local stable identity aliases from stored `workspace_id`,
   `chatgpt_account_id`, `account_id`, `chatgpt_user_id`, `user_id`, and OAuth
   token claims.
4. When stable identity data exists on both sides, require an identity match.
5. Accept a unique exact-email candidate as the compatibility fallback when
   old remote rows do not expose identity fields.
6. If no candidate exists, return `already_absent` and continue with local
   deletion.
7. If more than one candidate remains, return `ambiguous`, do not issue DELETE,
   and preserve the local account.
8. Delete the unique numeric ID with
   `DELETE /api/admin/accounts/{remote_id}`.

HTTP 200, 201, and 204 are successful deletions. HTTP 404 after a successful
list/identity resolution is an idempotent `already_absent` result. Authentication
errors, other non-success statuses, parse errors, timeouts, and transport errors
are failures.

The public Codex2API cleanup helper returns only a structured status, numeric
remote ID when known, and a bounded redacted message. It never returns or logs
the matching remote row.

## Deletion transaction order

When the setting is off, existing local-only deletion remains in effect.
Non-ChatGPT platforms are always local-only.

When the setting is on for a ChatGPT account:

1. Snapshot the local row and acquire the same per-account coordination lock
   used by re-login.
2. Resolve and delete the Codex2API credential under a shared remote-mutation
   lock.
3. Treat `deleted` and `already_absent` as permission to continue.
4. Delete the local row with an identity/concurrency guard.
5. Return the two cleanup states as structured data.

Remote cleanup failure leaves the local row intact. If remote cleanup succeeds
but the local compare-and-delete fails, the operation reports a real partial
failure and leaves the local row visible; a retry observes `already_absent` and
can complete the local deletion. Cross-system rollback is not claimed.

Automatic cleanup already holds the account re-login lock, so it uses the same
service without reacquiring that lock. A stop checkpoint runs before the remote
mutation and again before the local delete.

Every explicit ChatGPT deletion uses remote resolution when the switch is on,
including imported or legacy rows that currently contain only an Access Token.
The absence of a local Refresh Token is not evidence that no remote credential
exists. Internal rollback of a newly created, incomplete registration row keeps
its current local-only path because it occurs before the normal upload lifecycle.

Initial upload, credential replacement, and credential deletion all acquire the
same process-wide reentrant Codex2API mutation lock. This prevents an in-flight
upload from recreating a credential between remote deletion and the guarded
local delete, while allowing replacement code to use its existing internal
cleanup operation without self-deadlocking.

## Manual API behavior

### Single delete

`DELETE /api/accounts/{account_id}` keeps the existing route. A successful
response contains `ok`, `account_id`, `local_deleted`, and a `codex2api` object
with `enabled`, `status`, and `remote_id` when known.

- Missing local account: HTTP 404.
- Concurrent-account conflict: HTTP 409.
- Remote cleanup failure: HTTP 502 and `local_deleted=false`.
- Remote status `deleted`, `already_absent`, `skipped_disabled`, or
  `not_applicable`: local deletion may complete.

### Batch delete

`POST /api/accounts/batch-delete` keeps the existing request body and 1000-ID
limit. IDs are deduplicated while preserving request order. Each account is
processed and committed independently because completed remote side effects
cannot be rolled back as one database transaction.

The response retains `deleted`, `not_found`, and `total_requested` and adds:

- `total_unique`
- `failed`
- `remote_deleted`
- `remote_already_absent`
- `remote_skipped`
- `items`, with one bounded structured result per unique ID

The frontend shows success when every found account was deleted, warning for a
partial result, and error when all found accounts failed. Successfully deleted
and not-found IDs are removed from selection; failed IDs remain selected for a
retry.

## Automatic task outcomes and alert math

Add `AttemptOutcome.REMOVED` and `AttemptResult.removed()`. A confirmed account
removal returns this outcome instead of `FAILED`.

For a terminal task:

```text
processed = success + generic_failures + skipped + deleted_account_count
```

`deleted_account_count` increments only after the local record is confirmed
removed or already absent. A removed account does not enter `errors`, so the
task card renders `已删除账号 N` instead of `失败 N` for that result. Its per-account
history status is `removed`, not `failed`.

For automatic task cards, the summary endpoint derives the red account count
as `max(relogin_failed_count - deleted_account_count, 0)`. This gives the
operator's intended invariant: `失败` means an ordinary re-login failure, while
confirmed bans/deactivations are displayed exactly once under `已删除账号`.

The alert metric intentionally overlaps result categories:

- `invalid_rt_count` counts confirmed Codex2API authentication failures.
- `relogin_failed_count` counts every full login that did not succeed,
  including accounts that were confirmed deleted/deactivated and subsequently
  cleaned up.
- `deleted_account_count` is therefore a subset of
  `relogin_failed_count` for the normal deactivation flow.

The configured threshold compares against `relogin_failed_count` using `>=`.
For example, 3 ordinary full-login failures plus 17 confirmed deleted accounts
produce `relogin_failed_count=20`, trigger the alert at threshold 20, render
`失败 3`, and render `已删除账号 17`.

The email fourth metric remains **重登失败** and uses this inclusive count. The
email detail states how many of those accounts were deleted/deactivated so a
bulk ban is immediately recognizable.

Remote cleanup failure or local compare-and-delete failure remains a generic
failure, remains in the local list, and counts toward `relogin_failed_count`.

## Error handling and observability

- Task logs use `[REMOVE]` for completed deactivation cleanup and `[FAIL]` for
  remote or local cleanup failures.
- Task-history rows use status `removed` and the UI renders `已删除` with a
  neutral/warning tag rather than `失败`.
- Batch item errors expose stable codes such as `remote_ambiguous`,
  `codex2api_delete_failed`, and `local_delete_conflict`; they do not expose
  upstream bodies.
- Log entries include only the local account ID, a redacted email label, the
  numeric remote ID when safe, the stage, and the sanitized error type/status.

## Test strategy

### Codex2API cleanup helper

- Unique exact email and identity match issues the correct DELETE request.
- A unique legacy email candidate works without identity fields.
- Multiple remaining candidates return `ambiguous` without deletion.
- No match and DELETE 404 are idempotent success states.
- Authentication, server, parse, timeout, and transport failures are bounded
  and redact all configured credentials and token values.

### Local deletion service and APIs

- The default/disabled setting preserves local-only behavior.
- Non-ChatGPT rows never invoke Codex2API cleanup.
- Enabled single deletion performs remote deletion before local deletion.
- A remote failure preserves the local row.
- A local conflict after remote success reports partial failure and remains
  retryable.
- Batch IDs are deduplicated, successes commit independently, failures remain,
  and response counters/items are exact.

### Automatic cleanup and counters

- A deactivated account with the setting off is locally removed.
- With the setting on, remote `deleted` and `already_absent` allow local removal.
- Remote failure preserves the local account and is a real failure.
- Removed attempts increment `deleted_account_count`, do not enter `errors`, and
  are saved with task-history status `removed`.
- Removed attempts still increment `relogin_failed_count`, and 20 combined
  ordinary/deactivated failures trigger a threshold of 20.

### Frontend

- Settings loads, saves, and rehydrates the new boolean switch.
- Running Tasks displays `失败 N`, `重登失败 N`, and `已删除账号 N` from their
  distinct contracts.
- Task History renders `removed` as `已删除`.
- Batch deletion presents partial outcomes and keeps failed IDs selected.

## Production deployment

The production database is backed up and verified before deployment. After the
new release is healthy, set `codex2api_delete_on_account_remove_enabled=1` on
the production server without changing the existing automatic re-login,
interval, concurrency, or alert-threshold values.

Verification uses a synthetic/no-match account fixture or a mocked request path;
it does not delete a live operator account merely to test the switch. Production
checks confirm the config value, public bundle, service health, and absence of
credential material in recent logs.
