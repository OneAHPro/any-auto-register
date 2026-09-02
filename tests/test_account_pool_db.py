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
    with engine.connect() as connection:
        quota_columns = {
            row[1]
            for row in connection.exec_driver_sql(
                "PRAGMA table_info('account_quota_snapshots')"
            )
        }
    assert "continuous_billed_usd" in quota_columns
    with engine.connect() as connection:
        indexes = {
            row[1]
            for row in connection.exec_driver_sql(
                "PRAGMA index_list('account_target_bindings')"
            )
        }
    assert "uq_account_target_binding_identity_target" in indexes


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


def test_schema_migration_survives_duplicate_strong_aliases():
    engine = make_engine()
    db.init_account_pool_schema(engine)
    timestamp = "2026-09-02T00:00:00+00:00"
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "DROP INDEX uq_account_identity_alias_platform_type_value"
        )
        connection.exec_driver_sql(
            "INSERT INTO account_identities "
            "(id, platform, canonical_email, state, current_account_id, created_at, updated_at) "
            "VALUES ('i-1', 'chatgpt', 'one@example.com', 'active', 1, ?, ?)" ,
            (timestamp, timestamp),
        )
        connection.exec_driver_sql(
            "INSERT INTO account_identities "
            "(id, platform, canonical_email, state, current_account_id, created_at, updated_at) "
            "VALUES ('i-2', 'chatgpt', 'two@example.com', 'active', 2, ?, ?)" ,
            (timestamp, timestamp),
        )
        connection.exec_driver_sql(
            "INSERT INTO account_identity_aliases "
            "(identity_id, platform, alias_type, normalized_value, source, first_seen_at, last_seen_at) "
            "VALUES ('i-1', 'chatgpt', 'workspace_id', 'shared', '', ?, ?)"
            ,
            (timestamp, timestamp),
        )
        connection.exec_driver_sql(
            "INSERT INTO account_identity_aliases "
            "(identity_id, platform, alias_type, normalized_value, source, first_seen_at, last_seen_at) "
            "VALUES ('i-2', 'chatgpt', 'workspace_id', 'shared', '', ?, ?)"
            ,
            (timestamp, timestamp),
        )

    db.init_account_pool_schema(engine)

    with engine.connect() as connection:
        states = {
            row[0]: row[1]
            for row in connection.exec_driver_sql(
                "SELECT id, state FROM account_identities WHERE id IN ('i-1', 'i-2')"
            )
        }
    assert states == {"i-1": "ambiguous", "i-2": "ambiguous"}
