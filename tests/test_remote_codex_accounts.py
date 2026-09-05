from datetime import datetime, timezone

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine, select

from api.accounts import list_accounts
from core import db
from services.chatgpt_codex2api_health import fetch_codex2api_quota_accounts


NOW = datetime(2026, 9, 5, 7, 0, tzinfo=timezone.utc)


def make_engine():
    target_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    db.init_account_pool_schema(target_engine)
    with Session(target_engine) as session:
        session.add(
            db.Codex2APITargetModel(
                id=1,
                name="default",
                base_url="https://codex2api.example",
                admin_key_ref="key",
                default_pool_id="PUBLIC_POOL",
                enabled=True,
            )
        )
        session.commit()
    return target_engine


def remote_row(**overrides):
    row = {
        "target_id": 1,
        "remote_id": 90210,
        "email": "json-imported@example.com",
        "chatgpt_account_id": "chatgpt-acct-json",
        "effective_workspace_id": "workspace-json",
        "account_type": "oauth",
        "plan_type": "pro",
        "remote_status": "active",
        "enabled": True,
        "locked": False,
        "usage_percent_7d": 42,
        "display_billed_usd": 18.75,
        "billed_7d": None,
        "usage_7d_requests": 1234,
        "reset_7d_at": "2026-09-11T00:00:00+00:00",
        "quota_7d_updated_at": "2026-09-05T06:59:00+00:00",
        "updated_at": "2026-09-05T06:59:00+00:00",
        "created_at": "2026-09-04T10:00:00+00:00",
    }
    row.update(overrides)
    return row


def test_remote_only_accounts_are_listed_and_bound_for_scheduling(monkeypatch):
    target_engine = make_engine()
    monkeypatch.setattr(
        "services.chatgpt_codex2api_health.fetch_codex2api_quota_accounts",
        lambda **kwargs: [remote_row()],
    )

    with Session(target_engine) as session:
        result = list_accounts(
            platform="chatgpt",
            page=1,
            page_size=20,
            include_live=True,
            session=session,
        )

    assert result["total"] == 1
    item = result["items"][0]
    assert item["remote_only"] is True
    assert item["account_source"] == "codex2api"
    assert item["remote_id"] == 90210
    assert "password" not in item
    assert "token" not in item
    assert item["chatgpt_display"]["plan_type"] == "pro"
    assert item["chatgpt_display"]["quota"]["billed_usd"] == 18.75
    assert item["quota"]["7d"]["usage_percent"] == 42
    assert item["assignment"]["pool_id"] == "PUBLIC_POOL"

    with Session(target_engine) as session:
        identity = session.exec(select(db.AccountIdentityModel)).one()
        binding = session.exec(select(db.AccountTargetBindingModel)).one()
        assignment = session.exec(select(db.AccountAssignmentModel)).one()

    assert identity.canonical_email == "json-imported@example.com"
    assert identity.current_account_id == 0
    assert binding.remote_account_id == 90210
    assert binding.local_account_id == 0
    assert assignment.local_account_id == 0
    assert assignment.state == "active"


def test_remote_accounts_with_unusable_status_are_visible_but_not_active():
    target_engine = make_engine()
    monkeypatch = __import__("pytest").MonkeyPatch()
    monkeypatch.setattr(
        "services.chatgpt_codex2api_health.fetch_codex2api_quota_accounts",
        lambda **kwargs: [remote_row(remote_status="unauthorized")],
    )
    try:
        with Session(target_engine) as session:
            result = list_accounts(
                platform="chatgpt",
                page=1,
                page_size=20,
                include_live=True,
                session=session,
            )
        assert result["items"][0]["remote_only"] is True
        assert result["items"][0]["assignment"] is None
    finally:
        monkeypatch.undo()


def test_disabled_remote_accounts_are_not_promoted_to_active_assignments(monkeypatch):
    target_engine = make_engine()
    monkeypatch.setattr(
        "services.chatgpt_codex2api_health.fetch_codex2api_quota_accounts",
        lambda **kwargs: [remote_row(enabled=False, locked=True)],
    )

    with Session(target_engine) as session:
        result = list_accounts(
            platform="chatgpt",
            page=1,
            page_size=20,
            include_live=True,
            session=session,
        )
        binding = session.exec(select(db.AccountTargetBindingModel)).one()
        assignments = session.exec(select(db.AccountAssignmentModel)).all()

    assert result["items"][0]["remote_enabled"] is False
    assert result["items"][0]["remote_locked"] is True
    assert binding.enabled is False
    assert assignments == []


def test_live_refresh_runs_a_bounded_usage_probe_before_reading_accounts():
    class Client:
        def __init__(self):
            self.calls = []

        def trigger_usage_probe(self):
            self.calls.append("probe")
            return {"triggered": True}

        def runtime_status(self):
            self.calls.append("runtime")
            return {"probes": {"usage_probe_running": False}}

        def list_accounts(self):
            self.calls.append("list")
            return [
                {
                    "id": 12,
                    "email": "fresh@example.com",
                    "status": "active",
                    "usage_percent_7d": 20,
                    "usage_7d_detail": {
                        "requests": 4,
                        "account_billed": 2.5,
                    },
                }
            ]

    client = Client()
    rows = fetch_codex2api_quota_accounts(
        client=client,
        refresh=True,
        include_display_fields=True,
    )

    assert client.calls == ["probe", "runtime", "list"]
    assert rows[0]["display_billed_usd"] == 2.5


def test_remote_rows_without_email_use_a_stable_placeholder_card():
    target_engine = make_engine()
    from unittest.mock import patch

    row = remote_row(
        email="",
        name="managed-account-90210",
        chatgpt_account_id="chatgpt-acct-json",
    )
    row["_remote_email_missing"] = True
    with patch(
        "services.chatgpt_codex2api_health.fetch_codex2api_quota_accounts",
        return_value=[row],
    ):
        with Session(target_engine) as session:
            result = list_accounts(
                platform="chatgpt",
                page=1,
                page_size=20,
                include_live=True,
                session=session,
            )

    assert result["total"] == 1
    assert result["items"][0]["email"] == "managed-account-90210"
    assert result["items"][0]["remote_only"] is True


def test_one_unavailable_target_does_not_hide_accounts_from_healthy_targets(monkeypatch):
    from services import chatgpt_codex2api_health as health

    class GoodClient:
        def list_accounts(self):
            return [{"id": 1, "email": "healthy@example.com", "status": "active"}]

    monkeypatch.setattr(health, "_quota_target_context", lambda _engine: ([1, 2], {}))

    def client_for(target_id, _engine):
        if target_id == 2:
            raise RuntimeError("target down")
        return GoodClient()

    monkeypatch.setattr("services.codex2api_target_client.get_target_client", client_for)
    rows = health.fetch_codex2api_quota_accounts(database_engine=object())

    assert [row["email"] for row in rows] == ["healthy@example.com"]


def test_remote_reconcile_reuses_binding_left_by_deleted_local_account(monkeypatch):
    target_engine = make_engine()
    with Session(target_engine) as session:
        session.add(
            db.AccountIdentityModel(
                id="old-local-identity",
                platform="chatgpt",
                canonical_email="json-imported@example.com",
                current_account_id=0,
                state="retired",
            )
        )
        session.add(
            db.AccountTargetBindingModel(
                identity_id="old-local-identity",
                local_account_id=999,
                target_id=1,
                remote_account_id=90210,
                remote_email="json-imported@example.com",
                sync_status="retired",
                remote_status="active",
                enabled=True,
            )
        )
        session.commit()
    monkeypatch.setattr(
        "services.chatgpt_codex2api_health.fetch_codex2api_quota_accounts",
        lambda **kwargs: [remote_row()],
    )

    with Session(target_engine) as session:
        result = list_accounts(
            platform="chatgpt",
            page=1,
            page_size=20,
            include_live=True,
            session=session,
        )
        bindings = session.exec(select(db.AccountTargetBindingModel)).all()

    assert result["total"] == 1
    assert len(bindings) == 1
    assert bindings[0].identity_id == "old-local-identity"
    assert bindings[0].local_account_id == 0
