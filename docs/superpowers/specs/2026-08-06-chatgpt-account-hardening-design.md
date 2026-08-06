# ChatGPT Account Password and MFA Hardening Design

## Goal

After every successful ChatGPT account login, ensure the account has a durable
password and a locally recoverable TOTP MFA secret. Apply the same hardening to
all existing server-side ChatGPT accounts in a resumable, low-concurrency batch.
Subsequent login probes must prefer password plus TOTP and avoid mailbox OTP.

## Scope

This feature covers:

- Future registration and existing-account login successes.
- A one-time and repeatable batch for all existing ChatGPT account rows.
- Password creation when the account has no usable password.
- TOTP MFA enrollment and local verification.
- Recovery of already-enabled MFA secrets from local records, import pools, and
  database backups.
- Explicit classification of accounts whose remote MFA is already enabled but
  whose mailbox and local MFA secret are both unavailable.
- Crash-safe persistence, secret redaction, task progress, and retry behavior.

It does not automatically disable an existing remote MFA factor. Removing a
factor without first preserving a usable secret could lock out an otherwise
healthy account.

## Existing Capabilities

The project already supports:

- Passwordless mailbox OTP login.
- Password reset through mailbox OTP.
- Password plus TOTP login when `totp_secret` is available.
- Local TOTP generation and MFA challenge verification.
- Password and mailbox-login-context persistence in `AccountModel`.
- Resumable task state, safe-stop controls, and sensitive-log sanitization.

The missing capability is remote TOTP enrollment after authentication, followed
by durable promotion of the returned secret into the account's normal login
context.

## Architecture

Create a focused `ChatGPTAccountHardeningService` in
`services/chatgpt_account_hardening.py`. The service owns one account hardening
state machine and exposes two entry points:

1. `harden_authenticated_account(...)` for the active login session.
2. `harden_saved_account(...)` for the existing-account batch.

Both paths use the same password, MFA enrollment, persistence, redaction, and
classification logic. Future login flows call the first entry point immediately
after tokens have been obtained. The batch worker uses the second entry point and
reuses the existing-account login machinery only when an authenticated session
must be rebuilt.

The OpenAI-specific HTTP exchange belongs in
`platforms/chatgpt/account_hardening.py`. It receives an authenticated session
and performs MFA inventory, enrollment start, local TOTP verification, and
enrollment confirmation. It returns typed results and never writes the database.

## Account Classification

Before changing an account, classify it into one of these states:

- `ready`: password and verified local TOTP secret are already present.
- `needs_password`: password is absent or was explicitly rejected.
- `needs_mfa`: no remote TOTP factor is enabled.
- `recoverable_mfa`: remote TOTP is enabled and a candidate secret exists in the
  account row, mailbox login context, import pool, task binding, or backup.
- `replacement_candidate`: remote TOTP is enabled, no local secret exists, but an
  authenticated settings session supports adding or replacing a factor without
  removing the only working factor first.
- `missing_mfa_material`: remote TOTP is enabled, no secret or mailbox exists,
  and the authenticated settings session does not expose a safe replacement
  operation. The account and tokens remain untouched and the batch reports it
  separately for secret recovery.

Classification is idempotent. A verified `ready` account is skipped without
opening another MFA enrollment.

## Password Flow

1. Keep an existing usable password.
2. When no password exists, generate a unique strong password per account using
   `secrets`; never reuse a global password.
3. Use the project's existing password-reset flow and mailbox OTP once.
4. Persist the password only after the remote reset success state is returned.
5. If the mailbox is gone and the account has no password, retain the valid
   tokens and classify the password stage as pending; do not delete the account.

## MFA Enrollment Flow

1. Query the authenticated account's MFA factor inventory.
2. If no TOTP factor exists, start TOTP enrollment and parse the returned
   provisioning secret or `otpauth://` URI.
3. Validate the secret format locally and generate the current six-digit code.
4. Before confirming remote enrollment, persist a crash-recovery marker in the
   account row:
   - `mfa_hardening_status = "confirming"`
   - `mfa_pending_secret = <secret>`
   - `mfa_hardening_started_at = <UTC timestamp>`
5. Submit the local TOTP code to confirm enrollment.
6. On remote success, atomically promote the pending secret:
   - `totp_secret = <secret>`
   - `account_type = "chatgpt_password_totp"`
   - `mfa_hardening_status = "ready"`
   - `mfa_enabled_at = <UTC timestamp>`
   - remove `mfa_pending_secret`
7. Immediately run a local TOTP challenge verification when the remote flow
   exposes one. The canary deployment also performs a complete second login with
   mailbox access disabled.

If the process stops after remote confirmation but before promotion, the pending
secret remains recoverable and the next run resumes verification instead of
starting a second enrollment.

## Already-Enabled MFA Without Mailbox

For accounts that already have remote MFA but no mailbox:

1. Search current account extras, `mailbox_login_context`, imported credential
   pools, task bindings, and historical SQLite backups by normalized email.
2. Validate each candidate secret by submitting a locally generated TOTP through
   the existing MFA challenge flow. Never print or return candidate secrets.
3. Promote the first verified candidate into the account's standard
   `chatgpt_password_totp` context.
4. If no candidate exists but the current access or refresh token can establish
   an authenticated settings session, inspect the remote factor inventory and
   use only a safe add/replace operation that preserves the existing factor until
   the new factor is confirmed.
5. If neither recovery path is available, set
   `mfa_hardening_status = "missing_mfa_material"`; preserve the account, tokens,
   and remote factor unchanged.

## Persistence and Concurrency

- Account changes use ID, email, and `updated_at` compare-and-swap guards.
- Only one hardening worker may own an account at a time.
- Batch concurrency defaults to 1 and is capped at 2.
- The batch is resumable and skips `ready` accounts.
- Login success and tokens are saved before optional hardening begins.
- A hardening failure never removes a successful account and never clears valid
  tokens.
- Current manual and scheduled ChatGPT tasks keep their existing task gate and
  safe-stop priority.

## Future Login Integration

Future registration and existing-account login follow this sequence:

1. Complete the current OAuth flow and save tokens.
2. Run account hardening in the same authenticated session.
3. Mark the task detail as `ready`, `hardening_pending`, or
   `missing_mfa_material`.
4. On later login, prefer password plus local TOTP whenever the account is
   `ready`; use mailbox OTP only as a fallback for accounts not yet hardened.

Hardening is a post-login stage. A successful login remains successful even when
hardening must be retried, but the task summary reports that partial state.

## API and Task Surface

Add a ChatGPT hardening task that supports:

- All existing accounts.
- A selected account ID for canary and retry.
- Dry-run classification without remote mutation.
- Safe stop and resumable progress.

Task counters are:

- total
- ready_before
- hardened
- recovered_secret
- pending_password
- missing_mfa_material
- failed

Logs identify accounts by email only in the existing operator UI. Passwords,
TOTP secrets, OTP codes, recovery codes, tokens, provisioning URIs, and sensitive
query strings are always redacted.

## Failure Handling

- Transient network or service errors: preserve the current stage and retry in a
  later batch.
- Password reset failure: retain tokens and record `pending_password`.
- Enrollment start failure: retain the existing login method and record the
  typed error.
- Enrollment confirmation failure: retain the pending secret for a bounded retry
  and do not start another enrollment.
- Local persistence failure after remote confirmation: recover from
  `mfa_pending_secret` on the next run.
- Existing unknown remote MFA: preserve it and report
  `missing_mfa_material`; never disable it automatically.
- Task stop: finish or safely checkpoint the current remote confirmation, then
  stop dispatching new accounts.

## Security

- Generate passwords with `secrets`, independently per account.
- Generate TOTP codes locally; never send a TOTP secret to a third-party 2FA
  service.
- Reuse the project's current database protection model for stored account
  secrets and extend API sanitization to `totp_secret`, `mfa_pending_secret`,
  provisioning URIs, and recovery material.
- Never include secrets in exception messages, task snapshots, logs, or HTTP
  responses.
- Create a verified SQLite backup before deployment and before the production
  batch.

## Testing

Use strict test-first cycles for:

- MFA inventory parsing and already-enabled detection.
- Enrollment start, provisioning-secret parsing, local code generation, and
  confirmation.
- Pending-secret crash recovery.
- Idempotent skip for ready accounts.
- Password generation and remote-reset commit ordering.
- Accounts with existing MFA and no mailbox.
- Candidate-secret recovery from account data and backups.
- Preservation of tokens and account rows on every failure class.
- Compare-and-swap persistence and concurrent ownership.
- API and log sanitization.
- Batch counters, resume, safe stop, and concurrency cap.
- Future successful-login hook.

## Deployment

1. Run the focused and full local test suites.
2. Prepare a server-side release and rerun focused tests there.
3. Wait for all current tasks to become terminal.
4. Back up and integrity-check the production database.
5. Deploy with the existing atomic release switch and rollback path.
6. Dry-run classify all accounts.
7. Harden one recoverable account as a canary.
8. Verify a second full login with mailbox retrieval disabled.
9. Run the resumable batch at concurrency 1, raising to 2 only if service load
   and error rate remain normal.
10. Report all counters and preserve the recovery list for accounts whose MFA
    material is still missing.
