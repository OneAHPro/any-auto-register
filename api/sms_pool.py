from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlmodel import Session, func, select

from core.db import SmsPoolItemModel, engine
from core.sms_pool import DEFAULT_SMS_BASE_URL, mask_sms_code, sms_pool_service


router = APIRouter(prefix="/sms-pool", tags=["sms-pool"])


class SmsPoolImportRequest(BaseModel):
    content: str = Field(min_length=1, max_length=1_000_000)
    default_base_url: str = DEFAULT_SMS_BASE_URL


def _serialize_item(row: SmsPoolItemModel) -> dict:
    return {
        "id": row.id,
        "code": row.code,
        "code_hint": mask_sms_code(row.code),
        "base_url": row.base_url,
        "status": row.status,
        "reserved_task_id": row.reserved_task_id,
        "used_by_email": row.used_by_email,
        "created_at": row.created_at,
        "reserved_at": row.reserved_at,
        "used_at": row.used_at,
        "updated_at": row.updated_at,
    }


@router.get("")
def list_sms_pool_items(
    status: Optional[str] = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
):
    normalized_status = str(status or "").strip().lower()
    if normalized_status and normalized_status not in {
        "unused",
        "reserved",
        "active",
        "used",
    }:
        raise HTTPException(400, "SMS 接码池状态无效")
    with Session(engine) as session:
        query = select(SmsPoolItemModel)
        count_query = select(func.count()).select_from(SmsPoolItemModel)
        if normalized_status:
            query = query.where(SmsPoolItemModel.status == normalized_status)
            count_query = count_query.where(
                SmsPoolItemModel.status == normalized_status
            )
        total = int(session.exec(count_query).one() or 0)
        rows = session.exec(
            query.order_by(SmsPoolItemModel.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [_serialize_item(row) for row in rows],
    }


@router.get("/stats")
def get_sms_pool_stats():
    return sms_pool_service.stats()


@router.post("/import")
def import_sms_pool_items(body: SmsPoolImportRequest):
    try:
        return sms_pool_service.import_text(
            body.content,
            default_base_url=body.default_base_url,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
