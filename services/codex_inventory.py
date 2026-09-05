"""Durable, credential-free local inventory of Codex2API remote accounts."""
from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Mapping

from sqlmodel import Session, select

from core.db import (
    AccountAssignmentModel,
    AccountIdentityModel,
    AccountModel,
    AccountTargetBindingModel,
    CodexInventorySnapshotModel,
    Codex2APITargetModel,
)

_LOCKS: dict[int, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()
_PROBE_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="codex-inventory-probe")

# Explicit operational allow-list. Nested usage objects are filtered recursively
# to ensure credentials cannot enter summary_json even when an upstream adds fields.
_ALLOWED = {
    "id", "remote_id", "remote_account_id", "account_id", "email", "name", "status", "remote_status", "enabled", "locked",
    "account_type", "plan_type", "chatgpt_account_id", "effective_workspace_id",
    "user_id", "created_at", "updated_at", "usage_percent_7d", "display_billed_usd",
    "billed_7d", "usage_7d_requests", "usage_7d_detail", "quota", "quota_7d", "billed_source",
    "reset_7d_at", "reset_5h_at", "codex_reset_at", "codex_5h_reset_at",
    "quota_7d_updated_at", "quota_5h_updated_at", "codex_usage_updated_at", "codex_5h_usage_updated_at",
    "usage_percent_5h", "billed_5h", "workspace_name", "subscription_expires_at",
    "has_5h_window", "quota_placeholder", "_remote_email_missing", "source_updated_at",
}
_SECRET_MARKERS = ("token", "password", "secret", "cookie", "credential", "private_key", "admin_key", "api_key", "bearer")

def _is_secret(k: Any) -> bool:
    n = str(k or "").strip().lower().replace("-", "_")
    return any(m in n for m in _SECRET_MARKERS)

def _clean(value: Any, *, key: str = "") -> Any:
    if isinstance(value, Mapping):
        return {str(k): _clean(v, key=str(k)) for k, v in value.items() if not _is_secret(k)}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in list(value)[:1000]]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)

def _summary(raw: Mapping[str, Any]) -> dict[str, Any]:
    result = {}
    for key, value in raw.items():
        k = str(key)
        if k in _ALLOWED and not _is_secret(k):
            result[k] = _clean(value, key=k)
    rid = raw.get("remote_id") or raw.get("remote_account_id") or raw.get("account_id") or raw.get("id")
    try: rid = int(rid)
    except (TypeError, ValueError): rid = 0
    if rid > 0: result["remote_id"] = rid
    if "email" in result: result["email"] = str(result["email"] or "").strip()
    # Normalize common upstream aliases into the display-facing names used by
    # the account projection while retaining the raw operational fields.
    if "quota_7d_updated_at" not in result and result.get("codex_usage_updated_at"):
        result["quota_7d_updated_at"] = result["codex_usage_updated_at"]
    if "quota_5h_updated_at" not in result and result.get("codex_5h_usage_updated_at"):
        result["quota_5h_updated_at"] = result["codex_5h_usage_updated_at"]
    if "remote_status" not in result and result.get("status") is not None:
        result["remote_status"] = str(result.get("status") or "").lower()
    detail = result.get("usage_7d_detail")
    if isinstance(detail, Mapping):
        if result.get("usage_7d_requests") is None and detail.get("requests") is not None:
            result["usage_7d_requests"] = detail.get("requests")
        if result.get("billed_7d") is None and result.get("display_billed_usd") is None:
            for billing_key in ("account_billed", "user_billed"):
                if detail.get(billing_key) is not None:
                    result["display_billed_usd"] = detail.get(billing_key)
                    result["billed_source"] = "rolling_detail"
                    break
    return result

def _source_timestamp(summary: Mapping[str, Any]) -> str:
    for k in ("source_updated_at", "quota_7d_updated_at", "updated_at", "created_at"):
        if summary.get(k): return str(summary[k])
    return ""

def _lock_for(target_id: int) -> threading.Lock:
    with _LOCKS_GUARD: return _LOCKS.setdefault(int(target_id), threading.Lock())

def _resolve_clients(database_engine, target_id, clients):
    if clients is not None:
        if hasattr(clients, "list_accounts"):
            inferred = target_id
            if inferred is None:
                inferred = getattr(getattr(clients, "target", None), "id", None)
            return [(int(inferred), clients)] if inferred is not None else []
        if isinstance(clients, Mapping):
            return [(int(t), c) for t, c in clients.items() if (target_id is None or int(t)==int(target_id))]
        return [(int(target_id), c) for c in clients if target_id is not None]
    from services.codex2api_target_client import get_target_client
    from core.db import Codex2APITargetModel
    with Session(database_engine) as session:
        stmt = select(Codex2APITargetModel).where(Codex2APITargetModel.enabled == True)  # noqa: E712
        if target_id is not None: stmt = stmt.where(Codex2APITargetModel.id == int(target_id))
        ids = [int(x.id) for x in session.exec(stmt).all() if x.id is not None]
    out=[]
    for tid in ids:
        try: out.append((tid, get_target_client(tid, database_engine)))
        except Exception: out.append((tid, None))
    return out

def _background_probe(client) -> None:
    fn = getattr(client, "trigger_usage_probe", None)
    if callable(fn):
        try: fn()
        except Exception: pass

def sync_inventory(database_engine, target_id=None, refresh=False, clients=None) -> dict[str, int]:
    """Synchronize complete account lists into local snapshots.

    A target error leaves prior rows intact and marks them stale with ``error``.
    Missing flags are changed only after a successful complete list response.
    """
    # Ensure standalone test engines have the model table even when initialized before this model existed.
    CodexInventorySnapshotModel.__table__.create(bind=database_engine, checkfirst=True)
    targets = _resolve_clients(database_engine, target_id, clients)
    counts = {"targets": len(targets), "upserted": 0, "synced": 0, "missing": 0, "errors": 0, "stale": 0}
    for tid, client in targets:
        lock = _lock_for(tid)
        with lock:
            if client is None:
                err = "target client unavailable"; rows = None
            else:
                if refresh: _PROBE_EXECUTOR.submit(_background_probe, client)
                try:
                    rows = client.list_accounts()
                    if rows is None:
                        raise ValueError("account list response is empty")
                    if isinstance(rows, Mapping) and not ("accounts" in rows or "items" in rows):
                        raise ValueError("account list response format is invalid")
                except Exception as exc:
                    rows = None; err = str(exc)[:240]
            with Session(database_engine) as session:
                existing = session.exec(select(CodexInventorySnapshotModel).where(CodexInventorySnapshotModel.target_id == tid)).all()
                if rows is None:
                    counts["errors"] += 1
                    for row in existing: row.error = err; row.updated_at = datetime.now(timezone.utc); session.add(row)
                    counts["stale"] += len(existing)
                    session.commit(); continue
                if isinstance(rows, Mapping):
                    rows = rows.get("accounts") or rows.get("items") or []
                if not isinstance(rows, list):
                    rows = list(rows) if rows is not None else []
                seen=set()
                by_id = {int(x.remote_id): x for x in existing}
                for raw in rows:
                    if not isinstance(raw, Mapping): continue
                    summary = _summary(raw)
                    try: rid = int(summary.get("remote_id") or 0)
                    except (TypeError, ValueError): rid=0
                    if rid <= 0: continue
                    seen.add(rid)
                    row = by_id.get(rid)
                    now=datetime.now(timezone.utc)
                    if row is None:
                        row=CodexInventorySnapshotModel(target_id=tid, remote_id=rid, created_at=now)
                        by_id[rid] = row
                        existing.append(row)
                    row.summary_json=json.dumps(summary, ensure_ascii=False, separators=(",", ":")); row.source_updated_at=_source_timestamp(summary); row.fetched_at=now; row.missing=False; row.error=""; row.updated_at=now
                    session.add(row); counts["upserted"] += 1; counts["synced"] += 1
                for row in existing:
                    if int(row.remote_id) not in seen:
                        row.missing=True; row.error=""; row.updated_at=datetime.now(timezone.utc); session.add(row); counts["missing"] += 1
                session.commit()
    return counts

def read_inventory(database_engine) -> list[dict[str, Any]]:
    """Read snapshots as account-list compatible dictionaries."""
    CodexInventorySnapshotModel.__table__.create(bind=database_engine, checkfirst=True)
    with Session(database_engine) as session: rows=session.exec(select(CodexInventorySnapshotModel).order_by(CodexInventorySnapshotModel.target_id, CodexInventorySnapshotModel.remote_id)).all()
    result=[]
    for row in rows:
        try: data=json.loads(row.summary_json or "{}")
        except Exception: data={}
        if not isinstance(data, dict): data={}
        item=dict(data); item.update({"target_id": int(row.target_id), "remote_id": int(row.remote_id), "_inventory_fetched_at": row.fetched_at.isoformat() if row.fetched_at else "", "_inventory_source_updated_at": row.source_updated_at or "", "_inventory_error": row.error or "", "_inventory_stale": bool(row.error), "_inventory_missing": bool(row.missing)})
        result.append(item)
    return result


def _schedulable(summary: Mapping[str, Any]) -> bool:
    status = str(summary.get("remote_status") or summary.get("status") or "").strip().lower()
    return bool(summary.get("enabled", True)) and not bool(summary.get("locked", False)) and status in {"active", "ready", "rate_limited"}


def materialize_inventory(database_engine) -> dict[str, int]:
    """Create local, credential-free rows for remote accounts in the inventory."""

    rows = [row for row in read_inventory(database_engine) if not row.get("_inventory_missing")]
    created = 0
    updated = 0
    with Session(database_engine) as session:
        for row in rows:
            target_id = int(row.get("target_id") or 0)
            remote_id = int(row.get("remote_id") or 0)
            if target_id <= 0 or remote_id <= 0:
                continue
            email = str(row.get("email") or row.get("name") or f"remote-account-{remote_id}").strip()
            target = session.get(Codex2APITargetModel, target_id)
            pool_id = str(target.default_pool_id if target is not None else "PUBLIC_POOL")
            candidates = session.exec(select(AccountModel).where(AccountModel.platform == "chatgpt")).all()
            account = next((item for item in candidates if str(item.email or "").strip().lower() == email.lower() and item.extra_json.find('"remote_only": true') < 0), None)
            binding = session.exec(
                select(AccountTargetBindingModel)
                .where(AccountTargetBindingModel.target_id == target_id)
                .where(AccountTargetBindingModel.remote_account_id == remote_id)
            ).first()
            identity_id = str(binding.identity_id) if binding is not None else f"codex2api:{target_id}:{remote_id}"
            if account is None and binding is not None and int(binding.local_account_id or 0) > 0:
                account = session.get(AccountModel, int(binding.local_account_id))
            if account is None:
                remote_status = str(row.get("remote_status") or row.get("status") or "").strip().lower()
                account = AccountModel(
                    platform="chatgpt",
                    email=email,
                    password="",
                    token="",
                    status="invalid" if remote_status in {"unauthorized", "auth_error", "invalid", "token_invalidated"} else "registered",
                    identity_id=identity_id,
                    extra_json=json.dumps({
                        "account_source": "codex2api",
                        "remote_only": True,
                        "remote_target_id": target_id,
                        "remote_id": remote_id,
                        "codex_remote_snapshot": dict(row),
                    }, ensure_ascii=False),
                )
                session.add(account)
                session.flush()
                created += 1
            else:
                extra = account.get_extra() if hasattr(account, "get_extra") else {}
                extra["codex_remote_snapshot"] = dict(row)
                extra.setdefault("account_source", "codex2api")
                account.set_extra(extra)
                remote_status = str(row.get("remote_status") or row.get("status") or "").strip().lower()
                if extra.get("remote_only"):
                    account.status = "invalid" if remote_status in {"unauthorized", "auth_error", "invalid", "token_invalidated"} else "registered"
                updated += 1
            identity = session.get(AccountIdentityModel, identity_id)
            if identity is None:
                identity = AccountIdentityModel(id=identity_id, platform="chatgpt", canonical_email=email.lower(), current_account_id=int(account.id or 0))
                session.add(identity)
            else:
                identity.canonical_email = email.lower()
                identity.current_account_id = int(account.id or 0)
                identity.state = "active"
                session.add(identity)
            if binding is None:
                binding = AccountTargetBindingModel(identity_id=identity_id, local_account_id=int(account.id or 0), target_id=target_id, remote_account_id=remote_id)
            binding.local_account_id = int(account.id or 0)
            binding.remote_email = email.lower()
            binding.remote_status = str(row.get("remote_status") or row.get("status") or "")
            binding.enabled = bool(row.get("enabled", True)) and not bool(row.get("locked", False))
            binding.sync_status = "synced"
            binding.last_sync_at = datetime.now(timezone.utc)
            binding.updated_at = datetime.now(timezone.utc)
            session.add(binding)
            assignment = session.exec(select(AccountAssignmentModel).where(AccountAssignmentModel.identity_id == identity_id).where(AccountAssignmentModel.state.in_(["active", "standby"]))).first()
            if _schedulable(row):
                if assignment is None:
                    assignment = AccountAssignmentModel(identity_id=identity_id, local_account_id=int(account.id or 0), pool_id=pool_id, target_id=target_id, state="active", lease_reason="inventory_materialize", lease_started_at=datetime.now(timezone.utc), assignment_version=1)
                else:
                    assignment.local_account_id = int(account.id or 0)
                    assignment.target_id = target_id
                    assignment.state = "active"
                    assignment.updated_at = datetime.now(timezone.utc)
                session.add(assignment)
            elif assignment is not None:
                assignment.state = "standby"
                assignment.lease_reason = "remote_not_schedulable"
                session.add(assignment)
        session.commit()
    return {"created": created, "updated": updated, "total": len(rows)}

__all__=["sync_inventory", "read_inventory", "materialize_inventory"]
