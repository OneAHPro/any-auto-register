"""Persistent SMS card pool with task-scoped reservations."""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import func
from sqlmodel import Session, select

from core.db import SmsPoolItemModel, engine


DEFAULT_SMS_BASE_URL = "https://sms.leadbee.cn/smsbox"


class SmsPoolExhaustedError(ValueError):
    """Raised when a task asks for more cards than the pool can reserve."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize_sms_base_url(value: Any) -> str:
    raw = str(value or "").strip()
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("接码地址必须是有效的 http/https URL")
    if parsed.username or parsed.password:
        raise ValueError("接码地址不能包含用户名或密码")
    normalized = urlunsplit(
        (parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"), "", "")
    )
    return normalized.rstrip("/")


def mask_sms_code(value: Any) -> str:
    code = str(value or "").strip()
    if len(code) <= 10:
        return "****"
    prefix = code.split("-", 1)[0][:4] if "-" in code else code[:2]
    return f"{prefix}-****-{code[-4:]}"


class SmsPoolService:
    def __init__(self, db_engine=engine):
        self.engine = db_engine
        self._lock = threading.RLock()

    def _begin_write(self, session: Session) -> None:
        if self.engine.url.get_backend_name() == "sqlite":
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")

    @staticmethod
    def _parse_import_line(line: str, default_base_url: str) -> tuple[str, str]:
        raw = str(line or "").strip()
        if "----" in raw:
            code, base_url = raw.split("----", 1)
        elif "\t" in raw:
            code, base_url = raw.split("\t", 1)
        else:
            code, base_url = raw, default_base_url
        normalized_code = str(code or "").strip()
        if not normalized_code:
            raise ValueError("卡密不能为空")
        return normalized_code, normalize_sms_base_url(base_url)

    def import_text(self, text: str, *, default_base_url: str) -> dict[str, Any]:
        normalized_default = normalize_sms_base_url(default_base_url)
        candidates: list[tuple[int, str, str]] = []
        invalid: list[dict[str, Any]] = []
        for line_number, line in enumerate(str(text or "").splitlines(), start=1):
            if not str(line or "").strip():
                continue
            try:
                code, base_url = self._parse_import_line(line, normalized_default)
            except ValueError as exc:
                invalid.append({"line": line_number, "message": str(exc)})
                continue
            candidates.append((line_number, code, base_url))

        if not candidates:
            return {"imported": 0, "duplicates": 0, "invalid": invalid}

        imported = 0
        duplicates = 0
        with self._lock, Session(self.engine) as session:
            self._begin_write(session)
            incoming_codes = [code for _, code, _ in candidates]
            existing_codes = set(
                session.exec(
                    select(SmsPoolItemModel.code).where(
                        SmsPoolItemModel.code.in_(incoming_codes)
                    )
                ).all()
            )
            seen = set(existing_codes)
            now = _utcnow()
            for _, code, base_url in candidates:
                if code in seen:
                    duplicates += 1
                    continue
                seen.add(code)
                session.add(
                    SmsPoolItemModel(
                        code=code,
                        base_url=base_url,
                        status="unused",
                        created_at=now,
                        updated_at=now,
                    )
                )
                imported += 1
            session.commit()
        return {"imported": imported, "duplicates": duplicates, "invalid": invalid}

    def reserve(
        self,
        *,
        task_id: str,
        count: int,
        exclude_item_ids: set[int] | None = None,
    ) -> list[SmsPoolItemModel]:
        normalized_task_id = str(task_id or "").strip()
        normalized_count = int(count or 0)
        if not normalized_task_id:
            raise ValueError("任务 ID 不能为空")
        if normalized_count < 1:
            raise ValueError("领取数量必须大于 0")

        excluded_ids = {
            int(item_id)
            for item_id in (exclude_item_ids or set())
            if int(item_id or 0) > 0
        }

        with self._lock, Session(self.engine) as session:
            self._begin_write(session)
            query = (
                select(SmsPoolItemModel)
                .where(SmsPoolItemModel.status == "unused")
            )
            if excluded_ids:
                query = query.where(~SmsPoolItemModel.id.in_(excluded_ids))
            rows = session.exec(
                query.order_by(SmsPoolItemModel.id).limit(normalized_count)
            ).all()
            if len(rows) != normalized_count:
                session.rollback()
                raise SmsPoolExhaustedError(
                    f"SMS 接码池可用卡密不足（需要 {normalized_count} 个，当前 {len(rows)} 个）"
                )
            now = _utcnow()
            for row in rows:
                row.status = "reserved"
                row.reserved_task_id = normalized_task_id
                row.reserved_at = now
                row.updated_at = now
                session.add(row)
            session.commit()
            for row in rows:
                session.refresh(row)
                session.expunge(row)
            return rows

    def finalize(
        self,
        *,
        item_id: int,
        task_id: str,
        consumed: bool,
        restoration_confirmed: bool = False,
        account_email: str = "",
    ) -> bool:
        with self._lock, Session(self.engine) as session:
            self._begin_write(session)
            row = session.get(SmsPoolItemModel, int(item_id))
            if row is None:
                session.rollback()
                return False
            if row.status == "used":
                session.rollback()
                return bool(consumed)
            if row.status not in {"reserved", "active"} or row.reserved_task_id != str(task_id or ""):
                session.rollback()
                return False
            now = _utcnow()
            was_active = row.status == "active"
            must_mark_used = bool(consumed or (was_active and not restoration_confirmed))
            if must_mark_used:
                row.status = "used"
                row.used_at = now
                normalized_email = str(account_email or "").strip()
                if normalized_email:
                    row.used_by_email = normalized_email
                row.reserved_task_id = ""
                row.reserved_at = None
            else:
                row.status = "unused"
                row.reserved_task_id = ""
                row.reserved_at = None
                row.used_by_email = ""
                row.used_at = None
            row.updated_at = now
            session.add(row)
            session.commit()
            return True

    def mark_active(
        self,
        *,
        item_id: int,
        task_id: str,
        account_email: str = "",
    ) -> bool:
        """Record that provider work may consume this card before making the call."""
        normalized_task_id = str(task_id or "").strip()
        with self._lock, Session(self.engine) as session:
            self._begin_write(session)
            row = session.get(SmsPoolItemModel, int(item_id))
            if row is None or row.reserved_task_id != normalized_task_id:
                session.rollback()
                return False
            if row.status == "active":
                session.rollback()
                return True
            if row.status != "reserved":
                session.rollback()
                return False
            row.status = "active"
            normalized_email = str(account_email or "").strip()
            if normalized_email:
                row.used_by_email = normalized_email
            row.updated_at = _utcnow()
            session.add(row)
            session.commit()
            return True

    def mark_restored(
        self,
        *,
        item_id: int,
        task_id: str,
    ) -> bool:
        """Persist provider-confirmed restoration while keeping task ownership."""
        normalized_task_id = str(task_id or "").strip()
        with self._lock, Session(self.engine) as session:
            self._begin_write(session)
            row = session.get(SmsPoolItemModel, int(item_id))
            if row is None or row.reserved_task_id != normalized_task_id:
                session.rollback()
                return False
            if row.status == "reserved":
                session.rollback()
                return True
            if row.status != "active":
                session.rollback()
                return False
            row.status = "reserved"
            row.used_by_email = ""
            row.used_at = None
            row.updated_at = _utcnow()
            session.add(row)
            session.commit()
            return True

    def release_task(self, task_id: str) -> int:
        normalized_task_id = str(task_id or "").strip()
        if not normalized_task_id:
            return 0
        with self._lock, Session(self.engine) as session:
            self._begin_write(session)
            rows = session.exec(
                select(SmsPoolItemModel)
                .where(SmsPoolItemModel.status == "reserved")
                .where(SmsPoolItemModel.reserved_task_id == normalized_task_id)
            ).all()
            now = _utcnow()
            for row in rows:
                row.status = "unused"
                row.reserved_task_id = ""
                row.reserved_at = None
                row.updated_at = now
                session.add(row)
            session.commit()
            return len(rows)

    def recover_interrupted(self) -> int:
        with self._lock, Session(self.engine) as session:
            self._begin_write(session)
            rows = session.exec(
                select(SmsPoolItemModel).where(
                    SmsPoolItemModel.status.in_(["reserved", "active"])
                )
            ).all()
            now = _utcnow()
            for row in rows:
                was_active = row.status == "active"
                row.status = "used" if was_active else "unused"
                if was_active:
                    row.used_at = now
                row.reserved_task_id = ""
                row.reserved_at = None
                if not was_active:
                    row.used_by_email = ""
                row.updated_at = now
                session.add(row)
            session.commit()
            return len(rows)

    def stats(self) -> dict[str, int]:
        with Session(self.engine) as session:
            rows = session.exec(
                select(SmsPoolItemModel.status, func.count())
                .group_by(SmsPoolItemModel.status)
            ).all()
        counts = {str(status): int(count or 0) for status, count in rows}
        return {
            "total": sum(counts.values()),
            "unused": counts.get("unused", 0),
            "reserved": counts.get("reserved", 0) + counts.get("active", 0),
            "used": counts.get("used", 0),
        }


sms_pool_service = SmsPoolService(engine)
