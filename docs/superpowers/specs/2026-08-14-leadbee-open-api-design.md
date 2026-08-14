# LeadBee Open API Integration Design

## Goal

Replace the ChatGPT phone-verification dependency on LeadBee's browser-session
endpoints with LeadBee's signed Open API v1 while preserving the existing
single-account and batch-login behavior: acquire a number only after the OpenAI
OAuth flow reaches `add_phone`, poll for the SMS code, replace rejected numbers,
cancel unfinished work, and save the resulting Refresh Token.

## Current behavior

`LeadBeePhoneService` posts an exchange code to the browser application's
`/api/activate`, `/api/receive-sms`, `/api/replace-number`, and `/api/cancel`
routes. One HTTP session carries the cookies that identify the remote card.
Batch login therefore requires one exchange code per account or a reservation
from the local SMS-card pool.

The official API uses a different model:

- credentials are global `API Key` and `API Secret` values;
- each phone verification creates an order with a stable client order ID and
  idempotency key;
- the returned `order_id` identifies all later polling, replacement, and
  cancellation calls;
- the response status is the source of truth instead of a browser cookie or
  exchange-card payload.

## Chosen approach

Use the formal JSON v1 API at `https://api.leadbee.cn/api/open/v1`. Do not use
the lightweight Bearer compatibility endpoint because it does not document a
number-replacement operation, which is required when OpenAI rejects a number.

Keep the public phone-provider interface unchanged:

```text
acquire_phone()
wait_for_code()
request_replacement()
cancel_active()
```

The OAuth and phone-verification broker can therefore keep their current state
machine. A new Open API adapter owns signing, idempotency, order parsing,
polling delays, retries, and provider error classification.

The old exchange-card implementation remains available behind the legacy
configuration during rollout. When `leadbee_api_enabled` is true, all new
single-account and batch phone-verification tasks use Open API orders and do
not reserve or require SMS-pool cards.

## Configuration and secret handling

Add these configuration keys:

```text
leadbee_api_enabled
leadbee_api_key
leadbee_api_secret
leadbee_api_product_id
```

The production host is fixed to `https://api.leadbee.cn`; the UI does not
accept an arbitrary base URL, so a stored secret cannot be redirected to an
untrusted host. Tests may inject a base URL directly into the client.

The API key and secret are write-only:

- `GET /config` returns empty strings for both;
- blank updates preserve existing values;
- account records, task payloads, task logs, exception messages, and frontend
  state never contain either credential;
- connection testing accepts unsaved overrides but returns only sanitized
  product and balance metadata.

`leadbee_api_product_id` is selected from `GET /products` and validated before
order creation. The example product ID in the public documentation is not
treated as an immutable constant.

## Request signing

The client serializes JSON exactly once with compact UTF-8 encoding and sends
the same byte sequence used for the body hash. The canonical value is:

```text
HTTP_METHOD
REQUEST_PATH
CANONICAL_QUERY
SHA256_HEX(REQUEST_BODY)
X_TIMESTAMP
X_NONCE
IDEMPOTENCY_KEY
```

The query line is empty for v1. GET requests have an empty idempotency line.
Each attempt receives a new timestamp, nonce, and signature. Retries of a write
operation reuse the same request body, client order ID, and idempotency key.

Any 2xx response is accepted and then validated through the JSON envelope.
Non-2xx responses are parsed for `error.code`, `error.message`, `request_id`,
and `Retry-After` without exposing credentials or verification data.

## Order lifecycle

Each local phone flow gets a random, non-secret `client_order_id`. `POST
/orders` creates one quantity of the configured product. Its idempotency key is
stable for that client order ID.

The adapter maps official states as follows:

| LeadBee state | Local behavior |
| --- | --- |
| `PROCESSING` | Wait `next_poll_after_seconds`, then query the same order. |
| `WAITING_CODE` | Return the phone when present; continue querying for SMS. |
| `REPLACING` | Query the same order until a different phone is returned. |
| `CANCELING` | Query until the cancellation reaches a terminal state. |
| `COMPLETED` | Return a valid code if present and mark the order consumed. |
| `CANCELED` | Mark cleanup settled and stop. |
| `EXPIRED` | Stop and report the order as expired. |
| `UNKNOWN` / `MANUAL_REVIEW` | Quarantine the order; do not create a replacement order. |

Phone and SMS waits retain the existing bounded local deadlines. Normal polls
honor `next_poll_after_seconds`. HTTP 429 honors `Retry-After` and adds bounded
backoff. The existing five-flow semaphore stays in place for the first rollout,
which remains below the provider's global request and active-order limits.

The public documentation lists replacement and cancellation paths but omits
their request bodies. A production compatibility probe confirmed that LeadBee
rejects an empty byte string as `INVALID_JSON`; both no-argument writes must
therefore sign and send the exact JSON object body `{}`.

## Single-account flow

The phone-verification start request gains an explicit `leadbee_api` mode.
When selected, the backend verifies that the API is configured, generates the
client order ID, and starts the existing automatic broker. The frontend shows
"LeadBee API 自动接码" and no exchange-code input.

Legacy manual phone entry remains unchanged. Legacy exchange-card entry is
kept only while Open API is disabled.

## Batch login flow

When Open API is enabled, "登录并接码获取 RT" creates one client order ID per
account. Backend validation no longer requires card count to equal account
count and does not reserve from `sms_pool_items`. Retry bindings keep their
mailbox/account relationship but receive a fresh client order ID for a new
phone attempt.

The existing SMS pool and its database columns remain readable for rollback.
They are not deleted or migrated in this change.

Scheduled automatic re-login currently disables phone verification and does
not call LeadBee. It remains behaviorally unchanged. If automatic re-login later
enables `add_phone`, it will use the same provider factory and Open API adapter.

## Error handling

- Authentication, signature, permission, and IP allow-list errors fail fast
  with a sanitized operator message.
- `PRODUCT_NOT_FOUND` requires refreshing/choosing the product before another
  order is created.
- `RATE_LIMITED` and `REPLAY_PROTECTION_UNAVAILABLE` are retryable within the
  current deadline; write retries preserve their idempotency key.
- `IDEMPOTENCY_CONFLICT` queries the known order when possible and never
  creates a new client order ID inside the same attempt.
- `ORDER_NOT_CANCELABLE`, `UNKNOWN`, and `MANUAL_REVIEW` retain the remote order
  identity and report an unsettled provider state.
- Full API keys, secrets, signatures, phone numbers, and SMS codes are omitted
  from application logs.

## Testing

Backend tests cover:

- byte-exact HMAC canonicalization for GET and POST;
- a new nonce/signature with stable body and idempotency key on retry;
- acceptance of all 2xx statuses and rejection of malformed envelopes;
- product lookup and configured-product validation;
- create, processing, phone-ready, code-ready, replace, cancel, expired, and
  manual-review states;
- `next_poll_after_seconds`, 429 `Retry-After`, and bounded deadlines;
- redaction of key, secret, signature, phone, and code;
- single-account API start without an exchange code;
- batch API mode without SMS-pool reservation or card-count validation;
- legacy exchange-card behavior while Open API is disabled.

Frontend tests cover write-only credential fields, enable/product settings,
single-account API mode, and batch payloads that omit exchange codes.

Production verification begins with signed read-only `GET /products` and `GET
/balance` calls. A single controlled phone order then verifies create, polling,
replacement/cancellation body compatibility, and final settlement before the
feature is enabled for normal batches.
