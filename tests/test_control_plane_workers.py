from datetime import datetime, timezone

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine, select

from core import db


NOW = datetime(2026, 9, 3, 1, 0, tzinfo=timezone.utc)


def make_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    db.init_account_pool_schema(engine)
    with Session(engine) as session:
        session.add(
            db.Codex2APITargetModel(
                id=1,
                name="node-a",
                target_type="public",
                base_url="https://node-a",
                admin_key_ref="node-a-key",
                enabled=True,
            )
        )
        session.commit()
    return engine


class FakeClient:
    def __init__(self):
        self.fail_health = False
        self.calls = []

    def health(self):
        self.calls.append("health")
        if self.fail_health:
            raise RuntimeError("offline")
        return {"status": "ok"}

    def capabilities(self):
        self.calls.append("capabilities")
        return {"migratable": True, "restore": True}

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
                "id": 77,
                "email": "a@example.com",
                "status": "active",
                "enabled": True,
                "usage_percent_7d": 25,
                "billed_7d": 100,
                "reset_7d_at": "2026-09-07T00:00:00Z",
            }
        ]

    def api_key_usage(self, *, start, end):
        self.calls.append("api-key-usage")
        return [
            {"api_key_id": 11, "requests": 20, "user_billed": 12.34},
            {"api_key_id": 22, "requests": 99, "user_billed": 88.88},
        ]


def test_quota_collection_rejects_fractional_remote_id_without_truncation():
    from services.control_plane_workers import collect_target_quota

    engine = make_engine()
    client = FakeClient()
    client.list_accounts = lambda: [{"id": 1.5, "email": "fractional@example.com", "status": "active"}]

    try:
        collect_target_quota(engine, target_id=1, client=client, now=NOW)
    except RuntimeError as exc:
        assert "invalid remote ID" in str(exc)
    else:
        raise AssertionError("fractional remote ID was accepted")


def test_target_health_requires_two_successes_before_healthy():
    from services.control_plane_workers import collect_target_health

    engine = make_engine()
    client = FakeClient()

    first = collect_target_health(engine, target_id=1, client=client, now=NOW)
    second = collect_target_health(engine, target_id=1, client=client, now=NOW)

    assert first.health_status == "recovering"
    assert second.health_status == "healthy"
    assert second.health_success_count == 2
    assert second.health_failure_count == 0


def test_target_health_requires_two_failures_before_unreachable():
    from services.control_plane_workers import collect_target_health

    engine = make_engine()
    client = FakeClient()
    client.fail_health = True

    first = collect_target_health(engine, target_id=1, client=client, now=NOW)
    second = collect_target_health(engine, target_id=1, client=client, now=NOW)

    assert first.health_status == "degraded"
    assert second.health_status == "unreachable"
    assert second.health_failure_count == 2
    assert "offline" not in second.last_error


def test_quota_collection_batches_target_and_updates_binding_and_ledger():
    from services.control_plane_workers import collect_target_quota

    engine = make_engine()
    account = db.AccountModel(
        platform="chatgpt",
        email="a@example.com",
        password="password",
        identity_id="identity-1",
    )
    with Session(engine) as session:
        session.add(account)
        session.commit()
        session.refresh(account)
        session.add(
            db.AccountIdentityModel(
                id="identity-1",
                platform="chatgpt",
                canonical_email=account.email,
                current_account_id=account.id,
            )
        )
        session.add(
            db.AccountTargetBindingModel(
                identity_id="identity-1",
                local_account_id=account.id,
                target_id=1,
                remote_account_id=77,
                remote_email=account.email,
            )
        )
        session.commit()
    client = FakeClient()

    result = collect_target_quota(
        engine,
        target_id=1,
        client=client,
        now=NOW,
        freshness_seconds=60,
    )

    assert result.collected_accounts == 1
    assert client.calls == ["probe", "runtime", "list"]
    with Session(engine) as session:
        binding = session.exec(select(db.AccountTargetBindingModel)).one()
        snapshot = session.exec(select(db.AccountQuotaSnapshotModel)).one()
    assert binding.sync_status == "synced"
    assert binding.remote_status == "active"
    assert snapshot.identity_id == "identity-1"
    assert snapshot.window == "7d"
    assert snapshot.billed_cents == 10000
    assert snapshot.freshness_seconds == 60


def test_quota_collection_marks_missing_remote_binding_without_creating_snapshot():
    from services.control_plane_workers import collect_target_quota

    engine = make_engine()
    with Session(engine) as session:
        session.add(
            db.AccountTargetBindingModel(
                identity_id="identity-1",
                local_account_id=1,
                target_id=1,
                remote_account_id=999,
                remote_email="missing@example.com",
            )
        )
        session.commit()
    client = FakeClient()

    result = collect_target_quota(engine, target_id=1, client=client, now=NOW)

    assert result.missing_accounts == 1
    with Session(engine) as session:
        binding = session.exec(select(db.AccountTargetBindingModel)).one()
        snapshots = session.exec(select(db.AccountQuotaSnapshotModel)).all()
    assert binding.sync_status == "remote_missing"
    assert snapshots == []


def test_quota_collection_stands_by_missing_remote_only_assignment():
    from services.control_plane_workers import collect_target_quota

    engine = make_engine()
    with Session(engine) as session:
        session.add(
            db.AccountIdentityModel(
                id="codex2api:1:999",
                platform="chatgpt",
                canonical_email="missing@example.com",
                current_account_id=0,
            )
        )
        session.add(
            db.AccountTargetBindingModel(
                identity_id="codex2api:1:999",
                local_account_id=0,
                target_id=1,
                remote_account_id=999,
                remote_email="missing@example.com",
                remote_status="active",
                enabled=True,
            )
        )
        session.add(
            db.AccountAssignmentModel(
                identity_id="codex2api:1:999",
                local_account_id=0,
                pool_id="PUBLIC_POOL",
                target_id=1,
                state="active",
            )
        )
        session.commit()

    result = collect_target_quota(engine, target_id=1, client=FakeClient(), now=NOW)

    assert result.missing_accounts == 1
    with Session(engine) as session:
        binding = session.exec(select(db.AccountTargetBindingModel)).one()
        assignment = session.exec(select(db.AccountAssignmentModel)).one()
    assert binding.enabled is False
    assert binding.remote_status == "remote_missing"
    assert assignment.state == "standby"


def test_quota_collection_skips_enabled_binding_on_noncurrent_target():
    from services.control_plane_workers import collect_target_quota

    engine = make_engine()
    with Session(engine) as session:
        session.add(
            db.AccountIdentityModel(
                id="identity-moved",
                platform="chatgpt",
                canonical_email="moved@example.com",
                current_account_id=1,
            )
        )
        session.add(
            db.AccountAssignmentModel(
                identity_id="identity-moved",
                local_account_id=1,
                pool_id="PUBLIC_POOL",
                target_id=2,
                state="active",
            )
        )
        session.add(
            db.AccountTargetBindingModel(
                identity_id="identity-moved",
                local_account_id=1,
                target_id=1,
                remote_account_id=77,
                remote_email="moved@example.com",
                remote_status="active",
                enabled=True,
            )
        )
        session.commit()

    result = collect_target_quota(
        engine,
        target_id=1,
        client=FakeClient(),
        now=NOW,
    )

    assert result.collected_accounts == 0


def test_default_target_reconciliation_bootstraps_binding_and_assignment():
    from services.control_plane_workers import collect_target_quota

    engine = make_engine()
    account = db.AccountModel(
        platform="chatgpt",
        email="a@example.com",
        password="password",
        identity_id="identity-1",
    )
    with Session(engine) as session:
        target = session.get(db.Codex2APITargetModel, 1)
        target.default_pool_id = "PUBLIC_POOL"
        session.add(target)
        session.add(account)
        session.commit()
        session.refresh(account)
        session.add(
            db.AccountIdentityModel(
                id="identity-1",
                platform="chatgpt",
                canonical_email=account.email,
                current_account_id=account.id,
            )
        )
        session.commit()

    result = collect_target_quota(engine, target_id=1, client=FakeClient(), now=NOW)

    assert result.collected_accounts == 1
    with Session(engine) as session:
        binding = session.exec(select(db.AccountTargetBindingModel)).one()
        assignment = session.exec(select(db.AccountAssignmentModel)).one()
    assert binding.remote_account_id == 77
    assert assignment.target_id == 1
    assert assignment.pool_id == "PUBLIC_POOL"


def test_reconciliation_quarantines_a_stable_identity_alias_mismatch():
    from services.control_plane_workers import reconcile_target_bindings

    engine = make_engine()
    account = db.AccountModel(
        platform="chatgpt",
        email="a@example.com",
        password="password",
        identity_id="identity-alias-mismatch",
        extra_json='{"workspace_id":"workspace-local"}',
    )
    with Session(engine) as session:
        session.add(account)
        session.commit()
        session.refresh(account)
        session.add(db.AccountIdentityModel(
            id="identity-alias-mismatch", platform="chatgpt",
            canonical_email=account.email, current_account_id=account.id,
        ))
        session.add(db.AccountAssignmentModel(
            identity_id="identity-alias-mismatch", local_account_id=account.id,
            pool_id="PUBLIC_POOL", target_id=1, state="active",
        ))
        session.commit()

    reconcile_target_bindings(
        engine,
        target_id=1,
        rows=[{
            "id": 77, "email": "a@example.com", "workspace_id": "workspace-remote",
            "status": "active", "enabled": True,
        }],
        now=NOW,
    )

    with Session(engine) as session:
        identity = session.get(db.AccountIdentityModel, "identity-alias-mismatch")
        assignment = session.exec(select(db.AccountAssignmentModel)).one()
        bindings = session.exec(select(db.AccountTargetBindingModel)).all()
    assert identity.state == "ambiguous"
    assert assignment.state == "standby"
    assert bindings == []


def test_quota_collection_quarantines_a_reused_remote_id_with_different_identity():
    from services.control_plane_workers import collect_target_quota

    engine = make_engine()
    account = db.AccountModel(
        platform="chatgpt", email="a@example.com", password="",
        identity_id="identity-reused-id", extra_json='{"workspace_id":"workspace-a"}',
    )
    with Session(engine) as session:
        session.add(account); session.commit(); session.refresh(account)
        session.add(db.AccountIdentityModel(
            id="identity-reused-id", platform="chatgpt",
            canonical_email=account.email, current_account_id=account.id,
        ))
        session.add(db.AccountTargetBindingModel(
            identity_id="identity-reused-id", local_account_id=account.id,
            target_id=1, remote_account_id=77, remote_email=account.email,
            enabled=True, sync_status="synced",
        ))
        session.add(db.AccountAssignmentModel(
            identity_id="identity-reused-id", local_account_id=account.id,
            pool_id="PUBLIC_POOL", target_id=1, state="active",
        ))
        session.commit()

    class ReusedIdClient(FakeClient):
        def list_accounts(self):
            return [{
                "id": 77, "email": "a@example.com", "workspace_id": "workspace-b",
                "status": "active", "enabled": True,
            }]

    result = collect_target_quota(engine, target_id=1, client=ReusedIdClient(), now=NOW)

    assert result.collected_accounts == 0
    assert result.ambiguous_accounts == 1
    with Session(engine) as session:
        binding = session.exec(select(db.AccountTargetBindingModel)).one()
        assignment = session.exec(select(db.AccountAssignmentModel)).one()
    assert binding.enabled is False
    assert binding.sync_status == "ambiguous"
    assert assignment.state == "standby"


def test_quota_collection_reports_preexisting_ambiguous_identity_without_binding():
    """A state-level ambiguity remains visible after reconcile creates/quarantines a binding."""

    from services.control_plane_workers import collect_target_quota

    engine = make_engine()
    account = db.AccountModel(
        platform="chatgpt",
        email="ambiguous@example.com",
        password="",
        identity_id="identity-preexisting-ambiguous",
    )
    with Session(engine) as session:
        session.add(account)
        session.commit()
        session.refresh(account)
        session.add(
            db.AccountIdentityModel(
                id="identity-preexisting-ambiguous",
                platform="chatgpt",
                canonical_email=account.email,
                state="ambiguous",
                current_account_id=account.id,
            )
        )
        session.commit()

    class AmbiguousClient(FakeClient):
        def list_accounts(self):
            return [
                {
                    "id": 77,
                    "email": "ambiguous@example.com",
                    "status": "active",
                    "enabled": True,
                }
            ]

    result = collect_target_quota(
        engine,
        target_id=1,
        client=AmbiguousClient(),
        now=NOW,
    )

    assert result.collected_accounts == 0
    assert result.ambiguous_accounts == 1
    with Session(engine) as session:
        binding = session.exec(select(db.AccountTargetBindingModel)).one()
    assert binding.enabled is False
    assert binding.sync_status == "ambiguous"


def test_customer_usage_collection_filters_configured_api_keys():
    from services.control_plane_workers import collect_customer_usage

    engine = make_engine()
    with Session(engine) as session:
        session.add(
            db.CustomerModel(id="customer-a", name="企业 A")
        )
        session.add(
            db.AccountPoolModel(
                id="ENTERPRISE_A_POOL",
                name="企业 A 号池",
                pool_type="enterprise",
                customer_id="customer-a",
            )
        )
        session.add(
            db.PoolTargetPolicyModel(
                pool_id="ENTERPRISE_A_POOL",
                target_id=1,
                priority=1,
                remote_api_key_ids_json="[11]",
            )
        )
        session.commit()

    samples = collect_customer_usage(
        engine,
        target_id=1,
        client=FakeClient(),
        now=NOW,
    )

    assert len(samples) == 1
    assert samples[0].customer_id == "customer-a"
    assert samples[0].billed_cents == 1234
    assert samples[0].request_count == 20
