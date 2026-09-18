"""数据库模型 - SQLite via SQLModel"""
from datetime import datetime, timezone
import os
import logging
from decimal import Decimal, InvalidOperation
from typing import Optional
from sqlalchemy import delete, event, func, update, UniqueConstraint
from sqlalchemy import inspect as sqlalchemy_inspect, text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Field, SQLModel, create_engine, Session, select
import json
from core.purchase_cost_models import PurchaseBatchModel, PurchaseCostRecordModel


_LOGGER = logging.getLogger(__name__)


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
    # ``local`` means the credential was imported or logged in through this
    # application. ``codex2api`` is a credential-free remote projection.
    account_source: str = Field(default="local", index=True)
    trial_end_time: int = 0
    cashier_url: str = ""
    extra_json: str = "{}"   # JSON 存储平台自定义字段
    # Operator-managed purchase cost, isolated from credential snapshot writes.
    purchase_cost_cents: Optional[int] = None
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
    inventory_last_sync_at: Optional[datetime] = None
    inventory_last_error: str = ""
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
            existing.account_source = "local"
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
            account_source="local",
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
        update(PurchaseCostRecordModel)
        .where(PurchaseCostRecordModel.account_id == normalized_id)
        .values(account_id=None)
    )
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


_OPERATIONS_BILLING_SNAPSHOT_UNIQUE_INDEX = (
    "uq_operations_billing_snapshot_target_remote"
)
_CODEX_INVENTORY_SNAPSHOT_UNIQUE_INDEX = "uq_codex_inventory_target_remote"


def _coerce_snapshot_timestamp(value):
    """Return a comparable UTC datetime for a legacy snapshot timestamp."""
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _snapshot_row_sort_key(row: dict):
    """Sort legacy rows by capture time, then stable primary-key order."""
    captured = _coerce_snapshot_timestamp(row.get("captured_at"))
    try:
        row_id = int(row.get("id") or 0)
    except (TypeError, ValueError):
        row_id = 0
    # Rows without a timestamp are older than every valid captured snapshot;
    # their id still gives deterministic ordering when all timestamps are
    # missing (a common shape in the first release of this table).
    return (
        captured is not None,
        captured or datetime.min.replace(tzinfo=timezone.utc),
        row_id,
    )


def _snapshot_history_items(value) -> list[dict]:
    """Decode a legacy history payload while dropping malformed entries."""
    if isinstance(value, str):
        try:
            value = json.loads(value or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
    if not isinstance(value, list):
        return []
    result: list[dict] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        day = item.get("date") or item.get("day")
        if day is None or not str(day).strip():
            continue
        result.append(dict(item))
    return result


def _merge_snapshot_history(rows: list[dict]) -> str:
    """Union history days from duplicate rows, preferring newer data.

    During the migration a summary-only refresh may have written ``[]`` over a
    previously populated history payload.  Keep every known day and let the
    newest row win when the same day appears more than once; missing keys in
    that newer entry are filled from the older entry.
    """
    by_day: dict[str, dict] = {}
    # ``rows`` is passed newest-first, so the first value for a day is the
    # authoritative one. Older rows only contribute missing fields.
    for row in rows:
        for item in _snapshot_history_items(row.get("history_json")):
            day = str(item.get("date") or item.get("day") or "").strip()
            if not day:
                continue
            current = by_day.get(day)
            if current is None:
                by_day[day] = item
                continue
            merged = dict(current)
            for key, value in item.items():
                if key not in merged or merged[key] in (None, "", [], {}):
                    if value not in (None, "", [], {}):
                        merged[key] = value
                elif key in {"account_billed", "billed_usd", "requests", "tokens"}:
                    try:
                        if Decimal(str(value)) > Decimal(str(merged[key])):
                            merged[key] = value
                    except (InvalidOperation, TypeError, ValueError):
                        pass
            by_day[day] = merged
    if not by_day:
        return "[]"
    # Stable chronological output makes the result deterministic and easier
    # to inspect in backups. Keep malformed date strings after valid dates.
    def _day_key(item):
        day = item[0]
        try:
            return (0, datetime.fromisoformat(day).date().isoformat())
        except (TypeError, ValueError):
            return (1, day)

    ordered = [item for _, item in sorted(by_day.items(), key=_day_key)]
    return json.dumps(ordered, ensure_ascii=False, separators=(",", ":"))


def _migrate_operations_billing_snapshot_schema(conn, existing_tables: set[str]) -> None:
    """Upgrade and compact legacy billing snapshots before adding uniqueness.

    ``create_all`` cannot alter an existing SQLite table, and releases before
    the control-plane writer did not always enforce the natural
    ``(target_id, remote_id)`` key.  This migration is intentionally local and
    deterministic: it keeps the newest row (``captured_at``, then ``id``),
    carries forward missing history/aggregate values, removes older duplicates,
    and leaves a named unique index for future upserts.
    """
    table_name = "operations_billing_snapshots"
    if table_name not in existing_tables:
        return

    billing_table = conn.exec_driver_sql(
        "PRAGMA table_info('operations_billing_snapshots')"
    ).fetchall()
    if not billing_table:
        return
    billing_columns = {str(row[1]) for row in billing_table}
    additive_columns = {
        "total_requests": "INTEGER",
        "source": "TEXT DEFAULT 'codex2api'",
        "error": "TEXT DEFAULT ''",
        "last_request_at": "DATETIME",
    }
    for column, definition in additive_columns.items():
        if column not in billing_columns:
            conn.exec_driver_sql(
                "ALTER TABLE operations_billing_snapshots "
                f"ADD COLUMN {column} {definition}"
            )
    # Refresh after ALTER TABLE so the duplicate compactor can select every
    # field that is available on both old and current installations.
    billing_columns = {
        str(row[1])
        for row in conn.exec_driver_sql(
            "PRAGMA table_info('operations_billing_snapshots')"
        ).fetchall()
    }
    conn.exec_driver_sql(
        "UPDATE operations_billing_snapshots SET source = 'codex2api' "
        "WHERE source IS NULL OR TRIM(source) = ''"
    )
    conn.exec_driver_sql(
        "UPDATE operations_billing_snapshots SET error = '' "
        "WHERE error IS NULL"
    )

    if not {"target_id", "remote_id"} <= billing_columns:
        # A malformed/partially-created table cannot be made safe here. Leave
        # it untouched so the existing startup diagnostics can report it.
        return

    duplicate_groups = conn.exec_driver_sql(
        "SELECT target_id, remote_id "
        "FROM operations_billing_snapshots "
        "WHERE target_id IS NOT NULL AND remote_id IS NOT NULL "
        "GROUP BY target_id, remote_id HAVING COUNT(*) > 1"
    ).fetchall()
    if not duplicate_groups:
        return

    fields = (
        "id",
        "target_id",
        "remote_id",
        "total_billed_micros",
        "total_requests",
        "today_date",
        "today_billed_micros",
        "today_requests",
        "history_json",
        "captured_at",
        "source",
        "error",
        "last_request_at",
    )
    available_fields = [field for field in fields if field in billing_columns]
    id_column_expression = "id"
    if "id" not in billing_columns:
        # ``id`` is part of the model, but rowid keeps the migration useful for
        # an interrupted hand-created table that omitted the declared key.
        id_column_expression = "rowid"
        available_fields = [field for field in available_fields if field != "id"]
        available_fields.insert(0, "rowid AS id")
    select_sql = ", ".join(available_fields)

    for target_id, remote_id in duplicate_groups:
        rows = conn.exec_driver_sql(
            "SELECT " + select_sql + " FROM operations_billing_snapshots "
            "WHERE target_id = ? AND remote_id = ?",
            (target_id, remote_id),
        ).fetchall()
        result_fields = [field.split(" AS ")[-1] for field in available_fields]
        mapped_rows = [dict(zip(result_fields, row)) for row in rows]
        if len(mapped_rows) < 2:
            continue
        ordered = sorted(mapped_rows, key=_snapshot_row_sort_key, reverse=True)
        winner = ordered[0]
        losers = ordered[1:]
        updates: dict[str, object] = {}

        # All-time counters are monotonic. Preserve the largest known value if
        # an older row captured a later upstream total than the selected row.
        for field in ("total_billed_micros", "total_requests"):
            if field not in billing_columns:
                continue
            values = [
                row.get(field) for row in ordered if row.get(field) is not None
            ]
            if not values:
                continue
            try:
                maximum = max(int(value) for value in values)
            except (TypeError, ValueError):
                continue
            current = winner.get(field)
            try:
                current_value = int(current) if current is not None else None
            except (TypeError, ValueError):
                current_value = None
            if current_value is None or maximum > current_value:
                updates[field] = maximum

        # A summary row can omit today's fields. Fill them from a duplicate
        # only when the day matches; never replace a newer day's values with an
        # older day's snapshot.
        winner_day = str(winner.get("today_date") or "").strip()
        if not winner_day:
            for row in ordered:
                candidate_day = str(row.get("today_date") or "").strip()
                if candidate_day:
                    winner_day = candidate_day
                    updates["today_date"] = candidate_day
                    for field in ("today_billed_micros", "today_requests"):
                        if field in billing_columns and row.get(field) is not None:
                            updates[field] = row[field]
                    break
        if winner_day:
            for row in losers:
                if str(row.get("today_date") or "").strip() != winner_day:
                    continue
                for field in ("today_billed_micros", "today_requests"):
                    if field not in billing_columns or row.get(field) is None:
                        continue
                    current = updates.get(field, winner.get(field))
                    if current is None:
                        updates[field] = row[field]
                        continue
                    try:
                        if int(row[field]) > int(current):
                            updates[field] = int(row[field])
                    except (TypeError, ValueError):
                        continue

        if "history_json" in billing_columns:
            merged_history = _merge_snapshot_history(ordered)
            current_history = _merge_snapshot_history([winner])
            if merged_history != current_history:
                updates["history_json"] = merged_history

        if "last_request_at" in billing_columns:
            latest_request = None
            latest_value = None
            for row in ordered:
                parsed = _coerce_snapshot_timestamp(row.get("last_request_at"))
                if parsed is not None and (
                    latest_request is None or parsed > latest_request
                ):
                    latest_request = parsed
                    latest_value = row.get("last_request_at")
            if latest_value is not None:
                current_request = _coerce_snapshot_timestamp(winner.get("last_request_at"))
                if current_request is None or latest_request > current_request:
                    updates["last_request_at"] = latest_value

        winner_id = winner.get("id")
        if winner_id is None:
            continue
        if updates:
            assignments = ", ".join(f"{field} = ?" for field in updates)
            conn.exec_driver_sql(
                "UPDATE operations_billing_snapshots SET "
                + assignments
                + f" WHERE {id_column_expression} = ?",
                tuple(updates.values()) + (winner_id,),
            )
        for row in losers:
            loser_id = row.get("id")
            if loser_id is not None:
                conn.exec_driver_sql(
                    "DELETE FROM operations_billing_snapshots "
                    f"WHERE {id_column_expression} = ?",
                    (loser_id,),
                )


def _inventory_summary_dict(value) -> dict:
    """Decode one credential-free inventory summary for migration merging."""
    if isinstance(value, str):
        try:
            value = json.loads(value or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
    return dict(value) if isinstance(value, dict) else {}


def _inventory_summary_decodable(value) -> bool:
    """Whether an inventory summary is a JSON object (including ``{}`)."""
    if isinstance(value, dict):
        return True
    if not isinstance(value, str):
        return False
    try:
        return isinstance(json.loads(value or "{}"), dict)
    except (TypeError, ValueError, json.JSONDecodeError):
        return False


def _merge_inventory_summary_missing(rows: list[dict]) -> str:
    """Merge duplicate inventory summaries, preferring the newest row.

    Inventory rows are sanitized projections.  A newer response can omit an
    optional quota/identity field, so fill only empty values from older rows
    and recurse through nested objects.  Newer non-empty values always win.
    """

    def merge_missing(current: dict, older: dict) -> dict:
        merged = dict(current)
        for key, value in older.items():
            if key not in merged or merged[key] in (None, "", [], {}):
                if value not in (None, "", [], {}):
                    merged[key] = value
            elif isinstance(merged[key], dict) and isinstance(value, dict):
                merged[key] = merge_missing(merged[key], value)
        return merged

    # ``rows`` is newest-first. Start with the newest valid summary and fill
    # omitted fields from older rows without replacing current values.
    merged: dict = {}
    for row in reversed(rows):
        if str(row.get("error") or "").strip() or not _inventory_summary_decodable(
            row.get("summary_json")
        ):
            continue
        summary = _inventory_summary_dict(row.get("summary_json"))
        if summary:
            merged = merge_missing(summary, merged)
    # The loop above intentionally lets the newest object be authoritative;
    # apply a final pass in the same direction so nested fields are retained.
    newest_first = [
        _inventory_summary_dict(row.get("summary_json"))
        for row in rows
        if not str(row.get("error") or "").strip()
        and _inventory_summary_decodable(row.get("summary_json"))
    ]
    for summary in newest_first:
        if summary:
            merged = merge_missing(merged, summary)
    return json.dumps(merged, ensure_ascii=False, separators=(",", ":")) if merged else "{}"


def _inventory_snapshot_sort_key(row: dict):
    """Rank decodable, non-error projections before failed rows.

    ``missing=True`` is a valid result of a complete inventory response, so it
    must not be demoted behind an older non-missing row.  Only an explicit
    error (or malformed summary) makes a row ineligible for the primary slot.
    """
    valid = (
        not str(row.get("error") or "").strip()
        and _inventory_summary_decodable(row.get("summary_json"))
    )
    fetched = _coerce_snapshot_timestamp(row.get("fetched_at"))
    source_updated = _coerce_snapshot_timestamp(row.get("source_updated_at"))
    updated = _coerce_snapshot_timestamp(row.get("updated_at"))
    created = _coerce_snapshot_timestamp(row.get("created_at"))
    try:
        row_id = int(row.get("id") or 0)
    except (TypeError, ValueError):
        row_id = 0
    return (
        valid,
        fetched or datetime.min.replace(tzinfo=timezone.utc),
        source_updated or datetime.min.replace(tzinfo=timezone.utc),
        updated or datetime.min.replace(tzinfo=timezone.utc),
        created or datetime.min.replace(tzinfo=timezone.utc),
        row_id,
    )


def _migrate_codex_inventory_snapshot_schema(
    conn,
    existing_tables: set[str],
) -> None:
    """Compact legacy inventory rows and install their natural-key index.

    Older SQLite installations could contain duplicate target/remote rows or
    omit columns added after the first inventory release.  Keep the newest
    valid projection, merge optional fields from older rows, then delete only
    the redundant rows before the unique index is created.
    """

    table_name = "codex_inventory_snapshots"
    if table_name not in existing_tables:
        return
    table_info = conn.exec_driver_sql(
        "PRAGMA table_info('codex_inventory_snapshots')"
    ).fetchall()
    if not table_info:
        return
    columns = {str(row[1]) for row in table_info}
    additive_columns = {
        "summary_json": "TEXT DEFAULT '{}'",
        "fetched_at": "DATETIME",
        "source_updated_at": "TEXT DEFAULT ''",
        "missing": "INTEGER DEFAULT 0",
        "error": "TEXT DEFAULT ''",
        "created_at": "DATETIME",
        "updated_at": "DATETIME",
    }
    for column, definition in additive_columns.items():
        if column not in columns:
            conn.exec_driver_sql(
                "ALTER TABLE codex_inventory_snapshots "
                f"ADD COLUMN {column} {definition}"
            )
    columns = {
        str(row[1])
        for row in conn.exec_driver_sql(
            "PRAGMA table_info('codex_inventory_snapshots')"
        ).fetchall()
    }
    for column, value in (("source_updated_at", ""), ("error", "")):
        if column in columns:
            conn.exec_driver_sql(
                f"UPDATE codex_inventory_snapshots SET {column} = ? "
                f"WHERE {column} IS NULL",
                (value,),
            )
    if "missing" in columns:
        conn.exec_driver_sql(
            "UPDATE codex_inventory_snapshots SET missing = 0 WHERE missing IS NULL"
        )
    if not {"target_id", "remote_id"} <= columns:
        return

    duplicate_groups = conn.exec_driver_sql(
        "SELECT target_id, remote_id FROM codex_inventory_snapshots "
        "WHERE target_id IS NOT NULL AND remote_id IS NOT NULL "
        "GROUP BY target_id, remote_id HAVING COUNT(*) > 1"
    ).fetchall()
    if not duplicate_groups:
        return

    fields = (
        "id",
        "target_id",
        "remote_id",
        "summary_json",
        "fetched_at",
        "source_updated_at",
        "missing",
        "error",
        "created_at",
        "updated_at",
    )
    available_fields = [field for field in fields if field in columns]
    id_column_expression = "id"
    if "id" not in columns:
        id_column_expression = "rowid"
        available_fields.insert(0, "rowid AS id")
    select_fields = [field for field in available_fields if field in columns or " AS " in field]
    result_fields = [field.split(" AS ")[-1] for field in select_fields]
    select_sql = ", ".join(select_fields)

    for target_id, remote_id in duplicate_groups:
        raw_rows = conn.exec_driver_sql(
            "SELECT " + select_sql + " FROM codex_inventory_snapshots "
            "WHERE target_id = ? AND remote_id = ?",
            (target_id, remote_id),
        ).fetchall()
        rows = [dict(zip(result_fields, row)) for row in raw_rows]
        if len(rows) < 2:
            continue
        ordered = sorted(rows, key=_inventory_snapshot_sort_key, reverse=True)
        winner = ordered[0]
        losers = ordered[1:]
        updates: dict[str, object] = {}

        if "summary_json" in columns:
            merged_summary = _merge_inventory_summary_missing(ordered)
            current_summary = _inventory_summary_dict(winner.get("summary_json"))
            if merged_summary != "{}" and _inventory_summary_dict(merged_summary) != current_summary:
                updates["summary_json"] = merged_summary

        if "source_updated_at" in columns:
            newest_source = None
            newest_source_value = None
            for row in ordered:
                parsed = _coerce_snapshot_timestamp(row.get("source_updated_at"))
                if parsed is not None and (
                    newest_source is None or parsed > newest_source
                ):
                    newest_source = parsed
                    newest_source_value = row.get("source_updated_at")
            if newest_source_value is not None:
                current_source = _coerce_snapshot_timestamp(
                    winner.get("source_updated_at")
                )
                if current_source is None or newest_source > current_source:
                    updates["source_updated_at"] = newest_source_value

        if "created_at" in columns:
            oldest_created = None
            oldest_created_value = None
            for row in ordered:
                parsed = _coerce_snapshot_timestamp(row.get("created_at"))
                if parsed is not None and (
                    oldest_created is None or parsed < oldest_created
                ):
                    oldest_created = parsed
                    oldest_created_value = row.get("created_at")
            if oldest_created_value is not None:
                current_created = _coerce_snapshot_timestamp(winner.get("created_at"))
                if current_created is None or oldest_created < current_created:
                    updates["created_at"] = oldest_created_value

        if "updated_at" in columns:
            newest_updated = None
            newest_updated_value = None
            for row in ordered:
                parsed = _coerce_snapshot_timestamp(row.get("updated_at"))
                if parsed is not None and (
                    newest_updated is None or parsed > newest_updated
                ):
                    newest_updated = parsed
                    newest_updated_value = row.get("updated_at")
            if newest_updated_value is not None:
                current_updated = _coerce_snapshot_timestamp(winner.get("updated_at"))
                if current_updated is None or newest_updated > current_updated:
                    updates["updated_at"] = newest_updated_value

        winner_id = winner.get("id")
        if winner_id is None:
            continue
        if updates:
            assignments = ", ".join(f"{field} = ?" for field in updates)
            conn.exec_driver_sql(
                "UPDATE codex_inventory_snapshots SET "
                + assignments
                + f" WHERE {id_column_expression} = ?",
                tuple(updates.values()) + (winner_id,),
            )
        for row in losers:
            loser_id = row.get("id")
            if loser_id is not None:
                conn.exec_driver_sql(
                    "DELETE FROM codex_inventory_snapshots "
                    f"WHERE {id_column_expression} = ?",
                    (loser_id,),
                )


def _migrate_operations_billing_snapshot_relational_schema(database_engine) -> None:
    """Apply additive snapshot columns on non-SQLite control databases.

    ``SQLModel.metadata.create_all`` intentionally does not alter an existing
    table.  A deployment that stores the control plane in PostgreSQL therefore
    needs the same four columns that the SQLite startup migration adds before
    ORM reads/writes use the new provenance fields.
    """

    table_name = "operations_billing_snapshots"
    try:
        inspector = sqlalchemy_inspect(database_engine)
        schema = getattr(inspector, "default_schema_name", None)
        table_names = set(inspector.get_table_names(schema=schema))
        if table_name not in table_names:
            return
        columns = {
            str(column["name"])
            for column in inspector.get_columns(table_name, schema=schema)
        }
        missing = {
            "total_requests": "INTEGER",
            "source": "TEXT DEFAULT 'codex2api'",
            "error": "TEXT DEFAULT ''",
            "last_request_at": "TIMESTAMP WITH TIME ZONE",
        }
        missing = {name: ddl for name, ddl in missing.items() if name not in columns}
        backend = str(database_engine.url.get_backend_name()).lower()
        if backend == "postgresql":
            with database_engine.begin() as conn:
                for name, ddl in missing.items():
                    conn.exec_driver_sql(
                        "ALTER TABLE operations_billing_snapshots "
                        f"ADD COLUMN IF NOT EXISTS {name} {ddl}"
                    )
                conn.exec_driver_sql(
                    "UPDATE operations_billing_snapshots SET source = 'codex2api' "
                    "WHERE source IS NULL OR BTRIM(source) = ''"
                )
                conn.exec_driver_sql(
                    "UPDATE operations_billing_snapshots SET error = '' "
                    "WHERE error IS NULL"
                )
                _compact_postgresql_billing_snapshots(conn)
                existing_unique = conn.execute(
                    text(
                        "SELECT indexrelid::regclass::text, indisunique "
                        "FROM pg_index JOIN pg_class "
                        "ON pg_class.oid = pg_index.indexrelid "
                        "WHERE pg_class.relname = :index_name"
                    ),
                    {"index_name": _OPERATIONS_BILLING_SNAPSHOT_UNIQUE_INDEX},
                ).first()
                if existing_unique is not None and not bool(existing_unique[1]):
                    conn.exec_driver_sql(
                        "DROP INDEX IF EXISTS "
                        "uq_operations_billing_snapshot_target_remote"
                    )
                conn.exec_driver_sql(
                    "CREATE UNIQUE INDEX IF NOT EXISTS "
                    "uq_operations_billing_snapshot_target_remote "
                    "ON operations_billing_snapshots (target_id, remote_id)"
                )
            return
        if not missing:
            return
        else:
            # Keep the fallback useful for dialects with PostgreSQL-like
            # additive ALTER support; unsupported dialects surface their
            # normal startup error instead of silently losing snapshots.
            with database_engine.begin() as conn:
                for name, ddl in missing.items():
                    conn.exec_driver_sql(
                        "ALTER TABLE operations_billing_snapshots "
                        f"ADD COLUMN {name} {ddl}"
                    )
    except Exception as exc:
        # The main schema creation path has historically been best-effort for
        # optional operations tables.  Billing readers already fall back to
        # API/local memory when a rolling migration is in progress; preserving
        # startup here keeps that compatibility while the next boot retries.
        _LOGGER.warning(
            "operations billing snapshot schema migration deferred: %s",
            type(exc).__name__,
        )
        return


def _migrate_codex_inventory_snapshot_relational_schema(database_engine) -> None:
    """Upgrade inventory snapshots and enforce their target/remote key.

    PostgreSQL deployments created before the inventory natural-key constraint
    was added can contain duplicate rows just like the SQLite installations.
    Compact those rows before creating the unique index.  The operation is
    deliberately idempotent and also runs when all additive columns already
    exist; otherwise a partially applied prior migration would never repair
    the missing constraint.
    """

    table_name = "codex_inventory_snapshots"
    try:
        inspector = sqlalchemy_inspect(database_engine)
        schema = getattr(inspector, "default_schema_name", None)
        table_names = set(inspector.get_table_names(schema=schema))
        if table_name not in table_names:
            return
        columns = {
            str(column["name"])
            for column in inspector.get_columns(table_name, schema=schema)
        }
        missing = {
            "summary_json": "TEXT DEFAULT '{}'",
            "fetched_at": "TIMESTAMP WITH TIME ZONE",
            "source_updated_at": "TEXT DEFAULT ''",
            "missing": "BOOLEAN DEFAULT FALSE",
            "error": "TEXT DEFAULT ''",
            "created_at": "TIMESTAMP WITH TIME ZONE",
            "updated_at": "TIMESTAMP WITH TIME ZONE",
        }
        missing = {name: ddl for name, ddl in missing.items() if name not in columns}
        backend = str(database_engine.url.get_backend_name()).lower()
        if backend == "postgresql":
            with database_engine.begin() as conn:
                for name, ddl in missing.items():
                    conn.exec_driver_sql(
                        "ALTER TABLE codex_inventory_snapshots "
                        f"ADD COLUMN IF NOT EXISTS {name} {ddl}"
                    )
                conn.exec_driver_sql(
                    "UPDATE codex_inventory_snapshots SET source_updated_at = '' "
                    "WHERE source_updated_at IS NULL"
                )
                conn.exec_driver_sql(
                    "UPDATE codex_inventory_snapshots SET error = '' "
                    "WHERE error IS NULL"
                )
                conn.exec_driver_sql(
                    "UPDATE codex_inventory_snapshots SET missing = FALSE "
                    "WHERE missing IS NULL"
                )
                _compact_postgresql_inventory_snapshots(conn)
                existing_unique = conn.execute(
                    text(
                        "SELECT indexrelid::regclass::text, indisunique "
                        "FROM pg_index JOIN pg_class "
                        "ON pg_class.oid = pg_index.indexrelid "
                        "WHERE pg_class.relname = :index_name"
                    ),
                    {"index_name": _CODEX_INVENTORY_SNAPSHOT_UNIQUE_INDEX},
                ).first()
                if existing_unique is not None and not bool(existing_unique[1]):
                    conn.exec_driver_sql(
                        "DROP INDEX IF EXISTS uq_codex_inventory_target_remote"
                    )
                conn.exec_driver_sql(
                    "CREATE UNIQUE INDEX IF NOT EXISTS "
                    "uq_codex_inventory_target_remote "
                    "ON codex_inventory_snapshots (target_id, remote_id)"
                )
            return
        if not missing:
            return
        with database_engine.begin() as conn:
            for name, ddl in missing.items():
                conn.exec_driver_sql(
                    "ALTER TABLE codex_inventory_snapshots "
                    f"ADD COLUMN {name} {ddl}"
                )
    except Exception as exc:
        _LOGGER.warning(
            "codex inventory snapshot schema migration deferred: %s",
            type(exc).__name__,
        )
        return


def _migrate_codex_target_relational_schema(database_engine) -> None:
    """Add inventory-specific target markers on non-SQLite control DBs."""

    table_name = "codex2api_targets"
    try:
        inspector = sqlalchemy_inspect(database_engine)
        schema = getattr(inspector, "default_schema_name", None)
        if table_name not in set(inspector.get_table_names(schema=schema)):
            return
        columns = {
            str(column["name"])
            for column in inspector.get_columns(table_name, schema=schema)
        }
        missing = {
            "inventory_last_sync_at": "TIMESTAMP WITH TIME ZONE",
            "inventory_last_error": "TEXT DEFAULT ''",
        }
        missing = {name: ddl for name, ddl in missing.items() if name not in columns}
        if not missing:
            return
        backend = str(database_engine.url.get_backend_name()).lower()
        with database_engine.begin() as conn:
            for name, ddl in missing.items():
                suffix = " IF NOT EXISTS" if backend == "postgresql" else ""
                conn.exec_driver_sql(
                    "ALTER TABLE codex2api_targets ADD COLUMN"
                    + suffix
                    + f" {name} {ddl}"
                )
            conn.exec_driver_sql(
                "UPDATE codex2api_targets SET inventory_last_error = '' "
                "WHERE inventory_last_error IS NULL"
            )
    except Exception as exc:
        _LOGGER.warning(
            "codex target inventory schema migration deferred: %s",
            type(exc).__name__,
        )
        return


def _compact_postgresql_billing_snapshots(conn) -> None:
    """Merge legacy duplicate rows before creating the PostgreSQL key."""

    groups = conn.execute(
        text(
            "SELECT target_id, remote_id FROM operations_billing_snapshots "
            "WHERE target_id IS NOT NULL AND remote_id IS NOT NULL "
            "GROUP BY target_id, remote_id HAVING COUNT(*) > 1"
        )
    ).fetchall()
    for target_id, remote_id in groups:
        rows = conn.execute(
            text(
                "SELECT id, total_billed_micros, total_requests, today_date, "
                "today_billed_micros, today_requests, history_json, "
                "captured_at, source, error, last_request_at "
                "FROM operations_billing_snapshots "
                "WHERE target_id = :target_id AND remote_id = :remote_id "
                "ORDER BY captured_at DESC NULLS LAST, id DESC FOR UPDATE"
            ),
            {"target_id": target_id, "remote_id": remote_id},
        ).mappings().all()
        if len(rows) < 2:
            continue
        winner = dict(rows[0])
        losers = rows[1:]
        updates: dict[str, object] = {}
        for field in ("total_billed_micros", "total_requests"):
            values = [row[field] for row in rows if row[field] is not None]
            if values:
                try:
                    updates[field] = max(int(value) for value in values)
                except (TypeError, ValueError):
                    pass
        winner_day = str(winner.get("today_date") or "").strip()
        if not winner_day:
            for row in rows:
                candidate_day = str(row.get("today_date") or "").strip()
                if candidate_day:
                    winner_day = candidate_day
                    updates["today_date"] = candidate_day
                    if row.get("today_billed_micros") is not None:
                        updates["today_billed_micros"] = row["today_billed_micros"]
                    if row.get("today_requests") is not None:
                        updates["today_requests"] = row["today_requests"]
                    break
        if winner_day:
            for row in losers:
                if str(row.get("today_date") or "").strip() != winner_day:
                    continue
                for field in ("today_billed_micros", "today_requests"):
                    value = row.get(field)
                    if value is None:
                        continue
                    current = updates.get(field, winner.get(field))
                    try:
                        if current is None or int(value) > int(current):
                            updates[field] = int(value)
                    except (TypeError, ValueError):
                        continue
        # Union history days, preferring the newest row while filling omitted
        # fields from older snapshots.
        history_by_day: dict[str, dict] = {}
        for row in reversed(rows):
            try:
                history = json.loads(row.get("history_json") or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                history = []
            if not isinstance(history, list):
                continue
            for item in history:
                if not isinstance(item, dict):
                    continue
                day = str(item.get("date") or "").strip()
                if not day:
                    continue
                current = history_by_day.setdefault(day, {})
                for field, value in item.items():
                    if value not in (None, "", [], {}):
                        if field in {"account_billed", "billed_usd", "requests", "tokens"} and field in current:
                            try:
                                if Decimal(str(value)) > Decimal(str(current[field])):
                                    current[field] = value
                                continue
                            except (InvalidOperation, TypeError, ValueError):
                                pass
                        current[field] = value
        if history_by_day:
            updates["history_json"] = json.dumps(
                [history_by_day[day] for day in sorted(history_by_day)],
                ensure_ascii=False,
                separators=(",", ":"),
            )
        latest_request = None
        latest_request_value = None
        for row in rows:
            parsed = _coerce_snapshot_timestamp(row.get("last_request_at"))
            if parsed is not None and (latest_request is None or parsed > latest_request):
                latest_request = parsed
                latest_request_value = row.get("last_request_at")
        if latest_request_value is not None:
            updates["last_request_at"] = latest_request_value
        if updates:
            assignments = ", ".join(f"{field} = :{field}" for field in updates)
            params = dict(updates)
            params["id"] = winner["id"]
            conn.execute(
                text(
                    "UPDATE operations_billing_snapshots SET "
                    + assignments
                    + " WHERE id = :id"
                ),
                params,
            )
        for loser in losers:
            conn.execute(
                text("DELETE FROM operations_billing_snapshots WHERE id = :id"),
                {"id": loser["id"]},
            )


def _compact_postgresql_inventory_snapshots(conn) -> None:
    """Merge duplicate PostgreSQL inventory rows before key creation."""

    groups = conn.execute(
        text(
            "SELECT target_id, remote_id FROM codex_inventory_snapshots "
            "WHERE target_id IS NOT NULL AND remote_id IS NOT NULL "
            "GROUP BY target_id, remote_id HAVING COUNT(*) > 1"
        )
    ).fetchall()
    for target_id, remote_id in groups:
        raw_rows = conn.execute(
            text(
                "SELECT id, target_id, remote_id, summary_json, fetched_at, "
                "source_updated_at, missing, error, created_at, updated_at "
                "FROM codex_inventory_snapshots "
                "WHERE target_id = :target_id AND remote_id = :remote_id "
                "ORDER BY fetched_at DESC NULLS LAST, updated_at DESC NULLS LAST, id DESC "
                "FOR UPDATE"
            ),
            {"target_id": target_id, "remote_id": remote_id},
        ).mappings().all()
        rows = [dict(row) for row in raw_rows]
        if len(rows) < 2:
            continue
        ordered = sorted(rows, key=_inventory_snapshot_sort_key, reverse=True)
        winner = ordered[0]
        losers = ordered[1:]
        updates: dict[str, object] = {}
        merged_summary = _merge_inventory_summary_missing(ordered)
        if merged_summary != "{}" and _inventory_summary_dict(merged_summary) != _inventory_summary_dict(
            winner.get("summary_json")
        ):
            updates["summary_json"] = merged_summary

        latest_source = None
        latest_source_value = None
        for row in ordered:
            parsed = _coerce_snapshot_timestamp(row.get("source_updated_at"))
            if parsed is not None and (latest_source is None or parsed > latest_source):
                latest_source = parsed
                latest_source_value = row.get("source_updated_at")
        if latest_source_value is not None:
            current_source = _coerce_snapshot_timestamp(winner.get("source_updated_at"))
            if current_source is None or latest_source > current_source:
                updates["source_updated_at"] = latest_source_value

        oldest_created = None
        oldest_created_value = None
        for row in ordered:
            parsed = _coerce_snapshot_timestamp(row.get("created_at"))
            if parsed is not None and (oldest_created is None or parsed < oldest_created):
                oldest_created = parsed
                oldest_created_value = row.get("created_at")
        if oldest_created_value is not None:
            current_created = _coerce_snapshot_timestamp(winner.get("created_at"))
            if current_created is None or oldest_created < current_created:
                updates["created_at"] = oldest_created_value

        newest_updated = None
        newest_updated_value = None
        for row in ordered:
            parsed = _coerce_snapshot_timestamp(row.get("updated_at"))
            if parsed is not None and (newest_updated is None or parsed > newest_updated):
                newest_updated = parsed
                newest_updated_value = row.get("updated_at")
        if newest_updated_value is not None:
            current_updated = _coerce_snapshot_timestamp(winner.get("updated_at"))
            if current_updated is None or newest_updated > current_updated:
                updates["updated_at"] = newest_updated_value

        if updates:
            assignments = ", ".join(f"{field} = :{field}" for field in updates)
            params = dict(updates)
            params["id"] = winner["id"]
            conn.execute(
                text(
                    "UPDATE codex_inventory_snapshots SET "
                    + assignments
                    + " WHERE id = :id"
                ),
                params,
            )
        for loser in losers:
            conn.execute(
                text("DELETE FROM codex_inventory_snapshots WHERE id = :id"),
                {"id": loser["id"]},
            )


def init_account_pool_schema(database_engine=None) -> None:
    """Create account-pool tables and apply additive legacy migrations.

    The project intentionally has no external migration dependency.  This
    helper is safe to call from tests, startup, and a rolling deployment: it
    only creates missing tables/columns and never rewrites credential data.
    """

    target_engine = database_engine or engine
    # The operations snapshot models are imported by ``main`` in production,
    # but standalone workers and tests may call this initializer directly.
    # Register them before ``create_all`` so durable billing tables always
    # exist regardless of the entry point that boots the control plane.
    from core import operations_models as _operations_models  # noqa: F401
    SQLModel.metadata.create_all(target_engine)

    if target_engine.url.get_backend_name() != "sqlite":
        _migrate_operations_billing_snapshot_relational_schema(target_engine)
        _migrate_codex_inventory_snapshot_relational_schema(target_engine)
        _migrate_codex_target_relational_schema(target_engine)
        return

    with target_engine.begin() as conn:
        existing_tables = {
            str(row[0])
            for row in conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }

        _migrate_operations_billing_snapshot_schema(conn, existing_tables)
        _migrate_codex_inventory_snapshot_schema(conn, existing_tables)
        account_table = conn.exec_driver_sql(
            "PRAGMA table_info('accounts')"
        ).fetchall()
        if account_table:
            account_columns = {str(row[1]) for row in account_table}
            if "identity_id" not in account_columns:
                conn.exec_driver_sql(
                    "ALTER TABLE accounts ADD COLUMN identity_id TEXT DEFAULT ''"
                )
            if "purchase_cost_cents" not in account_columns:
                conn.exec_driver_sql(
                    "ALTER TABLE accounts ADD COLUMN purchase_cost_cents INTEGER"
                )
            if "account_source" not in account_columns:
                conn.exec_driver_sql(
                    "ALTER TABLE accounts ADD COLUMN account_source TEXT DEFAULT 'local'"
                )
            # Rows created by older inventory syncs only carried this marker
            # inside extra_json. Promote it once so list/stat paths have one
            # durable source of truth.
            if "extra_json" in account_columns:
                conn.exec_driver_sql(
                    "UPDATE accounts SET account_source = CASE "
                    "WHEN lower(extra_json) LIKE '%\"remote_only\":true%' "
                    "OR lower(extra_json) LIKE '%\"remote_only\": true%' "
                    "THEN 'codex2api' "
                    "WHEN account_source IS NULL OR trim(account_source) = '' "
                    "THEN 'local' ELSE account_source END"
                )
            else:
                conn.exec_driver_sql(
                    "UPDATE accounts SET account_source = 'local' "
                    "WHERE account_source IS NULL OR trim(account_source) = ''"
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
            for column, sql_type in (
                ("inventory_last_sync_at", "DATETIME"),
                ("inventory_last_error", "TEXT DEFAULT ''"),
            ):
                if column not in target_columns:
                    conn.exec_driver_sql(
                        "ALTER TABLE codex2api_targets "
                        f"ADD COLUMN {column} {sql_type}"
                    )
            conn.exec_driver_sql(
                "UPDATE codex2api_targets SET inventory_last_error = '' "
                "WHERE inventory_last_error IS NULL"
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
                _OPERATIONS_BILLING_SNAPSHOT_UNIQUE_INDEX,
                "operations_billing_snapshots",
                "target_id, remote_id",
                "",
            ),
            (
                _CODEX_INVENTORY_SNAPSHOT_UNIQUE_INDEX,
                "codex_inventory_snapshots",
                "target_id, remote_id",
                "",
            ),
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
            if index_name in {
                _OPERATIONS_BILLING_SNAPSHOT_UNIQUE_INDEX,
                _CODEX_INVENTORY_SNAPSHOT_UNIQUE_INDEX,
            }:
                # A partially-created legacy table may exist without the
                # natural-key columns.  The additive migration deliberately
                # leaves such a table untouched so startup diagnostics can
                # report it; do not turn that diagnostic into a hard startup
                # failure by issuing an index statement against missing
                # columns.
                natural_key_columns = {
                    str(row[1])
                    for row in conn.exec_driver_sql(
                        f"PRAGMA table_info('{table_name}')"
                    ).fetchall()
                }
                if not {"target_id", "remote_id"} <= natural_key_columns:
                    continue
                # ``IF NOT EXISTS`` does not upgrade an old same-named
                # non-unique index.  Drop that specific index so the following
                # statement can enforce the natural key.
                legacy_index = next(
                    (
                        row
                        for row in conn.exec_driver_sql(
                            f"PRAGMA index_list('{table_name}')"
                        ).fetchall()
                        if str(row[1]) == index_name
                    ),
                    None,
                )
                if legacy_index is not None:
                    legacy_columns = [
                        str(row[2])
                        for row in conn.exec_driver_sql(
                            f"PRAGMA index_info('{index_name}')"
                        ).fetchall()
                    ]
                    if not bool(legacy_index[2]) or legacy_columns != [
                        "target_id",
                        "remote_id",
                    ]:
                        conn.exec_driver_sql(f"DROP INDEX {index_name}")
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
    from services.account_purchase_costs import ensure_legacy_purchase_costs

    with Session(engine) as session:
        ensure_legacy_purchase_costs(session)
        session.commit()
    ensure_default_target(engine)
    ensure_default_pools(engine)
    from core.sms_pool import SmsPoolService

    sms_pool = SmsPoolService(engine)
    sms_pool.recover_interrupted()
    sms_pool.recover_stale_active()


def get_session():
    with Session(engine) as session:
        yield session
