import json
from datetime import datetime

from sqlmodel import Session, SQLModel, create_engine
from sqlalchemy.pool import StaticPool
from unittest.mock import Mock

from api.accounts import (
    _account_operational_summary,
    _hide_missing_remote_only_accounts,
    list_accounts,
)
from core.db import (
    AccountAssignmentModel,
    AccountModel,
    Codex2APITargetModel,
    CodexInventorySnapshotModel,
    init_account_pool_schema,
)


def _live_test_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    init_account_pool_schema(engine)
    return engine


def _remote_row(**overrides):
    row = {
        "target_id": 1,
        "remote_id": 90210,
        "email": "remote@example.com",
        "chatgpt_account_id": "remote-account",
        "effective_workspace_id": "remote-workspace",
        "plan_type": "pro",
        "remote_status": "active",
        "enabled": True,
        "locked": False,
        "usage_percent_7d": 42,
        "billed_7d": 18.75,
        "usage_7d_requests": 12,
        "updated_at": "2026-09-06T00:00:00+00:00",
    }
    row.update(overrides)
    return row


class _InventoryClient:
    def __init__(self, rows):
        self.rows = rows
        self.calls = 0

    def list_accounts(self):
        self.calls += 1
        return self.rows


def test_account_list_includes_full_filtered_operational_summary():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    rows = [
        AccountModel(platform="chatgpt", email="normal@example.com", password="p", status="registered", extra_json='{"account_type":"chatgpt_password"}'),
        AccountModel(platform="chatgpt", email="limited@example.com", password="p", status="registered", extra_json='{"account_type":"chatgpt_password","remote_status":"rate_limited","quota":{"window":"5h"}}'),
        AccountModel(platform="chatgpt", email="invalid@example.com", password="p", status="invalid", extra_json='{"account_type":"chatgpt_password"}'),
        AccountModel(platform="chatgpt", email="error@example.com", password="p", status="registered", extra_json='{"account_type":"chatgpt_password","chatgpt_local":{"auth":{"state":"probe_failed"}}}'),
    ]
    with Session(engine) as session:
        session.add_all(rows)
        session.commit()
        result = list_accounts(platform="chatgpt", page=1, page_size=1, session=session)

    assert result["total"] == 4
    assert result["summary"] == {
        "total": 4,
        "normal": 1,
        "scheduling": 0,
        "rate_limited": 1,
        "rate_limited_5h": 1,
        "rate_limited_7d": 0,
        "abnormal": 2,
        "auth_invalid": 1,
        "errors": 1,
    }


def test_account_summary_uses_control_plane_assignments_and_expired_statuses():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    rows = [
        AccountModel(
            platform="chatgpt",
            email="draining@example.com",
            password="p",
            status="registered",
            identity_id="identity-draining",
            extra_json='{"account_type":"chatgpt_password"}',
        ),
        AccountModel(
            platform="chatgpt",
            email="expired@example.com",
            password="p",
            status="expired",
            extra_json='{"account_type":"chatgpt_password"}',
        ),
    ]
    with Session(engine) as session:
        session.add_all(rows)
        session.add(
            AccountAssignmentModel(
                identity_id="identity-draining",
                local_account_id=0,
                pool_id="PUBLIC_POOL",
                target_id=1,
                state="draining",
                lease_owner="migration-worker",
            )
        )
        session.commit()
        result = list_accounts(platform="chatgpt", page=1, page_size=1, session=session)

    assert result["total"] == 2
    assert result["summary"]["scheduling"] == 1
    assert result["summary"]["abnormal"] == 1
    assert result["summary"]["errors"] == 1


def test_account_summary_includes_planned_assignment_states_as_scheduling():
    engine = _live_test_engine()
    account = AccountModel(
        platform="chatgpt",
        email="planned@example.com",
        password="p",
        status="registered",
        identity_id="identity-planned",
        extra_json='{"account_type":"chatgpt_password"}',
    )
    with Session(engine) as session:
        session.add(account)
        session.add(
            AccountAssignmentModel(
                identity_id="identity-planned",
                local_account_id=0,
                pool_id="PUBLIC_POOL",
                target_id=1,
                state="planned",
            )
        )
        session.commit()
        result = list_accounts(platform="chatgpt", page=1, page_size=20, session=session)

    assert result["summary"]["scheduling"] == 1
    assert result["summary"]["normal"] == 0


def test_account_summary_marks_disabled_remote_rows_as_errors():
    result = _account_operational_summary(
        [],
        [
            {
                "identity_id": "codex2api:1:42",
                "status": "active",
                "remote_enabled": "false",
                "remote_locked": False,
            }
        ],
        {},
    )

    assert result["total"] == 1
    assert result["abnormal"] == 1
    assert result["errors"] == 1


def test_account_summary_prioritizes_auth_invalid_over_generic_error():
    result = _account_operational_summary(
        [],
        [
            {
                "status": "unauthorized",
                "remote_enabled": True,
                "remote_locked": False,
            }
        ],
        {},
    )

    assert result["abnormal"] == 1
    assert result["auth_invalid"] == 1
    assert result["errors"] == 0

    banned = _account_operational_summary(
        [],
        [{"status": "active", "chatgpt_display": {"remote_status": "banned_like"}}],
        {},
    )
    assert banned["abnormal"] == 1
    assert banned["errors"] == 1


def test_account_summary_finds_assignment_by_local_account_id_for_legacy_rows():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    account = AccountModel(
        platform="chatgpt",
        email="legacy-assignment@example.com",
        password="p",
        status="registered",
        extra_json='{"account_type":"chatgpt_password"}',
    )
    with Session(engine) as session:
        session.add(account)
        session.flush()
        session.add(
            AccountAssignmentModel(
                identity_id="legacy-assignment-identity",
                local_account_id=int(account.id or 0),
                pool_id="PUBLIC_POOL",
                target_id=1,
                state="draining",
            )
        )
        session.commit()
        result = list_accounts(platform="chatgpt", page=1, page_size=1, session=session)

    assert result["summary"]["scheduling"] == 1
    assert result["summary"]["normal"] == 0


def test_account_summary_groups_all_quota_limit_status_variants():
    for status, expected_window in (
        ("rate_limited", "7d"),
        ("rate_limited_5h", "5h"),
        ("rate_limited_7d", "7d"),
        ("usage_exhausted", "7d"),
        ("usage_limited", "7d"),
        ("quota_paused", "7d"),
    ):
        result = _account_operational_summary(
            [],
            [{"status": status}],
            {},
        )
        assert result["normal"] == 0
        assert result["rate_limited"] == 1
        assert result[f"rate_limited_{expected_window}"] == 1


def test_account_summary_reads_persisted_remote_snapshot_without_live_display():
    account = AccountModel(
        platform="chatgpt",
        email="snapshot-limit@example.com",
        password="",
        status="registered",
        extra_json=json.dumps({
            "account_type": "chatgpt_password",
            "remote_only": True,
            "remote_target_id": 1,
            "remote_id": 77,
            "codex_remote_snapshot": {
                "remote_status": "rate_limited_5h",
                "usage_percent_5h": 100,
            },
        }),
    )
    result = _account_operational_summary([account], [], {})

    assert result["normal"] == 0
    assert result["rate_limited"] == 1
    assert result["rate_limited_5h"] == 1


def test_account_summary_marks_a_stale_inventory_snapshot_as_an_error():
    account = AccountModel(
        platform="chatgpt",
        email="stale-snapshot@example.com",
        password="",
        status="registered",
        extra_json=json.dumps({
            "remote_only": True,
            "remote_target_id": 1,
            "remote_id": 77,
            "codex_remote_snapshot": {
                "remote_status": "active",
                "_inventory_stale": True,
                "_inventory_error": "target unavailable",
            },
        }),
    )
    result = _account_operational_summary([account], [], {})

    assert result["normal"] == 0
    assert result["abnormal"] == 1
    assert result["errors"] == 1


def test_account_summary_keeps_persisted_invalid_status_when_live_probe_is_active():
    account = AccountModel(
        platform="chatgpt",
        email="invalid-local@example.com",
        password="p",
        status="invalid",
        extra_json='{"account_type":"chatgpt_password"}',
    )
    result = _account_operational_summary(
        [account],
        [],
        {None: {"remote_status": "active"}},
    )

    assert result["abnormal"] == 1
    assert result["auth_invalid"] == 1
    assert result["normal"] == 0


def test_account_summary_marks_missing_or_failed_live_rows_as_errors():
    for quota_status in ("error", "not_found"):
        account = AccountModel(
            platform="chatgpt",
            email=f"{quota_status}@example.com",
            password="p",
            status="registered",
            extra_json='{"account_type":"chatgpt_password"}',
        )
        result = _account_operational_summary(
            [account],
            [],
            {None: {"quota_status": quota_status, "remote_status": None}},
        )
        assert result["abnormal"] == 1
        assert result["errors"] == 1


def test_list_accounts_summary_includes_remote_rows_before_pagination(monkeypatch):
    engine = _live_test_engine()
    local = AccountModel(
        platform="chatgpt",
        email="local@example.com",
        password="p",
        status="registered",
        extra_json='{"account_type":"chatgpt_password"}',
    )
    monkeypatch.setattr(
        "services.chatgpt_codex2api_health.fetch_codex2api_quota_accounts",
        lambda **kwargs: [_remote_row(email="remote@example.com", remote_id=99)],
    )
    with Session(engine) as session:
        session.add(local)
        session.commit()
        result = list_accounts(
            platform="chatgpt",
            page=1,
            page_size=1,
            include_live=True,
            session=session,
        )

    assert len(result["items"]) == 1
    assert result["total"] == 2
    assert result["summary"]["total"] == 2
    assert sum(result["summary"][key] for key in ("normal", "scheduling", "rate_limited", "abnormal")) == 2


def test_list_accounts_summary_respects_email_filter(monkeypatch):
    engine = _live_test_engine()
    rows = [
        AccountModel(
            platform="chatgpt",
            email="one@example.com",
            password="p",
            status="registered",
            extra_json='{"account_type":"chatgpt_password"}',
        ),
        AccountModel(
            platform="chatgpt",
            email="two@example.com",
            password="p",
            status="invalid",
            extra_json='{"account_type":"chatgpt_password"}',
        ),
    ]
    with Session(engine) as session:
        session.add_all(rows)
        session.commit()
        result = list_accounts(
            platform="chatgpt",
            email="two@",
            page=1,
            page_size=1,
            session=session,
        )

    assert result["total"] == 1
    assert result["summary"]["total"] == 1
    assert result["summary"]["abnormal"] == 1
    assert result["summary"]["auth_invalid"] == 1


def test_list_accounts_summary_respects_subscription_plan_filter_without_remote_fetch(monkeypatch):
    engine = _live_test_engine()
    rows = [
        AccountModel(
            platform="chatgpt",
            email="pro@example.com",
            password="p",
            status="registered",
            extra_json='{"account_type":"chatgpt_password","codex_remote_snapshot":{"email":"pro@example.com","remote_status":"active","plan_type":"pro","usage_percent_7d":10}}',
        ),
        AccountModel(
            platform="chatgpt",
            email="plus@example.com",
            password="p",
            status="registered",
            extra_json='{"account_type":"chatgpt_password","codex_remote_snapshot":{"email":"plus@example.com","remote_status":"active","plan_type":"plus","usage_percent_7d":20}}',
        ),
    ]
    def unexpected_fetch(**kwargs):
        raise AssertionError("snapshot-backed rows should not fetch live data")

    monkeypatch.setattr(
        "services.chatgpt_codex2api_health.fetch_codex2api_quota_accounts",
        unexpected_fetch,
    )
    with Session(engine) as session:
        session.add_all(rows)
        session.commit()
        result = list_accounts(
            platform="chatgpt",
            subscription_plan="plus",
            page=1,
            page_size=1,
            include_live=True,
            session=session,
        )

    assert result["total"] == 1
    assert result["items"][0]["email"] == "plus@example.com"
    assert result["summary"]["total"] == 1
    assert result["summary"]["normal"] == 1


def test_list_accounts_summary_marks_live_probe_failure_as_error(monkeypatch):
    engine = _live_test_engine()
    account = AccountModel(
        platform="chatgpt",
        email="probe-error@example.com",
        password="p",
        status="registered",
        extra_json='{"account_type":"chatgpt_password"}',
    )
    def fail_fetch(**kwargs):
        raise RuntimeError("fixture probe failure")

    monkeypatch.setattr(
        "services.chatgpt_codex2api_health.fetch_codex2api_quota_accounts",
        fail_fetch,
    )
    with Session(engine) as session:
        session.add(account)
        session.commit()
        result = list_accounts(
            platform="chatgpt",
            page=1,
            page_size=1,
            include_live=True,
            session=session,
        )

    assert result["summary"]["total"] == 1
    assert result["summary"]["abnormal"] == 1
    assert result["summary"]["errors"] == 1


def test_list_accounts_summary_marks_unmatched_live_account_as_error(monkeypatch):
    engine = _live_test_engine()
    account = AccountModel(
        platform="chatgpt",
        email="missing-remote@example.com",
        password="p",
        status="registered",
        extra_json='{"account_type":"chatgpt_password"}',
    )
    monkeypatch.setattr(
        "services.chatgpt_codex2api_health.fetch_codex2api_quota_accounts",
        lambda **kwargs: [_remote_row(email="different@example.com", remote_id=100)],
    )
    with Session(engine) as session:
        session.add(account)
        session.commit()
        result = list_accounts(
            platform="chatgpt",
            page=1,
            page_size=20,
            include_live=True,
            session=session,
        )

    local_item = next(item for item in result["items"] if item["email"] == "missing-remote@example.com")
    assert local_item["chatgpt_display"]["quota_status"] == "not_found"
    assert result["summary"]["abnormal"] == 1
    assert result["summary"]["errors"] == 1


def test_refresh_live_syncs_inventory_before_rendering_the_account_list(monkeypatch):
    engine = _live_test_engine()
    initial = AccountModel(
        platform="chatgpt",
        email="initial@example.com",
        password="p",
        status="registered",
        extra_json='{"account_type":"chatgpt_password","codex_remote_snapshot":{"email":"initial@example.com","remote_status":"active","plan_type":"pro"}}',
    )
    sync = Mock(return_value={"targets": 1, "errors": 0})
    materialize = Mock()

    def materialize_after_sync(database_engine):
        with Session(database_engine) as session:
            session.add(
                AccountModel(
                    platform="chatgpt",
                    email="new@example.com",
                    password="",
                    status="registered",
                    extra_json='{"account_source":"codex2api","remote_only":true,"codex_remote_snapshot":{"email":"new@example.com","remote_status":"active","plan_type":"pro"}}',
                )
            )
            session.commit()
        return {"created": 1, "updated": 0, "total": 1}

    materialize.side_effect = materialize_after_sync
    monkeypatch.setattr("services.codex_inventory.sync_inventory", sync)
    monkeypatch.setattr("services.codex_inventory.materialize_inventory", materialize)
    with Session(engine) as session:
        session.add(initial)
        session.commit()
        result = list_accounts(
            platform="chatgpt",
            page=1,
            page_size=20,
            include_live=True,
            refresh_live=True,
            session=session,
        )

    assert sync.call_count == 1
    assert materialize.call_count == 1
    assert {item["email"] for item in result["items"]} == {"initial@example.com", "new@example.com"}


def test_refresh_live_reconciles_new_remote_rows_and_hides_missing_remote_only_rows(monkeypatch):
    engine = _live_test_engine()
    client = _InventoryClient([
        _remote_row(remote_id=2, email="current@example.com", remote_status="active"),
    ])
    stale = AccountModel(
        platform="chatgpt",
        email="stale@example.com",
        password="",
        status="registered",
        identity_id="codex2api:1:1",
        extra_json=json.dumps({
            "account_source": "codex2api",
            "remote_only": True,
            "remote_target_id": 1,
            "remote_id": 1,
            "codex_remote_snapshot": _remote_row(remote_id=1, email="stale@example.com"),
        }),
    )
    with Session(engine) as session:
        session.add(
            Codex2APITargetModel(
                id=1,
                name="default",
                base_url="https://codex2api.example",
                admin_key_ref="test-key",
                enabled=True,
            )
        )
        session.add(stale)
        session.commit()

    monkeypatch.setattr("services.codex2api_target_client.get_target_client", lambda *_args: client)
    with Session(engine) as session:
        result = list_accounts(
            platform="chatgpt",
            page=1,
            page_size=20,
            include_live=True,
            refresh_live=True,
            session=session,
        )

    assert client.calls == 1
    assert {item["email"] for item in result["items"]} == {"current@example.com"}
    assert result["summary"]["total"] == 1


def test_cached_requests_keep_missing_remote_only_rows_hidden_after_a_refresh(monkeypatch):
    engine = _live_test_engine()
    client = _InventoryClient([_remote_row(remote_id=2, email="current@example.com")])
    stale = AccountModel(
        platform="chatgpt",
        email="stale-cached@example.com",
        password="",
        status="registered",
        identity_id="codex2api:1:1",
        extra_json=json.dumps({
            "account_source": "codex2api",
            "remote_only": True,
            "remote_target_id": 1,
            "remote_id": 1,
            "codex_remote_snapshot": _remote_row(remote_id=1, email="stale-cached@example.com"),
        }),
    )
    with Session(engine) as session:
        session.add(Codex2APITargetModel(
            id=1,
            name="default",
            base_url="https://codex2api.example",
            admin_key_ref="test-key",
            enabled=True,
        ))
        session.add(stale)
        session.commit()
    monkeypatch.setattr("services.codex2api_target_client.get_target_client", lambda *_args: client)
    with Session(engine) as session:
        list_accounts(platform="chatgpt", include_live=True, refresh_live=True, session=session)
    with Session(engine) as session:
        cached = list_accounts(platform="chatgpt", include_live=True, refresh_live=False, session=session)

    assert {item["email"] for item in cached["items"]} == {"current@example.com"}


def test_missing_local_snapshot_is_reported_as_not_found_after_a_complete_sync(monkeypatch):
    engine = _live_test_engine()
    local = AccountModel(
        platform="chatgpt",
        email="gone-local@example.com",
        password="p",
        status="registered",
        extra_json=json.dumps({
            "account_type": "chatgpt_password",
            "remote_target_id": 1,
            "remote_id": 1,
            "codex_remote_snapshot": {
                "remote_status": "active",
                "email": "gone-local@example.com",
            },
        }),
    )
    client = _InventoryClient([_remote_row(remote_id=2, email="current@example.com")])
    with Session(engine) as session:
        session.add(Codex2APITargetModel(
            id=1,
            name="default",
            base_url="https://codex2api.example",
            admin_key_ref="test-key",
            enabled=True,
        ))
        session.add(local)
        session.commit()
    monkeypatch.setattr("services.codex2api_target_client.get_target_client", lambda *_args: client)
    with Session(engine) as session:
        result = list_accounts(
            platform="chatgpt",
            page=1,
            page_size=20,
            include_live=True,
            refresh_live=True,
            session=session,
        )

    old = next(item for item in result["items"] if item["email"] == "gone-local@example.com")
    assert old["chatgpt_display"]["quota_status"] == "not_found"
    assert result["summary"]["errors"] == 1


def test_inventory_sync_failure_marks_existing_snapshot_as_unavailable(monkeypatch):
    engine = _live_test_engine()
    local = AccountModel(
        platform="chatgpt",
        email="stale-local@example.com",
        password="p",
        status="registered",
        extra_json=json.dumps({
            "account_type": "chatgpt_password",
            "remote_target_id": 1,
            "remote_id": 1,
            "codex_remote_snapshot": {
                "remote_status": "active",
                "email": "stale-local@example.com",
            },
        }),
    )
    with Session(engine) as session:
        session.add(Codex2APITargetModel(
            id=1,
            name="default",
            base_url="https://codex2api.example",
            admin_key_ref="test-key",
            enabled=True,
        ))
        session.add(local)
        session.commit()
    class FailingClient:
        def list_accounts(self):
            raise RuntimeError("offline")

    monkeypatch.setattr("services.codex2api_target_client.get_target_client", lambda *_args: FailingClient())
    from services.codex_inventory import sync_inventory
    sync_inventory(
        engine,
        target_id=1,
        clients={1: _InventoryClient([_remote_row(remote_id=1, email="stale-local@example.com")])},
    )
    with Session(engine) as session:
        result = list_accounts(
            platform="chatgpt",
            page=1,
            page_size=20,
            include_live=True,
            refresh_live=True,
            session=session,
        )

    item = result["items"][0]
    assert item["chatgpt_display"]["quota_status"] == "error"
    assert result["summary"]["errors"] == 1


def test_cached_account_display_uses_latest_inventory_summary_over_local_snapshot():
    engine = _live_test_engine()
    account = AccountModel(
        platform="chatgpt",
        email="cached@example.com",
        password="p",
        status="registered",
        identity_id="codex2api:1:42",
        extra_json=json.dumps({
            "account_type": "chatgpt_password",
            "remote_target_id": 1,
            "remote_id": 42,
            "codex_remote_snapshot": _remote_row(
                remote_id=42,
                email="cached@example.com",
                usage_percent_7d=60,
                billed_7d=130.12,
                usage_7d_requests=1848,
                updated_at="2026-09-07T01:37:06+08:00",
            ),
        }),
    )
    latest = _remote_row(
        remote_id=42,
        email="cached@example.com",
        usage_percent_7d=94,
        billed_7d=143.33,
        usage_7d_requests=2049,
        updated_at="2026-09-07T04:07:13+08:00",
        quota_7d_updated_at="2026-09-07T04:06:58+08:00",
    )
    with Session(engine) as session:
        session.add(account)
        session.add(
            CodexInventorySnapshotModel(
                target_id=1,
                remote_id=42,
                summary_json=json.dumps(latest),
                source_updated_at=latest["updated_at"],
                fetched_at=datetime.fromisoformat("2026-09-07T04:08:00+00:00"),
                missing=False,
                error="",
            )
        )
        session.commit()
        result = list_accounts(
            platform="chatgpt",
            page=1,
            page_size=20,
            include_live=True,
            refresh_live=False,
            session=session,
        )

    display = result["items"][0]["chatgpt_display"]
    assert display["quota"]["usage_percent"] == 94
    assert display["quota"]["request_count"] == 2049
    assert display["quota"]["billed_usd"] == 143.33
    assert display["live_updated_at"] == "2026-09-07T04:06:58+08:00"


def test_missing_remote_only_rows_are_hidden_only_for_targets_with_complete_inventory():
    engine = _live_test_engine()
    target_one_row = CodexInventorySnapshotModel(
        target_id=1,
        remote_id=10,
        summary_json=json.dumps({"email": "current@example.com"}),
        missing=False,
        error="",
    )
    target_two_account = AccountModel(
        platform="chatgpt",
        email="target-two@example.com",
        password="",
        status="registered",
        extra_json=json.dumps({
            "remote_only": True,
            "remote_target_id": 2,
            "remote_id": 20,
            "codex_remote_snapshot": {"remote_status": "active"},
        }),
    )
    with Session(engine) as session:
        session.add(target_one_row)
        session.add(target_two_account)
        session.commit()
        visible = _hide_missing_remote_only_accounts([target_two_account], session)

    assert [account.email for account in visible] == ["target-two@example.com"]


def test_operational_filter_changes_items_but_keeps_global_summary_counts():
    engine = _live_test_engine()
    rows = [
        AccountModel(
            platform="chatgpt",
            email="normal-filter@example.com",
            password="p",
            status="registered",
            extra_json='{"account_type":"chatgpt_password"}',
        ),
        AccountModel(
            platform="chatgpt",
            email="invalid-filter@example.com",
            password="p",
            status="invalid",
            extra_json='{"account_type":"chatgpt_password"}',
        ),
    ]
    with Session(engine) as session:
        session.add_all(rows)
        session.commit()
        result = list_accounts(
            platform="chatgpt",
            operational_status="normal",
            page=1,
            page_size=20,
            session=session,
        )

    assert [item["email"] for item in result["items"]] == ["normal-filter@example.com"]
    assert result["total"] == 1
    assert result["summary"]["total"] == 2
    assert result["summary"]["normal"] == 1
    assert result["summary"]["abnormal"] == 1
