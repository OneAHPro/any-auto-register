from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine, select

from core import db


RESET = datetime(2026, 9, 5, 8, 0, tzinfo=timezone.utc)


def make_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    db.init_account_pool_schema(engine)
    return engine


def test_quota_does_not_drop_when_destination_counter_resets():
    from services import quota_ledger

    engine = make_engine()
    quota_ledger.record_snapshot(
        engine,
        identity_id="id-1",
        local_account_id=1,
        target_id=1,
        window="7d",
        billed_usd=1200,
        usage_percent=40,
        reset_at=RESET,
        captured_at=datetime(2026, 9, 2, 10, tzinfo=timezone.utc),
    )
    result = quota_ledger.record_snapshot(
        engine,
        identity_id="id-1",
        local_account_id=1,
        target_id=2,
        window="7d",
        billed_usd=40,
        usage_percent=2,
        reset_at=RESET,
        captured_at=datetime(2026, 9, 2, 11, tzinfo=timezone.utc),
    )

    assert result.continuous_billed_usd >= Decimal("1240")
    assert result.continuity_state == "node_counter_reset"
    assert result.remaining_scope == "target_local"


def test_destination_counter_adds_only_new_delta_after_first_observation():
    from services import quota_ledger

    engine = make_engine()
    quota_ledger.record_snapshot(
        engine,
        identity_id="id-1",
        local_account_id=1,
        target_id=1,
        window="7d",
        billed_usd=1200,
        usage_percent=40,
        reset_at=RESET,
        captured_at=datetime(2026, 9, 2, 10, tzinfo=timezone.utc),
        is_fresh=True,
    )
    first_destination = quota_ledger.record_snapshot(
        engine,
        identity_id="id-1",
        local_account_id=1,
        target_id=2,
        window="7d",
        billed_usd=40,
        usage_percent=2,
        reset_at=RESET,
        captured_at=datetime(2026, 9, 2, 11, tzinfo=timezone.utc),
        is_fresh=True,
    )
    second_destination = quota_ledger.record_snapshot(
        engine,
        identity_id="id-1",
        local_account_id=1,
        target_id=2,
        window="7d",
        billed_usd=50,
        usage_percent=3,
        reset_at=RESET,
        captured_at=datetime(2026, 9, 2, 12, tzinfo=timezone.utc),
        is_fresh=True,
    )

    assert first_destination.continuous_billed_usd == Decimal("1240.00")
    assert second_destination.continuous_billed_usd == Decimal("1250.00")


def test_first_destination_global_counter_is_not_double_counted():
    from services import quota_ledger

    engine = make_engine()
    quota_ledger.record_snapshot(
        engine,
        identity_id="id-1",
        local_account_id=1,
        target_id=1,
        window="7d",
        billed_usd=1200,
        usage_percent=40,
        reset_at=RESET,
        captured_at=datetime(2026, 9, 2, 10, tzinfo=timezone.utc),
        is_fresh=True,
    )
    result = quota_ledger.record_snapshot(
        engine,
        identity_id="id-1",
        local_account_id=1,
        target_id=2,
        window="7d",
        billed_usd=1210,
        usage_percent=41,
        reset_at=RESET,
        captured_at=datetime(2026, 9, 2, 11, tzinfo=timezone.utc),
        is_fresh=True,
    )

    assert result.continuous_billed_usd == Decimal("1210.00")


def test_older_reset_observation_cannot_drop_continuous_total():
    from services import quota_ledger

    engine = make_engine()
    quota_ledger.record_snapshot(
        engine,
        identity_id="id-1",
        local_account_id=1,
        target_id=1,
        window="7d",
        billed_usd=500,
        usage_percent=30,
        reset_at=RESET,
        is_fresh=True,
    )
    result = quota_ledger.record_snapshot(
        engine,
        identity_id="id-1",
        local_account_id=1,
        target_id=1,
        window="7d",
        billed_usd=10,
        usage_percent=1,
        reset_at=datetime(2026, 9, 4, 8, tzinfo=timezone.utc),
        is_fresh=True,
    )

    assert result.continuous_billed_usd == Decimal("500.00")
    assert result.scheduler_eligible is False


def test_old_snapshot_is_not_scheduler_eligible_by_default():
    from services import quota_ledger

    engine = make_engine()
    old = quota_ledger.record_snapshot(
        engine,
        identity_id="id-1",
        local_account_id=1,
        target_id=1,
        window="7d",
        billed_usd=100,
        usage_percent=10,
        reset_at=RESET,
        captured_at=datetime.now(timezone.utc) - __import__("datetime").timedelta(hours=4),
    )

    assert old.fresh is False
    assert old.scheduler_eligible is False


def test_same_target_snapshot_is_monotonic():
    from services import quota_ledger

    engine = make_engine()
    first = quota_ledger.record_snapshot(
        engine,
        identity_id="id-1",
        local_account_id=1,
        target_id=1,
        window="7d",
        billed_usd=100,
        usage_percent=10,
        reset_at=RESET,
    )
    second = quota_ledger.record_snapshot(
        engine,
        identity_id="id-1",
        local_account_id=1,
        target_id=1,
        window="7d",
        billed_usd=90,
        usage_percent=9,
        reset_at=RESET,
    )

    assert first.continuous_billed_usd == Decimal("100.00")
    assert second.continuous_billed_usd == Decimal("100.00")
    assert second.continuity_state == "monotonic_hold"


def test_quota_uncertainty_blocks_scheduler_eligibility():
    from services import quota_ledger

    engine = make_engine()
    result = quota_ledger.record_snapshot(
        engine,
        identity_id="id-1",
        local_account_id=1,
        target_id=2,
        window="7d",
        billed_usd=40,
        usage_percent=2,
        reset_at=None,
    )

    assert result.fresh is False
    assert result.scheduler_eligible is False


def test_identical_observation_is_deduplicated_within_short_window():
    from services import quota_ledger

    engine = make_engine()
    captured = datetime(2026, 9, 2, 10, tzinfo=timezone.utc)
    kwargs = {
        "identity_id": "id-1",
        "local_account_id": 1,
        "target_id": 1,
        "window": "7d",
        "billed_usd": 100,
        "usage_percent": 10,
        "reset_at": RESET,
        "captured_at": captured,
    }
    quota_ledger.record_snapshot(engine, **kwargs)
    quota_ledger.record_snapshot(
        engine,
        **{**kwargs, "captured_at": datetime(2026, 9, 2, 10, 4, tzinfo=timezone.utc)},
    )

    with Session(engine) as session:
        rows = session.exec(
            select(db.AccountQuotaSnapshotModel).where(
                db.AccountQuotaSnapshotModel.identity_id == "id-1"
            )
        ).all()
    assert len(rows) == 1


def test_remote_row_uses_window_reset_timestamp_for_freshness():
    from services import quota_ledger

    engine = make_engine()
    merged = quota_ledger.merge_remote_rows(
        engine,
        identity_id="id-1",
        local_account_id=1,
        target_id=1,
        rows=[
            {
                "billed_7d": 25,
                "usage_percent_7d": 5,
                "reset_7d_at": "2026-09-05T08:00:00Z",
            }
        ],
    )

    assert merged["7d"].fresh is True
    assert merged["7d"].reset_at == RESET


def test_remote_row_selection_does_not_assign_first_account_to_another_identity():
    from services import quota_ledger

    engine = make_engine()
    merged = quota_ledger.merge_remote_rows(
        engine,
        identity_id="id-1",
        local_account_id=1,
        target_id=1,
        email="wanted@example.com",
        rows=[
            {
                "id": 1,
                "email": "other@example.com",
                "billed_7d": 99,
                "usage_percent_7d": 9,
                "reset_7d_at": "2026-09-05T08:00:00Z",
            },
            {
                "id": 2,
                "email": "wanted@example.com",
                "billed_7d": 25,
                "usage_percent_7d": 5,
                "reset_7d_at": "2026-09-05T08:00:00Z",
            },
        ],
    )

    assert merged["7d"].billed_usd == Decimal("25.00")


def test_extreme_remote_money_is_marked_ineligible_instead_of_raising():
    from services import quota_ledger

    engine = make_engine()
    result = quota_ledger.record_snapshot(
        engine,
        identity_id="id-1",
        local_account_id=1,
        target_id=1,
        window="7d",
        billed_usd="1e1000",
        usage_percent=10,
        reset_at=RESET,
    )

    assert result.billed_usd is None
    assert result.scheduler_eligible is False


def test_monthly_snapshot_is_recorded_when_target_supplies_it():
    from services import quota_ledger

    engine = make_engine()
    merged = quota_ledger.merge_remote_rows(
        engine,
        identity_id="id-1",
        local_account_id=1,
        target_id=1,
        email="a@example.com",
        rows={
            "email": "a@example.com",
            "billed_monthly": 300,
            "usage_percent_monthly": 30,
            "reset_monthly_at": "2026-10-01T00:00:00Z",
        },
    )

    assert merged["monthly"].billed_usd == Decimal("300.00")
    assert merged["monthly"].fresh is True
