import json

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError
from sqlmodel import create_engine

from core import db


def _columns(engine, table_name: str) -> set[str]:
    return {str(column["name"]) for column in inspect(engine).get_columns(table_name)}


def test_operations_billing_snapshot_declares_aggregate_provenance_columns():
    engine = create_engine("sqlite://")

    db.init_account_pool_schema(engine)

    assert {
        "total_requests",
        "source",
        "error",
        "last_request_at",
    } <= _columns(engine, "operations_billing_snapshots")


def test_target_inventory_marker_columns_are_migrated_separately_from_health_state():
    engine = create_engine("sqlite://")
    db.init_account_pool_schema(engine)
    assert {
        "inventory_last_sync_at",
        "inventory_last_error",
    } <= _columns(engine, "codex2api_targets")


def test_operations_snapshot_schema_migrates_legacy_rows_incrementally():
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE operations_billing_snapshots ("
            "id INTEGER PRIMARY KEY, target_id INTEGER NOT NULL, remote_id INTEGER NOT NULL, "
            "total_billed_micros INTEGER, today_date TEXT DEFAULT '', "
            "today_billed_micros INTEGER, today_requests INTEGER, "
            "history_json TEXT DEFAULT '[]', captured_at DATETIME"
            ")"
        )
        connection.exec_driver_sql(
            "INSERT INTO operations_billing_snapshots "
            "(id, target_id, remote_id, total_billed_micros, today_requests) "
            "VALUES (1, 4, 19, 12345000, 7)"
        )

    db.init_account_pool_schema(engine)
    db.init_account_pool_schema(engine)

    assert {
        "total_requests",
        "source",
        "error",
        "last_request_at",
    } <= _columns(engine, "operations_billing_snapshots")
    with engine.connect() as connection:
        row = connection.exec_driver_sql(
            "SELECT total_billed_micros, today_requests, total_requests, "
            "source, error, last_request_at "
            "FROM operations_billing_snapshots WHERE id = 1"
        ).one()

    assert row == (12345000, 7, None, "codex2api", "", None)


def test_operations_snapshot_migration_preserves_existing_provenance_values():
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE operations_billing_snapshots ("
            "id INTEGER PRIMARY KEY, target_id INTEGER NOT NULL, remote_id INTEGER NOT NULL, "
            "total_billed_micros INTEGER, today_date TEXT DEFAULT '', "
            "today_billed_micros INTEGER, today_requests INTEGER, history_json TEXT DEFAULT '[]', "
            "captured_at DATETIME, total_requests INTEGER, source TEXT, error TEXT, "
            "last_request_at DATETIME"
            ")"
        )
        connection.exec_driver_sql(
            "INSERT INTO operations_billing_snapshots "
            "(id, target_id, remote_id, total_requests, source, error, last_request_at) "
            "VALUES (2, 4, 20, 42, 'postgresql', 'timeout', '2026-09-08T03:04:05+00:00')"
        )

    db.init_account_pool_schema(engine)

    with engine.connect() as connection:
        row = connection.exec_driver_sql(
            "SELECT total_requests, source, error, last_request_at "
            "FROM operations_billing_snapshots WHERE id = 2"
        ).one()

    assert row == (42, "postgresql", "timeout", "2026-09-08T03:04:05+00:00")


def test_operations_snapshot_migration_compacts_duplicates_and_keeps_history():
    """Legacy writers may have emitted two rows for one remote account.

    The newest capture remains the canonical row, while history and monotonic
    counters from an older row are carried forward before the unique index is
    installed.
    """
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE operations_billing_snapshots ("
            "id INTEGER PRIMARY KEY, target_id INTEGER NOT NULL, remote_id INTEGER NOT NULL, "
            "total_billed_micros INTEGER, today_date TEXT DEFAULT '', "
            "today_billed_micros INTEGER, today_requests INTEGER, "
            "history_json TEXT DEFAULT '[]', captured_at DATETIME"
            ")"
        )
        connection.exec_driver_sql(
            "INSERT INTO operations_billing_snapshots "
            "(id, target_id, remote_id, total_billed_micros, today_date, "
            "today_billed_micros, today_requests, history_json, captured_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                10,
                7,
                41,
                18_000_000,
                "2026-09-08",
                2_000_000,
                4,
                json.dumps([
                    {"date": "2026-09-06", "account_billed": "1.25", "requests": 2},
                    {"date": "2026-09-07", "account_billed": "2.50", "requests": 3},
                ]),
                "2026-09-08T02:00:00+00:00",
            ),
        )
        connection.exec_driver_sql(
            "INSERT INTO operations_billing_snapshots "
            "(id, target_id, remote_id, total_billed_micros, today_date, "
            "today_billed_micros, today_requests, history_json, captured_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                11,
                7,
                41,
                15_000_000,
                "2026-09-08",
                None,
                None,
                json.dumps([
                    {"date": "2026-09-08", "account_billed": "3.75", "requests": 5},
                ]),
                "2026-09-09T02:00:00+00:00",
            ),
        )

    db.init_account_pool_schema(engine)
    # Running startup migration again must not create another row or alter the
    # selected representative.
    db.init_account_pool_schema(engine)

    with engine.connect() as connection:
        rows = connection.exec_driver_sql(
            "SELECT id, total_billed_micros, today_billed_micros, today_requests, "
            "history_json FROM operations_billing_snapshots "
            "WHERE target_id = 7 AND remote_id = 41"
        ).fetchall()
        indexes = {
            row[1]
            for row in connection.exec_driver_sql(
                "PRAGMA index_list('operations_billing_snapshots')"
            )
        }

    assert len(rows) == 1
    row = rows[0]
    assert row[0] == 11  # newest captured_at wins
    assert row[1] == 18_000_000  # monotonic total survives the compaction
    assert row[2:] == (2_000_000, 4, json.dumps([
        {"date": "2026-09-06", "account_billed": "1.25", "requests": 2},
        {"date": "2026-09-07", "account_billed": "2.50", "requests": 3},
        {"date": "2026-09-08", "account_billed": "3.75", "requests": 5},
    ], ensure_ascii=False, separators=(",", ":")))
    assert "uq_operations_billing_snapshot_target_remote" in indexes

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO operations_billing_snapshots "
                "(target_id, remote_id) VALUES (7, 41)"
            )


def test_operations_snapshot_migration_uses_id_when_capture_time_is_missing():
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE operations_billing_snapshots ("
            "id INTEGER PRIMARY KEY, target_id INTEGER NOT NULL, remote_id INTEGER NOT NULL, "
            "total_billed_micros INTEGER, today_date TEXT DEFAULT '', "
            "today_billed_micros INTEGER, today_requests INTEGER, "
            "history_json TEXT DEFAULT '[]', captured_at DATETIME"
            ")"
        )
        connection.exec_driver_sql(
            "INSERT INTO operations_billing_snapshots "
            "(id, target_id, remote_id, total_billed_micros) VALUES (3, 9, 90, 3)"
        )
        connection.exec_driver_sql(
            "INSERT INTO operations_billing_snapshots "
            "(id, target_id, remote_id, total_billed_micros) VALUES (4, 9, 90, 4)"
        )

    db.init_account_pool_schema(engine)

    with engine.connect() as connection:
        row = connection.exec_driver_sql(
            "SELECT id, total_billed_micros FROM operations_billing_snapshots "
            "WHERE target_id = 9 AND remote_id = 90"
        ).one()
    assert row == (4, 4)


def test_codex_inventory_snapshot_migration_compacts_duplicates_and_adds_key():
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE codex_inventory_snapshots ("
            "id INTEGER PRIMARY KEY, target_id INTEGER NOT NULL, remote_id INTEGER NOT NULL, "
            "summary_json TEXT DEFAULT '{}', fetched_at DATETIME, "
            "source_updated_at TEXT DEFAULT '', missing INTEGER DEFAULT 0, "
            "error TEXT DEFAULT '', created_at DATETIME, updated_at DATETIME"
            ")"
        )
        # A partially-applied legacy upgrade could create the intended index
        # name without uniqueness. The initializer must replace it rather than
        # letting ``IF NOT EXISTS`` silently keep the weak index.
        connection.exec_driver_sql(
            "CREATE INDEX uq_codex_inventory_target_remote "
            "ON codex_inventory_snapshots (target_id, remote_id)"
        )
        connection.exec_driver_sql(
            "INSERT INTO codex_inventory_snapshots "
            "(id, target_id, remote_id, summary_json, fetched_at, "
            "source_updated_at, missing, error, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                4,
                2,
                17,
                json.dumps({
                    "email": "inventory@example.com",
                    "workspace_id": "workspace-17",
                    "quota": {"7d": 42},
                }),
                "2026-09-08T01:00:00+00:00",
                "2026-09-08T00:59:00+00:00",
                0,
                "",
                "2026-09-01T00:00:00+00:00",
                "2026-09-08T01:00:00+00:00",
            ),
        )
        connection.exec_driver_sql(
            "INSERT INTO codex_inventory_snapshots "
            "(id, target_id, remote_id, summary_json, fetched_at, "
            "source_updated_at, missing, error, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                5,
                2,
                17,
                json.dumps({
                    "email": "inventory@example.com",
                    "plan_type": "pro",
                    "quota": {"5h": 9},
                }),
                "2026-09-09T01:00:00+00:00",
                "2026-09-09T00:59:00+00:00",
                0,
                "",
                "2026-09-02T00:00:00+00:00",
                "2026-09-09T01:00:00+00:00",
            ),
        )

    db.init_account_pool_schema(engine)
    db.init_account_pool_schema(engine)

    with engine.connect() as connection:
        rows = connection.exec_driver_sql(
            "SELECT id, summary_json, missing, error, source_updated_at "
            "FROM codex_inventory_snapshots WHERE target_id = 2 AND remote_id = 17"
        ).fetchall()
        indexes = {
            row[1]: bool(row[2])
            for row in connection.exec_driver_sql(
                "PRAGMA index_list('codex_inventory_snapshots')"
            )
        }

    assert len(rows) == 1
    row = rows[0]
    assert row[0] == 5  # newest valid fetched_at wins
    summary = json.loads(row[1])
    assert summary["plan_type"] == "pro"
    assert summary["email"] == "inventory@example.com"
    assert summary["workspace_id"] == "workspace-17"
    assert summary["quota"] == {"5h": 9, "7d": 42}
    assert row[2:4] == (0, "")
    assert row[4] == "2026-09-09T00:59:00+00:00"
    assert indexes["uq_codex_inventory_target_remote"] is True

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO codex_inventory_snapshots "
                "(target_id, remote_id) VALUES (2, 17)"
            )


def test_codex_inventory_snapshot_migration_prefers_valid_row_over_newer_error():
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE codex_inventory_snapshots ("
            "id INTEGER PRIMARY KEY, target_id INTEGER NOT NULL, remote_id INTEGER NOT NULL, "
            "summary_json TEXT DEFAULT '{}', fetched_at DATETIME, missing INTEGER DEFAULT 0, "
            "error TEXT DEFAULT ''"
            ")"
        )
        connection.exec_driver_sql(
            "INSERT INTO codex_inventory_snapshots "
            "(id, target_id, remote_id, summary_json, fetched_at, missing, error) "
            "VALUES (1, 3, 22, '{\"status\":\"active\"}', ?, 0, '')",
            ("2026-09-08T00:00:00+00:00",),
        )
        connection.exec_driver_sql(
            "INSERT INTO codex_inventory_snapshots "
            "(id, target_id, remote_id, summary_json, fetched_at, missing, error) "
            "VALUES (2, 3, 22, '{\"status\":\"unknown\"}', ?, 1, 'timeout')",
            ("2026-09-09T00:00:00+00:00",),
        )

    db.init_account_pool_schema(engine)

    with engine.connect() as connection:
        row = connection.exec_driver_sql(
            "SELECT id, summary_json, missing, error FROM codex_inventory_snapshots "
            "WHERE target_id = 3 AND remote_id = 22"
        ).one()
    assert row == (1, '{"status":"active"}', 0, '')


def test_codex_inventory_snapshot_migration_keeps_newer_missing_observation():
    """A complete sync can validly report an account as missing."""
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE codex_inventory_snapshots ("
            "id INTEGER PRIMARY KEY, target_id INTEGER NOT NULL, remote_id INTEGER NOT NULL, "
            "summary_json TEXT DEFAULT '{}', fetched_at DATETIME, missing INTEGER DEFAULT 0, "
            "error TEXT DEFAULT ''"
            ")"
        )
        connection.exec_driver_sql(
            "INSERT INTO codex_inventory_snapshots "
            "(id, target_id, remote_id, summary_json, fetched_at, missing, error) "
            "VALUES (1, 3, 23, '{\"status\":\"active\"}', ?, 0, '')",
            ("2026-09-08T00:00:00+00:00",),
        )
        connection.exec_driver_sql(
            "INSERT INTO codex_inventory_snapshots "
            "(id, target_id, remote_id, summary_json, fetched_at, missing, error) "
            "VALUES (2, 3, 23, '{\"status\":\"active\"}', ?, 1, '')",
            ("2026-09-09T00:00:00+00:00",),
        )

    db.init_account_pool_schema(engine)

    with engine.connect() as connection:
        row = connection.exec_driver_sql(
            "SELECT id, missing, error FROM codex_inventory_snapshots "
            "WHERE target_id = 3 AND remote_id = 23"
        ).one()
    assert row == (2, 1, '')


def test_codex_inventory_snapshot_migration_keeps_latest_error_diagnostic():
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE codex_inventory_snapshots ("
            "id INTEGER PRIMARY KEY, target_id INTEGER NOT NULL, remote_id INTEGER NOT NULL, "
            "summary_json TEXT DEFAULT '{}', fetched_at DATETIME, missing INTEGER DEFAULT 0, "
            "error TEXT DEFAULT ''"
            ")"
        )
        connection.exec_driver_sql(
            "INSERT INTO codex_inventory_snapshots "
            "(id, target_id, remote_id, summary_json, fetched_at, missing, error) "
            "VALUES (1, 3, 24, '{\"status\":\"active\"}', ?, 0, 'timeout')",
            ("2026-09-08T00:00:00+00:00",),
        )
        connection.exec_driver_sql(
            "INSERT INTO codex_inventory_snapshots "
            "(id, target_id, remote_id, summary_json, fetched_at, missing, error) "
            "VALUES (2, 3, 24, '{\"status\":\"failed\"}', ?, 0, 'connection reset')",
            ("2026-09-09T00:00:00+00:00",),
        )

    db.init_account_pool_schema(engine)

    with engine.connect() as connection:
        row = connection.exec_driver_sql(
            "SELECT id, summary_json, error FROM codex_inventory_snapshots "
            "WHERE target_id = 3 AND remote_id = 24"
        ).one()
    assert row == (2, '{"status":"failed"}', 'connection reset')


def test_malformed_operations_snapshot_table_does_not_abort_startup():
    """Startup diagnostics must remain reachable for a partial legacy table."""
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE operations_billing_snapshots ("
            "id INTEGER PRIMARY KEY, total_billed_micros INTEGER"
            ")"
        )

    # The migration cannot infer a natural key for this table, but it should
    # leave it available for diagnostics rather than issuing an invalid index
    # statement and preventing the application from booting.
    db.init_account_pool_schema(engine)

    assert {"id", "total_billed_micros"} <= _columns(
        engine, "operations_billing_snapshots"
    )
