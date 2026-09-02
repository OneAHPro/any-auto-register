import sqlite3

from sqlalchemy import inspect
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine, select

from core import db


def make_engine():
    return create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


def test_pool_tables_and_account_identity_column_are_created():
    engine = make_engine()

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
    with engine.connect() as connection:
        columns = {
            row[1]
            for row in connection.exec_driver_sql(
                "PRAGMA table_info('accounts')"
            )
        }
    assert "identity_id" in columns


def test_schema_migration_is_idempotent():
    engine = make_engine()

    db.init_account_pool_schema(engine)
    db.init_account_pool_schema(engine)

    with Session(engine) as session:
        assert session.exec(select(db.Codex2APITargetModel)).all() == []


def test_schema_migration_adds_identity_column_to_legacy_accounts_table():
    engine = make_engine()
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE accounts (id INTEGER PRIMARY KEY, platform TEXT, email TEXT)"
        )

    db.init_account_pool_schema(engine)

    with engine.connect() as connection:
        columns = {
            row[1]
            for row in connection.exec_driver_sql(
                "PRAGMA table_info('accounts')"
            )
        }
    assert "identity_id" in columns
