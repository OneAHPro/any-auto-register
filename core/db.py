"""数据库模型 - SQLite via SQLModel"""
from datetime import datetime, timezone
import os
from typing import Optional
from sqlalchemy import delete, event, func
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
    status: str        # success | failed
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
    enabled: bool = True
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
            .where(AccountModel.platform == account.platform)
            .where(AccountModel.email == account.email)
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
            session.delete(account)
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
        if "account_type" not in existing_columns:
            conn.exec_driver_sql(
                "ALTER TABLE outlook_accounts ADD COLUMN account_type TEXT DEFAULT 'microsoft_oauth'"
            )
        if "mailapi_url" not in existing_columns:
            conn.exec_driver_sql(
                "ALTER TABLE outlook_accounts ADD COLUMN mailapi_url TEXT DEFAULT ''"
            )
        conn.exec_driver_sql(
            "UPDATE outlook_accounts SET account_type = 'microsoft_oauth' WHERE account_type IS NULL OR TRIM(account_type) = ''"
        )
        conn.exec_driver_sql(
            "UPDATE outlook_accounts SET mailapi_url = '' WHERE mailapi_url IS NULL"
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
    _recover_chatgpt_attempt_bindings()
    from core.sms_pool import SmsPoolService

    SmsPoolService(engine).recover_interrupted()


def get_session():
    with Session(engine) as session:
        yield session
