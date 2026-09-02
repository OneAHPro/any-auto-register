from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine, select

from core import db


NOW = datetime(2026, 9, 3, 0, 0, tzinfo=timezone.utc)


def make_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    db.init_account_pool_schema(engine)
    return engine


def test_desired_count_uses_quota_and_concurrency_max():
    from services.pool_scheduler import PoolInput, plan_pool

    plan = plan_pool(
        PoolInput(
            pool_id="ENTERPRISE_A_POOL",
            forecast_7d_usd=Decimal("5000"),
            safe_7d_quota=Decimal("1800"),
            peak_concurrency=12,
            safe_concurrency_per_account=3,
            pool_min_accounts=1,
            current_accounts=1,
            utilization=Decimal("0.90"),
        )
    )

    assert plan.desired_count == 4
    assert plan.scale_up_count == 3
    assert plan.scale_down_count == 0
    assert plan.requires_confirmation is True


def test_safe_quota_uses_p25_only_after_twenty_observations():
    from services.pool_scheduler import safe_quota

    assert safe_quota([Decimal("1000")] * 19) == Decimal("1800.00")
    observations = [Decimal(value) for value in range(1000, 3000, 100)]
    assert safe_quota(observations) == Decimal("1400.00")


def test_scale_down_requires_two_low_cycles_and_minimum_lease():
    from services.pool_scheduler import PoolInput, plan_pool

    blocked = plan_pool(
        PoolInput(
            pool_id="ENTERPRISE_A_POOL",
            forecast_7d_usd=Decimal("1800"),
            safe_7d_quota=Decimal("1800"),
            current_accounts=5,
            utilization=Decimal("0.50"),
            low_utilization_cycles=1,
            min_lease_elapsed=False,
        )
    )
    allowed = plan_pool(
        PoolInput(
            pool_id="ENTERPRISE_A_POOL",
            forecast_7d_usd=Decimal("1800"),
            safe_7d_quota=Decimal("1800"),
            current_accounts=5,
            utilization=Decimal("0.50"),
            low_utilization_cycles=2,
            min_lease_elapsed=True,
        )
    )

    assert blocked.scale_down_count == 0
    assert allowed.scale_down_count == 4


def test_stale_quota_or_unhealthy_target_is_observe_only():
    from services.pool_scheduler import PoolInput, plan_pool

    plan = plan_pool(
        PoolInput(
            pool_id="ENTERPRISE_A_POOL",
            forecast_7d_usd=Decimal("5000"),
            current_accounts=1,
            quota_fresh=False,
            target_healthy=False,
        )
    )

    assert plan.executable is False
    assert plan.scale_up_count == 0
    assert set(plan.blockers) == {"quota_stale", "target_unhealthy"}


def test_candidate_sort_prefers_healthy_high_quota_existing_target():
    from services.pool_scheduler import AccountCandidate, rank_candidates

    candidates = [
        AccountCandidate("low", health="healthy", remaining_usd=Decimal("200"), already_on_target=False),
        AccountCandidate("high", health="healthy", remaining_usd=Decimal("900"), already_on_target=False),
        AccountCandidate("existing", health="healthy", remaining_usd=Decimal("700"), already_on_target=True),
        AccountCandidate("bad", health="error", remaining_usd=Decimal("9999"), already_on_target=True),
    ]

    ranked = rank_candidates(candidates)

    assert [item.identity_id for item in ranked] == ["existing", "high", "low", "bad"]


def test_cost_estimate_uses_integer_safe_decimal_math():
    from services.pool_scheduler import estimate_costs

    result = estimate_costs(
        customer_usage_usd=Decimal("10000"),
        customer_price_cny_per_usd=Decimal("0.20"),
        account_count=3,
        account_monthly_rent_cny=Decimal("1080"),
        occupancy_ratio=Decimal("0.50"),
        bandwidth_mbps=100,
        bandwidth_price_per_mbps_cny=Decimal("30"),
        operations_cost_cny=Decimal("200"),
    )

    assert result.revenue_cny == Decimal("2000.00")
    assert result.account_cost_cny == Decimal("1620.00")
    assert result.bandwidth_cost_cny == Decimal("3000.00")
    assert result.margin_cny == Decimal("-2820.00")


def test_dry_run_must_be_confirmed_before_apply(monkeypatch):
    from services import pool_scheduler
    from services.pool_scheduler import AccountCandidate, PoolInput

    engine = make_engine()
    candidate = AccountCandidate(
        identity_id="identity-1",
        local_account_id=1,
        health="healthy",
        remaining_usd=Decimal("900"),
        source_target_id=1,
        destination_target_id=2,
        assignment_version=1,
    )
    run = pool_scheduler.create_dry_run(
        engine,
        PoolInput(
            pool_id="ENTERPRISE_A_POOL",
            forecast_7d_usd=Decimal("1800"),
            current_accounts=0,
            candidates=(candidate,),
        ),
        now=NOW,
    )

    with pytest.raises(pool_scheduler.PlanConfirmationRequired):
        pool_scheduler.apply_confirmed_plan(engine, run.id, migration_runner=lambda action: "m-1")

    pool_scheduler.confirm_plan(engine, run.id, now=NOW)
    applied = pool_scheduler.apply_confirmed_plan(
        engine,
        run.id,
        migration_runner=lambda action: f"migration-{action.identity_id}",
        now=NOW,
    )

    assert applied.status == "queued"
    with Session(engine) as session:
        actions = session.exec(select(db.SchedulerActionModel)).all()
    assert actions[0].status == "queued"
    assert "migration-identity-1" in actions[0].detail_json


def test_confirmed_plan_expires_before_apply():
    from services import pool_scheduler
    from services.pool_scheduler import PoolInput

    engine = make_engine()
    run = pool_scheduler.create_dry_run(
        engine,
        PoolInput(pool_id="PUBLIC_POOL", current_accounts=1),
        now=NOW,
    )
    pool_scheduler.confirm_plan(engine, run.id, now=NOW)

    with pytest.raises(pool_scheduler.PlanExpired):
        pool_scheduler.apply_confirmed_plan(
            engine,
            run.id,
            migration_runner=lambda action: "unused",
            now=NOW + timedelta(minutes=16),
        )


def test_generate_scheduled_plan_uses_customer_usage_and_float_candidate():
    from services import pool_scheduler

    engine = make_engine()
    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        session.add(
            db.Codex2APITargetModel(
                id=1,
                name="public",
                base_url="https://a",
                admin_key_ref="a",
                health_status="healthy",
            )
        )
        session.add(
            db.Codex2APITargetModel(
                id=2,
                name="enterprise",
                target_type="enterprise",
                base_url="https://b",
                admin_key_ref="b",
                health_status="healthy",
            )
        )
        session.add(
            db.AccountPoolModel(
                id="ENTERPRISE_A_POOL",
                name="企业 A",
                pool_type="enterprise",
                customer_id="customer-a",
                min_accounts=1,
                safe_concurrency_per_account=3,
            )
        )
        session.add(
            db.PoolTargetPolicyModel(
                pool_id="ENTERPRISE_A_POOL",
                target_id=2,
                priority=1,
            )
        )
        account = db.AccountModel(
            platform="chatgpt",
            email="float@example.com",
            password="password",
            identity_id="identity-1",
        )
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
            db.AccountAssignmentModel(
                identity_id="identity-1",
                local_account_id=account.id,
                pool_id="FLOAT_POOL",
                target_id=1,
                state="active",
                lease_started_at=now - timedelta(hours=7),
                assignment_version=1,
            )
        )
        session.add(
            db.AccountTargetBindingModel(
                identity_id="identity-1",
                local_account_id=account.id,
                target_id=1,
                remote_account_id=77,
                remote_status="active",
            )
        )
        session.add(
            db.AccountQuotaSnapshotModel(
                identity_id="identity-1",
                local_account_id=account.id,
                target_id=1,
                window="7d",
                billed_cents=90000,
                continuous_billed_cents=90000,
                remaining_cents=90000,
                reset_at=now + timedelta(days=3),
                captured_at=now,
                freshness_seconds=900,
                is_fresh=True,
            )
        )
        session.add(
            db.CustomerUsageSampleModel(
                customer_id="customer-a",
                pool_id="ENTERPRISE_A_POOL",
                target_id=2,
                bucket_start=now - timedelta(hours=1),
                bucket_end=now,
                billed_cents=360000,
                peak_concurrency=3,
                captured_at=now,
            )
        )
        session.commit()

    runs = pool_scheduler.generate_scheduled_plans(engine, now=now)
    enterprise = next(run for run in runs if run.pool_id == "ENTERPRISE_A_POOL")

    assert enterprise.status == "awaiting_confirmation"
    assert len(enterprise.actions) == 1
    assert enterprise.actions[0].identity_id == "identity-1"
    assert enterprise.actions[0].destination_target_id == 2
