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
    __tablename__ = 'operations_billing_snapshots'
    __table_args__ = (UniqueConstraint('target_id', 'remote_id'),)
    id: Optional[int] = Field(default=None, primary_key=True)
    target_id: int = Field(index=True)
    remote_id: int = Field(index=True)
    total_billed_micros: Optional[int] = None
    today_date: str = ''
    today_billed_micros: Optional[int] = None
    today_requests: Optional[int] = None
    history_json: str = '[]'
    captured_at: Optional[datetime] = None
