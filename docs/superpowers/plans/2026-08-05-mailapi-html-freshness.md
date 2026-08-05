# MailAPI HTML Freshness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent MailAPI HTML pages from returning an old ChatGPT email OTP while still accepting a newly delivered OTP with second-level timestamps.

**Architecture:** Extend `MailApiUrlOtpBackend` to normalize the newest `article.mail-card` into the existing `{content, received_at, message_id, status}` contract. Keep all existing polling and background-wait behavior, and allow a small timestamp precision tolerance when comparing a new message to `otp_sent_at`.

**Tech Stack:** Python 3, `unittest`, regular expressions, existing Outlook/MailAPI backend.

---

### Task 1: Parse the newest HTML mail card

**Files:**
- Modify: `core/base_mailbox.py:3970-4055`
- Test: `tests/test_outlook_mailbox_oauth.py:987-1107`

- [ ] **Step 1: Write the failing old-message test**

Add a test that serves two `article.mail-card` elements, records the first card as the baseline, then waits with `otp_sent_at` 30 seconds after its `span.date`. Assert that `wait_for_code` returns `None` and that the baseline contains a `mailapi_message:` ID instead of the OTP value.

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
python -m unittest tests.test_outlook_mailbox_oauth.OutlookMailboxOAuthTests.test_mailapi_html_rejects_old_latest_card -v
```

Expected: FAIL because the current parser reports no HTML `received_at` and uses `mailapi_code:<OTP>` as its baseline.

- [ ] **Step 3: Implement the minimal HTML normalizer**

Add a class method that:

```python
for article in re.finditer(r"<article\\b([^>]*)>(.*?)</article\\s*>", raw, re.I | re.S):
    # accept only an article whose class list contains mail-card
    # read the first span/time whose class list contains date
    # return the card HTML, parsed timestamp, and a SHA-256 message identity
```

Call it from `_parse_mailapi_message` only when JSON parsing does not return a dictionary. If there is no `mail-card`, retain the current raw-HTML fallback exactly.

- [ ] **Step 4: Run the focused test and verify GREEN**

Run the command from Step 2. Expected: PASS.

### Task 2: Accept a new card with second-level timestamp precision

**Files:**
- Modify: `core/base_mailbox.py:4550-4575`
- Test: `tests/test_outlook_mailbox_oauth.py`

- [ ] **Step 1: Write the failing same-second delivery test**

Return an old mail card for `get_current_ids`, then a new first card for `wait_for_code`. Give the new card a timestamp such as `19:25:44` and `otp_sent_at` of `19:25:44.900`. Assert that the new OTP is returned and that the baseline ID changes.

- [ ] **Step 2: Run the focused test and verify RED**

```bash
python -m unittest tests.test_outlook_mailbox_oauth.OutlookMailboxOAuthTests.test_mailapi_html_accepts_new_card_with_second_precision -v
```

Expected: FAIL because strict floating-point timestamp comparison considers the same-second mail 0.9 seconds too old.

- [ ] **Step 3: Add bounded timestamp tolerance**

Change the old-message comparison to reject only when:

```python
float(received_at) < otp_sent_at - 5.0
```

The five-second tolerance covers provider timestamp truncation and minor clock skew while still rejecting the stale codes observed minutes earlier.

- [ ] **Step 4: Run MailAPI and full backend tests**

```bash
python -m unittest tests.test_outlook_mailbox_oauth -v
python -m pytest -q
```

Expected: all tests PASS.

- [ ] **Step 5: Commit and deploy**

```bash
git add core/base_mailbox.py tests/test_outlook_mailbox_oauth.py
git commit -m "fix: reject stale MailAPI HTML OTP messages"
```

After confirming zero active tasks, deploy a new immutable release, restart `any-auto-register.service`, verify the service is active, and check `https://accounts.anhepro.com/` returns HTTP 200.
