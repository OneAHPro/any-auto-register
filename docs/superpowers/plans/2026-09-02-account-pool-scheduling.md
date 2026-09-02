# Account Pool Scheduling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不修改 Codex2API 的前提下，为 any-auto-register 增加多目标管理、稳定账号身份、连续额度账本、人工确认调度和可恢复跨实例迁移。

**Architecture:** any-auto-register 作为唯一控制面，使用 SQLite/SQLModel 持久化身份、目标、绑定、额度、计划和迁移 Saga。所有 Codex2API HTTP 请求经过带目标参数的客户端；计划器是纯计算模块，迁移 Worker 依据持久化步骤调用上游已有的导入、测试、enable、删除和恢复接口。旧的单目标配置通过兼容适配器映射到默认目标。

**Tech Stack:** Python 3.11+, FastAPI, SQLModel/SQLAlchemy, SQLite WAL, requests/curl_cffi, React 19, TypeScript, Vite, Vitest, pytest。

---

### Task 1: 建立结构化数据模型和幂等迁移

**Files:**
- Modify: `core/db.py`
- Test: `tests/test_account_pool_db.py`

- [ ] **Step 1: Write failing schema tests**

```python
def test_pool_tables_and_account_identity_column_are_created(tmp_path):
    engine = db._create_database_engine(f"sqlite:///{tmp_path / 'pool.db'}")
    db.init_account_pool_schema(engine)
    tables = set(inspect(engine).get_table_names())
    assert {
        "account_identities",
        "account_identity_aliases",
        "codex2api_targets",
        "account_target_bindings",
        "account_assignments",
        "account_quota_snapshots",
        "account_migrations",
        "scheduler_runs",
        "scheduler_actions",
    } <= tables
    columns = {row[1] for row in engine.connect().exec_driver_sql("PRAGMA table_info('accounts')")}
    assert "identity_id" in columns


def test_schema_migration_is_idempotent(tmp_path):
    engine = db._create_database_engine(f"sqlite:///{tmp_path / 'pool.db'}")
    db.init_account_pool_schema(engine)
    db.init_account_pool_schema(engine)
    with Session(engine) as session:
        assert session.exec(select(db.Codex2APITargetModel)).all() == []
```

- [ ] **Step 2: Run the focused tests and verify the expected missing-symbol failure**

Run: `pytest -q tests/test_account_pool_db.py`

Expected: FAIL because the new models and `init_account_pool_schema` do not exist.

- [ ] **Step 3: Add SQLModel models and additive SQLite migration**

Add these models to `core/db.py` with explicit indexes and defaults:

```python
class AccountIdentityModel(SQLModel, table=True):
    __tablename__ = "account_identities"
    id: str = Field(primary_key=True)
    platform: str = Field(index=True)
    canonical_email: str = Field(index=True)
    state: str = Field(default="active", index=True)
    current_account_id: int = Field(default=0, index=True)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow, index=True)


class AccountIdentityAliasModel(SQLModel, table=True):
    __tablename__ = "account_identity_aliases"
    id: Optional[int] = Field(default=None, primary_key=True)
    identity_id: str = Field(index=True)
    alias_type: str = Field(index=True)
    normalized_value: str = Field(index=True)
    source: str = ""
    first_seen_at: datetime = Field(default_factory=_utcnow)
    last_seen_at: datetime = Field(default_factory=_utcnow, index=True)


class Codex2APITargetModel(SQLModel, table=True):
    __tablename__ = "codex2api_targets"
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    target_type: str = Field(default="public", index=True)
    server_label: str = ""
    base_url: str
    admin_key_ref: str
    default_pool_id: str = "PUBLIC_POOL"
    enabled: bool = Field(default=True, index=True)
    health_status: str = Field(default="unknown", index=True)
    capability_json: str = "{}"
    last_health_at: Optional[datetime] = None
    last_sync_at: Optional[datetime] = None
    last_error: str = ""
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow, index=True)


class AccountTargetBindingModel(SQLModel, table=True):
    __tablename__ = "account_target_bindings"
    id: Optional[int] = Field(default=None, primary_key=True)
    identity_id: str = Field(index=True)
    local_account_id: int = Field(index=True)
    target_id: int = Field(index=True)
    remote_account_id: int = Field(default=0, index=True)
    remote_email: str = ""
    sync_status: str = Field(default="unknown", index=True)
    remote_status: str = ""
    enabled: bool = True
    credential_revision: str = ""
    last_sync_at: Optional[datetime] = None
    last_error: str = ""
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow, index=True)


class AccountAssignmentModel(SQLModel, table=True):
    __tablename__ = "account_assignments"
    id: Optional[int] = Field(default=None, primary_key=True)
    identity_id: str = Field(index=True)
    local_account_id: int = Field(index=True)
    pool_id: str = Field(index=True)
    target_id: int = Field(index=True)
    state: str = Field(default="active", index=True)
    lease_owner: str = ""
    lease_reason: str = ""
    lease_started_at: Optional[datetime] = None
    lease_expires_at: Optional[datetime] = Field(default=None, index=True)
    assignment_version: int = 1
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow, index=True)


class AccountQuotaSnapshotModel(SQLModel, table=True):
    __tablename__ = "account_quota_snapshots"
    id: Optional[int] = Field(default=None, primary_key=True)
    identity_id: str = Field(index=True)
    local_account_id: int = Field(index=True)
    target_id: Optional[int] = Field(default=None, index=True)
    window: str = Field(index=True)
    usage_percent: Optional[float] = None
    billed_usd: Optional[float] = None
    remaining_usd: Optional[float] = None
    reset_at: Optional[datetime] = None
    source: str = "codex2api"
    captured_at: datetime = Field(default_factory=_utcnow, index=True)
    is_fresh: bool = True
    raw_digest: str = ""
    continuity_state: str = "normal"


class AccountMigrationModel(SQLModel, table=True):
    __tablename__ = "account_migrations"
    id: str = Field(primary_key=True)
    identity_id: str = Field(index=True)
    local_account_id: int = Field(index=True)
    source_target_id: int = Field(index=True)
    destination_target_id: int = Field(index=True)
    source_remote_id: int = 0
    destination_remote_id: int = 0
    state: str = Field(default="planned", index=True)
    step: str = Field(default="planned", index=True)
    expected_assignment_version: int = 0
    expected_credential_revision: str = ""
    idempotency_key: str = Field(index=True)
    retry_count: int = 0
    error_json: str = "{}"
    plan_json: str = "{}"
    created_at: datetime = Field(default_factory=_utcnow, index=True)
    updated_at: datetime = Field(default_factory=_utcnow, index=True)
    completed_at: Optional[datetime] = None


class SchedulerRunModel(SQLModel, table=True):
    __tablename__ = "scheduler_runs"
    id: str = Field(primary_key=True)
    mode: str = Field(default="dry_run", index=True)
    status: str = Field(default="planned", index=True)
    trigger: str = Field(default="manual", index=True)
    plan_json: str = "{}"
    executed_json: str = "{}"
    error_json: str = "{}"
    created_at: datetime = Field(default_factory=_utcnow, index=True)
    completed_at: Optional[datetime] = None


class SchedulerActionModel(SQLModel, table=True):
    __tablename__ = "scheduler_actions"
    id: Optional[int] = Field(default=None, primary_key=True)
    run_id: str = Field(index=True)
    identity_id: str = Field(index=True)
    action: str = Field(index=True)
    source_target_id: int = 0
    destination_target_id: int = 0
    reason: str = ""
    status: str = Field(default="planned", index=True)
    detail_json: str = "{}"
    created_at: datetime = Field(default_factory=_utcnow, index=True)
    updated_at: datetime = Field(default_factory=_utcnow)
```

Add `identity_id` as a nullable-compatible `TEXT DEFAULT ''` column to `accounts`, create indexes, and make `init_db()` call `init_account_pool_schema(engine)` after `create_all`. The migration must use `PRAGMA table_info` and `ALTER TABLE` only when a column is absent.

- [ ] **Step 4: Run focused tests and the existing database tests**

Run: `pytest -q tests/test_account_pool_db.py tests/test_chatgpt_account_persistence.py tests/test_accounts_visibility.py`

Expected: all selected tests PASS.

- [ ] **Step 5: Commit**

```bash
git add core/db.py tests/test_account_pool_db.py
git commit -m "feat: add account pool scheduling schema"
```

### Task 2: Add target client and legacy single-target compatibility

**Files:**
- Create: `services/codex2api_target_client.py`
- Modify: `core/config_store.py`, `services/external_sync.py`, `services/chatgpt_codex2api_health.py`
- Test: `tests/test_codex2api_target_client.py`

- [ ] **Step 1: Write failing client contract tests**

```python
def test_client_lists_accounts_with_target_credentials(monkeypatch):
    calls = []
    monkeypatch.setattr(client_module, "_request", lambda **kwargs: calls.append(kwargs) or {"accounts": []})
    client = Codex2APITargetClient(TargetConfig(id=2, base_url="https://node-b", admin_key="KEY"))
    assert client.list_accounts() == []
    assert calls[0]["url"] == "https://node-b/api/admin/accounts?channel=codex"
    assert calls[0]["headers"]["X-Admin-Key"] == "KEY"


def test_legacy_config_is_materialized_as_default_target(tmp_path, monkeypatch):
    store = FakeConfigStore({"codex2api_api_url": "https://legacy", "codex2api_admin_key": "secret"})
    target = targets_from_config(store)[0]
    assert target.name == "default"
    assert target.base_url == "https://legacy"


def test_sensitive_client_errors_are_redacted():
    with pytest.raises(Codex2APITargetError) as exc_info:
        raise_target_error("https://node", "admin-secret", "admin-secret leaked")
    assert "admin-secret" not in str(exc_info.value)
```

- [ ] **Step 2: Run tests and verify they fail because the client does not exist**

Run: `pytest -q tests/test_codex2api_target_client.py`

Expected: FAIL with import or symbol errors.

- [ ] **Step 3: Implement the target client**

Implement `TargetConfig`, `Codex2APITargetError`, `Codex2APITargetClient` and these methods:

```python
class Codex2APITargetClient:
    def health(self) -> dict[str, object]:
        raise NotImplementedError
    def capabilities(self) -> dict[str, object]:
        raise NotImplementedError
    def list_accounts(self) -> list[dict[str, object]]:
        raise NotImplementedError
    def trigger_usage_probe(self) -> dict[str, object]:
        raise NotImplementedError
    def runtime_status(self) -> dict[str, object]:
        raise NotImplementedError
    def import_refresh_token(self, payload: dict[str, object]) -> dict[str, object]:
        raise NotImplementedError
    def import_access_token(self, payload: dict[str, object]) -> dict[str, object]:
        raise NotImplementedError
    def import_full_json(self, payload: dict[str, object]) -> dict[str, object]:
        raise NotImplementedError
    def test_account(self, remote_id: int) -> dict[str, object]:
        raise NotImplementedError
    def set_enabled(self, remote_id: int, enabled: bool) -> dict[str, object]:
        raise NotImplementedError
    def set_locked(self, remote_id: int, locked: bool) -> dict[str, object]:
        raise NotImplementedError
    def refresh_account(self, remote_id: int) -> dict[str, object]:
        raise NotImplementedError
    def delete_account(self, remote_id: int) -> dict[str, object]:
        raise NotImplementedError
    def restore_account(self, remote_id: int) -> dict[str, object]:
        raise NotImplementedError
    def update_scheduler(self, remote_id: int, payload: dict[str, object]) -> dict[str, object]:
        raise NotImplementedError
```

Use the same curl-cffi/browser headers and timeout policy as the existing upload/health modules. Parse JSON and final SSE `complete` events. Normalize errors to status code, endpoint, and bounded message; redact the target key and all supplied credential fields. `test_account` treats a remote usage-limit response as authenticated, matching existing behavior.

Add `load_target_configs()` and `ensure_default_target()` helpers. If no structured target exists, read legacy `codex2api_api_url`/`codex2api_admin_key`; structured target values take precedence. Existing `sync_codex2api_account(account)` and `fetch_codex2api_quota_accounts()` keep their signatures and delegate to the default target.

- [ ] **Step 4: Run focused and legacy integration tests**

Run: `pytest -q tests/test_codex2api_target_client.py tests/test_chatgpt_codex2api_health.py tests/test_external_sync_contribution_mode.py tests/test_codex2api_upload.py`

Expected: all selected tests PASS with no credential values in captured errors.

- [ ] **Step 5: Commit**

```bash
git add services/codex2api_target_client.py services/external_sync.py services/chatgpt_codex2api_health.py core/config_store.py tests/test_codex2api_target_client.py
git commit -m "feat: add multi-target Codex2API client"
```

### Task 3: Implement stable identity and continuous quota ledger

**Files:**
- Create: `services/account_identity.py`, `services/quota_ledger.py`
- Modify: `core/db.py`, `api/accounts.py`
- Test: `tests/test_account_identity.py`, `tests/test_quota_ledger.py`

- [ ] **Step 1: Write failing identity and continuity tests**

```python
def test_identity_prefers_workspace_alias_over_credential_fingerprint(test_engine):
    first = identity_service.ensure_identity(test_engine, account_id=7, platform="chatgpt", email="A@EXAMPLE.COM", workspace_id="ws-1", chatgpt_account_id="acct-1", credential_fingerprint="fp-1")
    second = identity_service.ensure_identity(test_engine, account_id=8, platform="chatgpt", email="a@example.com", workspace_id="ws-1", chatgpt_account_id="acct-2", credential_fingerprint="fp-2")
    assert first.identity_id == second.identity_id


def test_ambiguous_alias_does_not_merge_identities(test_engine):
    one = identity_service.ensure_identity(test_engine, account_id=1, platform="chatgpt", email="a@example.com", workspace_id="ws-a")
    two = identity_service.ensure_identity(test_engine, account_id=2, platform="chatgpt", email="a@example.com", workspace_id="ws-b")
    assert one.identity_id != two.identity_id
    assert identity_service.get_identity(test_engine, one.identity_id).state == "ambiguous"


def test_quota_does_not_drop_when_destination_counter_resets(test_engine):
    quota_ledger.record_snapshot(test_engine, identity_id="id-1", local_account_id=1, target_id=1, window="7d", billed_usd=1200, usage_percent=40, reset_at=RESET)
    result = quota_ledger.record_snapshot(test_engine, identity_id="id-1", local_account_id=1, target_id=2, window="7d", billed_usd=40, usage_percent=2, reset_at=RESET)
    assert result.continuous_billed_usd >= 1240
    assert result.continuity_state == "node_counter_reset"


def test_quota_uncertainty_blocks_scheduler_eligibility(test_engine):
    result = quota_ledger.record_snapshot(
        test_engine,
        identity_id="id-1",
        local_account_id=1,
        target_id=2,
        window="7d",
        billed_usd=40,
        usage_percent=2,
        reset_at=None,
    )
    assert result.fresh is False
    assert result.scheduler_eligible is False
```

- [ ] **Step 2: Run tests and verify the expected missing-module failure**

Run: `pytest -q tests/test_account_identity.py tests/test_quota_ledger.py`

Expected: FAIL because identity and ledger modules do not exist.

- [ ] **Step 3: Implement identity service**

Implement normalized email/alias helpers, deterministic credential fingerprinting (HMAC of presence/metadata, never the raw token), and `ensure_identity(engine, account_id, platform, email, workspace_id, chatgpt_account_id, credential_fingerprint)`. Store a UUID string in `AccountIdentityModel`, upsert aliases, update `accounts.identity_id` and `current_account_id`, and return an `IdentityResolution` containing `identity_id`, `created`, and `ambiguous`. Existing ChatGPT auth `credential_revision` is reused when present; it is never logged.

Add `reconcile_existing_accounts(engine)` for startup: every existing ChatGPT row gets an identity and aliases without changing credentials. Call it from `init_db()` after schema migration.

- [ ] **Step 4: Implement quota ledger**

Implement `record_snapshot`, `latest_snapshot`, `history`, `merge_remote_rows`, and `scheduler_eligibility`. Use decimal arithmetic internally, digest the normalized remote row, and deduplicate identical `(identity, target, window, reset_at, digest)` observations within 5 minutes. Keep raw target snapshots and expose a continuous view that never decreases solely because the target changed. Mark ambiguous reset/counter comparisons `stale` and ineligible. Support 5h/7d directly and monthly only when the source supplies a verifiable value.

- [ ] **Step 5: Extend account responses without exposing secrets**

Add a small enrichment helper in `api/accounts.py` that joins current assignment, latest continuous 7d quota, and binding status. Return empty/`unknown` fields for accounts not yet reconciled. Do not include target keys, credential JSON, or raw remote responses.

- [ ] **Step 6: Run focused tests and existing account suites**

Run: `pytest -q tests/test_account_identity.py tests/test_quota_ledger.py tests/test_accounts_api_sanitization.py tests/test_accounts_visibility.py tests/test_chatgpt_account_persistence.py`

Expected: all selected tests PASS.

- [ ] **Step 7: Commit**

```bash
git add services/account_identity.py services/quota_ledger.py core/db.py api/accounts.py tests/test_account_identity.py tests/test_quota_ledger.py
git commit -m "feat: add stable account identity and quota ledger"
```

### Task 4: Build the migration Saga and remote reconciliation

**Files:**
- Create: `services/account_migration.py`
- Modify: `services/chatgpt_account_coordination.py`, `services/external_sync.py`
- Test: `tests/test_account_migration.py`

- [ ] **Step 1: Write failing state-machine tests**

```python
def test_migration_disables_source_drains_uploads_verifies_and_enables_destination(db_engine, fake_targets):
    migration_id = migration_service.plan_migration(db_engine, identity_id="id-1", local_account_id=1, source_target_id=1, destination_target_id=2, expected_assignment_version=3, expected_credential_revision="rev-1")
    result = migration_service.run_migration(db_engine, migration_id, clients=fake_targets, now=NOW, drain_timeout_seconds=2, sleep_fn=lambda _: None)
    assert result.state == "committed"
    assert fake_targets[1].calls[:3] == [("lock", 55, True), ("enable", 55, False), ("list",)]
    assert ("import",) in fake_targets[2].calls
    assert ("enable", 77, True) in fake_targets[2].calls


def test_upload_failure_restores_source_and_removes_destination(db_engine, fake_targets):
    fake_targets[2].fail_on = "import"
    migration_id = migration_service.plan_migration(db_engine, identity_id="id-1", local_account_id=1, source_target_id=1, destination_target_id=2, expected_assignment_version=3, expected_credential_revision="rev-1")
    result = migration_service.run_migration(db_engine, migration_id, clients=fake_targets, now=NOW, drain_timeout_seconds=2, sleep_fn=lambda seconds: None)
    assert result.state == "rolled_back"
    assert ("enable", 55, True) in fake_targets[1].calls
    assert ("delete", 77) in fake_targets[2].calls


def test_drain_timeout_never_deletes_source(db_engine, fake_targets):
    fake_targets[1].active_requests = 2
    migration_id = migration_service.plan_migration(db_engine, identity_id="id-1", local_account_id=1, source_target_id=1, destination_target_id=2, expected_assignment_version=3, expected_credential_revision="rev-1")
    result = migration_service.run_migration(db_engine, migration_id, clients=fake_targets, now=NOW, drain_timeout_seconds=1, sleep_fn=lambda seconds: None)
    assert result.state == "rolled_back"
    assert ("delete", 55) not in fake_targets[1].calls


def test_assignment_version_conflict_stops_before_remote_cleanup(db_engine, fake_targets):
    migration_id = migration_service.plan_migration(db_engine, identity_id="id-1", local_account_id=1, source_target_id=1, destination_target_id=2, expected_assignment_version=99, expected_credential_revision="rev-1")
    result = migration_service.run_migration(db_engine, migration_id, clients=fake_targets, now=NOW, drain_timeout_seconds=2, sleep_fn=lambda seconds: None)
    assert result.state == "rollback_required"
    assert not any(call[0] == "delete" for call in fake_targets[1].calls)


def test_restart_reconciliation_is_idempotent(db_engine, fake_targets):
    migration_id = migration_service.plan_migration(db_engine, identity_id="id-1", local_account_id=1, source_target_id=1, destination_target_id=2, expected_assignment_version=3, expected_credential_revision="rev-1")
    migration_service.run_migration(db_engine, migration_id, clients=fake_targets, now=NOW, stop_after="verifying")
    resumed = migration_service.resume_pending_migrations(db_engine, clients=fake_targets)
    assert resumed[0].state == "committed"
```

- [ ] **Step 2: Run tests and verify they fail before implementation**

Run: `pytest -q tests/test_account_migration.py`

Expected: FAIL because the migration service and state transitions do not exist.

- [ ] **Step 3: Implement durable planning and CAS helpers**

Implement `plan_migration`, `get_migration`, `run_migration`, `resume_pending_migrations`, and `rollback_migration`. Every transition writes `AccountMigrationModel.step/state` before network I/O; each completion uses a conditional update such as `UPDATE account_migrations SET step=:next_step, retry_count=:retry_count WHERE id=:id AND step=:expected_step AND expected_assignment_version=:version`. Use `chatgpt_account_operation_lock` plus a per-target migration lock. Reject stale assignment or credential revisions before any destructive call.

- [ ] **Step 4: Implement the remote sequence**

Use the exact order below:

```python
source.set_locked(source_id, True)
source.set_enabled(source_id, False)
wait_until_zero(source, source_id, timeout=drain_timeout)
destination.import_full_json(credentials)
destination.set_locked(destination_id, True)
destination.set_enabled(destination_id, False)
verify_identity_and_test(destination, destination_id)
verify_destination_quota(destination, destination_id)
commit_assignment_cas()
source.delete_account(source_id)
destination.set_enabled(destination_id, True)
verify_enabled(destination, destination_id)
```

Pre-commit failures restore source `enabled=true`/`locked=false` and delete or restore the destination copy. A source cleanup failure after local commit becomes `cleanup_pending`; it does not silently retry an unknown delete. A destination enable failure restores the local assignment and source. Remote responses are reduced to safe status/detail fields.

- [ ] **Step 5: Add startup recovery hook**

Call `resume_pending_migrations` from a background startup task after database initialization; cap one active migration per target and do not block FastAPI startup. Add an explicit API/service method for manual rollback.

- [ ] **Step 6: Run migration and regression suites**

Run: `pytest -q tests/test_account_migration.py tests/test_chatgpt_account_coordination.py tests/test_chatgpt_relogin.py -x`

Expected: all selected tests PASS.

- [ ] **Step 7: Commit**

```bash
git add services/account_migration.py services/chatgpt_account_coordination.py services/external_sync.py tests/test_account_migration.py
git commit -m "feat: add recoverable cross-target account migration"
```

### Task 5: Add pure capacity planner and scheduler worker

**Files:**
- Create: `services/pool_scheduler.py`
- Modify: `core/scheduler.py`, `core/config_store.py`
- Test: `tests/test_pool_scheduler.py`, `tests/test_scheduler.py`

- [ ] **Step 1: Write failing planner tests**

```python
def test_desired_count_uses_quota_and_concurrency_max():
    plan = planner.plan_pool(PoolInput(forecast_7d_usd=5000, safe_7d_quota=1800, peak_concurrency=12, safe_concurrency_per_account=3, pool_min_accounts=1, current_accounts=1, utilization=0.9))
    assert plan.desired_count == 3
    assert plan.scale_up_count == 2


def test_scale_down_requires_two_low_utilization_cycles_and_min_lease():
    plan = planner.plan_pool(PoolInput(current_accounts=5, desired_count=3, utilization=0.5, low_utilization_cycles=1, min_lease_elapsed=False))
    assert plan.scale_down_count == 0
    plan = planner.plan_pool(PoolInput(current_accounts=5, desired_count=3, utilization=0.5, low_utilization_cycles=2, min_lease_elapsed=True))
    assert plan.scale_down_count == 2


def test_stale_quota_or_unhealthy_target_produces_observe_only_plan():
    plan = planner.plan_pool(PoolInput(quota_fresh=False, target_healthy=False, current_accounts=2, desired_count=4))
    assert plan.executable is False
    assert plan.actions == ()


def test_manual_confirmation_required_for_both_directions():
    plan = planner.plan_pool(PoolInput(current_accounts=1, desired_count=3, confirmation_required=True))
    assert plan.requires_confirmation is True
```

- [ ] **Step 2: Run tests and verify the planner symbols are missing**

Run: `pytest -q tests/test_pool_scheduler.py`

Expected: FAIL with import/symbol errors.

- [ ] **Step 3: Implement planner types and formulas**

Implement immutable `PoolInput`, `PoolAction`, `PoolPlan`, `safe_quota`, `desired_count`, `estimate_costs`, and `plan_pool`. Use Decimal for money, P25 only when at least 20 observations exist, otherwise 1800 USD. Do not emit actions for stale/unknown quota, unhealthy targets, ambiguous identity, active requests, or leases shorter than six hours. Sort candidate accounts by health, remaining quota, already-on-target, reset proximity, stability, then float-pool priority.

- [ ] **Step 4: Add persistent run/action creation and apply gate**

Implement `create_dry_run`, `load_plan`, `confirm_plan`, and `apply_confirmed_plan`. Store the input snapshot and action list before returning. `apply_confirmed_plan` revalidates health/freshness/assignment/credential versions and requires `mode="apply"` plus explicit confirmation; it calls `account_migration.run_migration` serially per target.

- [ ] **Step 5: Add configuration defaults and scheduler triggers**

Add these keys to the config API allowlist/default validation:

```text
codex2api_scheduler_enabled=0
codex2api_scheduler_interval_minutes=15
codex2api_scheduler_dry_run=1
codex2api_scheduler_min_lease_hours=6
codex2api_scheduler_scale_up_threshold_usd=0
codex2api_scheduler_scale_down_utilization_percent=60
codex2api_scheduler_quota_freshness_minutes=15
account_monthly_rent_cny=1080
customer_price_per_usd=0.20
bandwidth_price_per_mbps_cny=30
```

Extend `Scheduler.run_once` with lightweight due checks for target health, quota collection, and dry-run plan generation. Work happens in a daemon executor with one active job per target; existing auto-relogin remains unchanged and still uses concurrency 3.

- [ ] **Step 6: Run focused scheduler tests and current scheduler suite**

Run: `pytest -q tests/test_pool_scheduler.py tests/test_scheduler.py tests/test_chatgpt_auto_relogin.py -x`

Expected: all selected tests PASS.

- [ ] **Step 7: Commit**

```bash
git add services/pool_scheduler.py core/scheduler.py core/config_store.py tests/test_pool_scheduler.py tests/test_scheduler.py
git commit -m "feat: add manual-confirmation pool planner"
```

### Task 6: Expose control-plane APIs

**Files:**
- Create: `api/codex2api_control.py`
- Modify: `main.py`, `api/accounts.py`, `api/config.py`
- Test: `tests/test_codex2api_control_api.py`

- [ ] **Step 1: Write failing endpoint tests**

```python
def test_target_create_masks_admin_key(client):
    response = client.post("/api/codex2api/targets", json={"name": "node-b", "base_url": "https://b", "admin_key": "secret"})
    assert response.status_code == 200
    assert response.json()["target"]["admin_key"] == "********"


def test_plan_apply_requires_explicit_confirmation(client, monkeypatch):
    run_id = create_plan_fixture()
    response = client.post("/api/scheduler/apply", json={"run_id": run_id, "confirm": False})
    assert response.status_code == 409
    assert no_remote_write_occurred()


def test_assignment_endpoint_returns_operation_id(client):
    response = client.post("/api/accounts/1/assignment", json={"target_id": 2, "pool_id": "ENTERPRISE_A_POOL", "reason": "peak"})
    assert response.status_code == 202
    assert response.json()["operation_id"]


def test_rollback_endpoint_is_idempotent(client):
    first = client.post("/api/migrations/m-1/rollback")
    second = client.post("/api/migrations/m-1/rollback")
    assert first.status_code == second.status_code == 200
```

- [ ] **Step 2: Run tests and verify routes are absent**

Run: `pytest -q tests/test_codex2api_control_api.py`

Expected: FAIL with 404/import errors.

- [ ] **Step 3: Implement Pydantic request/response models and router**

Add routes for targets, target health, account quota/history/refresh, scheduler plan/runs/apply, assignment, migration listing, and rollback. Validate URLs, target IDs, pool IDs, confirmation flags, and bounded reason lengths. Return `202` for queued migration work, `409` for stale/unconfirmed plans, and `502` for target failures. Never serialize model fields containing admin keys or credential JSON.

- [ ] **Step 4: Register router and enrich legacy account/config responses**

Include `codex2api_control.router` under `/api`; add structured scheduler settings to `api/config.py`; keep old `codex2api_enabled`, URL, and key fields compatible. Add target/assignment/quota summaries to account list/detail responses.

- [ ] **Step 5: Run API tests and existing FastAPI-facing tests**

Run: `pytest -q tests/test_codex2api_control_api.py tests/test_accounts_api_sanitization.py tests/test_codex2api_frontend_contract.py tests/test_external_sync_contribution_mode.py`

Expected: all selected tests PASS.

- [ ] **Step 6: Commit**

```bash
git add api/codex2api_control.py main.py api/accounts.py api/config.py tests/test_codex2api_control_api.py
git commit -m "feat: expose account pool control APIs"
```

### Task 7: Add management UI for targets, plans, and account assignments

**Files:**
- Create: `frontend/src/pages/Codex2APITargets.tsx`, `frontend/src/pages/Codex2APIScheduler.tsx`
- Modify: `frontend/src/App.tsx`, `frontend/src/pages/Accounts.tsx`, `frontend/src/lib/utils.ts`
- Test: `frontend/src/pages/Codex2APITargets.test.tsx`, `frontend/src/pages/Codex2APIScheduler.test.tsx`, `frontend/src/pages/Accounts.test.tsx`

- [ ] **Step 1: Write failing UI tests**

```tsx
it('shows target health and masks the admin key', async () => {
  render(<Codex2APITargets />)
  expect(await screen.findByText('node-b')).toBeInTheDocument()
  expect(screen.getByText('********')).toBeInTheDocument()
})

it('requires confirmation before applying a plan', async () => {
  render(<Codex2APIScheduler />)
  await userEvent.click(await screen.findByRole('button', { name: '执行计划' }))
  expect(await screen.findByText('请确认后执行')).toBeInTheDocument()
})

it('renders assignment and continuous quota columns', async () => {
  render(<Accounts />)
  expect(await screen.findByText('当前目标')).toBeInTheDocument()
  expect(screen.getByText('7天剩余')).toBeInTheDocument()
})
```

- [ ] **Step 2: Run frontend tests and verify they fail**

Run: `cd frontend && npm test -- --run src/pages/Codex2APITargets.test.tsx src/pages/Codex2APIScheduler.test.tsx`

Expected: FAIL because the pages and columns do not exist.

- [ ] **Step 3: Implement target page and API helpers**

Use existing `apiFetch`/token handling. Add target list/create/edit/health actions, masked secret display, capability badges, and error notifications. The form never repopulates a secret with the masked value.

- [ ] **Step 4: Implement scheduler page**

Render current vs desired counts, action reasons, account emails (not credentials), lease expiry, cost deltas, freshness/health blockers, and a confirmation modal. A plan is refreshed before apply; stale plan responses disable the apply button.

- [ ] **Step 5: Add account list/detail fields and routes**

Add `/codex2api/targets` and `/codex2api/scheduler` routes/menu entries. Extend account rows with target, pool, continuous 7d remaining, reset time, lease state, and migration status. Add quota history and migration timeline to the detail modal.

- [ ] **Step 6: Run frontend tests, lint, and production build**

Run: `cd frontend && npm test -- --run && npm run lint && npm run build`

Expected: all tests pass, lint exits 0, and Vite build exits 0.

- [ ] **Step 7: Commit**

```bash
git add frontend/src
git commit -m "feat: add account pool scheduling console"
```

### Task 8: Contract fixtures, two-instance integration harness, and release verification

**Files:**
- Create: `tests/fixtures/codex2api_target.py`, `tests/test_codex2api_two_instance.py`, `scripts/test_codex2api_two_instances.sh`
- Modify: `docker-compose.yml` or add `docker-compose.codex2api-test.yml`, `docs/superpowers/specs/2026-09-02-account-pool-scheduling-design.md`

- [ ] **Step 1: Write failing integration tests**

```python
def test_two_target_fixture_preserves_identity_and_quota_after_migration(tmp_path):
    harness = TwoTargetHarness(tmp_path)
    harness.seed_source_account(email="user@example.test", billed_7d=1200)
    harness.collect_source_snapshot()
    migration = harness.plan_and_confirm_migration()
    assert migration.state == "committed"
    assert harness.control_plane.identity_count(email="user@example.test") == 1
    assert harness.control_plane.continuous_quota(email="user@example.test", window="7d") >= 1200


def test_failure_injection_restores_source(tmp_path):
    harness = TwoTargetHarness(tmp_path, destination_failure="verify")
    result = harness.plan_and_confirm_migration()
    assert result.state == "rolled_back"
    assert harness.source.enabled(email="user@example.test") is True
```

- [ ] **Step 2: Run integration tests and verify the harness is incomplete**

Run: `pytest -q tests/test_codex2api_two_instance.py`

Expected: FAIL because the fixture/harness is not implemented.

- [ ] **Step 3: Implement deterministic fake target and optional real-container harness**

The fake target must implement every `Codex2APITargetClient` method and support failure injection at import, drain, verify, delete, enable, and network layers. The shell harness starts two unmodified upstream Codex2API containers with isolated SQLite files and ports, waits for `/health`, runs the contract test, and removes only its temporary project resources.

- [ ] **Step 4: Run the full verification matrix**

Run:

```bash
pytest -q
cd frontend && npm test -- --run && npm run lint && npm run build
git diff --check
git status --short --branch
```

Expected: Python and frontend suites pass; build/lint/diff checks exit 0; only intended commits are present.

- [ ] **Step 5: Request code review and address all important findings**

Review the diff from the production baseline to `HEAD`, verify no Codex2API source files are included, and fix all critical/important findings before release.

- [ ] **Step 6: Commit integration assets**

```bash
git add tests/fixtures/codex2api_target.py tests/test_codex2api_two_instance.py scripts/test_codex2api_two_instances.sh docker-compose.codex2api-test.yml
git commit -m "test: add two-target Codex2API integration harness"
```

### Task 9: Production release, migration, and post-deploy checks

**Files/Systems:**
- Production host `103.144.241.126:55222`
- Service root `/www/any-auto-register`
- Nginx static root `/var/www/accounts.anhepro.com`

- [ ] **Step 1: Build and package an immutable release locally**

Record `git rev-parse HEAD`, run the full verification matrix, and create a tar archive excluding `.git`, local databases, caches, and secrets. Verify the archive contains the new Python modules, API router, frontend `static/` build, tests/docs, and no private key or `.env` file.

- [ ] **Step 2: Back up production state before any write**

Over SSH, stop no service yet; create a timestamped copy of `/www/any-auto-register/shared/data/account_manager.db` and its WAL/SHM state using SQLite’s online backup API, and record the current `current` symlink and service status. Keep the previous release untouched.

- [ ] **Step 3: Upload release to a new immutable directory**

Create `/www/any-auto-register/releases/<full-commit-sha>`, unpack as root, set ownership/permissions to match the existing release, preserve `shared/`, and build/copy the frontend static output. Do not overwrite `current` or any shared credential/config file.

- [ ] **Step 4: Run offline migration and smoke checks in the new release**

Use the production virtualenv and database backup to run `init_db`, inspect table/index creation, materialize the default Codex2API target from legacy config, and call only read-only target health/list endpoints. Confirm no credentials appear in output.

- [ ] **Step 5: Switch the symlink and restart service with rollback guard**

Atomically point `current` to the new release, restart `any-auto-register.service`, and wait up to 120 seconds for active status. If startup or health fails, restore the previous symlink and restart it before investigating.

- [ ] **Step 6: Verify production behavior**

Check systemd status, `127.0.0.1:18081/api/config`, `https://accounts.anhepro.com/`, target health, account list, quota read, and scheduler plan in dry-run. Confirm existing auto-relogin remains enabled and no remote write occurs until a plan is explicitly confirmed.

- [ ] **Step 7: Commit deployment record and report exact release/rollback data**

Record deployed commit, release path, database backup path, previous release path, HTTP/systemd results, and any follow-up needed to register target B. Do not include Admin Keys, tokens, cookies, or account credentials.
