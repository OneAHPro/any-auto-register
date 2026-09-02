from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine

from core import db


def test_account_response_includes_assignment_and_continuous_quota():
    from api.accounts import _account_for_response
    from services.account_identity import ensure_identity
    from services.quota_ledger import record_snapshot

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    db.init_account_pool_schema(engine)
    account = db.AccountModel(
        platform="chatgpt",
        email="a@example.com",
        password="password",
        token="access",
    )
    with Session(engine) as session:
        session.add(account)
        session.commit()
        session.refresh(account)
    identity = ensure_identity(
        engine,
        account_id=account.id,
        platform="chatgpt",
        email=account.email,
        workspace_id="ws-1",
    )
    with Session(engine) as session:
        session.add(
            db.AccountAssignmentModel(
                identity_id=identity.identity_id,
                local_account_id=account.id,
                pool_id="ENTERPRISE_A_POOL",
                target_id=2,
                state="active",
                assignment_version=4,
            )
        )
        session.commit()
    record_snapshot(
        engine,
        identity_id=identity.identity_id,
        local_account_id=account.id,
        target_id=2,
        window="7d",
        billed_usd=Decimal("100.00"),
        usage_percent=20,
        reset_at=datetime(2026, 9, 5, tzinfo=timezone.utc),
    )
    with Session(engine) as session:
        saved = session.get(db.AccountModel, account.id)
        response = _account_for_response(saved, session=session)

    assert response["assignment"]["pool_id"] == "ENTERPRISE_A_POOL"
    assert response["assignment"]["target_id"] == 2
    assert response["quota"]["7d"]["continuous_billed_usd"] == 100.0
    assert response["quota"]["7d"]["scheduler_eligible"] is True
