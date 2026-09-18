"""Durable, credential-free local inventory of Codex2API remote accounts."""
from __future__ import annotations

import json
import threading
from collections import Counter
from contextlib import ExitStack
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import uuid4

from sqlmodel import Session, select

from core.db import (
    AccountAssignmentModel,
    AccountIdentityModel,
    AccountModel,
    AccountTargetBindingModel,
    CodexInventorySnapshotModel,
    Codex2APITargetModel,
)
from services.codex2api_db import get_codex2api_db_adapter
from services.codex2api_remote_accounts import remote_account_email, remote_bool

_LOCKS: dict[int, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()
_PROBE_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="codex-inventory-probe")
_DB_METADATA_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="codex-inventory-db")
_DB_METADATA_TIMEOUT_SECONDS = 4.0

# Explicit operational allow-list. Nested usage objects are filtered recursively
# to ensure credentials cannot enter summary_json even when an upstream adds fields.
_ALLOWED = {
    "id", "remote_id", "remote_account_id", "account_id", "email", "name", "status", "remote_status", "enabled", "locked", "platform",
    "account_type", "plan_type", "chatgpt_account_id", "workspace_id", "effective_workspace_id",
    "user_id", "created_at", "updated_at", "usage_percent_7d", "display_billed_usd",
    "billed_7d", "usage_7d_requests", "usage_7d_detail", "quota", "quota_7d", "billed_source",
    "reset_7d_at", "reset_5h_at", "codex_reset_at", "codex_5h_reset_at", "cooldown_reason", "cooldown_until", "deleted_at",
    "quota_7d_updated_at", "quota_5h_updated_at", "codex_usage_updated_at", "codex_5h_usage_updated_at",
    "usage_percent_5h", "billed_5h", "workspace_name", "subscription_expires_at",
    "has_5h_window", "quota_placeholder", "_remote_email_missing", "source_updated_at",
    "source",
}
_SECRET_MARKERS = ("token", "password", "secret", "cookie", "credential", "private_key", "admin_key", "api_key", "bearer")
_STABLE_ACCOUNT_KEYS = {
    "account_id",
    "chatgpt_account_id",
    "user_id",
    "workspace_id",
    "effective_workspace_id",
}
_NON_CODEX_CHANNEL_VALUES = frozenset(
    {"grok", "xai", "antigravity", "claude"}
)
_ASSIGNMENT_STATES = {
    "active",
    "draining",
    "planned",
    "locking",
    "uploading",
    "target_disabled",
    "verifying",
    "assignment_committing",
    "source_cleaning",
    "target_enabling",
    "migrating",
    "pending",
}

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
    rid = 0
    for candidate in (
        raw.get("remote_id"),
        raw.get("remote_account_id"),
        raw.get("id"),
        raw.get("account_id"),
    ):
        if candidate not in (None, "") and not isinstance(candidate, (int, float)):
            try:
                parsed = int(candidate)
            except (TypeError, ValueError, OverflowError):
                # A populated ID field with junk text is malformed.  Do not
                # silently fall through to another alias and bind a different
                # remote row.
                return result
            if parsed <= 0:
                return result
            rid = parsed
            break
        if isinstance(candidate, bool) or (
            isinstance(candidate, float) and not candidate.is_integer()
        ):
            # An explicitly supplied primary ID with a lossy numeric type is
            # malformed.  Do not fall through to a secondary alias (for
            # example ``id=7``) and accidentally bind the wrong account.
            return result
        try:
            parsed = int(candidate or 0)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            rid = parsed
            break
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


def _strict_positive_target(value: Any) -> int | None:
    """Parse an optional target marker without lossy coercion."""

    if value in (None, ""):
        return None
    if isinstance(value, bool) or (
        isinstance(value, float) and not value.is_integer()
    ):
        return 0
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return parsed if parsed > 0 else 0


def _row_is_deleted_or_non_codex(row: Mapping[str, Any]) -> bool:
    """Filter soft-deleted and explicitly non-Codex channel rows."""

    deleted_at = row.get("deleted_at")
    if deleted_at not in (None, "", False, 0):
        return True
    status = str(row.get("status") or row.get("remote_status") or "").strip().casefold()
    if status in {"deleted", "removed", "soft_deleted"}:
        return True
    if str(row.get("error_message") or "").strip().casefold() == "deleted":
        return True
    sources: list[Mapping[str, Any]] = [row]
    for nested_key in ("credentials", "account", "identity"):
        nested = row.get(nested_key)
        if isinstance(nested, Mapping):
            sources.append(nested)
    markers = tuple(
        source.get(key)
        for source in sources
        for key in ("upstream_type", "channel", "provider", "platform", "account_type", "type")
    )
    return any(
        str(marker or "").strip().casefold() in _NON_CODEX_CHANNEL_VALUES
        for marker in markers
    )

def _source_timestamp(summary: Mapping[str, Any]) -> str:
    for k in ("source_updated_at", "quota_7d_updated_at", "updated_at", "created_at"):
        if summary.get(k): return str(summary[k])
    return ""


def _utc_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _stable_id(value: Any) -> str:
    return str(value or "").strip().casefold()


def _collect_stable_ids(value: Any, result: set[str]) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized_key = str(key or "").strip().lower().replace("-", "_")
            if normalized_key in _STABLE_ACCOUNT_KEYS:
                if isinstance(child, (str, int)) and not isinstance(child, bool):
                    normalized = _stable_id(child)
                    if normalized:
                        result.add(normalized)
                elif isinstance(child, (list, tuple, set)):
                    for item in child:
                        normalized = _stable_id(item)
                        if normalized:
                            result.add(normalized)
            _collect_stable_ids(child, result)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            _collect_stable_ids(item, result)


def _row_stable_ids(row: Mapping[str, Any]) -> set[str]:
    result: set[str] = set()
    _collect_stable_ids(row, result)
    return result


def _account_stable_ids(account: AccountModel) -> set[str]:
    result: set[str] = set()
    for value in (
        getattr(account, "user_id", ""),
        getattr(account, "identity_id", ""),
    ):
        normalized = _stable_id(value)
        if normalized:
            result.add(normalized)
    try:
        extra = account.get_extra()
    except Exception:
        extra = {}
    _collect_stable_ids(extra, result)
    return result


def _remember_remote_aliases(extra: dict[str, Any], row: Mapping[str, Any]) -> None:
    """Persist non-secret provider aliases across target snapshots."""

    aliases = extra.get("codex_remote_aliases")
    aliases = dict(aliases) if isinstance(aliases, Mapping) else {}
    for alias_type, keys in {
        "workspace_id": ("workspace_id", "workspaceId", "effective_workspace_id"),
        "chatgpt_account_id": (
            "chatgpt_account_id", "chatgptAccountId", "account_id", "accountId", "user_id",
        ),
    }.items():
        values: list[str] = []
        existing = aliases.get(alias_type)
        if isinstance(existing, (list, tuple, set)):
            values.extend(str(value).strip() for value in existing if str(value).strip())
        elif existing not in (None, ""):
            values.append(str(existing).strip())
        for key in keys:
            value = row.get(key)
            if value not in (None, ""):
                values.append(str(value).strip())
        deduped = list(dict.fromkeys(value for value in values if value))
        if deduped:
            aliases[alias_type] = deduped
    if aliases:
        extra["codex_remote_aliases"] = aliases


def _account_is_remote_only(account: AccountModel) -> bool:
    try:
        extra = account.get_extra()
    except Exception:
        extra = {}
    return isinstance(extra, Mapping) and bool(extra.get("remote_only"))


def _account_remote_key(account: AccountModel) -> tuple[int, int] | None:
    try:
        extra = account.get_extra()
    except Exception:
        extra = {}
    if not isinstance(extra, Mapping):
        return None
    snapshot = extra.get("codex_remote_snapshot")
    snapshot = snapshot if isinstance(snapshot, Mapping) else {}
    try:
        target_id = int(extra.get("remote_target_id") or snapshot.get("target_id") or 0)
        remote_id = int(extra.get("remote_id") or snapshot.get("remote_id") or 0)
    except (TypeError, ValueError):
        return None
    return (target_id, remote_id) if target_id > 0 and remote_id > 0 else None

def _lock_for(target_id: int) -> threading.Lock:
    with _LOCKS_GUARD: return _LOCKS.setdefault(int(target_id), threading.Lock())


def _acquire_inventory_database_lock(session: Session, target_id: int) -> bool:
    """Fence complete-list reconciliation across application processes."""

    try:
        dialect = str(session.get_bind().dialect.name).lower()
        connection = session.connection()
        if dialect == "sqlite":
            # SQLite's process-wide write reservation complements the
            # in-process target lock used by the normal worker path.
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        elif dialect == "postgresql":
            # Advisory locks are transaction-scoped and do not require a new
            # schema column.  Keep the key in a private numeric range.
            connection.exec_driver_sql(
                "SELECT pg_advisory_xact_lock(%s)",
                (8_310_000 + int(target_id),),
            )
        return True
    except Exception as exc:
        # A lock/contention failure must not silently proceed as if the target
        # were fenced.  The caller records a target error and leaves prior
        # snapshots untouched; only engines that explicitly do not support a
        # primitive may opt into the generation-fence fallback.
        try:
            session.rollback()
        except Exception:
            pass
        message = str(exc).lower()
        if any(marker in message for marker in ("no such function", "syntax error", "not supported")):
            return True
        return False


def _record_inventory_sync_marker(
    session: Session,
    target_id: int,
    observed_at: datetime,
    *,
    error: str = "",
) -> None:
    target = session.get(Codex2APITargetModel, int(target_id))
    if target is None:
        return
    current_sync = _utc_datetime(target.inventory_last_sync_at)
    if current_sync is not None and current_sync > observed_at:
        return
    target.inventory_last_error = str(error or "")[:240]
    if not error:
        target.inventory_last_sync_at = observed_at
    target.updated_at = datetime.now(timezone.utc)
    session.add(target)


_DB_UNAVAILABLE = object()


class _DatabasePreferredClient:
    """Expose the account-list client with PostgreSQL-first resolution."""

    def __init__(self, target_id: int, database_engine: Any, adapter: Any) -> None:
        self.target_id = int(target_id)
        self.database_engine = database_engine
        self.adapter = adapter
        self._http_client: Any = None
        self.last_source = ""
        self.last_error = ""

    def _database_rows(self) -> Any:
        method = getattr(self.adapter, "fetch_account_metadata", None)
        if not callable(method):
            method = getattr(self.adapter, "list_account_metadata", None)
        if not callable(method):
            self.last_error = "database adapter has no account metadata method"
            return _DB_UNAVAILABLE
        def read_rows():
            try:
                return method()
            except TypeError:
                # Permit simple adapters whose method takes the target ID.
                return method(self.target_id)

        future = _DB_METADATA_EXECUTOR.submit(read_rows)
        try:
            rows = future.result(timeout=max(float(_DB_METADATA_TIMEOUT_SECONDS), 0.01))
        except TimeoutError:
            self.last_error = "database account metadata timed out"
            future.cancel()
            return _DB_UNAVAILABLE
        except Exception as exc:
            self.last_error = str(exc)[:240]
            return _DB_UNAVAILABLE
        status = getattr(self.adapter, "last_status", None)
        if isinstance(status, Mapping) and status.get("available") is False:
            self.last_error = str(status.get("error") or status.get("status") or "database unavailable")[:240]
            return _DB_UNAVAILABLE
        if rows is None:
            self.last_error = "database account metadata is empty"
            return _DB_UNAVAILABLE
        if isinstance(rows, Mapping):
            if "accounts" in rows:
                rows = rows.get("accounts")
            elif "items" in rows:
                rows = rows.get("items")
            else:
                self.last_error = "database account metadata format is invalid"
                return _DB_UNAVAILABLE
            if rows is None:
                self.last_error = "database account metadata field is empty"
                return _DB_UNAVAILABLE
        if not isinstance(rows, list):
            try:
                rows = list(rows)
            except Exception as exc:
                self.last_error = (
                    f"database account metadata iteration failed ({type(exc).__name__})"
                )[:240]
                return _DB_UNAVAILABLE
        # A reader is scoped to exactly one target.  Reject a projection that
        # claims another target (or contains a non-object row) before the
        # caller can mark local rows missing or materialize a remote binding.
        normalized_rows: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, Mapping):
                self.last_error = "database account metadata contains an invalid row"
                return _DB_UNAVAILABLE
            raw_target = row.get("target_id")
            if raw_target not in (None, ""):
                if isinstance(raw_target, bool) or (
                    isinstance(raw_target, float) and not raw_target.is_integer()
                ):
                    self.last_error = "database account metadata target_id is invalid"
                    return _DB_UNAVAILABLE
                try:
                    if int(raw_target) != self.target_id:
                        self.last_error = "database account metadata target_id does not match target"
                        return _DB_UNAVAILABLE
                except (TypeError, ValueError, OverflowError):
                    self.last_error = "database account metadata target_id is invalid"
                    return _DB_UNAVAILABLE
            normalized_rows.append(dict(row))
        rows = normalized_rows
        if not rows and not (
            isinstance(status, Mapping) and status.get("available") is True
        ):
            # An unmarked empty result is indistinguishable from a failed
            # database query. Treat it as unavailable so a transient adapter
            # problem cannot mark every previously known account missing.
            self.last_error = "database account metadata availability is unconfirmed"
            return _DB_UNAVAILABLE
        return rows

    def _http(self) -> Any:
        if self._http_client is None:
            from services.codex2api_target_client import get_target_client

            self._http_client = get_target_client(self.target_id, self.database_engine)
        return self._http_client

    def list_accounts(self) -> list[dict[str, Any]]:
        rows = self._database_rows()
        if rows is not _DB_UNAVAILABLE:
            self.last_source = "postgresql"
            return rows
        self.last_source = "http"
        return self._http().list_accounts()

    def trigger_usage_probe(self) -> Any:
        # A probe is an API-side operation; keep it available when refresh=True
        # while account listing itself remains database-first.
        return self._http().trigger_usage_probe()

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
        try:
            adapter = get_codex2api_db_adapter(tid)
        except Exception:
            adapter = None
        if adapter is not None:
            out.append((tid, _DatabasePreferredClient(tid, database_engine, adapter)))
            continue
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
    from services.account_identity import move_assignments_to_standby
    from services.chatgpt_account_coordination import codex2api_target_lock
    targets = _resolve_clients(database_engine, target_id, clients)
    counts = {"targets": len(targets), "upserted": 0, "synced": 0, "missing": 0, "errors": 0, "stale": 0}
    for tid, client in targets:
        lock = _lock_for(tid)
        with lock, codex2api_target_lock(tid):
            response_observed_at = datetime.now(timezone.utc)
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
                    response_observed_at = datetime.now(timezone.utc)
                except Exception as exc:
                    rows = None; err = str(exc)[:240]
                    response_observed_at = datetime.now(timezone.utc)
            with Session(database_engine) as session:
                if not _acquire_inventory_database_lock(session, tid):
                    counts["errors"] += 1
                    continue
                existing = session.exec(select(CodexInventorySnapshotModel).where(CodexInventorySnapshotModel.target_id == tid)).all()
                if rows is None:
                    counts["errors"] += 1
                    _record_inventory_sync_marker(
                        session, tid, response_observed_at, error=err
                    )
                    changed_rows = 0
                    for row in existing:
                        if (
                            _utc_datetime(row.updated_at) is not None
                            and _utc_datetime(row.updated_at) > response_observed_at
                        ):
                            continue
                        row.error = err; row.updated_at = datetime.now(timezone.utc); session.add(row); changed_rows += 1
                    counts["stale"] += changed_rows
                    session.commit(); continue
                if isinstance(rows, Mapping):
                    if "accounts" in rows:
                        rows = rows.get("accounts")
                    elif "items" in rows:
                        rows = rows.get("items")
                    else:
                        rows = None
                    if rows is None:
                        err = "目标节点账号清单字段为空"
                    elif not isinstance(rows, list):
                        err = "目标节点账号清单字段格式无效"
                        rows = None
                if not isinstance(rows, list):
                    try:
                        rows = list(rows) if rows is not None else []
                    except Exception as exc:
                        rows = None
                        err = f"账号清单迭代失败（{type(exc).__name__}）"
                if rows is None:
                    _record_inventory_sync_marker(
                        session,
                        tid,
                        response_observed_at,
                        error=err,
                    )
                    counts["errors"] += 1
                    changed_rows = 0
                    for row in existing:
                        current_updated = _utc_datetime(row.updated_at)
                        if current_updated is not None and current_updated > response_observed_at:
                            continue
                        row.error = err
                        row.updated_at = datetime.now(timezone.utc)
                        session.add(row)
                        changed_rows += 1
                    counts["stale"] += changed_rows
                    session.commit()
                    continue
                # Normalize each provider row once. Large enterprise pools can
                # contain thousands of accounts; repeating the recursive
                # credential scrub for validation, duplicate detection, and
                # persistence needlessly multiplies the JSON work.
                normalized_entries: list[tuple[Mapping[str, Any], dict[str, Any]]] = []
                invalid_rows = []
                filtered_rows: list[Mapping[str, Any]] = []
                for raw in rows:
                    if not isinstance(raw, Mapping):
                        invalid_rows.append(raw)
                        continue
                    # A client supplied for one target must never be able to
                    # write a row explicitly belonging to another target.
                    # The database adapter already scopes this field, but the
                    # HTTP/fixture path needs the same fence.
                    if "target_id" in raw and raw.get("target_id") not in (None, ""):
                        marker = _strict_positive_target(raw.get("target_id"))
                        if marker != int(tid):
                            invalid_rows.append(raw)
                            continue
                    # Deleted and non-Codex rows are absent from the logical
                    # inventory. They are filtered before ID validation so a
                    # soft-deleted record cannot poison an otherwise complete
                    # response.
                    if _row_is_deleted_or_non_codex(raw):
                        continue
                    filtered_rows.append(raw)
                rows = filtered_rows
                for raw in rows:
                    summary = _summary(raw)
                    normalized_entries.append((raw, summary))
                    if int(summary.get("remote_id") or 0) <= 0:
                        invalid_rows.append(raw)
                if invalid_rows:
                    error = "目标节点返回的账号清单包含无效远端 ID"
                    _record_inventory_sync_marker(
                        session, tid, response_observed_at, error=error
                    )
                    counts["errors"] += 1
                    changed_rows = 0
                    for row in existing:
                        current_updated = _utc_datetime(row.updated_at)
                        if current_updated is not None and current_updated > response_observed_at:
                            continue
                        row.error = error
                        row.updated_at = datetime.now(timezone.utc)
                        session.add(row)
                        changed_rows += 1
                    counts["stale"] += changed_rows
                    session.commit()
                    continue
                # A complete list with the same positive provider ID twice is
                # ambiguous.  Silently letting the later row win can attach a
                # different email/quota history to an existing binding, so
                # retain the last known snapshots and surface a stale target
                # error until the upstream list is corrected.
                normalized_ids = [
                    int(summary.get("remote_id") or 0)
                    for _raw, summary in normalized_entries
                    if int(summary.get("remote_id") or 0) > 0
                ]
                duplicate_ids = {
                    remote_id
                    for remote_id, count in Counter(normalized_ids).items()
                    if count > 1
                }
                if duplicate_ids:
                    error = (
                        "目标节点返回重复远端账号 ID: "
                        + ",".join(str(value) for value in sorted(duplicate_ids))
                    )[:240]
                    _record_inventory_sync_marker(
                        session, tid, response_observed_at, error=error
                    )
                    counts["errors"] += 1
                    changed_rows = 0
                    for row in existing:
                        current_updated = _utc_datetime(row.updated_at)
                        if current_updated is not None and current_updated > response_observed_at:
                            continue
                        row.error = error
                        row.updated_at = datetime.now(timezone.utc)
                        session.add(row)
                        changed_rows += 1
                    counts["stale"] += changed_rows
                    session.commit()
                    continue
                seen=set()
                by_id = {int(x.remote_id): x for x in existing}
                for _raw, summary in normalized_entries:
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
                    else:
                        # A second worker may have completed a newer full
                        # snapshot while this response was in flight.  Do not
                        # let the older response overwrite it or make it look
                        # missing; refresh the object before comparing the
                        # generation fence.
                        try:
                            session.refresh(row)
                        except Exception:
                            pass
                        current_fetched = _utc_datetime(row.fetched_at)
                        if current_fetched is not None and current_fetched > response_observed_at:
                            seen.add(rid)
                            continue
                        incoming_source = _utc_datetime(_source_timestamp(summary))
                        current_source = _utc_datetime(row.source_updated_at)
                        if (
                            incoming_source is not None
                            and current_source is not None
                            and incoming_source < current_source
                        ):
                            # The provider returned an older quota projection
                            # after a newer one was already stored. Preserve
                            # the complete row and count it as observed.
                            seen.add(rid)
                            continue
                    row.summary_json=json.dumps(summary, ensure_ascii=False, separators=(",", ":")); row.source_updated_at=_source_timestamp(summary); row.fetched_at=response_observed_at; row.missing=False; row.error=""; row.updated_at=now
                    session.add(row); counts["upserted"] += 1; counts["synced"] += 1
                for row in existing:
                    current_fetched = _utc_datetime(row.fetched_at)
                    if current_fetched is not None and current_fetched > response_observed_at:
                        continue
                    if int(row.remote_id) not in seen:
                        row.missing=True; row.error=""; row.updated_at=datetime.now(timezone.utc); session.add(row); counts["missing"] += 1
                _record_inventory_sync_marker(
                    session, tid, response_observed_at
                )
                missing_remote_ids = {
                    int(row.remote_id)
                    for row in existing
                    if bool(row.missing) and int(row.remote_id or 0) > 0
                }
                if missing_remote_ids:
                    missing_bindings = session.exec(
                        select(AccountTargetBindingModel)
                        .where(AccountTargetBindingModel.target_id == tid)
                        .where(AccountTargetBindingModel.remote_account_id.in_(missing_remote_ids))
                        .where(AccountTargetBindingModel.enabled == True)  # noqa: E712
                    ).all()
                    for binding in missing_bindings:
                        binding_sync = _utc_datetime(binding.last_sync_at)
                        if binding_sync is not None and binding_sync > response_observed_at:
                            continue
                        binding.enabled = False
                        binding.sync_status = "remote_missing"
                        binding.remote_status = "remote_missing"
                        binding.last_error = "目标节点未找到账号"
                        binding.updated_at = datetime.now(timezone.utc)
                        session.add(binding)
                        move_assignments_to_standby(
                            session,
                            identity_id=str(binding.identity_id),
                            target_id=int(tid),
                            reason="remote_account_missing",
                        )
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
    return (
        remote_bool(summary.get("enabled"), True)
        and not remote_bool(summary.get("locked"), False)
        and status in {"active", "ready", "rate_limited"}
    )


_MATERIALIZE_LOCK = threading.RLock()


def materialize_inventory(database_engine) -> dict[str, int]:
    # The account page and operations page can discover the same new row together.
    with _MATERIALIZE_LOCK:
        from services.chatgpt_account_coordination import codex2api_target_lock

        target_ids = {
            int(row.get("target_id") or 0)
            for row in read_inventory(database_engine)
            if int(row.get("target_id") or 0) > 0
        }
        with ExitStack() as lock_stack:
            for target_id in sorted(target_ids):
                lock_stack.enter_context(codex2api_target_lock(target_id))
            return _materialize_inventory(database_engine)


def _materialize_inventory(database_engine) -> dict[str, int]:
    """Create local, credential-free rows for remote accounts in the inventory."""

    from services.account_identity import (
        move_assignments_to_standby,
        supersede_other_target_bindings,
    )

    rows = [
        row
        for row in read_inventory(database_engine)
        if not row.get("_inventory_missing")
        and not row.get("_inventory_stale")
        and not str(row.get("_inventory_error") or "").strip()
    ]
    created = 0
    updated = 0
    with Session(database_engine) as session:
        candidates = session.exec(
            select(AccountModel).where(AccountModel.platform == "chatgpt")
        ).all()
        candidate_object_ids = {id(candidate) for candidate in candidates}
        by_stable_id: dict[str, list[AccountModel]] = {}
        by_email: dict[str, list[AccountModel]] = {}
        target_bindings = session.exec(
            select(AccountTargetBindingModel)
        ).all()
        bound_account_ids: dict[tuple[int, int], set[int]] = {}
        for existing_binding in target_bindings:
            target_key = int(existing_binding.target_id or 0)
            remote_key = int(existing_binding.remote_account_id or 0)
            if target_key <= 0 or remote_key <= 0:
                continue
            identity_key = str(existing_binding.identity_id or "").strip()
            local_key = int(existing_binding.local_account_id or 0)
            if local_key > 0:
                bound_account_ids.setdefault((local_key, target_key), set()).add(remote_key)
        claimed_account_ids: set[tuple[int, int]] = set()
        for candidate in candidates:
            for stable_id in _account_stable_ids(candidate):
                by_stable_id.setdefault(stable_id, []).append(candidate)
            email_key = _stable_id(candidate.email)
            if email_key:
                by_email.setdefault(email_key, []).append(candidate)
        for row in rows:
            target_id = int(row.get("target_id") or 0)
            remote_id = int(row.get("remote_id") or 0)
            if target_id <= 0 or remote_id <= 0:
                continue
            target = session.get(Codex2APITargetModel, target_id)
            pool_id = str(target.default_pool_id if target is not None else "PUBLIC_POOL")
            binding = session.exec(
                select(AccountTargetBindingModel)
                .where(AccountTargetBindingModel.target_id == target_id)
                .where(AccountTargetBindingModel.remote_account_id == remote_id)
            ).first()
            raw_remote_email = str(row.get("email") or "").strip()
            remote_email_value = remote_account_email(row).strip()
            # ``remote_account_email`` falls back to ``name`` for display.
            # Keep a separate flag so a missing provider email does not make
            # a stable-ID match look like an email mismatch.
            email_missing = bool(row.get("_remote_email_missing")) or not bool(raw_remote_email)
            email = remote_email_value
            if not email:
                email = str(
                    binding.remote_email if binding is not None else f"remote-account-{remote_id}"
                ).strip()
            account = None
            row_stable_ids = _row_stable_ids(row)
            if binding is not None and int(binding.local_account_id or 0) > 0:
                account = session.get(AccountModel, int(binding.local_account_id))
                if account is not None and str(account.platform or "").lower() != "chatgpt":
                    account = None
                account_stable_ids = _account_stable_ids(account) if account is not None else set()
                stable_identity_matches = bool(
                    row_stable_ids
                    and account_stable_ids
                    and account_stable_ids.intersection(row_stable_ids)
                )
                account_email = _stable_id(account.email) if account is not None else ""
                binding_email = _stable_id(binding.remote_email) if binding is not None else ""
                # A changed provider email is safe to follow when the existing
                # binding was already attached to this credential email (or
                # had no email). If neither side agrees, prefer an exact
                # credential email match and leave the old binding quarantined.
                rotation_is_consistent = not binding_email or binding_email == account_email
                if account is not None and (
                    (target_id, int(account.id or 0)) in claimed_account_ids
                    or (
                        _stable_id(account.email) != _stable_id(email)
                        and not (stable_identity_matches and rotation_is_consistent)
                    )
                    or (row_stable_ids and account_stable_ids and not stable_identity_matches)
                ):
                    # A stale binding can point at a different row that
                    # shares a provider account ID. Let the exact email match
                    # below repair that mapping during reconciliation.
                    account = None
                if account is not None:
                    claimed_account_ids.add((target_id, int(account.id or 0)))
            # Email is the strongest match when the provider has reused a
            # ChatGPT/workspace ID across several remote rows. Prefer the
            # exact credential email before falling back to that shared ID;
            # otherwise one row can inherit another row's billing history.
            if account is None or _account_is_remote_only(account):
                email_matches = by_email.get(_stable_id(email), [])
                credential_email_matches = [
                    candidate for candidate in email_matches
                    if (
                        not _account_is_remote_only(candidate)
                        and (target_id, int(candidate.id or 0)) not in claimed_account_ids
                        # An exact credential email is sufficient when the
                        # provider row has no stable alias.  If it does carry
                        # one, require an intersection so a reused account ID
                        # cannot attach the row to the wrong credential.
                        and (
                            not row_stable_ids
                            or _account_stable_ids(candidate).intersection(row_stable_ids)
                        )
                    )
                ]
                if len(credential_email_matches) == 1:
                    account = credential_email_matches[0]
            # Capture the binding identity before a stale-row repair changes
            # its local owner.  If the exact credential email resolves to a
            # different identity, the old assignment must be quarantined and
            # the natural target/remote key must move to the credential's
            # identity instead of silently inheriting the stale one.
            binding_identity_before_retarget = (
                str(binding.identity_id or "").strip()
                if binding is not None
                else ""
            )
            account_identity_before_retarget = (
                str(account.identity_id or "").strip()
                if account is not None
                else ""
            )
            binding_retargeted = bool(
                binding is not None
                and account is not None
                and not _account_is_remote_only(account)
                and binding_identity_before_retarget != account_identity_before_retarget
                and (
                    binding_identity_before_retarget
                    or account_identity_before_retarget
                )
            )
            if binding_retargeted and binding_identity_before_retarget:
                move_assignments_to_standby(
                    session,
                    identity_id=binding_identity_before_retarget,
                    target_id=target_id,
                    reason="stale_binding_retargeted",
                )
            if account is not None and not _account_is_remote_only(account):
                if binding is not None and int(binding.local_account_id or 0) != int(account.id or 0):
                    # Transfer the target row from a duplicate remote-only
                    # local account to the credential-bearing account while
                    # retaining the unique remote slot.
                    preferred_binding = None
                    if str(account.identity_id or "").strip():
                        preferred_binding = session.exec(
                            select(AccountTargetBindingModel)
                            .where(AccountTargetBindingModel.identity_id == str(account.identity_id).strip())
                            .where(AccountTargetBindingModel.target_id == target_id)
                        ).first()
                    if preferred_binding is not None and int(preferred_binding.id or 0) != int(binding.id or 0):
                        binding.remote_account_id = 0
                        binding.remote_email = ""
                        binding.enabled = False
                        binding.sync_status = "superseded"
                        binding.remote_status = "superseded"
                        binding.last_error = "远端账号身份已转移到当前凭据账号"
                        binding.updated_at = datetime.now(timezone.utc)
                        session.add(binding)
                        session.flush()
                        binding = preferred_binding
                    else:
                        binding.local_account_id = int(account.id or 0)
                        if str(account.identity_id or "").strip():
                            binding.identity_id = str(account.identity_id).strip()
                        binding.last_error = "远端账号身份已转移到当前凭据账号"
                        binding.updated_at = datetime.now(timezone.utc)
                        session.add(binding)
            if account is None and row_stable_ids:
                stable_matches = []
                seen_ids: set[int] = set()
                for stable_id in row_stable_ids:
                    for candidate in by_stable_id.get(stable_id, []):
                        candidate_id = int(candidate.id or 0)
                        if (
                            candidate_id > 0
                            and candidate_id not in seen_ids
                            and (target_id, candidate_id) not in claimed_account_ids
                        ):
                            seen_ids.add(candidate_id)
                            stable_matches.append(candidate)
                credential_matches = [
                    candidate for candidate in stable_matches
                    # A credential-backed row is preferred, but a
                    # credential-free row from another target is still the
                    # same account when the provider gives us an exact
                    # stable workspace/account alias.  Reusing that row
                    # lets one identity own bindings in multiple pools while
                    # retaining each target/remote billing key separately.
                    if (
                        (
                            not _account_is_remote_only(candidate)
                            and (
                                email_missing
                                or not _stable_id(candidate.email)
                                or not _stable_id(email)
                                or _stable_id(candidate.email) == _stable_id(email)
                            )
                        )
                        or (
                            bool(_account_stable_ids(candidate).intersection(row_stable_ids))
                            and (
                                email_missing
                                or not _stable_id(candidate.email)
                                or not _stable_id(email)
                                or _stable_id(candidate.email) == _stable_id(email)
                            )
                        )
                    )
                ]
                if len(credential_matches) == 1:
                    account = credential_matches[0]
            if account is None:
                email_matches = by_email.get(_stable_id(email), [])
                remote_email_matches = [
                    candidate for candidate in email_matches
                    if (
                        _account_is_remote_only(candidate)
                        and (target_id, int(candidate.id or 0)) not in claimed_account_ids
                        and not bound_account_ids.get((int(candidate.id or 0), target_id), set())
                        and (
                            _account_remote_key(candidate) is None
                            or _account_remote_key(candidate) == (target_id, remote_id)
                        )
                    )
                ]
                if len(remote_email_matches) == 1:
                    account = remote_email_matches[0]
            if binding_retargeted:
                # Keep a credential account's existing identity whenever it
                # has one.  Legacy rows can have no identity yet; generate a
                # fresh value so the stale binding identity is never reused.
                identity_id = account_identity_before_retarget or (
                    f"codex2api:retarget:{int(account.id or 0)}:{uuid4().hex}"
                )
            else:
                identity_id = (
                    str(binding.identity_id)
                    if binding is not None
                    else str(getattr(account, "identity_id", "") or "").strip()
                    or f"codex2api:{target_id}:{remote_id}"
                )
            if binding is None:
                # A stable identity may keep its project binding while the
                # provider rotates the numeric remote ID. Reuse that row
                # instead of attempting a duplicate identity/target insert.
                binding = session.exec(
                    select(AccountTargetBindingModel)
                    .where(AccountTargetBindingModel.identity_id == identity_id)
                    .where(AccountTargetBindingModel.target_id == target_id)
                ).first()
            if account is None and binding is not None and int(binding.local_account_id or 0) > 0:
                account = session.get(AccountModel, int(binding.local_account_id))
            if binding_retargeted and binding is not None:
                binding.identity_id = identity_id
            if account is None:
                remote_status = str(row.get("remote_status") or row.get("status") or "").strip().lower()
                account = AccountModel(
                    platform="chatgpt",
                    email=email,
                    password="",
                    token="",
                    account_source="codex2api",
                    status="invalid" if remote_status in {"unauthorized", "auth_error", "invalid", "token_invalidated"} else "registered",
                    identity_id=identity_id,
                    extra_json=json.dumps({
                        "account_source": "codex2api",
                        "remote_only": True,
                        "remote_email_missing": email_missing,
                        "remote_target_id": target_id,
                        "remote_id": remote_id,
                        "codex_remote_snapshot": dict(row),
                    }, ensure_ascii=False),
                )
                try:
                    created_extra = account.get_extra()
                    _remember_remote_aliases(created_extra, row)
                    account.set_extra(created_extra)
                except Exception:
                    pass
                session.add(account)
                session.flush()
                created += 1
            else:
                extra = account.get_extra() if hasattr(account, "get_extra") else {}
                if not isinstance(extra, dict):
                    extra = {}
                extra["codex_remote_snapshot"] = dict(row)
                extra["remote_target_id"] = target_id
                extra["remote_id"] = remote_id
                if extra.get("remote_only"):
                    extra["remote_email_missing"] = email_missing
                _remember_remote_aliases(extra, row)
                if extra.get("remote_only"):
                    extra["account_source"] = "codex2api"
                    account.account_source = "codex2api"
                account.set_extra(extra)
                remote_status = str(row.get("remote_status") or row.get("status") or "").strip().lower()
                if extra.get("remote_only"):
                    account.status = "invalid" if remote_status in {"unauthorized", "auth_error", "invalid", "token_invalidated"} else "registered"
                updated += 1
            if not str(account.identity_id or "").strip():
                account.identity_id = identity_id
            claimed_account_ids.add((target_id, int(account.id or 0)))
            if id(account) not in candidate_object_ids:
                candidates.append(account)
                candidate_object_ids.add(id(account))
                for stable_id in _account_stable_ids(account):
                    by_stable_id.setdefault(stable_id, []).append(account)
                email_key = _stable_id(account.email)
                if email_key:
                    by_email.setdefault(email_key, []).append(account)
            identity = session.get(AccountIdentityModel, identity_id)
            identity_ambiguous = identity is not None and identity.state == "ambiguous"
            if identity is None:
                identity = AccountIdentityModel(id=identity_id, platform="chatgpt", canonical_email=email.lower(), current_account_id=int(account.id or 0))
                session.add(identity)
            else:
                identity.canonical_email = email.lower()
                identity.current_account_id = int(account.id or 0)
                if identity.state != "ambiguous":
                    identity.state = "active"
                session.add(identity)
            if binding is None:
                binding = AccountTargetBindingModel(identity_id=identity_id, local_account_id=int(account.id or 0), target_id=target_id, remote_account_id=remote_id)
            # A target can be disabled after its last successful inventory
            # snapshot. Its cached rows remain useful for audit/display, but
            # must never reactivate a binding or assignment. Legacy databases
            # may have inventory rows before the target registry is
            # materialized, so an absent target remains a compatible path.
            target_enabled = target is None or bool(target.enabled)
            schedulable = target_enabled and _schedulable(row)
            binding.local_account_id = int(account.id or 0)
            binding.remote_account_id = remote_id
            binding.remote_email = email.lower()
            binding.remote_status = str(row.get("remote_status") or row.get("status") or "")
            binding.enabled = schedulable
            binding.sync_status = "synced" if target_enabled else "target_disabled"
            if not target_enabled:
                binding.remote_status = "target_disabled"
                binding.last_error = "目标节点已停用"
            binding.last_sync_at = datetime.now(timezone.utc)
            binding.updated_at = datetime.now(timezone.utc)
            if identity_ambiguous:
                binding.enabled = False
                binding.sync_status = "ambiguous"
                binding.remote_status = "ambiguous"
                binding.last_error = "身份存在歧义，等待人工确认"
            elif schedulable:
                # A non-schedulable or disabled row is only an observation of
                # this target.  It must not displace a currently usable
                # binding on another target; otherwise a stale disabled pool
                # copy can quarantine the account that is still serving.
                supersede_other_target_bindings(
                    session,
                    identity_id=identity_id,
                    current_target_id=target_id,
                    reason="inventory_target_changed",
                )
            session.add(binding)
            all_assignments = session.exec(
                select(AccountAssignmentModel).where(
                    AccountAssignmentModel.identity_id == identity_id
                )
            ).all()
            current_assignments = [
                item
                for item in all_assignments
                if item.state in _ASSIGNMENT_STATES or item.state == "standby"
            ]
            current_assignments.sort(
                key=lambda item: (
                    _utc_datetime(item.updated_at)
                    or datetime.min.replace(tzinfo=timezone.utc),
                    int(item.id or 0),
                ),
                reverse=True,
            )
            assignment = current_assignments[0] if current_assignments else None
            # Transitional duplicate rows can exist because the partial
            # current-assignment index deliberately permits migration states.
            # Fence every non-canonical row before activating the one we keep;
            # otherwise an old migration could later write its target back.
            for stale_assignment in current_assignments[1:]:
                stale_assignment.state = "superseded"
                stale_assignment.assignment_version = max(
                    1, int(stale_assignment.assignment_version or 0)
                ) + 1
                stale_assignment.lease_owner = ""
                stale_assignment.lease_expires_at = None
                stale_assignment.lease_reason = "inventory_duplicate_assignment"
                stale_assignment.updated_at = datetime.now(timezone.utc)
                session.add(stale_assignment)
            if identity_ambiguous:
                supersede_other_target_bindings(
                    session,
                    identity_id=identity_id,
                    current_target_id=target_id,
                    reason="identity_ambiguous",
                )
                move_assignments_to_standby(
                    session,
                    identity_id=identity_id,
                    reason="identity_ambiguous",
                )
            elif schedulable:
                if assignment is None:
                    assignment = AccountAssignmentModel(identity_id=identity_id, local_account_id=int(account.id or 0), pool_id=pool_id, target_id=target_id, state="active", lease_reason="inventory_materialize", lease_started_at=datetime.now(timezone.utc), assignment_version=1)
                else:
                    assignment.local_account_id = int(account.id or 0)
                    previous_state = str(assignment.state or "")
                    previous_version = int(assignment.assignment_version or 0)
                    target_changed = int(assignment.target_id or 0) != target_id
                    if target_changed:
                        move_assignments_to_standby(
                            session,
                            identity_id=identity_id,
                            reason="inventory_target_changed",
                        )
                        if int(assignment.assignment_version or 0) <= previous_version:
                            assignment.assignment_version = max(1, previous_version) + 1
                    elif previous_state not in {"active", "standby"}:
                        move_assignments_to_standby(
                            session,
                            identity_id=identity_id,
                            target_id=target_id,
                            reason="inventory_recovered",
                        )
                    elif previous_state == "standby":
                        assignment.assignment_version = max(1, previous_version) + 1
                    assignment.target_id = target_id
                    # A target move can also change the target's default
                    # pool.  Keep the current assignment's pool in sync with
                    # the target selected for this inventory row; otherwise a
                    # cross-pool duplicate is shown under the old pool while
                    # its binding points at the new target.
                    assignment.pool_id = pool_id
                    assignment.state = "active"
                    assignment.lease_reason = (
                        "inventory_target_changed"
                        if target_changed
                        else "inventory_recovered"
                        if previous_state != "active"
                        else assignment.lease_reason
                    )
                    assignment.lease_owner = ""
                    assignment.lease_expires_at = None
                    assignment.updated_at = datetime.now(timezone.utc)
                session.add(assignment)
            elif assignment is not None and assignment.state in (_ASSIGNMENT_STATES | {"standby"}):
                move_assignments_to_standby(
                    session,
                    identity_id=identity_id,
                    target_id=target_id,
                    reason="remote_not_schedulable",
                )
        session.commit()
    return {"created": created, "updated": updated, "total": len(rows)}

__all__=["sync_inventory", "read_inventory", "materialize_inventory"]
