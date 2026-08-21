# Durable ChatGPT Auth Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace unsafe MFA recovery, delete-on-claim mailboxes, and in-memory-only automatic relogin coordination with a restart-safe single-server pipeline using SQLite leases, explicit auth state, bounded email fallback, durable maintenance items, backoff, and idempotent Codex2API synchronization.

**Architecture:** SQLite remains the source of truth. Short CAS transactions own account, mailbox, MFA, and maintenance leases; external OAuth and mailbox calls run outside database transactions. Existing TaskRun and mailbox context shapes remain compatibility projections while new state tables control eligibility and writes.

**Tech Stack:** Python 3.11, FastAPI, SQLModel/SQLAlchemy, SQLite WAL, pytest, systemd release deployment.

---

### Task 1: Canonical auth state and operation-scoped MFA recovery

**Files:**
- Modify: `core/db.py`
- Create: `services/chatgpt_auth_state.py`
- Modify: `platforms/chatgpt/refresh_token_registration_engine.py`
- Test: `tests/test_chatgpt_auth_state.py`
- Test: `tests/test_chatgpt_mfa_manager.py`
- Test: `tests/test_chatgpt_existing_account_login.py`

- [ ] **Step 1: Write failing auth state and MFA operation tests**

Cover these public behaviors:

```python
def test_staged_operation_never_replaces_confirmed_totp():
    state = ensure_chatgpt_auth_state(account_id=7, primary_confirmed=True)
    active = stage_mfa_operation(
        account_id=7,
        email="demo@example.com",
        totp_secret="CONFIRMED",
        base_auth_version=state.auth_version,
    )
    assert transition_mfa_operation(
        active.operation_id,
        expected_state="staged",
        new_state="activated_remote",
        expected_generation=active.generation,
    )
    commit_auth_projection(
        account_id=7,
        expected_version=state.auth_version,
        active_operation_id=active.operation_id,
    )
    operation = stage_mfa_operation(
        account_id=7,
        email="demo@example.com",
        totp_secret="UNCONFIRMED",
        base_auth_version=state.auth_version + 1,
    )
    assert load_login_mfa_candidate(7).generation == active.generation
    assert operation.status == "staged"


def test_old_operation_callback_cannot_activate_new_operation():
    first = stage_mfa_operation(account_id=7, email="demo@example.com", totp_secret="A")
    second = stage_mfa_operation(account_id=7, email="demo@example.com", totp_secret="B")
    assert not transition_mfa_operation(
        first.operation_id,
        expected_state="staged",
        new_state="activated_remote",
        expected_generation=second.generation,
    )


def test_auth_commit_rejects_stale_version():
    current = ensure_chatgpt_auth_state(account_id=7)
    commit_auth_projection(account_id=7, expected_version=current.auth_version, password="pw")
    with pytest.raises(ChatGPTAuthVersionConflict):
        commit_auth_projection(account_id=7, expected_version=current.auth_version, password="old")
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
PYTHONPATH=. .venv/bin/python -m pytest -q tests/test_chatgpt_auth_state.py tests/test_chatgpt_mfa_manager.py tests/test_chatgpt_existing_account_login.py
```

Expected: collection or assertion failures because canonical auth state and operation-scoped functions do not exist and staged journal still overrides saved TOTP.

- [ ] **Step 3: Add additive SQLModel tables**

Add `ChatGPTAuthStateModel` and `ChatGPTMfaOperationModel` to `core/db.py` with these stable fields:

```python
class ChatGPTAuthStateModel(SQLModel, table=True):
    __tablename__ = "chatgpt_auth_states"
    id: int | None = Field(default=None, primary_key=True)
    account_id: int = Field(index=True, sa_column_kwargs={"unique": True})
    auth_version: int = 1
    primary_state: str = Field(default="absent", index=True)
    mfa_state: str = Field(default="absent", index=True)
    active_mfa_generation: str = ""
    email_recovery_state: str = "unverified"
    credential_revision: str = ""
    last_success_at: datetime | None = None
    failure_domain: str = ""
    error_code: str = ""
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow, index=True)


class ChatGPTMfaOperationModel(SQLModel, table=True):
    __tablename__ = "chatgpt_mfa_operations"
    operation_id: str = Field(primary_key=True)
    account_id: int = Field(default=0, index=True)
    email: str = Field(default="", index=True)
    generation: str = Field(index=True)
    base_auth_version: int = 0
    status: str = Field(default="staged", index=True)
    totp_secret: str
    recovery_code: str = ""
    recovery_code_state: str = "available"
    remote_activated_at: str = ""
    created_at: datetime = Field(default_factory=_utcnow, index=True)
    updated_at: datetime = Field(default_factory=_utcnow, index=True)
```

Tables are additive through `SQLModel.metadata.create_all`; existing journal remains readable only by migration tooling.

- [ ] **Step 4: Implement CAS auth and MFA operation service**

`services/chatgpt_auth_state.py` must expose typed enums/dataclasses and:

```python
ensure_chatgpt_auth_state(account_id, *, primary_confirmed=False, session=None)
stage_mfa_operation(account_id, email, totp_secret, *, base_auth_version, session=None)
transition_mfa_operation(operation_id, *, expected_state, new_state, expected_generation="", recovery_code=None, rotated_at="", session=None)
load_login_mfa_candidate(account_id, *, session=None)
commit_auth_projection(account_id, *, expected_version, password=None, mailbox_context=None, active_operation_id=None, tokens=None, session=None)
quarantine_legacy_staged_journals(*, session=None)
```

Use `UPDATE ... WHERE auth_version = expected_version` and require exactly one affected row. Only `activated_remote` operations may be committed as active. Credential revisions use HMAC-SHA256 over canonical field-presence/version data and never include a reusable secret hash in logs.

- [ ] **Step 5: Stop loading legacy staged secrets during login**

Replace the unconditional journal override in `_create_email()` with canonical candidate loading. If canonical state is absent, keep the saved account secret; legacy staged rows are reported as quarantined metadata and never copied into `email_info`.

- [ ] **Step 6: Run focused tests and verify GREEN**

Run the Task 1 command. Expected: all selected tests pass.

- [ ] **Step 7: Commit**

```bash
git add core/db.py services/chatgpt_auth_state.py platforms/chatgpt/refresh_token_registration_engine.py tests/test_chatgpt_auth_state.py tests/test_chatgpt_mfa_manager.py tests/test_chatgpt_existing_account_login.py
git commit -m "fix: make ChatGPT MFA recovery generation safe"
```

### Task 2: Typed authentication outcomes and self-healing email fallback

**Files:**
- Create: `platforms/chatgpt/auth_outcomes.py`
- Modify: `platforms/chatgpt/oauth_client.py`
- Modify: `platforms/chatgpt/refresh_token_registration_engine.py`
- Modify: `services/chatgpt_relogin.py`
- Test: `tests/test_chatgpt_register.py`
- Test: `tests/test_chatgpt_existing_account_login.py`
- Test: `tests/test_chatgpt_relogin.py`

- [ ] **Step 1: Write failing routing tests**

Tests must prove:

```python
def test_network_failure_does_not_fall_back_to_email(): ...
def test_explicit_totp_rejection_can_fall_back_to_email_once(): ...
def test_server_email_risk_challenge_uses_fresh_mailbox_code(): ...
def test_mfa_email_fallback_marks_generation_suspect_and_rotates(): ...
def test_repeated_email_fallback_inside_cooldown_is_deferred(): ...
def test_passwordless_managed_mfa_requires_primary_bootstrap(): ...
```

- [ ] **Step 2: Verify RED**

```bash
PYTHONPATH=. .venv/bin/python -m pytest -q tests/test_chatgpt_register.py tests/test_chatgpt_existing_account_login.py tests/test_chatgpt_relogin.py
```

Expected: new assertions fail because outcomes are string matched and fallback is not persisted as a repair signal.

- [ ] **Step 3: Add typed outcomes**

Define:

```python
class AuthFailureDomain(StrEnum):
    NETWORK = "network"
    RATE_LIMIT = "rate_limit"
    SESSION = "session"
    CREDENTIAL = "credential"
    MFA = "mfa"
    EMAIL = "email"
    REMOTE_ACCOUNT = "remote_account"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class AuthOutcome:
    ok: bool
    stage: str
    domain: AuthFailureDomain | None = None
    code: str = ""
    retryable: bool = False
    credential_rejected: bool = False
    email_fallback_used: bool = False
    email_risk_challenge: bool = False
```

OAuth helpers set `last_auth_outcome` while preserving sanitized `last_error` compatibility.

- [ ] **Step 4: Enforce fallback policy**

`_submit_mfa_challenge()` may move from TOTP to recovery/email only when `credential_rejected=True`. Network, timeout, 429, 5xx, session expiry, malformed response, and cancellation return without factor downgrade. Reserve recovery codes before use and mark ambiguous submissions `unknown`.

Track MFA email fallback separately from independent email risk challenge. Persist a bounded fallback timestamp in canonical auth state. A successful MFA email fallback sets the prior generation to `suspect` and triggers `_rotate_mfa_after_login()` in the same authenticated session; an independent risk challenge does not rotate a valid factor.

- [ ] **Step 5: Make email fallback self-healing**

After fallback, require operation-scoped staging, remote activation, and a fresh password+TOTP proof before `commit_auth_projection()`. Codex2API sync runs only after local auth commit, and sync failure does not undo the commit.

- [ ] **Step 6: Verify GREEN and commit**

Run the Task 2 command, then:

```bash
git add platforms/chatgpt/auth_outcomes.py platforms/chatgpt/oauth_client.py platforms/chatgpt/refresh_token_registration_engine.py services/chatgpt_relogin.py tests/test_chatgpt_register.py tests/test_chatgpt_existing_account_login.py tests/test_chatgpt_relogin.py
git commit -m "fix: make email fallback bounded and self healing"
```

### Task 3: Replace delete-on-claim Outlook/MailAPI handling with leases

**Files:**
- Modify: `core/db.py`
- Modify: `core/base_mailbox.py`
- Test: `tests/test_outlook_mailbox_oauth.py`
- Test: `tests/test_chatgpt_relogin.py`

- [ ] **Step 1: Write failing lease lifecycle tests**

```python
def test_claim_keeps_mailbox_row_and_marks_it_leased(): ...
def test_two_claimers_cannot_lease_same_mailbox(): ...
def test_successful_account_binding_is_not_reallocated(): ...
def test_expired_unbound_lease_is_recovered_on_startup(): ...
def test_uncertain_failure_quarantines_instead_of_releasing(): ...
def test_password_reset_updates_a_leased_or_bound_row(): ...
```

- [ ] **Step 2: Verify RED**

```bash
PYTHONPATH=. .venv/bin/python -m pytest -q tests/test_outlook_mailbox_oauth.py tests/test_chatgpt_relogin.py
```

Expected: assertions show the claimed row is deleted and restart cannot recover it.

- [ ] **Step 3: Add additive mailbox lease columns and migration**

Add to `OutlookAccountModel`:

```python
state: str = Field(default="available", index=True)
lease_owner: str = Field(default="", index=True)
lease_expires_at: datetime | None = Field(default=None, index=True)
lease_version: int = 0
bound_account_id: int = Field(default=0, index=True)
bound_at: datetime | None = None
```

Extend `_migrate_outlook_accounts_schema()` with SQLite `ALTER TABLE` statements and migrate `enabled=1` to `available`, `enabled=0` to `disabled` unless already bound.

- [ ] **Step 4: Implement atomic claim/bind/release/recovery**

`OutlookMailbox` must use an explicit lease owner supplied by task/attempt context. Claim uses a short `UPDATE ... WHERE state='available' AND lease_version=:old` CAS, then reloads the same row. Implement:

```python
claim_account(target_email="", lease_owner="", lease_seconds=900)
bind_account(account, account_id)
release_account(account, *, uncertain=False)
recover_expired_leases(now=None)
```

Keep `get_email()` and `get_email_by_address()` as compatibility wrappers. `requeue_account()` transitions only an owned leased/quarantined row; `bound` never returns to the general pool.

- [ ] **Step 5: Wire account save to mailbox binding**

After account persistence, bind the lease to the saved account ID. All exception paths classify remote state as unchanged or uncertain and release/quarantine accordingly. Service startup calls `recover_expired_leases()`.

- [ ] **Step 6: Verify GREEN and commit**

Run Task 3 tests, then:

```bash
git add core/db.py core/base_mailbox.py tests/test_outlook_mailbox_oauth.py tests/test_chatgpt_relogin.py
git commit -m "fix: lease ChatGPT mailboxes instead of deleting them"
```

### Task 4: Durable maintenance items, account leases, backoff, and circuit breaking

**Files:**
- Modify: `core/db.py`
- Create: `services/chatgpt_maintenance_state.py`
- Modify: `services/chatgpt_relogin.py`
- Modify: `services/chatgpt_codex2api_health.py`
- Test: `tests/test_chatgpt_maintenance_state.py`
- Test: `tests/test_chatgpt_relogin.py`
- Test: `tests/test_chatgpt_codex2api_health.py`

- [ ] **Step 1: Write failing durable scheduling tests**

Cover duplicate planning, lease fencing, lease expiry recovery, full-jitter range, deterministic quarantine, circuit open/half-open, manual superseding automatic work, and stale worker commit rejection.

- [ ] **Step 2: Verify RED**

```bash
PYTHONPATH=. .venv/bin/python -m pytest -q tests/test_chatgpt_maintenance_state.py tests/test_chatgpt_relogin.py tests/test_chatgpt_codex2api_health.py
```

- [ ] **Step 3: Add maintenance tables**

Add `ChatGPTMaintenanceJobModel`, `ChatGPTMaintenanceItemModel`, and `ChatGPTMaintenanceAccountStateModel` exactly as specified by the design. Enforce unique job `dedupe_key` and item `idempotency_key` with SQLAlchemy unique constraints.

- [ ] **Step 4: Implement state service**

Expose:

```python
plan_job(kind, source, priority, account_ids, due_at, dedupe_key)
claim_next_item(worker_id, *, now=None, lease_seconds=120)
heartbeat_item(item_id, worker_id, fencing_token, lease_seconds=120)
finish_item(item_id, worker_id, fencing_token, outcome)
recover_expired_items(now=None)
supersede_pending_automatic_items(account_id)
classify_failure(exc_or_outcome)
compute_full_jitter(failures, base_seconds, cap_seconds, rng=random.random)
```

Item completion and account backoff/circuit update occur in one transaction. Five consecutive same-domain failures open a six-hour circuit; half-open permits one lease.

- [ ] **Step 5: Add deadlines and cancellation to health probes**

`probe_codex2api_account_health()` and confirmation polling accept deadline/cancel callbacks. Timeouts and service-wide errors are typed and do not count as account credential failures.

- [ ] **Step 6: Verify GREEN and commit**

```bash
git add core/db.py services/chatgpt_maintenance_state.py services/chatgpt_relogin.py services/chatgpt_codex2api_health.py tests/test_chatgpt_maintenance_state.py tests/test_chatgpt_relogin.py tests/test_chatgpt_codex2api_health.py
git commit -m "feat: persist ChatGPT maintenance state and backoff"
```

### Task 5: Integrate durable execution, remove hard process exits, and add sync outbox

**Files:**
- Modify: `core/db.py`
- Create: `services/chatgpt_maintenance_worker.py`
- Create: `services/chatgpt_sync_outbox.py`
- Modify: `api/tasks.py`
- Modify: `main.py`
- Modify: `services/chatgpt_account_coordination.py`
- Modify: `core/chatgpt_task_gate.py`
- Test: `tests/test_chatgpt_relogin_task.py`
- Test: `tests/test_chatgpt_task_gate.py`
- Test: `tests/test_chatgpt_account_coordination.py`
- Test: `tests/test_task_snapshot_persistence.py`
- Test: `tests/test_chatgpt_sync_outbox.py`

- [ ] **Step 1: Write failing integration tests**

Tests must prove restart recovery, manual priority, safe automatic yielding, no `os._exit`, DB account lease across worker instances, TaskRun projection, durable TaskLog linkage, and idempotent outbox retry.

- [ ] **Step 2: Verify RED**

```bash
PYTHONPATH=. .venv/bin/python -m pytest -q tests/test_chatgpt_relogin_task.py tests/test_chatgpt_task_gate.py tests/test_chatgpt_account_coordination.py tests/test_task_snapshot_persistence.py tests/test_chatgpt_sync_outbox.py
```

- [ ] **Step 3: Add sync outbox**

Add a unique `(account_id, auth_version, destination)` outbox model. Enqueue in the same transaction as auth commit. Worker retries typed transient errors with backoff and treats an already-applied credential revision as success.

- [ ] **Step 4: Add the durable worker loop**

`ChatGPTMaintenanceWorker` polls/claims due items, heartbeats around long external waits, executes existing relogin/probe services, and records structured outcome. It is started and stopped through FastAPI lifespan in `main.py`; all state remains restart-safe so the loop can later move to a separate systemd process.

- [ ] **Step 5: Route manual and automatic work through the same planner**

Manual priority is 100; automatic priority is 10. Manual planning supersedes queued automatic work for the same account. Existing TaskRun IDs are attached to jobs/items and updated as projections. Replace daemon `TaskLog` writes with direct durable writes tied to job/item/attempt.

- [ ] **Step 6: Remove the hard-exit watchdog**

Delete every automatic-task `os._exit(75)` branch. Stop requests only prevent new claims and allow current external requests to reach checkpoints; expired leases are recovered after restart.

- [ ] **Step 7: Verify GREEN and commit**

```bash
git add core/db.py services/chatgpt_maintenance_worker.py services/chatgpt_sync_outbox.py api/tasks.py main.py services/chatgpt_account_coordination.py core/chatgpt_task_gate.py tests/test_chatgpt_relogin_task.py tests/test_chatgpt_task_gate.py tests/test_chatgpt_account_coordination.py tests/test_task_snapshot_persistence.py tests/test_chatgpt_sync_outbox.py
git commit -m "feat: run ChatGPT maintenance from durable jobs"
```

### Task 6: Legacy audit/migration, secret sanitization, and rollout controls

**Files:**
- Create: `services/chatgpt_auth_migration.py`
- Modify: `platforms/chatgpt/log_sanitizer.py`
- Modify: `services/chatgpt_auto_relogin.py`
- Modify: `api/tasks.py`
- Test: `tests/test_chatgpt_auth_migration.py`
- Test: `tests/test_chatgpt_log_sanitizer.py`
- Test: `tests/test_chatgpt_auto_relogin.py`
- Create: `docs/runbooks/chatgpt-auth-pipeline-rollout.md`

- [ ] **Step 1: Write failing migration and leakage tests**

Cover complete credentials, missing password, missing TOTP, journal same/different/orphan, saved-wins, staged-wins, both rejected, ambiguous remote response, no mailbox, duplicate identity, kill switch, revision conflict, and sentinel redaction for every secret type and signed URL.

- [ ] **Step 2: Verify RED**

```bash
PYTHONPATH=. .venv/bin/python -m pytest -q tests/test_chatgpt_auth_migration.py tests/test_chatgpt_log_sanitizer.py tests/test_chatgpt_auto_relogin.py
```

- [ ] **Step 3: Implement audit-first migration service**

Provide `audit`, `canary`, and `full` modes plus a kill switch. The ledger stores only account ID, operation ID, booleans, relation enums, safe error codes, lease, attempts, and timestamps. It must never persist email or reusable secret hashes.

Candidate verification order is saved active TOTP, then legacy staged only after explicit rejection, then email repair. Ambiguous responses defer without switching candidates. Orphan journal secrets are quarantined and cleared without remote calls.

- [ ] **Step 4: Expand recursive sanitization**

Redact password, access/refresh/id/session tokens, TOTP/MFA secrets, recovery codes, MailAPI tokens, authorization headers, signed URL query strings, and six-digit codes from exception traces. Tests scan TaskRun, TaskLog, migration ledger/report, retry binding, API responses, and observer messages.

- [ ] **Step 5: Add rollout runbook**

Document backup, schema-only deploy, audit count gate, canary order, 5/10/25/50/rest batches, concurrency 1 then 2, health checks, kill switch, forward recovery after remote activation, and why a full DB restore is forbidden after MFA mutation.

- [ ] **Step 6: Verify GREEN and commit**

```bash
git add services/chatgpt_auth_migration.py platforms/chatgpt/log_sanitizer.py services/chatgpt_auto_relogin.py api/tasks.py tests/test_chatgpt_auth_migration.py tests/test_chatgpt_log_sanitizer.py tests/test_chatgpt_auto_relogin.py docs/runbooks/chatgpt-auth-pipeline-rollout.md
git commit -m "feat: audit and migrate legacy ChatGPT auth state"
```

### Task 7: Full verification, repository synchronization, and production rollout

**Files:**
- Modify only files required by discovered verification defects.

- [ ] **Step 1: Verify migration on a copy of production SQLite**

Download or securely copy the online backup into a temporary local directory, run `init_db()`, audit mode, `PRAGMA integrity_check`, and assert the source backup remains unchanged.

- [ ] **Step 2: Run focused and full backend tests**

```bash
PYTHONPATH=. .venv/bin/python -m pytest -q tests/test_chatgpt_auth_state.py tests/test_chatgpt_auth_migration.py tests/test_chatgpt_maintenance_state.py tests/test_chatgpt_sync_outbox.py tests/test_outlook_mailbox_oauth.py tests/test_chatgpt_existing_account_login.py tests/test_chatgpt_relogin.py tests/test_chatgpt_relogin_task.py tests/test_chatgpt_auto_relogin.py tests/test_chatgpt_log_sanitizer.py
PYTHONPATH=. .venv/bin/python -m pytest -q
```

- [ ] **Step 3: Run frontend verification**

Use the package manager declared by `frontend/package.json` and run its build command. Expected: exit 0 with generated assets and no TypeScript/Vue compilation error.

- [ ] **Step 4: Run diff and secret checks**

```bash
git diff --check origin/main...HEAD
rg -n "TBD|TODO|implement later" docs/superpowers/specs/2026-08-21-durable-chatgpt-auth-pipeline-design.md docs/superpowers/plans/2026-08-21-durable-chatgpt-auth-pipeline.md docs/runbooks/chatgpt-auth-pipeline-rollout.md
```

Run the repository secret/sanitizer tests and inspect staged filenames before pushing.

- [ ] **Step 5: Request final code review and fix all critical/important findings**

Review the complete range from backup tag `backup/pre-auth-pipeline-20260821-175619` to HEAD for spec compliance, concurrency, data migration, secret leakage, and rollback safety.

- [ ] **Step 6: Push branch and fast-forward repository main**

Push the reviewed branch, update `origin/main` through a normal fast-forward merge, and record the deployed commit SHA. Do not force-push.

- [ ] **Step 7: Build an immutable production release**

Create a timestamped release from the reviewed commit, reuse the shared virtual environment and data paths exactly as the existing systemd unit expects, install any unchanged requirements, and stage the release without switching the `current` symlink.

- [ ] **Step 8: Schema-only production gate**

Stop automatic maintenance intake, take a second online SQLite backup, run additive `init_db()`, `PRAGMA integrity_check`, and audit mode with remote mutation disabled. Verify service config, journal quarantine counts, mailbox state counts, and absence of running leases.

- [ ] **Step 9: Atomically switch and verify**

Switch `current` symlink atomically, restart the service, verify systemd active state, HTTP health, database integrity, worker heartbeat, queue depth, logs without tracebacks/secrets, and two scheduler cycles. Run a bounded canary that does not mutate remote MFA unless audit identifies a required repair and the rollout state permits it.

- [ ] **Step 10: Complete staged migration and monitor**

Process safe credential states first and conflict repairs last, with concurrency 1 then at most 2. After every batch verify no stuck leases, no repeated email fallback, no task-level hard exits, and successful Codex2API outbox drainage. Preserve backups and previous release for forward recovery.
