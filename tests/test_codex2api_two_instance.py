from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine, func, select

from core import db
from tests.fixtures.codex2api_target import FakeCodex2APITarget


NOW = datetime(2026, 9, 3, tzinfo=timezone.utc)


class TwoTargetHarness:
    def __init__(
        self,
        *,
        source_failure: str = "",
        destination_failure: str = "",
    ) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        db.init_account_pool_schema(self.engine)
        self.source = FakeCodex2APITarget(1, fail_on=source_failure)
        self.destination = FakeCodex2APITarget(2, fail_on=destination_failure)
        self.clients = {1: self.source, 2: self.destination}
        self.account_id = 0
        self.identity_id = "identity-fixture"

    def seed_source_account(
        self,
        *,
        email: str = "user@example.test",
        billed_7d: float = 1200,
    ) -> None:
        self.source.seed_account(
            email=email,
            workspace_id="workspace-fixture",
            remote_id=55,
            billed_7d=billed_7d,
            usage_percent_7d=66.6667,
        )
        self.destination.default_reset_at = self.source.accounts[55]["reset_7d_at"]
        account = db.AccountModel(
            platform="chatgpt",
            email=email,
            password="fixture-password",
            token="fixture-access-token",
            identity_id=self.identity_id,
            extra_json=(
                '{"refresh_token":"fixture-refresh-token",'
                '"access_token":"fixture-access-token",'
                '"workspace_id":"workspace-fixture"}'
            ),
        )
        with Session(self.engine) as session:
            session.add_all(
                [
                    db.Codex2APITargetModel(
                        id=1,
                        name="source",
                        base_url="https://source.example.test",
                        admin_key_ref="source-key",
                        health_status="healthy",
                        capability_json='{"migratable":true,"restore":true}',
                    ),
                    db.Codex2APITargetModel(
                        id=2,
                        name="destination",
                        target_type="enterprise",
                        base_url="https://destination.example.test",
                        admin_key_ref="destination-key",
                        health_status="healthy",
                        capability_json='{"migratable":true,"restore":true}',
                    ),
                    db.AccountPoolModel(
                        id="PUBLIC_POOL",
                        name="公共池",
                        pool_type="public",
                    ),
                    db.AccountPoolModel(
                        id="ENTERPRISE_FIXTURE_POOL",
                        name="企业测试池",
                        pool_type="enterprise",
                        min_accounts=1,
                        max_accounts=5,
                    ),
                    account,
                ]
            )
            session.commit()
            session.refresh(account)
            self.account_id = int(account.id or 0)
            session.add_all(
                [
                    db.AccountIdentityModel(
                        id=self.identity_id,
                        platform="chatgpt",
                        canonical_email=email,
                        current_account_id=self.account_id,
                    ),
                    db.AccountIdentityAliasModel(
                        identity_id=self.identity_id,
                        platform="chatgpt",
                        alias_type="workspace_id",
                        normalized_value="workspace-fixture",
                    ),
                    db.AccountAssignmentModel(
                        identity_id=self.identity_id,
                        local_account_id=self.account_id,
                        pool_id="PUBLIC_POOL",
                        target_id=1,
                        state="active",
                        assignment_version=1,
                        lease_started_at=NOW - timedelta(hours=8),
                    ),
                    db.AccountTargetBindingModel(
                        identity_id=self.identity_id,
                        local_account_id=self.account_id,
                        target_id=1,
                        remote_account_id=55,
                        remote_email=email,
                        sync_status="synced",
                        remote_status="active",
                        enabled=True,
                    ),
                ]
            )
            session.commit()

    def collect_source_snapshot(self) -> None:
        from services.control_plane_workers import collect_target_quota

        result = collect_target_quota(
            self.engine,
            target_id=1,
            client=self.source,
            now=NOW,
            freshness_seconds=900,
        )
        assert result.collected_accounts == 1

    def plan_and_run_migration(self):
        from services.account_migration import plan_migration, run_migration

        migration_id = plan_migration(
            self.engine,
            identity_id=self.identity_id,
            local_account_id=self.account_id,
            source_target_id=1,
            destination_target_id=2,
            expected_assignment_version=1,
            expected_credential_revision="",
            idempotency_key=f"fixture:{self.identity_id}",
            plan={
                "source_pool_id": "PUBLIC_POOL",
                "destination_pool_id": "ENTERPRISE_FIXTURE_POOL",
                "reason": "fixture_scale_up",
            },
        )
        return run_migration(
            self.engine,
            migration_id,
            clients=self.clients,
            now=NOW,
            drain_timeout_seconds=1,
            poll_interval_seconds=0,
            sleep_fn=lambda _seconds: None,
        )

    def collect_destination_snapshot(self) -> None:
        from services.control_plane_workers import collect_target_quota

        collect_target_quota(
            self.engine,
            target_id=2,
            client=self.destination,
            now=NOW + timedelta(minutes=1),
            freshness_seconds=900,
        )

    def assignment(self) -> db.AccountAssignmentModel:
        with Session(self.engine) as session:
            return session.exec(
                select(db.AccountAssignmentModel).where(
                    db.AccountAssignmentModel.identity_id == self.identity_id,
                    db.AccountAssignmentModel.state == "active",
                )
            ).one()


def test_fake_target_implements_complete_client_surface():
    target = FakeCodex2APITarget(1)
    expected = {
        "health",
        "capabilities",
        "list_accounts",
        "trigger_usage_probe",
        "runtime_status",
        "api_key_usage",
        "import_refresh_token",
        "import_access_token",
        "import_full_json",
        "test_account",
        "set_enabled",
        "set_locked",
        "refresh_account",
        "delete_account",
        "restore_account",
        "update_scheduler",
        "wait_for_zero_active_requests",
    }

    assert all(callable(getattr(target, method, None)) for method in expected)


def test_two_targets_preserve_identity_and_continuous_quota_after_migration():
    from services.quota_ledger import latest_snapshot

    harness = TwoTargetHarness()
    harness.seed_source_account(billed_7d=1200)
    harness.collect_source_snapshot()

    migration = harness.plan_and_run_migration()
    harness.collect_destination_snapshot()

    assert migration.state == "committed"
    assert harness.assignment().target_id == 2
    assert 55 not in harness.source.accounts
    assert len(harness.destination.accounts) == 1
    with Session(harness.engine) as session:
        identity_count = session.exec(
            select(func.count(db.AccountIdentityModel.id)).where(
                db.AccountIdentityModel.canonical_email == "user@example.test"
            )
        ).one()
    quota = latest_snapshot(
        harness.engine,
        identity_id=harness.identity_id,
        window="7d",
    )
    assert identity_count == 1
    assert quota is not None
    assert quota.continuous_billed_usd >= Decimal("1200")


@pytest.mark.parametrize("failure", ["import", "verify", "enable_true"])
def test_destination_failures_restore_source_and_local_assignment(failure: str):
    harness = TwoTargetHarness(destination_failure=failure)
    harness.seed_source_account()
    harness.collect_source_snapshot()

    result = harness.plan_and_run_migration()

    assert result.state == "rolled_back"
    assert harness.assignment().target_id == 1
    assert harness.source.accounts[55]["enabled"] is True
    assert harness.source.accounts[55]["locked"] is False


def test_drain_failure_restores_source_without_importing_destination():
    harness = TwoTargetHarness(source_failure="drain")
    harness.seed_source_account()
    harness.collect_source_snapshot()

    result = harness.plan_and_run_migration()

    assert result.state == "rolled_back"
    assert harness.source.accounts[55]["enabled"] is True
    assert harness.destination.accounts == {}


def test_source_delete_failure_is_visible_and_destination_serves_account():
    harness = TwoTargetHarness(source_failure="delete")
    harness.seed_source_account()
    harness.collect_source_snapshot()

    result = harness.plan_and_run_migration()

    assert result.state == "cleanup_pending"
    assert harness.assignment().target_id == 2
    assert harness.source.accounts[55]["enabled"] is False
    assert next(iter(harness.destination.accounts.values()))["enabled"] is True


def test_network_failure_never_changes_assignment_silently():
    harness = TwoTargetHarness(source_failure="network")
    harness.seed_source_account()

    result = harness.plan_and_run_migration()

    assert result.state == "rollback_required"
    assert harness.assignment().target_id == 1
