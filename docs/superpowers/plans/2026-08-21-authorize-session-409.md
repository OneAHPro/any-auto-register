# ChatGPT MailAPI Login 409 Fix Plan

**Goal:** Make imported MailAPI accounts start only from genuinely available
mailboxes and recover password-reset logins from an expired Web authorize
transaction.

## Root cause

- The Microsoft import snapshot counts disabled rows, so the modal can submit a
  provider plan larger than the claimable pool.
- Password-reset logins reuse a Web authorize fallback transaction after the
  original entry returned 403. The fallback cookie can exist while the server
  has already invalidated that transaction, causing authorize/continue to return
  409.

## Implementation

1. Count only enabled Microsoft mailbox rows in import snapshots.
2. Validate and atomically reserve provider-plan capacity on task submission;
   hand each reservation to the real mailbox claim under the same lock.
3. If password-reset login receives a transient invalid-session response at
   authorize/continue, reset the password through a fresh PKCE OAuth session,
   persist it, then restart the Web login once with the saved password.
4. Add focused regression tests, run the affected suites and full suite, then
   deploy and verify one requeued MailAPI account in production.
