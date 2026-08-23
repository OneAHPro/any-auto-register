"""数据库模型 - SQLite via SQLModel"""
from datetime import datetime, timezone
import os
from typing import Optional
from sqlalchemy import delete, event, func, update
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


def save_account_with_creation_state(account) -> tuple['AccountModel', bool]:
    """Save an account and atomically report whether this call inserted it."""
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
            if (
                account.platform == "chatgpt"
                and incoming_extra.get("chatgpt_token_source")
                == "existing_account_web_login"
            ):
                merged_extra = existing.get_extra()
                for key, value in incoming_extra.items():
                    if value in (None, "") and key in merged_extra:
                        continue
                    merged_extra[key] = value
                if incoming_extra.get("phone_oauth_ready") is False:
                    merged_extra.pop("oauth_resume_context", None)
                incoming_extra = merged_extra
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
            return existing, False
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
        return m, True


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
        row = session.exec(
            select(ChatGPTMfaRotationJournalModel).where(
                ChatGPTMfaRotationJournalModel.email == normalized_email
            )
        ).first()
        if row is None:
            row = ChatGPTMfaRotationJournalModel(
                email=normalized_email,
                totp_secret=normalized_secret,
            )
        else:
            row.totp_secret = normalized_secret
            row.recovery_code = ""
            row.status = "staged"
            row.rotated_at = ""
            row.updated_at = _utcnow()
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
                    state="available",
                    enabled=True,
                    lease_owner="",
                    lease_expires_at=None,
                    lease_version=version + 1,
                    bound_account_id=0,
                    bound_at=None,
                    quarantine_reason="",
                    last_error="",
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


def init_db():
    SQLModel.metadata.create_all(engine)
    _migrate_outlook_accounts_schema()
    recover_expired_outlook_leases()
    _migrate_chatgpt_auth_state_schema()
    _recover_chatgpt_attempt_bindings()
    from core.sms_pool import SmsPoolService

    sms_pool = SmsPoolService(engine)
    sms_pool.recover_interrupted()
    sms_pool.recover_stale_active()


def get_session():
    with Session(engine) as session:
        yield session
