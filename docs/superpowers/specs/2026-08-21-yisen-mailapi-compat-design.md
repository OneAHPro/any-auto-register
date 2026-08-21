# Yisen MailAPI Compatibility Design

## Goal

Accept supplier rows in the form `email----password----jwt` for `@yisen.uk`
mailboxes, poll `https://mail.yisen.uk` from production, and extract OpenAI
email verification codes without exposing mailbox JWTs.

## Confirmed protocol

- The supplier JWT contains the mailbox address and address identifier.
- Mail is listed with `GET /api/mails?login=<email>&limit=20&offset=0`.
- The request requires `Authorization: Bearer <jwt>`.
- Production receives HTTP 403 with the default Python client fingerprint and
  HTTP 200 with the browser-style headers used by the Yisen frontend.
- The response is `{count, results}`. Each result contains `message_id`,
  `metadata`, `raw`, and `created_at`; `metadata` is a JSON string whose
  `subject` identifies OpenAI mail, while `raw` contains the complete MIME
  message. The optional `lite=1` response omits `raw` and therefore cannot be
  used for OTP extraction.

## Data model and import

Add `mailapi_token` to the Microsoft mailbox table and its SQLite migration.
The token remains separate from `mailapi_url`, so it is not placed in URLs,
snapshots, errors, or routine logs.

The Microsoft and automatic import parsers recognize exactly three fields as
Yisen when all of these conditions hold:

1. the first field is a valid `@yisen.uk` email;
2. the second field is non-empty and remains the supplied ChatGPT login
   password;
3. the third field is a structurally valid three-part JWT whose decoded
   `address` claim matches the first field.

The resulting record keeps `account_type=mailapi_url`, stores
`https://mail.yisen.uk/api/mails?login=<email>&limit=20&offset=0` as the
MailAPI URL, and stores the JWT in `mailapi_token`. Invalid or mismatched JWT
rows fail with a credential-safe error.

## Runtime polling

Propagate `mailapi_token` through mailbox claim, retry, password commit, and
saved mailbox context paths. `MailApiUrlOtpBackend` recognizes the Yisen host,
adds the Authorization and browser-compatible request headers, and never logs
the token.

Extend MailAPI parsing for `results` responses. Parse `metadata.subject`, decode
the text/plain or text/html part of the bounded `raw` MIME message, parse
`created_at`, and derive stable message IDs from `message_id` or the record
identifier. Only OpenAI/ChatGPT authentication subjects with a six-digit code
are eligible; newest eligible mail wins.

## Error handling and security

- HTTP 401/403 becomes a concise Yisen authentication error without including
  the URL, email, JWT, or response body.
- Empty or malformed `results` is treated as no current code, not a parser
  crash.
- Generic MailAPI behavior remains unchanged for other providers.
- Detection and snapshot responses expose account type and availability only.

## Existing baseline repair

The current `main` contains a failing regression test added by commit
`32e113f`: SMS pool mode is expected to enable existing-account phone
verification, but the implementation only reads the older explicit boolean.
Restore the intended behavior by treating an SMS pool request as enabling phone
verification. This is independent of Yisen and is required to return `main` to
a green baseline before release.

## Verification and release

1. Prove new tests fail before production-code changes.
2. Pass focused import, schema, MailAPI parsing/request, retry-persistence, and
   existing-account SMS tests.
3. Pass the complete Python suite.
4. Push the feature branch and fast-forward `main` after verifying the remote
   has not moved.
5. Deploy with a new release directory, preserve shared data/static assets,
   retain the previous release as rollback, and verify systemd plus HTTP health.
6. Import the supplied batch through the production service and perform one
   read-only mailbox probe before starting any account task. Report counts and
   stages only; never print credentials.
