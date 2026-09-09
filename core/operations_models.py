"""Durable, non-secret operational billing and instance sale prices."""
from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel

class InstanceSalePriceModel(SQLModel, table=True):
    __tablename__ = 'operations_instance_sale_prices'
    target_id: int = Field(primary_key=True)
    price_micros: int
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class OperationsBillingSnapshotModel(SQLModel, table=True):
    """Durable usage/billing aggregate for one remote account.

    ``total_requests`` and the provenance fields are intentionally separate
    from ``history_json``.  A history payload can be trimmed or unavailable on
    a summary-only read, while these columns preserve the aggregate and the
    state of the read that produced it.
    """
    __tablename__ = 'operations_billing_snapshots'
    __table_args__ = (
        UniqueConstraint(
            'target_id',
            'remote_id',
            name='uq_operations_billing_snapshot_target_remote',
        ),
    )
    id: Optional[int] = Field(default=None, primary_key=True)
    target_id: int = Field(index=True)
    remote_id: int = Field(index=True)
    total_billed_micros: Optional[int] = None
    total_requests: Optional[int] = None
    today_date: str = ''
    today_billed_micros: Optional[int] = None
    today_requests: Optional[int] = None
    history_json: str = '[]'
    captured_at: Optional[datetime] = None
    source: str = 'codex2api'
    error: str = ''
    last_request_at: Optional[datetime] = None
