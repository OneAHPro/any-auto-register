from fastapi import APIRouter, HTTPException, Query

from services.mail_imports import (
    MailImportBatchDeleteRequest,
    MailImportDeleteRequest,
    MailImportDetectionRequest,
    MailImportExecuteRequest,
    MailImportSnapshotRequest,
    mail_import_registry,
)
from services.mail_imports.auto_import import AutoMailImportService

router = APIRouter(prefix="/mail-imports", tags=["mail-imports"])
auto_mail_import_service = AutoMailImportService(mail_import_registry)


@router.get("/providers")
def list_mail_import_providers():
    return {"items": mail_import_registry.descriptors()}


@router.get("/snapshot")
def get_mail_import_snapshot(
    provider_type: str = Query(alias="type"),
    pool_dir: str = "",
    pool_file: str = "",
    preview_limit: int = 100,
):
    try:
        strategy = mail_import_registry.get(provider_type)
        request = MailImportSnapshotRequest(
            type=strategy.descriptor.type,
            pool_dir=pool_dir,
            pool_file=pool_file,
            preview_limit=preview_limit,
        )
        return strategy.get_snapshot(request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("")
def execute_mail_import(body: MailImportExecuteRequest):
    try:
        if body.type == "auto":
            return auto_mail_import_service.execute(body)
        strategy = mail_import_registry.get(body.type)
        return strategy.execute(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/detect")
def detect_mail_import(body: MailImportDetectionRequest):
    return auto_mail_import_service.detect(body.content)


@router.post("/delete")
def delete_mail_import_item(body: MailImportDeleteRequest):
    try:
        strategy = mail_import_registry.get(body.type)
        return strategy.delete(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/batch-delete")
def batch_delete_mail_import_items(body: MailImportBatchDeleteRequest):
    try:
        strategy = mail_import_registry.get(body.type)
        return strategy.batch_delete(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
