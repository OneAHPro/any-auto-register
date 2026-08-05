# Mailbox OTP Timeout Auto-Removal Design

## Goal

During scheduled ChatGPT authentication maintenance, remove a local account when its full-login flow spends at least 180 seconds in the email OTP stage and obtains zero candidate verification codes.

## Safety boundary

All of the following must be true:

- the failure comes from the OAuth email OTP stage;
- the configured mailbox wait budget is at least 180 seconds;
- the measured login attempt elapsed time reaches that budget;
- the OAuth result reports zero attempted OTP codes;
- the caller explicitly enables timeout cleanup for an automatic maintenance run.

Password failures, rejected OTP codes, mailbox HTTP errors, MFA/TOTP errors, task interruption, manual relogin, and waits shorter than 180 seconds keep the local account.

## Approach

Three implementation locations were considered:

1. Parse the final OAuth failure inside the saved-credential login service and convert only the exact exhausted-wait result into a typed exception. This is the selected approach because classification remains next to the login result and deletion remains inside the existing per-account lock.
2. Teach the OAuth client to delete accounts. This would couple a protocol client to database policy and is rejected.
3. Parse task log strings after a failed account. This is vulnerable to wording changes and would perform deletion after releasing the account lock, so it is rejected.

The scheduled task passes an explicit cleanup flag to the relogin service. The relogin service catches the typed timeout, calls the existing ordered account-removal service, and returns the standard `account_removed` result with `removal_reason=mailbox_otp_timeout`. Existing Codex2API remote-deletion configuration remains authoritative.

## Two-phase automatic dispatch

The Codex2API service already completes one global `wham_only` probe before any local login. After that snapshot returns, the task must also preserve a strict dispatch boundary:

1. Process every `healthy`, `deferred`, `ambiguous`, and locally missing probe result first. These accounts update task progress without starting a browser login.
2. Only after every probe-only result is counted may `auth_failed` and `remote_missing` accounts enter confirmation and full login.

Probe-only account IDs are ordered ahead of login candidates. A task-local event gates login candidates without consuming an active login slot; stop and skip checkpoints remain responsive while a candidate waits for the phase boundary. This makes task progress reflect the global probe before slow mailboxes begin.

## Observability

The account result and task log state that the mailbox OTP wait reached its budget with zero codes and the local record was removed. The task records the item as `removed`, increments `deleted_account_count`, and does not add it to ordinary task errors.

## Verification

Tests cover the strict timeout classifier, all non-triggering boundaries, local deletion inside the account lock, manual relogin preservation, automatic-task flag propagation, removed-item logging, strict probe-before-login ordering, and existing deactivation cleanup behavior.
