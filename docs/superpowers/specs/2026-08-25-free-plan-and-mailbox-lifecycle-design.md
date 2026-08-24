# Free Plan Gate and Mailbox Lifecycle Design

## Goal

After a successful ChatGPT credential login, detect the subscription before MFA rotation, phone verification, account persistence, or external sync. Free accounts are permanently skipped and hidden from both the local account list and import pool. Running and failed mailbox records remain visible in the import page.

## Login gate

The existing-account engine calls `backend-api/me` immediately after receiving a valid Access Token. `plus`, `pro`, `team`, and `enterprise` continue. `free` returns a typed skipped result before MFA rotation and phone-provider startup. A failed or ambiguous subscription probe fails the attempt and returns the mailbox as failed rather than risking paid downstream work.

The plugin consumes a Free mailbox without creating an `AccountModel`, raises a typed skip, and the task summary increments `free_skipped_count`. The task log states the email and `free` plan without exposing credentials.

## Mailbox lifecycle

AppleMail records use `available`, `claimed`, `failed`, and `used`. Failed records are claimable on a later task; claimed and failed records are visible in snapshots; used records remain hidden. Free records are consumed as `used` and therefore disappear.

Microsoft records keep the equivalent database lifecycle: `available`, `leased`, `failed`, `bound`, and `discarded`. `available`, `leased`, `failed`, and quarantined failures are visible; bound/discarded rows are hidden. Failed rows remain claimable.

Every failure path calls a mailbox failure transition with the redacted stage and task id. On process startup, orphan AppleMail claims are recovered as failed. Outlook lease recovery uses failed rather than silently available state.

## Import UI

Snapshot items include `pool_state`, `last_error`, and `last_task_id`, plus separate `available_count` and `visible_count`. The page shows Available, Processing, and Login Failed badges. Processing rows cannot be deleted or selected. It refreshes periodically while mounted. Successful and Free rows disappear.

## Verification

- Free plan stops before MFA rotation and provider startup.
- Free leaves no local account and no visible import record.
- Claimed and failed records remain visible while used records stay hidden.
- Failed records can be claimed by a later task.
- Backend and frontend complete suites pass before immutable deployment.
