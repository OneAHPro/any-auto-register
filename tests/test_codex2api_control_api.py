from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine, select

from core import db
from core.db import get_session


def build_client(monkeypatch):
    from api import codex2api_control

    monkeypatch.setenv("ACCOUNT_MANAGER_SECRET_KEY", Fernet.generate_key().decode())
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    db.init_account_pool_schema(engine)

    def session_override():
        with Session(engine) as session:
            yield session

    app = FastAPI()
    app.include_router(codex2api_control.router, prefix="/api")
    app.dependency_overrides[get_session] = session_override
    return TestClient(app), engine, codex2api_control


def seed_migratable_account(engine):
    account = db.AccountModel(
        platform="chatgpt",
        email="a@example.com",
        password="password",
        token="access",
        identity_id="identity-1",
        extra_json='{"refresh_token":"refresh","workspace_id":"ws-1"}',
    )
    with Session(engine) as session:
        session.add_all(
            [
                db.Codex2APITargetModel(
                    id=1,
                    name="node-a",
                    base_url="https://a",
                    admin_key_ref="a-key",
                    enabled=True,
                    health_status="healthy",
                    capability_json='{"migratable":true}',
                ),
                db.Codex2APITargetModel(
                    id=2,
                    name="node-b",
                    target_type="enterprise",
                    base_url="https://b",
                    admin_key_ref="b-key",
                    enabled=True,
                    health_status="healthy",
                    capability_json='{"migratable":true}',
                ),
                account,
            ]
        )
        session.commit()
        session.refresh(account)
        account_id = int(account.id or 0)
        session.add_all(
            [
                db.AccountPoolModel(
                    id="ENTERPRISE_A_POOL",
                    name="企业 A",
                    pool_type="enterprise",
                    min_accounts=0,
                    max_accounts=10,
                    safe_concurrency_per_account=3,
                ),
                db.AccountPoolModel(
                    id="FLOAT_POOL",
                    name="浮动池",
                    pool_type="float",
                ),
                db.AccountIdentityModel(
                    id="identity-1",
                    platform="chatgpt",
                    canonical_email=account.email,
                    current_account_id=account.id,
                ),
                db.AccountIdentityAliasModel(
                    identity_id="identity-1",
                    platform="chatgpt",
                    alias_type="workspace_id",
                    normalized_value="ws-1",
                ),
                db.AccountAssignmentModel(
                    identity_id="identity-1",
                    local_account_id=account.id,
                    pool_id="PUBLIC_POOL",
                    target_id=1,
                    state="active",
                    assignment_version=1,
                ),
                db.AccountTargetBindingModel(
                    identity_id="identity-1",
                    local_account_id=account.id,
                    target_id=1,
                    remote_account_id=55,
                    remote_email=account.email,
                    sync_status="synced",
                    remote_status="active",
                    enabled=True,
                ),
            ]
        )
        session.commit()
    return account_id


def test_target_create_and_list_mask_admin_key(monkeypatch):
    client, engine, _module = build_client(monkeypatch)

    response = client.post(
        "/api/codex2api/targets",
        json={
            "name": "node-b",
            "target_type": "enterprise",
            "server_label": "us-b",
            "base_url": "https://node-b.example.com/",
            "admin_key": "admin-secret",
            "default_pool_id": "ENTERPRISE_B_POOL",
        },
    )

    assert response.status_code == 201
    payload = response.json()["target"]
    assert payload["admin_key"] == "********"
    assert payload["base_url"] == "https://node-b.example.com"
    assert "admin-secret" not in response.text

    listed = client.get("/api/codex2api/targets")
    assert listed.status_code == 200
    assert listed.json()["targets"][0]["admin_key"] == "********"
    with Session(engine) as session:
        target = session.exec(select(db.Codex2APITargetModel)).one()
        from core.config_store import ConfigItem

        stored = session.get(ConfigItem, target.admin_key_ref)
    assert "admin-secret" not in stored.value


def test_target_create_rejects_url_credentials_or_query(monkeypatch):
    client, _engine, _module = build_client(monkeypatch)

    response = client.post(
        "/api/codex2api/targets",
        json={
            "name": "bad",
            "base_url": "https://user:pass@example.com/?token=x",
            "admin_key": "secret",
        },
    )

    assert response.status_code == 422


def test_target_health_endpoint_returns_persisted_status(monkeypatch):
    client, engine, module = build_client(monkeypatch)
    create = client.post(
        "/api/codex2api/targets",
        json={"name": "node", "base_url": "https://node", "admin_key": "secret"},
    )
    target_id = create.json()["target"]["id"]

    class FakeTarget:
        def health(self):
            return {"status": "ok"}

        def capabilities(self):
            return {"migratable": True, "restore": True}

    monkeypatch.setattr(module, "get_target_client", lambda target_id, engine: FakeTarget())
    first = client.post(f"/api/codex2api/targets/{target_id}/health")
    second = client.post(f"/api/codex2api/targets/{target_id}/health")

    assert first.json()["health_status"] == "recovering"
    assert second.json()["health_status"] == "healthy"


def test_target_health_failure_is_reported_as_bad_gateway(monkeypatch):
    from services.codex2api_target_client import Codex2APITargetError

    client, _engine, module = build_client(monkeypatch)
    created = client.post(
        "/api/codex2api/targets",
        json={"name": "node", "base_url": "https://node", "admin_key": "secret"},
    ).json()["target"]
    monkeypatch.setattr(
        module,
        "get_target_client",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            Codex2APITargetError("target unavailable", endpoint="health")
        ),
    )

    response = client.post(f"/api/codex2api/targets/{created['id']}/health")

    assert response.status_code == 502
    assert response.json()["detail"] == "target unavailable"


def test_pool_create_persists_target_policy(monkeypatch):
    client, engine, _module = build_client(monkeypatch)
    target = client.post(
        "/api/codex2api/targets",
        json={"name": "node", "base_url": "https://node", "admin_key": "secret"},
    ).json()["target"]

    response = client.post(
        "/api/codex2api/pools",
        json={
            "id": "ENTERPRISE_A_POOL",
            "name": "企业 A",
            "pool_type": "enterprise",
            "customer_id": "customer-a",
            "customer_name": "企业 A",
            "target_id": target["id"],
            "remote_api_key_ids": [11],
            "min_accounts": 2,
            "max_accounts": 10,
            "safe_concurrency_per_account": 3,
        },
    )

    assert response.status_code == 201
    assert response.json()["pool"]["target_id"] == target["id"]
    with Session(engine) as session:
        pool = session.get(db.AccountPoolModel, "ENTERPRISE_A_POOL")
        policy = session.exec(select(db.PoolTargetPolicyModel)).one()
    assert pool.customer_id == "customer-a"
    assert policy.remote_api_key_ids_json == "[11]"


def test_account_quota_endpoint_never_returns_credentials(monkeypatch):
    client, engine, _module = build_client(monkeypatch)
    account_id = seed_migratable_account(engine)
    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        session.add(
            db.AccountQuotaSnapshotModel(
                identity_id="identity-1",
                local_account_id=account_id,
                target_id=1,
                window="7d",
                billed_cents=10000,
                continuous_billed_cents=10000,
                remaining_cents=90000,
                reset_at=now + timedelta(days=2),
                captured_at=now,
                is_fresh=True,
                freshness_seconds=900,
            )
        )
        session.commit()

    response = client.get(f"/api/accounts/{account_id}/quota")

    assert response.status_code == 200
    assert response.json()["windows"]["7d"]["continuous_billed_usd"] == 100.0
    assert "password" not in response.text
    assert "refresh" not in response.text


def test_plan_apply_requires_explicit_confirmation(monkeypatch):
    client, engine, module = build_client(monkeypatch)
    from services.pool_scheduler import PoolInput, create_dry_run

    run = create_dry_run(
        engine,
        PoolInput(pool_id="PUBLIC_POOL", current_accounts=1),
    )
    called = []
    monkeypatch.setattr(module, "apply_confirmed_plan", lambda *args, **kwargs: called.append(True))

    response = client.post(
        "/api/scheduler/apply",
        json={"run_id": run.id, "confirm": False},
    )

    assert response.status_code == 409
    assert called == []


def test_scheduler_plan_response_enriches_email_and_hides_credential_revision(monkeypatch):
    client, engine, _module = build_client(monkeypatch)
    account_id = seed_migratable_account(engine)
    from services.pool_scheduler import AccountCandidate, PoolInput, create_dry_run

    create_dry_run(
        engine,
        PoolInput(
            pool_id="ENTERPRISE_A_POOL",
            forecast_7d_usd=1800,
            current_accounts=0,
            candidates=(
                AccountCandidate(
                    identity_id="identity-1",
                    local_account_id=account_id,
                    source_target_id=1,
                    destination_target_id=2,
                    assignment_version=1,
                    credential_revision="credential-digest",
                    health="healthy",
                    remaining_usd=900,
                ),
            ),
        ),
    )

    response = client.get("/api/scheduler/plan")

    assert response.status_code == 200
    action = response.json()["run"]["plan"]["actions"][0]
    assert action["email"] == "a@example.com"
    assert "credential_revision" not in response.text


def test_scheduler_scale_down_reassigns_same_target_pool_without_remote_copy(monkeypatch):
    client, engine, _module = build_client(monkeypatch)
    account_id = seed_migratable_account(engine)
    from services.pool_scheduler import AccountCandidate, PoolInput, create_dry_run

    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        session.add(
            db.AccountQuotaSnapshotModel(
                identity_id="identity-1",
                local_account_id=account_id,
                target_id=1,
                window="7d",
                billed_cents=100,
                continuous_billed_cents=100,
                remaining_cents=100000,
                reset_at=now + timedelta(days=3),
                captured_at=now,
                is_fresh=True,
                freshness_seconds=900,
            )
        )
        session.commit()

    run = create_dry_run(
        engine,
        PoolInput(
            pool_id="PUBLIC_POOL",
            forecast_7d_usd=0,
            safe_7d_quota=1800,
            current_accounts=1,
            utilization=0.1,
            low_utilization_cycles=2,
            candidates=(
                AccountCandidate(
                    identity_id="identity-1",
                    local_account_id=account_id,
                    pool_type="PUBLIC_POOL",
                    source_target_id=1,
                    destination_target_id=1,
                    assignment_version=1,
                    lease_elapsed=True,
                ),
            ),
        ),
    )

    response = client.post(
        "/api/scheduler/apply",
        json={"run_id": run.id, "confirm": True},
    )

    assert response.status_code == 202
    with Session(engine) as session:
        assignment = session.exec(
            select(db.AccountAssignmentModel).where(
                db.AccountAssignmentModel.identity_id == "identity-1"
            )
        ).one()
    assert assignment.pool_id == "FLOAT_POOL"


def test_scheduler_apply_rejects_plan_with_unavailable_capacity(monkeypatch):
    client, engine, _module = build_client(monkeypatch)
    account_id = seed_migratable_account(engine)
    from services.pool_scheduler import AccountCandidate, PoolInput, create_dry_run

    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        session.add(
            db.AccountQuotaSnapshotModel(
                identity_id="identity-1",
                local_account_id=account_id,
                target_id=1,
                window="7d",
                billed_cents=100,
                continuous_billed_cents=100,
                remaining_cents=100000,
                reset_at=now + timedelta(days=3),
                captured_at=now,
                is_fresh=True,
                freshness_seconds=900,
            )
        )
        session.commit()
    run = create_dry_run(
        engine,
        PoolInput(
            pool_id="ENTERPRISE_A_POOL",
            forecast_7d_usd=5000,
            safe_7d_quota=1800,
            current_accounts=1,
            candidates=(
                AccountCandidate(
                    identity_id="identity-1",
                    local_account_id=account_id,
                    source_target_id=1,
                    destination_target_id=2,
                    assignment_version=1,
                    health="healthy",
                    remaining_usd=900,
                ),
            ),
        ),
    )

    response = client.post(
        "/api/scheduler/apply",
        json={"run_id": run.id, "confirm": True},
    )

    assert response.status_code == 409
    assert "不可执行" in response.json()["detail"]


def test_assignment_endpoint_queues_migration_operation(monkeypatch):
    client, engine, module = build_client(monkeypatch)
    account_id = seed_migratable_account(engine)
    monkeypatch.setattr(module, "run_migration", lambda *args, **kwargs: None)

    response = client.post(
        f"/api/accounts/{account_id}/assignment",
        json={
            "target_id": 2,
            "pool_id": "ENTERPRISE_A_POOL",
            "reason": "enterprise_peak_scale_up",
        },
    )

    assert response.status_code == 202
    operation_id = response.json()["operation_id"]
    assert operation_id
    with Session(engine) as session:
        migration = session.get(db.AccountMigrationModel, operation_id)
    assert migration.destination_target_id == 2
    assert migration.state == "planned"


def test_rollback_endpoint_is_idempotent(monkeypatch):
    client, _engine, module = build_client(monkeypatch)
    monkeypatch.setattr(
        module,
        "rollback_migration",
        lambda *_args, **_kwargs: SimpleNamespace(
            id="migration-1",
            state="rolled_back",
            step="rolled_back",
            source_remote_id=55,
            destination_remote_id=77,
            error="",
        ),
    )

    first = client.post("/api/migrations/migration-1/rollback")
    second = client.post("/api/migrations/migration-1/rollback")

    assert first.status_code == second.status_code == 200
    assert first.json()["state"] == second.json()["state"] == "rolled_back"
