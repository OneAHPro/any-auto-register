"""Project-owned Codex2API-compatible credential import and inventory sync."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from core.db import (
    AccountAssignmentModel,
    AccountIdentityModel,
    AccountModel,
    AccountPoolModel,
    AccountTargetBindingModel,
    Codex2APITargetModel,
    PoolTargetPolicyModel,
    get_session,
)
from services.codex_import_parser import ImportFormatError, parse_import_content, parse_import_files
from services.codex_inventory import materialize_inventory, read_inventory, sync_inventory


router = APIRouter(prefix="/codex-import", tags=["codex-import"])
_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="codex-import")
_LOCK = threading.Lock()


class ImportFile(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    content: str = Field(max_length=20 * 1024 * 1024)


class CodexImportRequest(BaseModel):
    pool_id: str = "PUBLIC_POOL"
    target_id: int | None = None
    format: str = "txt"
    files: list[ImportFile] = Field(min_length=1, max_length=1000)


@dataclass
class _ImportJob:
    id: str
    status: str = "queued"
    total: int = 0
    processed: int = 0
    success: int = 0
    updated: int = 0
    duplicate: int = 0
    failed: int = 0
    error: str = ""
    items: list[dict[str, Any]] = field(default_factory=list)

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "total": self.total,
            "processed": self.processed,
            "success": self.success,
            "updated": self.updated,
            "duplicate": self.duplicate,
            "failed": self.failed,
            "items": list(self.items[-1000:]),
            "error": self.error or None,
        }


_JOBS: dict[str, _ImportJob] = {}


def _job_get(job_id: str) -> _ImportJob | None:
    with _LOCK:
        return _JOBS.get(str(job_id))


def _target_for_pool(session: Session, pool_id: str, target_id: int | None) -> Codex2APITargetModel | None:
    if target_id is not None:
        target = session.get(Codex2APITargetModel, int(target_id))
        if target is None or not target.enabled:
            raise HTTPException(status_code=409, detail="目标节点不可用")
        belongs_to_pool = str(target.default_pool_id or "") == str(pool_id)
        if not belongs_to_pool:
            belongs_to_pool = session.exec(
                select(PoolTargetPolicyModel)
                .where(PoolTargetPolicyModel.pool_id == str(pool_id))
                .where(PoolTargetPolicyModel.target_id == int(target.id))
                .where(PoolTargetPolicyModel.enabled == True)  # noqa: E712
            ).first() is not None
        if not belongs_to_pool:
            raise HTTPException(status_code=409, detail="目标节点不属于所选号池")
        return target
    policies = session.exec(
        select(PoolTargetPolicyModel)
        .where(PoolTargetPolicyModel.pool_id == pool_id)
        .where(PoolTargetPolicyModel.enabled == True)  # noqa: E712
        .order_by(PoolTargetPolicyModel.priority, PoolTargetPolicyModel.id)
    ).all()
    if policies:
        target = session.get(Codex2APITargetModel, int(policies[0].target_id))
        if target is not None and target.enabled:
            return target
    target = session.exec(
        select(Codex2APITargetModel)
        .where(Codex2APITargetModel.default_pool_id == pool_id)
        .where(Codex2APITargetModel.enabled == True)  # noqa: E712
        .order_by(Codex2APITargetModel.id)
    ).first()
    return target


def _ensure_pool(session: Session, pool_id: str) -> AccountPoolModel:
    normalized = str(pool_id or "PUBLIC_POOL").strip().upper() or "PUBLIC_POOL"
    pool = session.get(AccountPoolModel, normalized)
    if pool is None or not pool.enabled:
        raise HTTPException(status_code=404, detail="号池不存在或已停用")
    return pool


def _credential_payload(row: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "name", "email", "refresh_token", "session_token", "access_token",
        "id_token", "account_id", "chatgpt_account_id", "plan_type", "expires_at",
        "chatgpt_user_id", "agent_runtime_id", "agent_private_key", "agent_task_id",
    )
    return {key: str(row[key]).strip() for key in keys if str(row.get(key) or "").strip()}


def _identity_and_assignment(session: Session, account: AccountModel, pool: AccountPoolModel, target: Codex2APITargetModel | None) -> None:
    if not account.identity_id:
        identity_id = str(uuid4())
        identity = AccountIdentityModel(
            id=identity_id,
            platform="chatgpt",
            canonical_email=str(account.email or "").strip().lower(),
            current_account_id=int(account.id or 0),
        )
        account.identity_id = identity_id
        session.add(identity)
    if target is None:
        return
    assignment = session.exec(
        select(AccountAssignmentModel)
        .where(AccountAssignmentModel.identity_id == account.identity_id)
        .where(AccountAssignmentModel.state.in_(["active", "draining", "standby"]))
    ).first()
    if assignment is None:
        session.add(AccountAssignmentModel(
            identity_id=account.identity_id,
            local_account_id=int(account.id or 0),
            pool_id=str(pool.id),
            target_id=int(target.id),
            state="active",
            lease_reason="codex_import",
            lease_started_at=datetime.now(timezone.utc),
            assignment_version=1,
        ))
    else:
        assignment.pool_id = str(pool.id)
        assignment.target_id = int(target.id)
        assignment.updated_at = datetime.now(timezone.utc)
        session.add(assignment)


def _match_remote(client: Any, row: dict[str, Any]) -> dict[str, Any] | None:
    try:
        remote_rows = client.list_accounts()
    except Exception:
        return None
    wanted_id = str(row.get("chatgpt_account_id") or row.get("account_id") or "").strip()
    wanted_email = str(row.get("email") or "").strip().lower()
    matches = []
    for item in remote_rows if isinstance(remote_rows, list) else []:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("chatgpt_account_id") or item.get("effective_workspace_id") or item.get("account_id") or "").strip()
        item_email = str(item.get("email") or item.get("name") or "").strip().lower()
        if wanted_id and item_id == wanted_id:
            matches.append(item)
        elif not wanted_id and wanted_email and item_email == wanted_email:
            matches.append(item)
    return matches[0] if len(matches) == 1 else None


def _credential_identity(row: Mapping[str, Any]) -> str:
    for key in (
        "agent_runtime_id", "chatgpt_account_id", "account_id",
        "refresh_token", "session_token", "access_token",
    ):
        value = str(row.get(key) or "").strip()
        if value:
            return f"{key}:{value}"
    return ""


def _import_job(job: _ImportJob, request: CodexImportRequest, database_engine) -> None:
    try:
        job.status = "running"
        files = {item.name: item.content for item in request.files}
        rows = (
            parse_import_files(files)
            if request.format == "auto"
            else [row for item in request.files for row in parse_import_content(item.content, request.format)]
        )
        job.total = len(rows)
        if not rows:
            raise ImportFormatError("文件中未找到有效的 Refresh Token、Session Token 或 Access Token")
        with Session(database_engine) as session:
            pool = _ensure_pool(session, request.pool_id)
            target = _target_for_pool(session, str(pool.id), request.target_id)
            client = None
            if target is not None:
                from services.codex2api_target_client import get_target_client
                client = get_target_client(int(target.id), database_engine)
            seen: set[str] = set()
            for index, row in enumerate(rows, start=1):
                identity = _credential_identity(row).lower()
                item_result = {"index": index, "file": request.files[min(index - 1, len(request.files) - 1)].name, "status": "failed"}
                if not identity or identity in seen:
                    job.duplicate += 1
                    job.processed += 1
                    item_result["status"] = "duplicate"
                    job.items.append(item_result)
                    continue
                seen.add(identity)
                email = str(row.get("email") or row.get("name") or f"import-{index}@codex2api.local").strip()
                existing = session.exec(
                    select(AccountModel).where(AccountModel.platform == "chatgpt").where(AccountModel.email == email)
                ).first()
                if existing is None:
                    for candidate in session.exec(select(AccountModel).where(AccountModel.platform == "chatgpt")).all():
                        try:
                            candidate_extra = candidate.get_extra()
                        except Exception:
                            candidate_extra = {}
                        if _credential_identity(candidate_extra).lower() == identity:
                            existing = candidate
                            break
                elif identity.startswith("chatgpt_account_id:"):
                    try:
                        existing_extra = existing.get_extra()
                    except Exception:
                        existing_extra = {}
                    if _credential_identity(existing_extra).lower() != identity:
                        # Codex2API permits multiple identities sharing an
                        # email; the stable ChatGPT account ID is authoritative.
                        existing = None
                if existing is not None:
                    job.duplicate += 1
                    job.processed += 1
                    item_result.update({"status": "duplicate", "email": email})
                    job.items.append(item_result)
                    continue
                extra = {key: value for key, value in row.items() if key not in {"name", "email", "access_token"}}
                extra["account_source"] = "project_import"
                extra["import_format"] = str(request.format)
                if row.get("access_token"):
                    extra["access_token"] = row["access_token"]
                account = AccountModel(
                    platform="chatgpt",
                    email=email,
                    password="",
                    token=str(row.get("access_token") or ""),
                    status="registered",
                    extra_json=json.dumps(extra, ensure_ascii=False),
                )
                session.add(account)
                session.flush()
                _identity_and_assignment(session, account, pool, target)
                session.commit()
                if client is not None:
                    payload = _credential_payload({**row, "email": email, "name": email})
                    try:
                        if row.get("agent_runtime_id") and row.get("agent_private_key"):
                            importer = getattr(client, "import_agent_identity", None)
                            remote_result = (
                                importer(payload)
                                if callable(importer)
                                else client.import_full_json(payload)
                            )
                        elif row.get("refresh_token") and row.get("access_token"):
                            remote_result = client.import_full_json(payload)
                        elif row.get("refresh_token") or row.get("session_token"):
                            remote_result = client.import_refresh_token(payload)
                        else:
                            remote_result = client.import_access_token(payload)
                        remote = _match_remote(client, row)
                        if remote is not None:
                            extra["codex2api_remote"] = {
                                "target_id": int(target.id),
                                "remote_id": int(remote.get("id") or remote.get("remote_id") or 0),
                                "summary": {k: v for k, v in remote.items() if k not in {"credentials", "refresh_token", "access_token", "session_token", "id_token", "password", "cookies"}},
                            }
                            extra["remote_target_id"] = int(target.id)
                            extra["remote_id"] = int(remote.get("id") or remote.get("remote_id") or 0)
                            extra["codex_remote_snapshot"] = extra["codex2api_remote"]["summary"]
                            extra["codex_remote_snapshot"].update({
                                "target_id": int(target.id),
                                "remote_id": int(remote.get("id") or remote.get("remote_id") or 0),
                            })
                            account.set_extra(extra)
                            binding = AccountTargetBindingModel(
                                identity_id=account.identity_id,
                                local_account_id=int(account.id or 0),
                                target_id=int(target.id),
                                remote_account_id=int(remote.get("id") or remote.get("remote_id") or 0),
                                remote_email=str(remote.get("email") or email).strip().lower(),
                                sync_status="synced",
                                remote_status=str(remote.get("status") or ""),
                                enabled=bool(remote.get("enabled", True)) and not bool(remote.get("locked", False)),
                            )
                            session.add(binding)
                        elif not remote_result:
                            raise RuntimeError("目标节点未确认导入")
                    except Exception as exc:
                        account.status = "invalid"
                        item_result["message"] = f"目标同步失败（{type(exc).__name__}）"
                        job.failed += 1
                        job.processed += 1
                        item_result["email"] = email
                        job.items.append(item_result)
                        continue
                job.success += 1
                job.processed += 1
                item_result.update({"status": "success", "email": email})
                job.items.append(item_result)
            session.commit()
        job.status = "completed"
    except Exception as exc:
        job.status = "failed"
        job.error = str(exc)[:240]


@router.get("/options")
def import_options(session: Session = Depends(get_session)):
    from services.pool_scheduler import ensure_default_pools

    ensure_default_pools(session.get_bind())
    pools = session.exec(select(AccountPoolModel).where(AccountPoolModel.enabled == True)).all()  # noqa: E712
    pools.sort(key=lambda pool: (0 if str(pool.id) == "PUBLIC_POOL" else 1, str(pool.id)))
    targets = session.exec(select(Codex2APITargetModel).where(Codex2APITargetModel.enabled == True).order_by(Codex2APITargetModel.id)).all()  # noqa: E712
    policies = session.exec(select(PoolTargetPolicyModel).where(PoolTargetPolicyModel.enabled == True)).all()  # noqa: E712
    return {
        "default_pool_id": "PUBLIC_POOL",
        "pools": [{
            "id": pool.id,
            "name": pool.name,
            "targets": [
                {"id": int(target.id), "name": target.name, "enabled": bool(target.enabled)}
                for target in targets
                if int(target.id) in {
                    int(policy.target_id) for policy in policies if str(policy.pool_id) == str(pool.id)
                } or str(target.default_pool_id) == str(pool.id)
            ],
        } for pool in pools],
    }


@router.post("", status_code=status.HTTP_202_ACCEPTED)
def start_import(body: CodexImportRequest, session: Session = Depends(get_session)):
    from services.pool_scheduler import ensure_default_pools

    ensure_default_pools(session.get_bind())
    pool = _ensure_pool(session, body.pool_id)
    _target_for_pool(session, str(pool.id), body.target_id)
    job = _ImportJob(id=f"codex-import-{uuid4().hex}")
    with _LOCK:
        _JOBS[job.id] = job
    _EXECUTOR.submit(_import_job, job, body, session.get_bind())
    return {"job_id": job.id, "status": job.status}


@router.get("/jobs/{job_id}")
def get_import_job(job_id: str):
    job = _job_get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="导入任务不存在")
    return job.public()


@router.post("/sync")
def sync_import_inventory(session: Session = Depends(get_session)):
    # First capture the complete account inventory so remote-only rows have a
    # local identity/binding before the usage probe writes quota snapshots.
    result = sync_inventory(session.get_bind(), refresh=False)
    materialized = materialize_inventory(session.get_bind())
    quota_results: list[dict[str, Any]] = []
    try:
        from services.control_plane_workers import collect_target_quota
        from services.codex2api_target_client import get_target_client

        targets = session.exec(
            select(Codex2APITargetModel)
            .where(Codex2APITargetModel.enabled == True)  # noqa: E712
            .order_by(Codex2APITargetModel.id)
        ).all()
        for target in targets:
            try:
                client = get_target_client(int(target.id), session.get_bind())
                quota = collect_target_quota(
                    session.get_bind(), target_id=int(target.id), client=client,
                )
                quota_results.append({"target_id": quota.target_id, "collected_accounts": quota.collected_accounts})
            except Exception as exc:
                quota_results.append({"target_id": int(target.id), "error": type(exc).__name__})
    except Exception as exc:
        quota_results.append({"error": type(exc).__name__})
    result_after_quota = sync_inventory(session.get_bind(), refresh=False)
    materialized_after_quota = materialize_inventory(session.get_bind())
    return {
        "status": "completed",
        "inventory": result_after_quota,
        "initial_inventory": result,
        "materialized": materialized_after_quota,
        "initial_materialized": materialized,
        "quota": quota_results,
    }


__all__ = ["router"]
