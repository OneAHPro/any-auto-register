from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest


class FakeCursor:
    def __init__(self, connection: "FakeConnection", rows_by_marker: dict[str, list], descriptions: dict[str, list] | None = None):
        self.connection = connection
        self.rows_by_marker = rows_by_marker
        self.descriptions = descriptions or {}
        self.marker = ""
        self.description = None
        self.closed = False

    def execute(self, query, params=()):
        self.connection.executed.append((query, tuple(params or ())))
        normalized = " ".join(str(query).split()).lower()
        if normalized.startswith("select 1"):
            self.marker = "probe"
        elif "from accounts" in normalized:
            self.marker = "accounts"
        elif "from usage_logs" in normalized and "group by account_id" in normalized:
            self.marker = "history" if "date_trunc" in normalized or "group by account_id, day" in normalized else "summary"
        elif "from usage_logs" in normalized:
            self.marker = "history"
        else:
            self.marker = "settings"
        self.description = self.descriptions.get(self.marker)

    def fetchone(self):
        rows = self.rows_by_marker.get(self.marker, [])
        return rows[0] if rows else None

    def fetchall(self):
        return list(self.rows_by_marker.get(self.marker, []))

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, rows_by_marker: dict[str, list], descriptions: dict[str, list] | None = None):
        self.rows_by_marker = rows_by_marker
        self.descriptions = descriptions or {}
        self.executed: list[tuple[str, tuple]] = []
        self.closed = False
        self.rolled_back = False
        self.readonly_calls: list[dict] = []

    def set_session(self, **kwargs):
        self.readonly_calls.append(kwargs)

    def cursor(self):
        return FakeCursor(self, self.rows_by_marker, self.descriptions)

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


def _adapter(monkeypatch, connection, **kwargs):
    from services.codex2api_db import Codex2APIDBAdapter, Codex2APIDBConfig

    config = Codex2APIDBConfig(dsn="postgresql://reader:secret@db/codex2api", **kwargs)
    return Codex2APIDBAdapter(config, connect_factory=lambda _dsn, _config: connection)


def test_config_prefers_explicit_dsn_and_redacts_password(monkeypatch):
    from services.codex2api_db import Codex2APIDBConfig

    monkeypatch.setenv("CODEX2API_DATABASE_URL", "postgresql://env:env-secret@db/envdb")
    config = Codex2APIDBConfig.from_env(
        {"CODEX2API_DATABASE_URL": "postgresql://env:env-secret@db/envdb"},
        dsn="postgresql://reader:super-secret@db/codex2api",
    )

    assert config.dsn == "postgresql://reader:super-secret@db/codex2api"
    assert config.configured is True
    assert "super-secret" not in config.redacted_dsn
    assert config.redacted_dsn.startswith("postgresql://reader:")


def test_config_without_environment_is_disabled():
    from services.codex2api_db import Codex2APIDBConfig

    config = Codex2APIDBConfig.from_env({})

    assert config.configured is False
    assert config.dsn == ""


def test_target_dsn_resolution_is_explicit_and_does_not_cross_instances(monkeypatch):
    from services.codex2api_db import database_dsn_for_target

    monkeypatch.setenv("CODEX2API_DATABASE_URL", "postgresql://shared/db")
    monkeypatch.setenv("CODEX2API_DATABASE_URL_2", "postgresql://node2/db")

    assert database_dsn_for_target(1) == "postgresql://shared/db"
    assert database_dsn_for_target(2) == "postgresql://node2/db"
    assert database_dsn_for_target(3) == ""

    monkeypatch.setenv("CODEX2API_DATABASE_TARGET_ID", "3")
    assert database_dsn_for_target(1) == ""
    assert database_dsn_for_target(3) == "postgresql://shared/db"


def test_config_accepts_libpq_keyword_dsn_and_normalizes_safe_options():
    from services.codex2api_db import Codex2APIDBConfig

    config = Codex2APIDBConfig.from_env(
        {
            "CODEX2API_DATABASE_URL": "host=db dbname=codex2api user=reader password=secret",
            "CODEX2API_DATABASE_CONNECT_TIMEOUT": "0",
            "CODEX2API_DATABASE_STATEMENT_TIMEOUT_MS": "bad",
        }
    )

    assert config.configured is True
    assert config.connect_timeout == 1
    assert config.statement_timeout_ms == 15_000
    assert "password=***" in config.redacted_dsn


def test_database_timeouts_are_bounded():
    from services.codex2api_db import Codex2APIDBConfig

    config = Codex2APIDBConfig.from_env({
        "CODEX2API_DATABASE_URL": "postgresql://reader@db/codex2api",
        "CODEX2API_DATABASE_CONNECT_TIMEOUT": "9999",
        "CODEX2API_DATABASE_STATEMENT_TIMEOUT_MS": "9999999",
    })
    assert config.connect_timeout == 60
    assert config.statement_timeout_ms == 120_000


def test_probe_sets_connection_read_only_and_reports_server_health():
    from services.codex2api_db import Codex2APIDBAdapter

    connection = FakeConnection({"probe": [(1,)]})
    adapter = _adapter(None, connection)

    result = adapter.probe()

    assert result["available"] is True
    assert result["read_only"] is True
    assert result["source"] == "postgresql"
    assert connection.readonly_calls == [{"readonly": True, "autocommit": False}]
    assert connection.closed is True
    assert connection.rolled_back is True
    assert any(query.lower().startswith("select 1") for query, _ in connection.executed)


def test_probe_reports_schema_error_when_required_relation_check_fails():
    from services.codex2api_db import Codex2APIDBAdapter, Codex2APIDBConfig

    class SchemaFailCursor(FakeCursor):
        def execute(self, query, params=()):
            super().execute(query, params)
            if "from usage_logs" in str(query).lower():
                raise RuntimeError("permission denied for relation usage_logs")

    class SchemaFailConnection(FakeConnection):
        def cursor(self):
            return SchemaFailCursor(self, self.rows_by_marker, self.descriptions)

    connection = SchemaFailConnection({"probe": [(1,)]})
    adapter = Codex2APIDBAdapter(
        Codex2APIDBConfig(dsn="postgresql://reader@db/codex2api"),
        connect_factory=lambda *_: connection,
    )

    result = adapter.probe()

    assert result["status"] == "schema_error"
    assert result["available"] is False


def test_batch_fetch_returns_target_remote_key_and_aggregates_usage_without_email_join():
    descriptions = {
        "accounts": [
            ("remote_id",), ("name",), ("platform",), ("account_type",),
            ("credentials",), ("status",), ("enabled",), ("locked",),
            ("cooldown_reason",), ("cooldown_until",), ("created_at",),
            ("updated_at",), ("deleted_at",),
        ],
        "summary": [
            ("account_id",), ("total_requests",), ("total_tokens",),
            ("total_account_billed",), ("total_user_billed",),
            ("today_requests",), ("today_tokens",), ("today_account_billed",),
            ("today_user_billed",), ("last_request_at",),
        ],
        "history": [
            ("account_id",), ("day",), ("requests",), ("tokens",),
            ("account_billed",), ("user_billed",),
        ],
    }
    connection = FakeConnection(
        {
            "accounts": [
                (
                    77,
                    "remote label",
                    "openai",
                    "oauth",
                    {
                        "email": "Same@Example.com",
                        "account_id": "chatgpt-account-77",
                        "workspace_id": "workspace-77",
                        "plan_type": "pro",
                        "codex_7d_used_percent": 42,
                        "codex_7d_reset_at": "2026-09-10T00:00:00Z",
                        "codex_credits": {"balance": "0", "has_credits": False},
                    },
                    "active",
                    True,
                    False,
                    "",
                    None,
                    datetime(2026, 9, 1, tzinfo=timezone.utc),
                    datetime(2026, 9, 9, tzinfo=timezone.utc),
                    None,
                )
            ],
            "summary": [
                (
                    77,
                    3,
                    1200,
                    12.5,
                    12.5,
                    1,
                    300,
                    1.25,
                    1.25,
                    datetime(2026, 9, 9, 1, 2, 3, tzinfo=timezone.utc),
                )
            ],
                "history": [
                    (77, date(2026, 9, 8), 2, 900, 11.25, 11.25),
                    (77, date(2026, 9, 9), 1, 300, 1.25, 1.25),
                ],
                "settings": [(True,)],
            },
        descriptions,
    )
    adapter = _adapter(None, connection, timezone_name="Asia/Shanghai")

    result = adapter.fetch_account_snapshots(
        [(3, 77), (3, 77)],
        as_of=datetime(2026, 9, 9, 4, tzinfo=timezone.utc),
    )

    assert list(result) == [(3, 77)]
    row = result[(3, 77)]
    assert row["remote_id"] == 77
    assert row["target_id"] == 3
    assert row["email"] == "Same@Example.com"
    assert row["account_id"] == "chatgpt-account-77"
    assert row["workspace_id"] == "workspace-77"
    assert row["status"] == "active"
    assert row["plan_type"] == "pro"
    assert row["quota"]["7d_used_percent"] == 42.0
    assert row["quota"]["7d_reset_at"] == "2026-09-10T00:00:00Z"
    assert row["total"]["requests"] == 3
    assert row["total"]["billed_usd"] == 12.5
    assert row["today"]["requests"] == 1
    assert row["today"]["billed_usd"] == 1.25
    assert row["today"]["date"] == "2026-09-09"
    assert row["total_account_billed"] == 12.5
    assert row["today_account_billed"] == 1.25
    assert row["history"] == [
        {"date": "2026-09-08", "requests": 2, "tokens": 900, "billed_usd": 11.25, "user_billed_usd": 11.25},
        {"date": "2026-09-09", "requests": 1, "tokens": 300, "billed_usd": 1.25, "user_billed_usd": 1.25},
    ]
    all_sql = " ".join(query for query, _ in connection.executed).lower()
    assert "join usage_logs" not in all_sql
    assert " where a.id in (" in all_sql
    assert "email" not in all_sql.split("from usage_logs", 1)[-1]
    assert "trim(coalesce(l.internal_reason, '')) = ''" in all_sql
    account_sql = next(query for query, _ in connection.executed if "from accounts" in query.lower() and "where a.id in" in query.lower())
    normalized_account_sql = " ".join(account_sql.lower().split())
    assert "lower(coalesce(a.credentials->>'upstream_type', '')) not in ('grok', 'antigravity', 'claude')" in normalized_account_sql
    assert "jsonb_build_object" in normalized_account_sql
    assert "a.credentials," not in normalized_account_sql
    summary_query, summary_params = next(
        (query, params)
        for query, params in connection.executed
        if "COUNT(*) FILTER" in query
    )
    assert summary_params[-1].astimezone(timezone.utc) == datetime(2026, 9, 9, 16, tzinfo=timezone.utc)
    history_query, history_params = next(
        (query, params)
        for query, params in connection.executed
        if "AT TIME ZONE" in query
    )
    assert history_params[-1].astimezone(timezone.utc) == datetime(2026, 9, 9, 16, tzinfo=timezone.utc)


def test_batch_fetch_uses_literal_statement_timeout_before_read_queries():
    connection = FakeConnection({"accounts": [], "summary": [], "history": []})
    adapter = _adapter(None, connection, statement_timeout_ms=1234)

    adapter.fetch_account_snapshots([(1, 7)])

    assert any("SET LOCAL statement_timeout = 1234" in query for query, _ in connection.executed)


def test_batch_fetch_can_skip_history_for_fast_summary_reads():
    descriptions = {
        "accounts": [("remote_id",), ("name",), ("platform",), ("account_type",),
                     ("credentials",), ("status",), ("enabled",), ("locked",),
                     ("cooldown_reason",), ("cooldown_until",), ("created_at",),
                     ("updated_at",), ("deleted_at",)],
        "summary": [("account_id",), ("total_requests",), ("total_tokens",),
                    ("total_account_billed",), ("total_user_billed",),
                    ("today_requests",), ("today_tokens",), ("today_account_billed",),
                    ("today_user_billed",), ("last_request_at",)],
    }
    connection = FakeConnection(
        {
            "accounts": [(7, "", "openai", "oauth", {}, "active", True, False, "", None, None, None, None)],
            "summary": [(7, 0, 0, 0, 0, 0, 0, 0, 0, None)],
        },
        descriptions,
    )
    adapter = _adapter(None, connection)

    result = adapter.fetch_account_snapshots([(1, 7)], include_history=False)

    assert result[(1, 7)]["history"] == []
    assert not any("group by account_id, day" in query.lower() for query, _ in connection.executed)


def test_batch_fetch_rejects_mixed_target_ids_to_prevent_cross_instance_reads():
    connection = FakeConnection({})
    adapter = _adapter(None, connection)

    with pytest.raises(ValueError, match="one target"):
        adapter.fetch_account_snapshots([(1, 7), (2, 8)])

    assert connection.executed == []


@pytest.mark.parametrize(
    "key",
    [[(True, 7)], [(1, True)], [(1.5, 7)], [(1, 7.5)], ["17"], [(1, 7, 9)]],
)
def test_batch_fetch_rejects_non_integer_primary_key_values(key):
    connection = FakeConnection({})
    adapter = _adapter(None, connection)

    with pytest.raises(ValueError, match="integer pairs"):
        adapter.fetch_account_snapshots(key)

    assert connection.executed == []


def test_missing_postgres_driver_is_reported_as_unavailable_without_raising(monkeypatch):
    from services import codex2api_db as module

    adapter = module.Codex2APIDBAdapter(module.Codex2APIDBConfig(dsn="postgresql://db"))
    monkeypatch.setattr(module, "_load_driver", lambda: (_ for _ in ()).throw(ImportError("driver missing")))

    probe = adapter.probe()
    snapshots = adapter.fetch_account_snapshots([(1, 7)])

    assert probe["available"] is False
    assert probe["status"] == "dependency_missing"
    assert snapshots == {}
    assert adapter.last_status["status"] == "dependency_missing"


def test_unconfigured_adapter_is_unavailable_and_empty_fetch_is_side_effect_free(monkeypatch):
    from services import codex2api_db as module

    called = []
    monkeypatch.setattr(module, "_load_driver", lambda: called.append(True))
    adapter = module.Codex2APIDBAdapter(module.Codex2APIDBConfig())

    assert adapter.probe()["status"] == "not_configured"
    assert adapter.fetch_account_snapshots([]) == {}
    assert called == []


def test_psycopg2_connection_receives_only_the_dsn_and_timeout(monkeypatch):
    from services import codex2api_db as module

    calls = []

    class Driver:
        @staticmethod
        def connect(*args, **kwargs):
            calls.append((args, kwargs))
            return FakeConnection({})

    monkeypatch.setattr(module, "_load_driver", lambda: ("psycopg2", Driver))
    adapter = module.Codex2APIDBAdapter(
        module.Codex2APIDBConfig(
            dsn="postgresql://reader:secret@db/codex2api",
            connect_timeout=9,
        )
    )

    connection = adapter._connect()

    assert calls == [
        (("postgresql://reader:secret@db/codex2api",), {"connect_timeout": 9}),
    ]
    connection.close()


def test_connection_errors_do_not_echo_the_database_password(monkeypatch):
    from services import codex2api_db as module

    class Driver:
        @staticmethod
        def connect(*args, **kwargs):
            raise RuntimeError("connect failed for postgresql://reader:secret@db/codex2api")

    monkeypatch.setattr(module, "_load_driver", lambda: ("psycopg", Driver))
    adapter = module.Codex2APIDBAdapter(
        module.Codex2APIDBConfig(dsn="postgresql://reader:secret@db/codex2api")
    )

    result = adapter.probe()

    assert result["status"] == "connection_error"
    assert "secret" not in result["error"]


def test_fetch_account_metadata_reads_all_accounts_without_usage_queries_or_credentials():
    """The inventory path should get a credential-free account projection."""
    descriptions = {
        "accounts": [
            ("remote_id",), ("name",), ("platform",), ("account_type",),
            ("credentials",), ("status",), ("enabled",), ("locked",),
            ("cooldown_reason",), ("cooldown_until",), ("created_at",),
            ("updated_at",), ("deleted_at",),
        ],
    }
    connection = FakeConnection(
        {
            "accounts": [
                (
                    77,
                    "remote label",
                    "openai",
                    "oauth",
                    {
                        "email": "Same@Example.com",
                        "account_id": "chatgpt-account-77",
                        "workspace_id": "workspace-77",
                        "plan_type": "pro",
                        "codex_7d_used_percent": 42,
                        "codex_7d_reset_at": "2026-09-10T00:00:00Z",
                        "codex_credits": {
                            "balance": "0",
                            "access_token": "DO_NOT_RETURN",
                        },
                        "refresh_token": "DO_NOT_RETURN",
                    },
                    "active",
                    True,
                    False,
                    "",
                    None,
                    datetime(2026, 9, 1, tzinfo=timezone.utc),
                    datetime(2026, 9, 9, tzinfo=timezone.utc),
                    None,
                )
            ],
        },
        descriptions,
    )
    adapter = _adapter(None, connection, target_id=3)

    rows = adapter.fetch_account_metadata()

    assert len(rows) == 1
    row = rows[0]
    assert row["target_id"] == 3
    assert row["remote_id"] == 77
    assert row["email"] == "Same@Example.com"
    assert row["account_id"] == row["chatgpt_account_id"] == "chatgpt-account-77"
    assert row["workspace_id"] == row["effective_workspace_id"] == "workspace-77"
    assert row["status"] == row["remote_status"] == "active"
    assert row["quota"]["7d_used_percent"] == 42.0
    assert row["quota"]["7d_reset_at"] == "2026-09-10T00:00:00Z"
    assert row["quota"]["credits"] == {"balance": "0"}
    assert row["source"] == "codex2api_postgresql"
    assert adapter.last_status["available"] is True
    all_sql = " ".join(query for query, _ in connection.executed).lower()
    assert "from usage_logs" not in all_sql
    assert "from accounts" in all_sql
    assert "do_not_return" not in repr(rows).lower()


def test_fetch_account_metadata_matches_codex_active_account_scope():
    descriptions = {
        "accounts": [
            ("remote_id",), ("name",), ("platform",), ("account_type",),
            ("credentials",), ("status",), ("enabled",), ("locked",),
            ("cooldown_reason",), ("cooldown_until",), ("created_at",),
            ("updated_at",), ("deleted_at",),
        ],
    }
    connection = FakeConnection({"accounts": []}, descriptions)
    adapter = _adapter(None, connection, target_id=1)

    adapter.fetch_account_metadata()

    query = next(query for query, _ in connection.executed if "from accounts" in query.lower())
    normalized = " ".join(query.lower().split())
    assert "where a.status <> 'deleted'" in normalized
    assert "coalesce(a.error_message, '') <> 'deleted'" in normalized
    assert "lower(coalesce(a.credentials->>'upstream_type', '')) not in ('grok', 'antigravity', 'claude')" in normalized


def test_metadata_rejects_a_target_override_that_crosses_the_configured_database():
    connection = FakeConnection({"accounts": []})
    adapter = _adapter(None, connection, target_id=1)

    assert adapter.fetch_account_metadata(target_id=2) == []
    assert adapter.last_status["status"] == "invalid_target"
    assert connection.executed == []


def test_metadata_rejects_unknown_channel_instead_of_using_codex_scope():
    connection = FakeConnection({"accounts": []})
    adapter = _adapter(None, connection, target_id=1)

    assert adapter.fetch_account_metadata(channel="antigravity") == []
    assert adapter.last_status["status"] == "invalid_channel"
    assert connection.executed == []


def test_metadata_projects_persisted_rate_limit_into_remote_status():
    descriptions = {
        "accounts": [
            ("remote_id",), ("name",), ("platform",), ("account_type",),
            ("credentials",), ("status",), ("enabled",), ("locked",),
            ("cooldown_reason",), ("cooldown_until",), ("created_at",),
            ("updated_at",), ("deleted_at",),
        ],
    }
    connection = FakeConnection({
        "accounts": [(
            7, "", "openai", "oauth", {"email": "rate@example.com"},
            "active", True, False, "responses_rate_limited",
            datetime(2026, 9, 10, tzinfo=timezone.utc), None, None, None,
        )],
    }, descriptions)
    adapter = _adapter(None, connection, target_id=1)

    rows = adapter.fetch_account_metadata()

    assert rows[0]["remote_status"] == "rate_limited"


def test_database_text_projection_decodes_bytes_from_non_utf8_dbapi_rows():
    descriptions = {
        "accounts": [
            ("remote_id",), ("name",), ("platform",), ("account_type",),
            ("credentials",), ("status",), ("enabled",), ("locked",),
            ("cooldown_reason",), ("cooldown_until",), ("created_at",),
            ("updated_at",), ("deleted_at",),
        ],
    }
    connection = FakeConnection(
        {
            "accounts": [(
                9, b"remote-name", b"openai", b"oauth",
                {"email": b"bytes@example.com", "account_id": b"bytes-id"},
                b"active", True, False, b"", None, None, None, None,
            )],
        },
        descriptions,
    )
    adapter = _adapter(None, connection, target_id=1)

    rows = adapter.fetch_account_metadata()

    assert rows[0]["name"] == "remote-name"
    assert rows[0]["platform"] == "openai"
    assert rows[0]["status"] == rows[0]["remote_status"] == "active"
    assert rows[0]["email"] == "bytes@example.com"
    assert rows[0]["account_id"] == "bytes-id"


def test_integer_metrics_do_not_round_large_postgresql_bigints_through_float():
    from services.codex2api_db import _int_value

    assert _int_value("9007199254740993") == 9007199254740993
