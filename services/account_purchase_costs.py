"""Transactional purchase-cost allocation. Callers own commit and rollback."""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING
from uuid import uuid4

from sqlalchemy.dialects.sqlite import insert
from sqlalchemy import and_, or_, update
from sqlmodel import Session, select

from core.purchase_cost_models import PurchaseBatchModel, PurchaseCostRecordModel

if TYPE_CHECKING:
    from core.db import AccountModel

_SOURCES = frozenset({'task_login', 'json_import', 'manual', 'legacy'})
_MAX_COST_CENTS = 99_999_999_999


def _serialize_cost_write(session: Session) -> None:
    """Keep the ledger aggregate and account projection in one SQLite writer."""
    connection = session.connection()
    if connection.dialect.name == 'sqlite' and not connection.connection.in_transaction:
        connection.exec_driver_sql('BEGIN IMMEDIATE')


def validate_purchase_cost_cny(value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError('购号成本必须是金额')
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError('购号成本必须是有效金额') from exc
    if not amount.is_finite() or amount < 0 or amount > Decimal('999999999.99'):
        raise ValueError('购号成本须在 0 至 999999999.99 元之间')
    normalized = amount.quantize(Decimal('0.01'))
    if normalized != amount:
        raise ValueError('购号成本最多保留两位小数')
    return normalized


def _validate_cents(value, *, allow_unknown=False):
    if value is None and allow_unknown:
        return
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= _MAX_COST_CENTS:
        raise ValueError('购号成本须为非负整数分')


def _records_for_account(session: Session, account: 'AccountModel'):
    statement = select(PurchaseCostRecordModel).where(PurchaseCostRecordModel.account_id == account.id)
    if account.identity_id:
        statement = statement.where(PurchaseCostRecordModel.identity_id.in_(['', account.identity_id]))
    return session.exec(statement.order_by(PurchaseCostRecordModel.id).execution_options(populate_existing=True)).all()


def ensure_legacy_purchase_costs(session: Session) -> None:
    from core.db import AccountModel

    _serialize_cost_write(session)
    # Read only migrated columns so this also tolerates minimal legacy fixtures.
    accounts = session.exec(select(AccountModel.id, AccountModel.purchase_cost_cents, AccountModel.identity_id).where(
        AccountModel.platform == 'chatgpt', AccountModel.purchase_cost_cents.is_not(None),
    )).all()
    for account_id, amount, identity_id in accounts:
        existing = session.exec(select(PurchaseCostRecordModel.id).where(PurchaseCostRecordModel.account_id == account_id)).first()
        if existing is not None:
            continue
        session.execute(insert(PurchaseCostRecordModel).values(
            record_key=f'legacy:account:{account_id}:{identity_id or uuid4()}', account_id=account_id,
            identity_id=str(identity_id or ''), cost_cents=amount,
            incurred_at=None,
        ).on_conflict_do_nothing(index_elements=['record_key']))
    session.flush()


def create_purchase_batch(session: Session, total_cost_cents: int, expected_count: int,
                          source: str, request_key: str, incurred_at: datetime | None = None) -> PurchaseBatchModel:
    _validate_cents(total_cost_cents)
    if isinstance(expected_count, bool) or not isinstance(expected_count, int) or not 1 <= expected_count <= 100_000:
        raise ValueError('购号批次人数须在 1 至 100000 之间')
    if source not in _SOURCES or not isinstance(request_key, str) or not request_key.strip():
        raise ValueError('购号批次来源或请求标识无效')
    request_key = request_key.strip()
    batch_id = str(uuid4())
    now = datetime.now(timezone.utc)
    result = session.execute(insert(PurchaseBatchModel).values(
        id=batch_id, source=source, request_key=request_key, expected_count=expected_count,
        declared_total_cost_cents=total_cost_cents, created_at=now,
    ).on_conflict_do_nothing(index_elements=['request_key']))
    if result.rowcount == 0:
        batch = session.exec(select(PurchaseBatchModel).where(PurchaseBatchModel.request_key == request_key)).one()
        if (batch.expected_count, batch.source, batch.declared_total_cost_cents) != (expected_count, source, total_cost_cents):
            raise ValueError('购号请求标识已用于其他金额或人数的批次')
        return batch
    base, remainder = divmod(total_cost_cents, expected_count)
    session.add_all([
        PurchaseCostRecordModel(record_key=f'batch:{batch_id}:slot:{index}', batch_id=batch_id,
                                cost_cents=base + (index < remainder), incurred_at=incurred_at if incurred_at is not None else now)
        for index in range(expected_count)
    ])
    session.flush()
    return session.get(PurchaseBatchModel, batch_id)


def bind_purchase_slot(session: Session, batch_id: str, slot_index: int, account: 'AccountModel') -> None:
    if not account.id:
        raise ValueError('账号须先保存后绑定购号成本')
    _serialize_cost_write(session)
    batch = session.get(PurchaseBatchModel, str(batch_id))
    if batch is None or isinstance(slot_index, bool) or not isinstance(slot_index, int) or not 0 <= slot_index < batch.expected_count:
        raise ValueError('购号批次或分摊位置无效')
    record = session.exec(select(PurchaseCostRecordModel).where(PurchaseCostRecordModel.record_key == f'batch:{batch_id}:slot:{slot_index}')).one()
    if record.account_id is not None and (record.account_id != account.id or (record.identity_id and account.identity_id and record.identity_id != account.identity_id)):
        raise ValueError('该购号位置已绑定其他账号')
    previous_records = _records_for_account(session, account)
    claimed = session.execute(update(PurchaseCostRecordModel).where(
        PurchaseCostRecordModel.id == record.id,
        or_(PurchaseCostRecordModel.account_id.is_(None), and_(
            PurchaseCostRecordModel.account_id == account.id,
            PurchaseCostRecordModel.identity_id.in_(['', account.identity_id]),
        )),
    ).values(account_id=account.id, identity_id=account.identity_id).execution_options(synchronize_session=False))
    if claimed.rowcount != 1:
        raise ValueError('该购号位置已绑定其他账号')
    # Preserve a pre-ledger account expense before adding another purchase.
    if not previous_records and account.purchase_cost_cents is not None:
        session.add(PurchaseCostRecordModel(record_key=f'legacy:account:{account.id}:{account.identity_id or uuid4()}', account_id=account.id,
                                           identity_id=account.identity_id, cost_cents=account.purchase_cost_cents, incurred_at=None))
    session.flush()
    session.refresh(record)
    rows = _records_for_account(session, account)
    account.purchase_cost_cents = None if any(row.cost_cents is None for row in rows) else sum(row.cost_cents for row in rows)
    session.add(account)
    session.flush()


def update_account_purchase_cost(session: Session, account: 'AccountModel', cost_cents: int | None) -> None:
    _validate_cents(cost_cents, allow_unknown=True)
    _serialize_cost_write(session)
    rows = _records_for_account(session, account)
    if not rows and account.purchase_cost_cents is not None:
        record = PurchaseCostRecordModel(record_key=f'legacy:account:{account.id}:{account.identity_id or uuid4()}',
                                         account_id=account.id, identity_id=account.identity_id,
                                         cost_cents=account.purchase_cost_cents, incurred_at=None)
        session.add(record)
        rows = [record]
    if not rows and cost_cents is not None:
        record = PurchaseCostRecordModel(record_key=f'manual:account:{account.id}:{account.identity_id or uuid4()}', account_id=account.id,
                                         identity_id=account.identity_id, cost_cents=cost_cents,
                                         incurred_at=datetime.now(timezone.utc))
        session.add(record)
        rows = [record]
    if rows:
        if cost_cents is None:
            values = [None] * len(rows)
        else:
            weights = [1] * len(rows) if any(row.cost_cents is None for row in rows) else [row.cost_cents for row in rows]
            total = sum(weights)
            values = [cost_cents * weight // total for weight in weights] if total else [cost_cents // len(rows)] * len(rows)
            remainder = cost_cents - sum(values)
            for index in range(remainder):
                values[index] += 1
        for row, value in zip(rows, values):
            row.cost_cents = value
            session.add(row)
    account.purchase_cost_cents = cost_cents
    session.add(account)
    session.flush()
