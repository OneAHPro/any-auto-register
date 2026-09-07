"""Local purchase expenses, independent of account and credential lifetime."""
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from sqlalchemy import CheckConstraint
from sqlmodel import Field, SQLModel


class PurchaseBatchModel(SQLModel, table=True):
    __tablename__ = 'account_purchase_batches'
    __table_args__ = (CheckConstraint('expected_count > 0'), CheckConstraint('declared_total_cost_cents >= 0'))

    id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True)
    source: str = Field(index=True)
    request_key: str = Field(index=True, sa_column_kwargs={'unique': True})
    expected_count: int
    # Original request value is retained for idempotency, not used in expense sums.
    declared_total_cost_cents: int = 0
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), index=True)


class PurchaseCostRecordModel(SQLModel, table=True):
    __tablename__ = 'account_purchase_cost_records'
    __table_args__ = (CheckConstraint('cost_cents IS NULL OR cost_cents >= 0'),)

    id: Optional[int] = Field(default=None, primary_key=True)
    record_key: str = Field(index=True, sa_column_kwargs={'unique': True})
    # Deliberately no account FK: deletion must never erase an incurred expense.
    batch_id: Optional[str] = Field(default=None, index=True)
    account_id: Optional[int] = Field(default=None, index=True)
    identity_id: str = Field(default='', index=True)
    cost_cents: Optional[int] = None
    incurred_at: Optional[datetime] = Field(default=None, index=True)
