"""数据库模型 - SQLite via SQLModel"""
from datetime import datetime, timezone
import os
from typing import Optional
from sqlalchemy import delete, event, func, update, UniqueConstraint
from sqlalchemy.exc import IntegrityError
from sqlmodel import Field, SQLModel, create_engine, Session, select
import json


def _utcnow():
    return datetime.now(timezone.utc)

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///account_manager.db")


def _configure_sqlite_connection(dbapi_connection, _connection_record) -> None:
    """Apply durable, concurrency-friendly settings to every SQLite connection."""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=FULL")
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


def _create_database_engine(database_url: str):
    if str(database_url or "").lower().startswith("sqlite:"):
        database_engine = create_engine(
            database_url,
            connect_args={"timeout": 30, "check_same_thread": False},
            hide_parameters=True,
        )
        event.listen(database_engine, "connect", _configure_sqlite_connection)
        return database_engine
    return create_engine(database_url, hide_parameters=True)


engine = _create_database_engine(DATABASE_URL)


class AccountModel(SQLModel, table=True):
    __tablename__ = "accounts"

    id: Optional[int] = Field(default=None, primary_key=True)
    platform: str = Field(index=True)
    email: str = Field(index=True)
    password: str
    user_id: str = ""
    region: str = ""
    token: str = ""
    status: str = "registered"
    trial_end_time: int = 0
    cashier_url: str = ""
    extra_json: str = "{}"   # JSON 存储平台自定义字段
    # Stable control-plane identity.  Empty keeps rows created by older
    # releases compatible until the startup reconciliation fills it.
    identity_id: str = Field(default="", index=True)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    def get_extra(self) -> dict:
        return json.loads(self.extra_json or "{}")

    def set_extra(self, d: dict):
        self.extra_json = json.dumps(d, ensure_ascii=False)


class TaskLog(SQLModel, table=True):
    __tablename__ = "task_logs"

    id: Optional[int] = Field(default=None, primary_key=True)
    platform: str
    email: str
    status: str        # success | failed | skipped | removed
    error: str = ""
    detail_json: str = "{}"
    created_at: datetime = Field(default_factory=_utcnow)


class TaskRunModel(SQLModel, table=True):
    __tablename__ = "task_runs"

    id: str = Field(primary_key=True)
    platform: str = Field(index=True)
    source: str = Field(default="manual", index=True)
    status: str = Field(default="pending", index=True)
    total: int = 0
    progress: str = "0/0"
    success: int = 0
    registered: int = 0
    skipped: int = 0
    error: str = ""
    meta_json: str = "{}"
    logs_json: str = "[]"
    errors_json: str = "[]"
    cashier_urls_json: str = "[]"
    control_json: str = "{}"
    created_at: datetime = Field(default_factory=_utcnow, index=True)
    updated_at: datetime = Field(default_factory=_utcnow, index=True)


class ChatGPTAttemptBindingModel(SQLModel, table=True):
    """Local retry record binding one mailbox to one LeadBee card."""

    __tablename__ = "chatgpt_attempt_bindings"

    id: Optional[int] = Field(default=None, primary_key=True)
    task_id: str = Field(index=True)
    attempt_index: int = Field(default=0, index=True)
    email: str = Field(default="", index=True)
    leadbee_code: str = ""
    account_id: int = Field(default=0, index=True)
    stage: str = Field(default="login", index=True)
    status: str = Field(default="pending", index=True)
    error: str = ""
    mailbox_context_json: str = "{}"
    parent_binding_id: int = Field(default=0, index=True)
    retry_count: int = 0
    created_at: datetime = Field(default_factory=_utcnow, index=True)
    updated_at: datetime = Field(default_factory=_utcnow, index=True)


class ChatGPTMfaRotationJournalModel(SQLModel, table=True):
    """Durable write-ahead record for a newly enrolled ChatGPT MFA secret."""

    __tablename__ = "chatgpt_mfa_rotation_journal"

    id: Optional[int] = Field(default=None, primary_key=True)
    email: str = Field(index=True, sa_column_kwargs={"unique": True})
    totp_secret: str
    recovery_code: str = ""
    status: str = Field(default="staged", index=True)
    rotated_at: str = ""
    created_at: datetime = Field(default_factory=_utcnow, index=True)
    updated_at: datetime = Field(default_factory=_utcnow, index=True)


class ChatGPTAuthStateModel(SQLModel, table=True):
    """Canonical, versioned authentication state for one ChatGPT account."""

    __tablename__ = "chatgpt_auth_states"

    id: Optional[int] = Field(default=None, primary_key=True)
    account_id: int = Field(index=True, sa_column_kwargs={"unique": True})
    auth_version: int = 1
    primary_state: str = Field(default="absent", index=True)
    mfa_state: str = Field(default="absent", index=True)
    active_mfa_generation: str = ""
    email_recovery_state: str = "unverified"
    credential_revision: str = ""
    last_success_at: Optional[datetime] = None
    failure_domain: str = ""
    error_code: str = ""
    failure_count: int = 0
    next_retry_at: Optional[datetime] = Field(default=None, index=True)
    circuit_state: str = Field(default="closed", index=True)
    last_failure_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow, index=True)


class ChatGPTMfaOperationModel(SQLModel, table=True):
    """One immutable-generation MFA enrollment or recovery operation."""

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
    remote_activated_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=_utcnow, index=True)
    updated_at: datetime = Field(default_factory=_utcnow, index=True)


class SmsPoolItemModel(SQLModel, table=True):
    """A LeadBee card and its receive endpoint managed by the local SMS pool."""

    __tablename__ = "sms_pool_items"

    id: Optional[int] = Field(default=None, primary_key=True)
    code: str = Field(index=True, sa_column_kwargs={"unique": True})
    base_url: str
    status: str = Field(default="unused", index=True)
    reserved_task_id: str = Field(default="", index=True)
    reserved_at: Optional[datetime] = None
    used_at: Optional[datetime] = None
    used_by_email: str = ""
    created_at: datetime = Field(default_factory=_utcnow, index=True)
    updated_at: datetime = Field(default_factory=_utcnow, index=True)


class OutlookAccountModel(SQLModel, table=True):
    __tablename__ = "outlook_accounts"

    id: Optional[int] = Field(default=None, primary_key=True)
    email: str = Field(index=True, sa_column_kwargs={"unique": True})
    password: str
    client_id: str = ""
    refresh_token: str = ""
    account_type: str = "microsoft_oauth"
    mailapi_url: str = ""
    mailapi_token: str = ""
    enabled: bool = True
    # Durable mailbox allocation state. ``enabled`` remains a compatibility
    # projection for older readers; state/lease fields are authoritative.
    state: str = Field(default="available", index=True)
    lease_owner: str = Field(default="", index=True)
    lease_expires_at: Optional[datetime] = Field(default=None, index=True)
    lease_version: int = 0
    bound_account_id: int = Field(default=0, index=True)
    bound_at: Optional[datetime] = None
    quarantine_reason: str = ""
    last_error: str = ""
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
    last_used: Optional[datetime] = None


class ProxyModel(SQLModel, table=True):
    __tablename__ = "proxies"

    id: Optional[int] = Field(default=None, primary_key=True)
    url: str = Field(unique=True)
    region: str = ""
    success_count: int = 0
    fail_count: int = 0
    is_active: bool = True
    last_checked: Optional[datetime] = None


class AccountIdentityModel(SQLModel, table=True):
    """Stable identity that survives credential refreshes and pool moves."""

    __tablename__ = "account_identities"

    id: str = Field(primary_key=True)
    platform: str = Field(index=True)
    canonical_email: str = Field(index=True)
    state: str = Field(default="active", index=True)
    current_account_id: int = Field(default=0, index=True)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow, index=True)


class AccountIdentityAliasModel(SQLModel, table=True):
    """Normalized identity aliases used for conservative deduplication."""

    __tablename__ = "account_identity_aliases"

    id: Optional[int] = Field(default=None, primary_key=True)
    identity_id: str = Field(index=True)
    platform: str = Field(default="chatgpt", index=True)
    alias_type: str = Field(index=True)
    normalized_value: str = Field(index=True)
    source: str = ""
    first_seen_at: datetime = Field(default_factory=_utcnow)
    last_seen_at: datetime = Field(default_factory=_utcnow, index=True)


class Codex2APITargetModel(SQLModel, table=True):
    """One externally managed Codex2API instance."""

    __tablename__ = "codex2api_targets"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True, sa_column_kwargs={"unique": True})
    target_type: str = Field(default="public", index=True)
    server_label: str = ""
    base_url: str
    admin_key_ref: str
    default_pool_id: str = "PUBLIC_POOL"
    enabled: bool = Field(default=True, index=True)
    health_status: str = Field(default="unknown", index=True)
    health_success_count: int = 0
    health_failure_count: int = 0
    capability_json: str = "{}"
    last_health_at: Optional[datetime] = None
    last_sync_at: Optional[datetime] = None
    last_error: str = ""
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow, index=True)


class CodexInventorySnapshotModel(SQLModel, table=True):
    """Credential-free durable snapshot of a remote Codex2API account."""
    __tablename__ = "codex_inventory_snapshots"
    __table_args__ = (UniqueConstraint("target_id", "remote_id", name="uq_codex_inventory_target_remote"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    target_id: int = Field(index=True)
    remote_id: int = Field(index=True)
    summary_json: str = "{}"
    fetched_at: datetime = Field(default_factory=_utcnow, index=True)
    source_updated_at: str = ""
    missing: bool = Field(default=False, index=True)
    error: str = ""
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow, index=True)


class AccountTargetBindingModel(SQLModel, table=True):
    """Mapping of one stable identity to one target's remote account."""

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
    """Current pool/target lease for a stable account identity."""

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


class CustomerModel(SQLModel, table=True):
    """Business customer whose demand drives an enterprise pool."""

    __tablename__ = "customers"

    id: str = Field(primary_key=True)
    name: str = Field(index=True, sa_column_kwargs={"unique": True})
    enabled: bool = Field(default=True, index=True)
    price_cny_micros_per_usd: int = 200000
    operations_cost_cents_monthly: int = 0
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow, index=True)


class AccountPoolModel(SQLModel, table=True):
    """Logical public/enterprise/float/standby account pool."""

    __tablename__ = "account_pools"

    id: str = Field(primary_key=True)
    name: str = Field(index=True, sa_column_kwargs={"unique": True})
    pool_type: str = Field(default="public", index=True)
    customer_id: str = Field(default="", index=True)
    min_accounts: int = 0
    max_accounts: int = 0
    safe_concurrency_per_account: int = 1
    min_lease_hours: int = 6
    enabled: bool = Field(default=True, index=True)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow, index=True)


class PoolTargetPolicyModel(SQLModel, table=True):
    """Allowed target and capacity preference for one logical pool."""

    __tablename__ = "pool_target_policies"

    id: Optional[int] = Field(default=None, primary_key=True)
    pool_id: str = Field(index=True)
    target_id: int = Field(index=True)
    priority: int = 100
    min_accounts: int = 0
    max_accounts: int = 0
    remote_api_key_ids_json: str = "[]"
    bandwidth_mbps: int = 0
    enabled: bool = Field(default=True, index=True)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow, index=True)


class AccountAssignmentEventModel(SQLModel, table=True):
    """Append-only audit event for assignment and lease changes."""

    __tablename__ = "account_assignment_events"

    id: Optional[int] = Field(default=None, primary_key=True)
    identity_id: str = Field(index=True)
    local_account_id: int = Field(index=True)
    event_type: str = Field(index=True)
    from_pool_id: str = ""
    to_pool_id: str = ""
    from_target_id: int = 0
    to_target_id: int = 0
    assignment_version: int = 0
    migration_id: str = Field(default="", index=True)
    reason: str = ""
    detail_json: str = "{}"
    created_at: datetime = Field(default_factory=_utcnow, index=True)


class AccountQuotaSnapshotModel(SQLModel, table=True):
    """Point-in-time quota evidence from one target."""

    __tablename__ = "account_quota_snapshots"

    id: Optional[int] = Field(default=None, primary_key=True)
    identity_id: str = Field(index=True)
    local_account_id: int = Field(index=True)
    target_id: Optional[int] = Field(default=None, index=True)
    window: str = Field(index=True)
    usage_percent: Optional[float] = None
    billed_usd: Optional[float] = None
    billed_cents: Optional[int] = None
    # Control-plane cumulative value; differs from the target-local counter
    # when a credential is imported into a fresh Codex2API instance.
    continuous_billed_usd: Optional[float] = None
    continuous_billed_cents: Optional[int] = None
    remaining_usd: Optional[float] = None
    remaining_cents: Optional[int] = None
    continuous_remaining_usd: Optional[float] = None
    continuous_remaining_cents: Optional[int] = None
    remaining_scope: str = "target_local"
    reset_at: Optional[datetime] = None
    source: str = "codex2api"
    source_updated_at: Optional[datetime] = None
    captured_at: datetime = Field(default_factory=_utcnow, index=True)
    freshness_seconds: int = 900
    is_fresh: bool = True
    raw_digest: str = ""
    continuity_state: str = "normal"


class AccountQuotaRollupModel(SQLModel, table=True):
    """Hourly/daily compact quota history retained beyond raw snapshots."""

    __tablename__ = "account_quota_rollups"

    id: Optional[int] = Field(default=None, primary_key=True)
    identity_id: str = Field(index=True)
    window: str = Field(index=True)
    bucket: str = Field(index=True)
    bucket_start: datetime = Field(index=True)
    bucket_end: datetime
    min_billed_cents: Optional[int] = None
    max_billed_cents: Optional[int] = None
    final_continuous_billed_cents: Optional[int] = None
    sample_count: int = 0
    created_at: datetime = Field(default_factory=_utcnow)


class CustomerUsageSampleModel(SQLModel, table=True):
    """Target/API-key demand sample used for forecast and margin planning."""

    __tablename__ = "customer_usage_samples"

    id: Optional[int] = Field(default=None, primary_key=True)
    customer_id: str = Field(index=True)
    pool_id: str = Field(index=True)
    target_id: int = Field(index=True)
    remote_api_key_id: int = Field(default=0, index=True)
    bucket_start: datetime = Field(index=True)
    bucket_end: datetime
    billed_cents: int = 0
    request_count: int = 0
    peak_concurrency: int = 0
    captured_at: datetime = Field(default_factory=_utcnow, index=True)


class AccountMigrationModel(SQLModel, table=True):
    """Durable Saga record for a cross-target account migration."""

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
    """One immutable capacity-planning run."""

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
    """An individual account action inside a scheduler run."""

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


def _merge_nonempty_mapping(existing: dict, incoming: dict) -> dict:
    """Merge nested credential projections without erasing saved values."""

    merged = dict(existing or {})
    for key, value in dict(incoming or {}).items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = _merge_nonempty_mapping(current, value)
            if value.get("password_reset_required") is False:
                merged[key].pop("new_password", None)
        elif value in (None, "") and key in merged:
            continue
        else:
            merged[key] = value
    return merged


def save_account_with_creation_state(account) -> tuple['AccountModel', bool]:
    """Save an account and atomically report whether this call inserted it."""

    def attach_stable_identity(saved: AccountModel) -> AccountModel:
        """Best-effort identity projection kept outside the credential write."""

        try:
            from services.account_identity import ensure_identity_for_model

            resolution = ensure_identity_for_model(engine, saved)
            saved.identity_id = resolution.identity_id
        except Exception as exc:
            # Account login must remain usable during a rolling upgrade.  The
            # startup reconciler retries this projection before migrations are
            # allowed to run.
            import logging

            logging.getLogger(__name__).warning(
                "stable account identity projection deferred (%s)",
                type(exc).__name__,
            )
        return saved

    with Session(engine) as session:
        existing = session.exec(
            select(AccountModel)
            .where(
                func.lower(AccountModel.platform)
                == str(account.platform or "").strip().lower()
            )
            .where(
                func.lower(AccountModel.email)
                == str(account.email or "").strip().lower()
            )
        ).first()
        if existing:
            incoming_extra = dict(account.extra or {})
            existing_web_login = bool(
                str(account.platform or "").strip().lower() == "chatgpt"
                and incoming_extra.get("chatgpt_token_source")
                == "existing_account_web_login"
            )
            if existing_web_login:
                merged_extra = _merge_nonempty_mapping(
                    existing.get_extra(),
                    incoming_extra,
                )
                if incoming_extra.get("phone_oauth_ready") is False:
                    merged_extra.pop("oauth_resume_context", None)
                incoming_extra = merged_extra
            if not existing_web_login or str(account.password or ""):
                existing.password = account.password
            existing.user_id = account.user_id or ""
            existing.region = account.region or ""
            existing.token = account.token or ""
            existing.status = account.status.value
            existing.extra_json = json.dumps(incoming_extra, ensure_ascii=False)
            existing.cashier_url = incoming_extra.get("cashier_url", "")
            existing.updated_at = _utcnow()
            session.add(existing)
            session.commit()
            session.refresh(existing)
            return attach_stable_identity(existing), False
        m = AccountModel(
            platform=account.platform,
            email=account.email,
            password=account.password,
            user_id=account.user_id or "",
            region=account.region or "",
            token=account.token or "",
            status=account.status.value,
            extra_json=json.dumps(account.extra or {}, ensure_ascii=False),
            cashier_url=(account.extra or {}).get("cashier_url", ""),
        )
        session.add(m)
        session.commit()
        session.refresh(m)
        return attach_stable_identity(m), True


def save_account(account) -> 'AccountModel':
    """从 base_platform.Account 存入数据库（同平台同邮箱则更新）"""
    saved, _created = save_account_with_creation_state(account)
    return saved


def _ensure_chatgpt_mfa_rotation_journal(database_engine=None) -> None:
    target_engine = database_engine or engine
    ChatGPTMfaRotationJournalModel.__table__.create(
        bind=target_engine,
        checkfirst=True,
    )


def stage_chatgpt_mfa_rotation(
    email: str,
    totp_secret: str,
    *,
    database_engine=None,
) -> None:
    normalized_email = str(email or "").strip().lower()
    normalized_secret = str(totp_secret or "").strip()
    if not normalized_email or not normalized_secret:
        raise ValueError("MFA 写前记录缺少邮箱或密钥")
    target_engine = database_engine or engine
    _ensure_chatgpt_mfa_rotation_journal(target_engine)
    with Session(target_engine) as session:
        now = _utcnow()
        row = session.exec(
            select(ChatGPTMfaRotationJournalModel).where(
                ChatGPTMfaRotationJournalModel.email == normalized_email
            )
        ).first()
        if row is None:
            row = ChatGPTMfaRotationJournalModel(
                email=normalized_email,
                totp_secret=normalized_secret,
                created_at=now,
                updated_at=now,
            )
        else:
            row.totp_secret = normalized_secret
            row.recovery_code = ""
            row.status = "staged"
            row.rotated_at = ""
            # The table is keyed by email, but every restage is a new WAL
            # generation.  Reset its creation fence so a legitimate rotation
            # after account recreation is not mistaken for a stale identity.
            row.created_at = now
            row.updated_at = now
        session.add(row)
        session.commit()


def mark_chatgpt_mfa_rotation_activated(
    email: str,
    *,
    rotated_at: str = "",
    database_engine=None,
) -> None:
    normalized_email = str(email or "").strip().lower()
    target_engine = database_engine or engine
    _ensure_chatgpt_mfa_rotation_journal(target_engine)
    with Session(target_engine) as session:
        row = session.exec(
            select(ChatGPTMfaRotationJournalModel).where(
                ChatGPTMfaRotationJournalModel.email == normalized_email
            )
        ).first()
        if row is None:
            raise RuntimeError("MFA 写前记录不存在")
        row.status = "activated"
        row.rotated_at = str(rotated_at or "").strip()
        row.updated_at = _utcnow()
        session.add(row)
        session.commit()


def update_chatgpt_mfa_rotation_recovery_code(
    email: str,
    recovery_code: str,
    *,
    database_engine=None,
) -> None:
    normalized_email = str(email or "").strip().lower()
    target_engine = database_engine or engine
    _ensure_chatgpt_mfa_rotation_journal(target_engine)
    with Session(target_engine) as session:
        row = session.exec(
            select(ChatGPTMfaRotationJournalModel).where(
                ChatGPTMfaRotationJournalModel.email == normalized_email
            )
        ).first()
        if row is None:
            raise RuntimeError("MFA 写前记录不存在")
        row.recovery_code = str(recovery_code or "").strip()
        row.updated_at = _utcnow()
        session.add(row)
        session.commit()


def load_chatgpt_mfa_rotation(
    email: str,
    *,
    database_engine=None,
) -> dict:
    normalized_email = str(email or "").strip().lower()
    if not normalized_email:
        return {}
    target_engine = database_engine or engine
    _ensure_chatgpt_mfa_rotation_journal(target_engine)
    with Session(target_engine) as session:
        row = session.exec(
            select(ChatGPTMfaRotationJournalModel).where(
                ChatGPTMfaRotationJournalModel.email == normalized_email
            )
        ).first()
        if row is None:
            return {}
        return {
            "email": row.email,
            "totp_secret": row.totp_secret,
            "recovery_code": row.recovery_code,
            "status": row.status,
            "rotated_at": row.rotated_at,
        }


def finalize_chatgpt_mfa_rotation(
    email: str,
    *,
    database_engine=None,
) -> None:
    normalized_email = str(email or "").strip().lower()
    if not normalized_email:
        return
    target_engine = database_engine or engine
    _ensure_chatgpt_mfa_rotation_journal(target_engine)
    with Session(target_engine) as session:
        row = session.exec(
            select(ChatGPTMfaRotationJournalModel).where(
                ChatGPTMfaRotationJournalModel.email == normalized_email
            )
        ).first()
        if row is not None:
            session.delete(row)
            session.commit()


def cleanup_chatgpt_account_dependents(
    session: Session,
    account_id: int,
    *,
    quarantine_reason: str = "account_deleted",
) -> None:
    """Remove identity-scoped auth rows after the owning account is deleted.

    ``accounts.id`` is an SQLite integer primary key and may be reused after a
    deletion.  Keeping canonical MFA rows or a bound mailbox attached to that
    number would therefore transfer the deleted account's identity to a future
    account.  Callers must invoke this only after their account delete CAS has
    succeeded, in the same transaction.
    """

    normalized_id = int(account_id)
    if normalized_id <= 0:
        raise ValueError("ChatGPT account id must be positive")
    now = _utcnow()
    session.exec(
        delete(ChatGPTMfaOperationModel).where(
            ChatGPTMfaOperationModel.account_id == normalized_id
        )
    )
    session.exec(
        delete(ChatGPTAuthStateModel).where(
            ChatGPTAuthStateModel.account_id == normalized_id
        )
    )
    session.exec(
        update(ChatGPTAttemptBindingModel)
        .where(ChatGPTAttemptBindingModel.account_id == normalized_id)
        .values(account_id=0, updated_at=now)
    )
    session.exec(
        update(OutlookAccountModel)
        .where(OutlookAccountModel.bound_account_id == normalized_id)
        .values(
            state="quarantined",
            enabled=False,
            lease_owner="",
            lease_expires_at=None,
            lease_version=OutlookAccountModel.lease_version + 1,
            bound_account_id=0,
            quarantine_reason=str(quarantine_reason or "account_deleted")[:120],
            last_error="local ChatGPT account identity was deleted",
            updated_at=now,
        )
    )
    session.exec(
        update(AccountIdentityModel)
        .where(AccountIdentityModel.current_account_id == normalized_id)
        .values(
            state="retired",
            current_account_id=0,
            updated_at=now,
        )
    )
    session.exec(
        update(AccountAssignmentModel)
        .where(AccountAssignmentModel.local_account_id == normalized_id)
        .where(AccountAssignmentModel.state.in_(["active", "draining", "standby"]))
        .values(state="revoked", updated_at=now)
    )
    session.exec(
        update(AccountTargetBindingModel)
        .where(AccountTargetBindingModel.local_account_id == normalized_id)
        .values(
            enabled=False,
            sync_status="retired",
            updated_at=now,
        )
    )
    session.exec(
        update(AccountMigrationModel)
        .where(AccountMigrationModel.local_account_id == normalized_id)
        .where(
            AccountMigrationModel.state.not_in(
                ["committed", "rolled_back", "rollback_required"]
            )
        )
        .values(
            state="rollback_required",
            error_json=json.dumps(
                {"message": "local account deleted during migration"},
                ensure_ascii=False,
            ),
            updated_at=now,
        )
    )


def delete_incomplete_chatgpt_account(
    account_id: int,
    *,
    expected_email: str,
    expected_created_at: datetime,
    expected_extra_json: str,
    database_engine=None,
) -> bool:
    """Delete one unchanged ChatGPT row only while it still has no RT."""
    from services.chatgpt_account_state import chatgpt_account_refresh_token

    target_engine = database_engine or engine
    with Session(target_engine) as session:
        account = session.get(AccountModel, int(account_id))
        if account is None:
            return False
        if str(account.platform or "").strip().lower() != "chatgpt":
            return False
        if str(account.email or "").strip().lower() != str(
            expected_email or ""
        ).strip().lower():
            return False
        if account.created_at != expected_created_at:
            return False
        if str(account.extra_json or "") != str(expected_extra_json or ""):
            return False
        if chatgpt_account_refresh_token(account):
            return False

        result = session.exec(
            delete(AccountModel)
            .where(AccountModel.id == int(account_id))
            .where(AccountModel.platform == account.platform)
            .where(func.lower(AccountModel.email) == account.email.lower())
            .where(AccountModel.created_at == expected_created_at)
            .where(AccountModel.extra_json == expected_extra_json)
        )
        deleted_count = int(getattr(result, "rowcount", 0) or 0)
        if deleted_count == 1:
            cleanup_chatgpt_account_dependents(session, int(account_id))
            session.commit()
            return True
        session.rollback()
        return False


def purge_incomplete_chatgpt_accounts(*, database_engine=None) -> int:
    """Remove historical ChatGPT rows that never obtained a refresh token."""
    from services.chatgpt_account_state import chatgpt_account_refresh_token

    target_engine = database_engine or engine
    with Session(target_engine) as session:
        accounts = session.exec(
            select(AccountModel).where(func.lower(AccountModel.platform) == "chatgpt")
        ).all()
        incomplete = [
            account
            for account in accounts
            if not chatgpt_account_refresh_token(account)
        ]
        for account in incomplete:
            account_id = int(account.id or 0)
            session.delete(account)
            if account_id > 0:
                cleanup_chatgpt_account_dependents(session, account_id)
        if incomplete:
            session.commit()
        return len(incomplete)


def _migrate_outlook_accounts_schema() -> None:
    if engine.url.get_backend_name() != "sqlite":
        return
    with engine.begin() as conn:
        rows = conn.exec_driver_sql("PRAGMA table_info('outlook_accounts')").fetchall()
        if not rows:
            return
        existing_columns = {str(row[1]) for row in rows}
        state_was_added = "state" not in existing_columns
        if "account_type" not in existing_columns:
            conn.exec_driver_sql(
                "ALTER TABLE outlook_accounts ADD COLUMN account_type TEXT DEFAULT 'microsoft_oauth'"
            )
        if "mailapi_url" not in existing_columns:
            conn.exec_driver_sql(
                "ALTER TABLE outlook_accounts ADD COLUMN mailapi_url TEXT DEFAULT ''"
            )
        if "mailapi_token" not in existing_columns:
            conn.exec_driver_sql(
                "ALTER TABLE outlook_accounts ADD COLUMN mailapi_token TEXT DEFAULT ''"
            )
        additive_columns = {
            "enabled": "BOOLEAN DEFAULT 1",
            "state": "TEXT DEFAULT 'available'",
            "lease_owner": "TEXT DEFAULT ''",
            "lease_expires_at": "DATETIME",
            "lease_version": "INTEGER DEFAULT 0",
            "bound_account_id": "INTEGER DEFAULT 0",
            "bound_at": "DATETIME",
            "quarantine_reason": "TEXT DEFAULT ''",
            "last_error": "TEXT DEFAULT ''",
        }
        for column, definition in additive_columns.items():
            if column not in existing_columns:
                conn.exec_driver_sql(
                    f"ALTER TABLE outlook_accounts ADD COLUMN {column} {definition}"
                )
        conn.exec_driver_sql(
            "UPDATE outlook_accounts SET account_type = 'microsoft_oauth' WHERE account_type IS NULL OR TRIM(account_type) = ''"
        )
        conn.exec_driver_sql(
            "UPDATE outlook_accounts SET mailapi_url = '' WHERE mailapi_url IS NULL"
        )
        conn.exec_driver_sql(
            "UPDATE outlook_accounts SET mailapi_token = '' WHERE mailapi_token IS NULL"
        )
        # Existing rows were either selectable or deliberately disabled. Do
        # not resurrect disabled records during a schema upgrade.
        if state_was_added:
            conn.exec_driver_sql(
                "UPDATE outlook_accounts SET state = CASE "
                "WHEN enabled = 1 THEN 'available' ELSE 'disabled' END"
            )
        else:
            conn.exec_driver_sql(
                "UPDATE outlook_accounts SET state = CASE "
                "WHEN enabled = 1 THEN 'available' ELSE 'disabled' END "
                "WHERE state IS NULL OR TRIM(state) = ''"
            )
        # A few import versions wrote ``enabled=False`` before the state
        # column existed (or while relying on its ORM default of ``available``).
        # Preserve that explicit disablement instead of resurrecting it when
        # the compatibility projection is synchronized below.
        conn.exec_driver_sql(
            "UPDATE outlook_accounts SET state = 'disabled' "
            "WHERE state = 'available' AND enabled = 0"
        )
        conn.exec_driver_sql(
            "UPDATE outlook_accounts SET lease_owner = '' WHERE lease_owner IS NULL"
        )
        conn.exec_driver_sql(
            "UPDATE outlook_accounts SET lease_version = 0 WHERE lease_version IS NULL"
        )
        conn.exec_driver_sql(
            "UPDATE outlook_accounts SET bound_account_id = 0 "
            "WHERE bound_account_id IS NULL"
        )
        conn.exec_driver_sql(
            "UPDATE outlook_accounts SET quarantine_reason = '' "
            "WHERE quarantine_reason IS NULL"
        )
        conn.exec_driver_sql(
            "UPDATE outlook_accounts SET last_error = '' WHERE last_error IS NULL"
        )
        conn.exec_driver_sql(
            "UPDATE outlook_accounts SET enabled = CASE WHEN state = 'available' THEN 1 ELSE 0 END"
        )
        for index_name, column in (
            ("ix_outlook_accounts_state", "state"),
            ("ix_outlook_accounts_lease_owner", "lease_owner"),
            ("ix_outlook_accounts_lease_expires_at", "lease_expires_at"),
            ("ix_outlook_accounts_bound_account_id", "bound_account_id"),
        ):
            conn.exec_driver_sql(
                f"CREATE INDEX IF NOT EXISTS {index_name} "
                f"ON outlook_accounts ({column})"
            )


def recover_expired_outlook_leases(
    *,
    now: datetime | None = None,
    database_engine=None,
) -> int:
    """Recover only expired, unbound Outlook mailbox leases."""
    target_engine = database_engine or engine
    cutoff = now or _utcnow()
    if cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=timezone.utc)
    from sqlalchemy import or_, update

    try:
        session_context = Session(target_engine)
    except Exception:
        return 0
    with session_context as session:
        try:
            rows = session.exec(
                select(OutlookAccountModel)
                .where(OutlookAccountModel.state == "leased")
                # A row that is already associated with a local ChatGPT
                # account is fenced permanently.  Only leases with no
                # binding may be reclaimed after a worker crash.
                .where(
                    or_(
                        OutlookAccountModel.bound_account_id == 0,
                        OutlookAccountModel.bound_account_id.is_(None),
                    )
                )
                .where(OutlookAccountModel.lease_expires_at.is_not(None))
            ).all()
        except Exception as exc:
            # ``init_db`` is also exercised by migration/startup callers that
            # replace ``create_all``; a missing legacy table simply has no
            # leases to recover and must not block service startup.
            if "no such table" in str(exc).lower():
                session.rollback()
                return 0
            raise
        recovered = 0
        for row in rows:
            expires = row.lease_expires_at
            if expires is None:
                continue
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if expires > cutoff:
                continue
            version = int(row.lease_version or 0)
            result = session.exec(
                update(OutlookAccountModel)
                .where(OutlookAccountModel.id == row.id)
                .where(OutlookAccountModel.state == "leased")
                .where(OutlookAccountModel.lease_owner == str(row.lease_owner or ""))
                .where(OutlookAccountModel.lease_version == version)
                .where(OutlookAccountModel.lease_expires_at == row.lease_expires_at)
                .values(
                    state="failed",
                    enabled=False,
                    lease_owner="",
                    lease_expires_at=None,
                    lease_version=version + 1,
                    bound_account_id=0,
                    bound_at=None,
                    quarantine_reason="worker_interrupted",
                    last_error="任务进程中断，邮箱租约已自动恢复为失败状态",
                    updated_at=_utcnow(),
                )
            )
            if int(getattr(result, "rowcount", 0) or 0) == 1:
                recovered += 1
        if recovered:
            session.commit()
        return recovered


def sync_bound_outlook_credentials(
    account_id: int,
    email: str,
    *,
    password: str | None = None,
    mailapi_url: str | None = None,
    mailapi_token: str | None = None,
    database_engine=None,
) -> bool:
    """Refresh non-authoritative mailbox projections for a bound account.

    The ChatGPT account remains the canonical credential source.  This small
    CAS-by-identity projection keeps a bound Outlook/MailAPI row usable for a
    future explicit email-risk fallback without re-opening its lease.
    """
    target_engine = database_engine or engine
    normalized_email = str(email or "").strip().lower()
    try:
        normalized_account_id = int(account_id)
    except (TypeError, ValueError):
        return False
    if normalized_account_id <= 0 or not normalized_email:
        return False
    values: dict[str, object] = {"updated_at": _utcnow()}
    if password is not None and str(password):
        values["password"] = str(password)
    if mailapi_url is not None and str(mailapi_url):
        values["mailapi_url"] = str(mailapi_url)
    if mailapi_token is not None and str(mailapi_token):
        values["mailapi_token"] = str(mailapi_token)
    if len(values) == 1:
        return False
    with Session(target_engine) as session:
        result = session.exec(
            update(OutlookAccountModel)
            .where(OutlookAccountModel.state == "bound")
            .where(OutlookAccountModel.bound_account_id == normalized_account_id)
            .where(func.lower(OutlookAccountModel.email) == normalized_email)
            .values(**values)
        )
        changed = int(getattr(result, "rowcount", 0) or 0) == 1
        if changed:
            session.commit()
        else:
            session.rollback()
        return changed


def _migrate_chatgpt_auth_state_schema() -> None:
    """Add durable retry columns to installations created before backoff."""
    if engine.url.get_backend_name() != "sqlite":
        return
    with engine.begin() as conn:
        rows = conn.exec_driver_sql("PRAGMA table_info('chatgpt_auth_states')").fetchall()
        if not rows:
            return
        existing_columns = {str(row[1]) for row in rows}
        additive_columns = {
            "failure_count": "INTEGER DEFAULT 0",
            "next_retry_at": "DATETIME",
            "circuit_state": "TEXT DEFAULT 'closed'",
            "last_failure_at": "DATETIME",
        }
        for column, definition in additive_columns.items():
            if column not in existing_columns:
                conn.exec_driver_sql(
                    f"ALTER TABLE chatgpt_auth_states ADD COLUMN {column} {definition}"
                )
        conn.exec_driver_sql(
            "UPDATE chatgpt_auth_states SET failure_count = 0 WHERE failure_count IS NULL"
        )
        conn.exec_driver_sql(
            "UPDATE chatgpt_auth_states SET circuit_state = 'closed' "
            "WHERE circuit_state IS NULL OR TRIM(circuit_state) = ''"
        )


def _recover_chatgpt_attempt_bindings() -> None:
    """Make interrupted local retries selectable again after a service restart."""
    with Session(engine) as session:
        rows = session.exec(
            select(ChatGPTAttemptBindingModel).where(
                ChatGPTAttemptBindingModel.status.in_(["running", "retrying"])
            )
        ).all()
        if not rows:
            return
        for row in rows:
            row.status = "failed"
            if not str(row.error or "").strip():
                row.error = "任务因服务重启中断，可按原邮箱重试；接码池任务会重新领取卡密"
            row.updated_at = _utcnow()
            session.add(row)
        session.commit()


def init_account_pool_schema(database_engine=None) -> None:
    """Create account-pool tables and apply additive legacy migrations.

    The project intentionally has no external migration dependency.  This
    helper is safe to call from tests, startup, and a rolling deployment: it
    only creates missing tables/columns and never rewrites credential data.
    """

    target_engine = database_engine or engine
    SQLModel.metadata.create_all(target_engine)

    if target_engine.url.get_backend_name() != "sqlite":
        return

    with target_engine.begin() as conn:
        existing_tables = {
            str(row[0])
            for row in conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        account_table = conn.exec_driver_sql(
            "PRAGMA table_info('accounts')"
        ).fetchall()
        if account_table:
            account_columns = {str(row[1]) for row in account_table}
            if "identity_id" not in account_columns:
                conn.exec_driver_sql(
                    "ALTER TABLE accounts ADD COLUMN identity_id TEXT DEFAULT ''"
                )
            conn.exec_driver_sql(
                "UPDATE accounts SET identity_id = '' WHERE identity_id IS NULL"
            )
            conn.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_accounts_identity_id "
                "ON accounts (identity_id)"
            )

        quota_table = conn.exec_driver_sql(
            "PRAGMA table_info('account_quota_snapshots')"
        ).fetchall()
        if quota_table:
            quota_columns = {str(row[1]) for row in quota_table}
            for column, sql_type in (
                ("billed_usd", "FLOAT"),
                ("continuous_billed_usd", "FLOAT"),
                ("remaining_usd", "FLOAT"),
                ("continuous_remaining_usd", "FLOAT"),
                ("source_updated_at", "DATETIME"),
            ):
                if column not in quota_columns:
                    conn.exec_driver_sql(
                        "ALTER TABLE account_quota_snapshots "
                        f"ADD COLUMN {column} {sql_type}"
                    )
            # Refresh after ALTER TABLE so legacy databases missing one of the
            # amount columns are safe for the backfill below.
            quota_columns = {
                str(row[1])
                for row in conn.exec_driver_sql(
                    "PRAGMA table_info('account_quota_snapshots')"
                ).fetchall()
            }
            for column in (
                "billed_cents",
                "continuous_billed_cents",
                "remaining_cents",
                "continuous_remaining_cents",
            ):
                if column not in quota_columns:
                    conn.exec_driver_sql(
                        f"ALTER TABLE account_quota_snapshots ADD COLUMN {column} INTEGER"
                    )
            for cents_column, amount_column in (
                ("billed_cents", "billed_usd"),
                ("continuous_billed_cents", "continuous_billed_usd"),
                ("remaining_cents", "remaining_usd"),
                ("continuous_remaining_cents", "continuous_remaining_usd"),
            ):
                conn.exec_driver_sql(
                    f"UPDATE account_quota_snapshots "
                    f"SET {cents_column} = CAST(ROUND({amount_column} * 100) AS INTEGER) "
                    f"WHERE {cents_column} IS NULL AND {amount_column} IS NOT NULL"
                )
            if "remaining_scope" not in quota_columns:
                conn.exec_driver_sql(
                    "ALTER TABLE account_quota_snapshots "
                    "ADD COLUMN remaining_scope TEXT DEFAULT 'target_local'"
                )
            conn.exec_driver_sql(
                "UPDATE account_quota_snapshots SET remaining_scope = 'target_local' "
                "WHERE remaining_scope IS NULL OR TRIM(remaining_scope) = ''"
            )
            if "freshness_seconds" not in quota_columns:
                conn.exec_driver_sql(
                    "ALTER TABLE account_quota_snapshots "
                    "ADD COLUMN freshness_seconds INTEGER DEFAULT 900"
                )
            conn.exec_driver_sql(
                "UPDATE account_quota_snapshots SET freshness_seconds = 900 "
                "WHERE freshness_seconds IS NULL OR freshness_seconds <= 0"
            )

        alias_table = conn.exec_driver_sql(
            "PRAGMA table_info('account_identity_aliases')"
        ).fetchall()
        if alias_table:
            alias_columns = {str(row[1]) for row in alias_table}
            if "platform" not in alias_columns:
                conn.exec_driver_sql(
                    "ALTER TABLE account_identity_aliases "
                    "ADD COLUMN platform TEXT DEFAULT 'chatgpt'"
                )
            conn.exec_driver_sql(
                "UPDATE account_identity_aliases SET platform = COALESCE(("
                "SELECT platform FROM account_identities "
                "WHERE account_identities.id = account_identity_aliases.identity_id"
                "), 'chatgpt') WHERE platform IS NULL OR TRIM(platform) = ''"
            )

        target_table = conn.exec_driver_sql(
            "PRAGMA table_info('codex2api_targets')"
        ).fetchall()
        if target_table:
            target_columns = {str(row[1]) for row in target_table}
            for column in ("health_success_count", "health_failure_count"):
                if column not in target_columns:
                    conn.exec_driver_sql(
                        f"ALTER TABLE codex2api_targets ADD COLUMN {column} INTEGER DEFAULT 0"
                    )
                conn.exec_driver_sql(
                    f"UPDATE codex2api_targets SET {column} = 0 WHERE {column} IS NULL"
                )

        policy_table = conn.exec_driver_sql(
            "PRAGMA table_info('pool_target_policies')"
        ).fetchall()
        if policy_table:
            policy_columns = {str(row[1]) for row in policy_table}
            if "remote_api_key_ids_json" not in policy_columns:
                conn.exec_driver_sql(
                    "ALTER TABLE pool_target_policies "
                    "ADD COLUMN remote_api_key_ids_json TEXT DEFAULT '[]'"
                )
            if "bandwidth_mbps" not in policy_columns:
                conn.exec_driver_sql(
                    "ALTER TABLE pool_target_policies "
                    "ADD COLUMN bandwidth_mbps INTEGER DEFAULT 0"
                )

        # Field(index=True) covers fresh databases.  Explicit IF NOT EXISTS
        # statements also repair installations created by older SQLModel
        # versions whose metadata did not include every index.
        index_specs = (
            (
                "ix_account_identity_alias_lookup",
                "account_identity_aliases",
                "alias_type, normalized_value",
            ),
            (
                "ix_account_target_binding_identity_target",
                "account_target_bindings",
                "identity_id, target_id",
            ),
            (
                "ix_account_assignment_identity_state",
                "account_assignments",
                "identity_id, state",
            ),
            (
                "ix_account_quota_snapshot_identity_window_time",
                "account_quota_snapshots",
                "identity_id, window, captured_at",
            ),
            (
                "ix_account_migration_identity_state",
                "account_migrations",
                "identity_id, state",
            ),
            (
                "ix_scheduler_action_run_status",
                "scheduler_actions",
                "run_id, status",
            ),
            (
                "ix_assignment_event_identity_time",
                "account_assignment_events",
                "identity_id, created_at",
            ),
            (
                "ix_customer_usage_customer_time",
                "customer_usage_samples",
                "customer_id, bucket_start",
            ),
        )
        for index_name, table_name, columns in index_specs:
            if table_name not in existing_tables:
                continue
            conn.exec_driver_sql(
                f"CREATE INDEX IF NOT EXISTS {index_name} "
                f"ON {table_name} ({columns})"
            )
        unique_index_specs = (
            (
                "uq_account_identity_alias_platform_type_value",
                "account_identity_aliases",
                "platform, alias_type, normalized_value",
                "WHERE alias_type != 'email'",
            ),
            (
                "uq_account_target_binding_identity_target",
                "account_target_bindings",
                "identity_id, target_id",
                "",
            ),
            (
                "uq_account_target_binding_remote_id",
                "account_target_bindings",
                "target_id, remote_account_id",
                "WHERE remote_account_id > 0",
            ),
            (
                "uq_account_assignment_current_identity",
                "account_assignments",
                "identity_id",
                "WHERE state IN ('active', 'draining', 'standby')",
            ),
            (
                "uq_account_migration_idempotency_key",
                "account_migrations",
                "idempotency_key",
                "",
            ),
            (
                "uq_pool_target_policy_pool_target",
                "pool_target_policies",
                "pool_id, target_id",
                "",
            ),
            (
                "uq_quota_rollup_identity_window_bucket",
                "account_quota_rollups",
                "identity_id, window, bucket, bucket_start",
                "",
            ),
            (
                "uq_customer_usage_sample_bucket",
                "customer_usage_samples",
                "customer_id, target_id, remote_api_key_id, bucket_start",
                "",
            ),
        )
        # Older experimental builds could write duplicate strong aliases or
        # bindings before the control-plane constraints existed.  Preserve
        # every affected identity by marking it ambiguous, then keep one
        # representative row so the new unique indexes can be installed.
        if "account_identity_aliases" in existing_tables:
            duplicate_alias_rows = conn.exec_driver_sql(
                "SELECT platform, alias_type, normalized_value "
                "FROM account_identity_aliases "
                "WHERE alias_type != 'email' "
                "GROUP BY platform, alias_type, normalized_value "
                "HAVING COUNT(*) > 1"
            ).fetchall()
            for platform, alias_type, normalized_value in duplicate_alias_rows:
                rows = conn.exec_driver_sql(
                    "SELECT id, identity_id FROM account_identity_aliases "
                    "WHERE platform = ? AND alias_type = ? AND normalized_value = ? "
                    "ORDER BY id",
                    (platform, alias_type, normalized_value),
                ).fetchall()
                if len(rows) <= 1:
                    continue
                for _alias_id, identity_id in rows:
                    conn.exec_driver_sql(
                        "UPDATE account_identities SET state = 'ambiguous' "
                        "WHERE id = ?",
                        (identity_id,),
                    )
                for alias_id, _identity_id in rows[1:]:
                    conn.exec_driver_sql(
                        "DELETE FROM account_identity_aliases WHERE id = ?",
                        (alias_id,),
                    )
        if "account_target_bindings" in existing_tables:
            duplicate_binding_groups = conn.exec_driver_sql(
                "SELECT identity_id, target_id FROM account_target_bindings "
                "GROUP BY identity_id, target_id HAVING COUNT(*) > 1"
            ).fetchall()
            for identity_id, target_id in duplicate_binding_groups:
                rows = conn.exec_driver_sql(
                    "SELECT id FROM account_target_bindings "
                    "WHERE identity_id = ? AND target_id = ? ORDER BY id",
                    (identity_id, target_id),
                ).fetchall()
                conn.exec_driver_sql(
                    "UPDATE account_identities SET state = 'ambiguous' WHERE id = ?",
                    (identity_id,),
                )
                for (binding_id,) in rows[1:]:
                    conn.exec_driver_sql(
                        "UPDATE account_target_bindings SET sync_status = 'ambiguous', enabled = 0 WHERE id = ?",
                        (binding_id,),
                    )
                    conn.exec_driver_sql(
                        "DELETE FROM account_target_bindings WHERE id = ?",
                        (binding_id,),
                    )
            duplicate_remote_groups = conn.exec_driver_sql(
                "SELECT target_id, remote_account_id FROM account_target_bindings "
                "WHERE remote_account_id > 0 GROUP BY target_id, remote_account_id "
                "HAVING COUNT(*) > 1"
            ).fetchall()
            for target_id, remote_id in duplicate_remote_groups:
                rows = conn.exec_driver_sql(
                    "SELECT id, identity_id FROM account_target_bindings "
                    "WHERE target_id = ? AND remote_account_id = ? ORDER BY id",
                    (target_id, remote_id),
                ).fetchall()
                for _binding_id, identity_id in rows:
                    conn.exec_driver_sql(
                        "UPDATE account_identities SET state = 'ambiguous' WHERE id = ?",
                        (identity_id,),
                    )
                for binding_id, _identity_id in rows[1:]:
                    conn.exec_driver_sql(
                        "DELETE FROM account_target_bindings WHERE id = ?",
                        (binding_id,),
                    )
        if "account_assignments" in existing_tables:
            duplicate_assignment_groups = conn.exec_driver_sql(
                "SELECT identity_id FROM account_assignments "
                "WHERE state IN ('active', 'draining', 'standby') "
                "GROUP BY identity_id HAVING COUNT(*) > 1"
            ).fetchall()
            for (identity_id,) in duplicate_assignment_groups:
                rows = conn.exec_driver_sql(
                    "SELECT id FROM account_assignments WHERE identity_id = ? "
                    "AND state IN ('active', 'draining', 'standby') "
                    "ORDER BY id",
                    (identity_id,),
                ).fetchall()
                conn.exec_driver_sql(
                    "UPDATE account_identities SET state = 'ambiguous' WHERE id = ?",
                    (identity_id,),
                )
                for (assignment_id,) in rows[1:]:
                    conn.exec_driver_sql(
                        "UPDATE account_assignments SET state = 'revoked', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (assignment_id,),
                    )
        if "account_migrations" in existing_tables:
            duplicate_migration_groups = conn.exec_driver_sql(
                "SELECT idempotency_key FROM account_migrations "
                "GROUP BY idempotency_key HAVING COUNT(*) > 1"
            ).fetchall()
            for (idempotency_key,) in duplicate_migration_groups:
                rows = conn.exec_driver_sql(
                    "SELECT id FROM account_migrations WHERE idempotency_key = ? ORDER BY id",
                    (idempotency_key,),
                ).fetchall()
                for (migration_id,) in rows[1:]:
                    legacy_key = f"{idempotency_key}#legacy-{migration_id}"
                    conn.exec_driver_sql(
                        "UPDATE account_migrations SET idempotency_key = ?, state = 'rollback_required', "
                        "error_json = ? WHERE id = ?",
                        (
                            legacy_key,
                            json.dumps({"message": "legacy duplicate idempotency key"}, ensure_ascii=False),
                            migration_id,
                        ),
                    )
        for index_name, table_name, columns, condition in unique_index_specs:
            if table_name not in existing_tables:
                continue
            try:
                conn.exec_driver_sql(
                    f"CREATE UNIQUE INDEX IF NOT EXISTS {index_name} "
                    f"ON {table_name} ({columns}) {condition}"
                )
            except IntegrityError:
                # A legacy duplicate in a non-critical index must not stop a
                # rolling deployment.  The application still performs
                # identity/assignment CAS checks; a later maintenance run can
                # install the index after an operator resolves the conflict.
                import logging

                logging.getLogger(__name__).warning(
                    "deferred unique index creation for %s due to legacy duplicates",
                    index_name,
                )


def init_db():
    SQLModel.metadata.create_all(engine)
    _migrate_outlook_accounts_schema()
    recover_expired_outlook_leases()
    _migrate_chatgpt_auth_state_schema()
    init_account_pool_schema(engine)
    _recover_chatgpt_attempt_bindings()
    from services.account_identity import reconcile_existing_accounts
    from services.codex2api_target_client import ensure_default_target
    from services.pool_scheduler import ensure_default_pools

    reconcile_existing_accounts(engine)
    ensure_default_target(engine)
    ensure_default_pools(engine)
    from core.sms_pool import SmsPoolService

    sms_pool = SmsPoolService(engine)
    sms_pool.recover_interrupted()
    sms_pool.recover_stale_active()


def get_session():
    with Session(engine) as session:
        yield session
