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
