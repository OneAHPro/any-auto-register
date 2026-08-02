"""Configuration and observable state for ChatGPT automatic re-login."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable, Mapping, Protocol


ENABLED_CONFIG_KEY = "chatgpt_auto_relogin_enabled"
INTERVAL_MINUTES_CONFIG_KEY = "chatgpt_auto_relogin_interval_minutes"
CONCURRENCY_CONFIG_KEY = "chatgpt_auto_relogin_concurrency"

DEFAULT_INTERVAL_MINUTES = 30
MIN_INTERVAL_MINUTES = 20
MAX_INTERVAL_MINUTES = 1440
DEFAULT_CONCURRENCY = 10
MIN_CONCURRENCY = 1
MAX_CONCURRENCY = 10

_STATUS_KEY_BY_FIELD = {
    "state": "chatgpt_auto_relogin_status_state",
    "reason": "chatgpt_auto_relogin_status_reason",
    "eligible_accounts": "chatgpt_auto_relogin_status_eligible_accounts",
    "active_task_id": "chatgpt_auto_relogin_status_active_task_id",
    "last_task_id": "chatgpt_auto_relogin_status_last_task_id",
    "last_started_at": "chatgpt_auto_relogin_status_last_started_at",
    "next_run_at": "chatgpt_auto_relogin_status_next_run_at",
    "scheduled_interval_minutes": (
        "chatgpt_auto_relogin_status_scheduled_interval_minutes"
    ),
}
INTERNAL_STATUS_CONFIG_KEYS = tuple(_STATUS_KEY_BY_FIELD.values())
_UNSET = object()
_STATUS_TRANSITION_LOCK = threading.RLock()


class ConfigStoreLike(Protocol):
    def get(self, key: str, default: str = "") -> object: ...

    def get_all(self) -> dict[str, object]: ...

    def set_many(self, data: dict[str, str]) -> None: ...


@dataclass(frozen=True)
class ChatGPTAutoReloginSettings:
    enabled: bool = False
    interval_minutes: int = DEFAULT_INTERVAL_MINUTES
    concurrency: int = DEFAULT_CONCURRENCY


def _get_config_store() -> ConfigStoreLike:
    from core.config_store import config_store

    return config_store


def _to_bool(value: object, default: bool = False) -> bool:
    normalized = str(value or "").strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _bounded_int(value: object, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(str(value or "").strip())
    except (TypeError, ValueError):
        return default
    return min(maximum, max(minimum, parsed))


def _optional_text(value: object) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _settings_from_snapshot(
    snapshot: Mapping[str, object],
) -> ChatGPTAutoReloginSettings:
    return ChatGPTAutoReloginSettings(
        enabled=_to_bool(snapshot.get(ENABLED_CONFIG_KEY, "0")),
        interval_minutes=_bounded_int(
            snapshot.get(INTERVAL_MINUTES_CONFIG_KEY, ""),
            DEFAULT_INTERVAL_MINUTES,
            MIN_INTERVAL_MINUTES,
            MAX_INTERVAL_MINUTES,
        ),
        concurrency=_bounded_int(
            snapshot.get(CONCURRENCY_CONFIG_KEY, ""),
            DEFAULT_CONCURRENCY,
            MIN_CONCURRENCY,
            MAX_CONCURRENCY,
        ),
    )


def get_chatgpt_auto_relogin_settings(
    store: ConfigStoreLike | None = None,
) -> ChatGPTAutoReloginSettings:
    resolved_store = store or _get_config_store()
    return _settings_from_snapshot(dict(resolved_store.get_all()))


def get_chatgpt_auto_relogin_status(
    store: ConfigStoreLike | None = None,
) -> dict[str, object]:
    resolved_store = store or _get_config_store()
    snapshot = dict(resolved_store.get_all())
    return _status_from_snapshot(snapshot)


def _status_from_snapshot(snapshot: Mapping[str, object]) -> dict[str, object]:
    settings = _settings_from_snapshot(snapshot)
    raw_state = _optional_text(snapshot.get(_STATUS_KEY_BY_FIELD["state"], ""))
    raw_reason = _optional_text(snapshot.get(_STATUS_KEY_BY_FIELD["reason"], ""))

    raw_active_task_id = _optional_text(
        snapshot.get(_STATUS_KEY_BY_FIELD["active_task_id"], "")
    )
    if settings.enabled:
        state = "idle" if raw_state in {None, "disabled"} else raw_state
        reason = None if raw_reason == "disabled_by_config" else raw_reason
        active_task_id = raw_active_task_id
        next_run_at = _optional_text(
            snapshot.get(_STATUS_KEY_BY_FIELD["next_run_at"], "")
        )
    elif raw_state == "stopping" and raw_active_task_id is not None:
        state = "stopping"
        reason = raw_reason or "disabled_stopping"
        active_task_id = raw_active_task_id
        next_run_at = None
    else:
        state = "disabled"
        reason = "disabled_by_config"
        active_task_id = None
        next_run_at = None

    return {
        "enabled": settings.enabled,
        "state": state,
        "reason": reason,
        "eligible_accounts": _bounded_int(
            snapshot.get(_STATUS_KEY_BY_FIELD["eligible_accounts"], ""),
            0,
            0,
            2_147_483_647,
        ),
        "active_task_id": active_task_id,
        "last_task_id": _optional_text(
            snapshot.get(_STATUS_KEY_BY_FIELD["last_task_id"], "")
        ),
        "last_started_at": _optional_text(
            snapshot.get(_STATUS_KEY_BY_FIELD["last_started_at"], "")
        ),
        "next_run_at": next_run_at,
        "interval_minutes": settings.interval_minutes,
        "concurrency": settings.concurrency,
    }


def update_chatgpt_auto_relogin_status(
    *,
    store: ConfigStoreLike | None = None,
    state: object = _UNSET,
    reason: object = _UNSET,
    eligible_accounts: object = _UNSET,
    active_task_id: object = _UNSET,
    last_task_id: object = _UNSET,
    last_started_at: object = _UNSET,
    next_run_at: object = _UNSET,
    scheduled_interval_minutes: object = _UNSET,
) -> None:
    """Persist scheduler-owned status fields without exposing them to config PUT."""

    values = {
        "state": state,
        "reason": reason,
        "eligible_accounts": eligible_accounts,
        "active_task_id": active_task_id,
        "last_task_id": last_task_id,
        "last_started_at": last_started_at,
        "next_run_at": next_run_at,
        "scheduled_interval_minutes": scheduled_interval_minutes,
    }
    payload = {
        _STATUS_KEY_BY_FIELD[field]: "" if value is None else str(value)
        for field, value in values.items()
        if value is not _UNSET
    }
    if payload:
        with _STATUS_TRANSITION_LOCK:
            (store or _get_config_store()).set_many(payload)


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _aware_utc(value: datetime | None) -> datetime:
    resolved = value or datetime.now(timezone.utc)
    if resolved.tzinfo is None:
        resolved = resolved.replace(tzinfo=timezone.utc)
    return resolved.astimezone(timezone.utc)


def _parse_utc_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return _aware_utc(value)
    text = _optional_text(value)
    if text is None:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return _aware_utc(parsed)


def _ordered_account_ids(values: Iterable[int]) -> list[int]:
    return sorted({int(value) for value in values})


def _persist_tick_transition(
    store: ConfigStoreLike,
    snapshot: Mapping[str, object],
    **fields: object,
) -> dict[str, object]:
    payload = {
        _STATUS_KEY_BY_FIELD[field]: "" if value is None else str(value)
        for field, value in fields.items()
    }
    store.set_many(payload)
    merged = dict(snapshot)
    merged.update(payload)
    return _status_from_snapshot(merged)


def tick_chatgpt_auto_relogin(
    *,
    store: ConfigStoreLike | None = None,
    list_eligible: Callable[[], Iterable[int]] | None = None,
    try_enqueue: (
        Callable[[Iterable[int], int], Mapping[str, object]] | None
    ) = None,
    observe: Callable[[str], Mapping[str, object] | None] | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    """Advance the persisted auto-relogin state machine by exactly one tick."""

    with _STATUS_TRANSITION_LOCK:
        resolved_store = store or _get_config_store()
        snapshot = dict(resolved_store.get_all())
        settings = _settings_from_snapshot(snapshot)
        wall_now = _aware_utc(now)

        if not settings.enabled:
            active_task_id = _optional_text(
                snapshot.get(_STATUS_KEY_BY_FIELD["active_task_id"], "")
            )
            if active_task_id is not None:
                if observe is None:
                    from api.tasks import observe_chatgpt_task

                    observe = observe_chatgpt_task
                observation = observe(active_task_id)
                observed_status = _optional_text(
                    (observation or {}).get("status", "")
                )
                observed_live = bool((observation or {}).get("live"))
                if observed_live and observed_status in {"pending", "running"}:
                    try:
                        from api.tasks import stop_task

                        stop_task(active_task_id)
                        stop_reason = "disabled_stopping"
                    except Exception:
                        stop_reason = "disabled_stop_failed"
                    return _persist_tick_transition(
                        resolved_store,
                        snapshot,
                        state="stopping",
                        reason=stop_reason,
                        active_task_id=active_task_id,
                        next_run_at=None,
                        scheduled_interval_minutes=None,
                    )
            return _persist_tick_transition(
                resolved_store,
                snapshot,
                state="disabled",
                reason="disabled_by_config",
                eligible_accounts=0,
                active_task_id=None,
                next_run_at=None,
                scheduled_interval_minutes=None,
            )

        if list_eligible is None:
            from services.chatgpt_relogin import list_relogin_eligible_account_ids

            list_eligible = list_relogin_eligible_account_ids
        if try_enqueue is None or observe is None:
            from api.tasks import (
                observe_chatgpt_task,
                try_enqueue_scheduled_chatgpt_relogin,
            )

            if try_enqueue is None:
                try_enqueue = try_enqueue_scheduled_chatgpt_relogin
            if observe is None:
                observe = observe_chatgpt_task

        eligible_ids = _ordered_account_ids(list_eligible())
        eligible_count = len(eligible_ids)
        interval = timedelta(minutes=settings.interval_minutes)
        active_task_id = _optional_text(
            snapshot.get(_STATUS_KEY_BY_FIELD["active_task_id"], "")
        )

        if active_task_id is not None:
            observation = observe(active_task_id)
            observed_status = _optional_text(
                (observation or {}).get("status", "")
            )
            observed_live = bool((observation or {}).get("live"))
            observed_orphaned = bool((observation or {}).get("orphaned"))

            if observed_live and observed_status in {"pending", "running"}:
                return _persist_tick_transition(
                    resolved_store,
                    snapshot,
                    state="running",
                    reason="task_running",
                    eligible_accounts=eligible_count,
                    active_task_id=active_task_id,
                    next_run_at=None,
                )

            if observation is None or observed_orphaned or (
                observed_status in {"pending", "running"} and not observed_live
            ):
                if eligible_count == 0:
                    return _persist_tick_transition(
                        resolved_store,
                        snapshot,
                        state="paused_no_accounts",
                        reason="no_eligible_accounts",
                        eligible_accounts=0,
                        active_task_id=None,
                        next_run_at=None,
                    )
                return _persist_tick_transition(
                    resolved_store,
                    snapshot,
                    state="idle",
                    reason=(
                        "active_task_missing"
                        if observation is None
                        else "task_orphaned"
                    ),
                    eligible_accounts=eligible_count,
                    active_task_id=None,
                    next_run_at=_utc_iso(wall_now + interval),
                    scheduled_interval_minutes=settings.interval_minutes,
                )

            if observed_status in {"done", "failed", "stopped"}:
                if eligible_count == 0:
                    return _persist_tick_transition(
                        resolved_store,
                        snapshot,
                        state="paused_no_accounts",
                        reason="no_eligible_accounts",
                        eligible_accounts=0,
                        active_task_id=None,
                        next_run_at=None,
                    )
                completed_at = _parse_utc_datetime(
                    (observation or {}).get("updated_at")
                ) or wall_now
                cycle_started_at = _parse_utc_datetime(
                    snapshot.get(_STATUS_KEY_BY_FIELD["last_started_at"], "")
                )
                return _persist_tick_transition(
                    resolved_store,
                    snapshot,
                    state="idle",
                    reason="scheduled",
                    eligible_accounts=eligible_count,
                    active_task_id=None,
                    next_run_at=_utc_iso(
                        (cycle_started_at or completed_at) + interval
                    ),
                    scheduled_interval_minutes=settings.interval_minutes,
                )

            if eligible_count == 0:
                return _persist_tick_transition(
                    resolved_store,
                    snapshot,
                    state="paused_no_accounts",
                    reason="no_eligible_accounts",
                    eligible_accounts=0,
                    active_task_id=None,
                    next_run_at=None,
                )
            return _persist_tick_transition(
                resolved_store,
                snapshot,
                state="idle",
                reason="active_task_missing",
                eligible_accounts=eligible_count,
                active_task_id=None,
                next_run_at=_utc_iso(wall_now + interval),
                scheduled_interval_minutes=settings.interval_minutes,
            )

        if eligible_count == 0:
            return _persist_tick_transition(
                resolved_store,
                snapshot,
                state="paused_no_accounts",
                reason="no_eligible_accounts",
                eligible_accounts=0,
                active_task_id=None,
                next_run_at=None,
                scheduled_interval_minutes=None,
            )

        current_state = _optional_text(
            snapshot.get(_STATUS_KEY_BY_FIELD["state"], "")
        )
        raw_next_run_at = _optional_text(
            snapshot.get(_STATUS_KEY_BY_FIELD["next_run_at"], "")
        )
        next_run_at = _parse_utc_datetime(raw_next_run_at)
        raw_scheduled_interval = _optional_text(
            snapshot.get(
                _STATUS_KEY_BY_FIELD["scheduled_interval_minutes"],
                "",
            )
        )
        try:
            scheduled_interval_minutes = (
                int(raw_scheduled_interval)
                if raw_scheduled_interval is not None
                else None
            )
        except (TypeError, ValueError):
            scheduled_interval_minutes = None
        if (
            current_state in {None, "disabled", "paused_no_accounts"}
            or next_run_at is None
        ):
            return _persist_tick_transition(
                resolved_store,
                snapshot,
                state="idle",
                reason="scheduled",
                eligible_accounts=eligible_count,
                active_task_id=None,
                next_run_at=_utc_iso(wall_now + interval),
                scheduled_interval_minutes=settings.interval_minutes,
            )

        if (
            scheduled_interval_minutes is not None
            and scheduled_interval_minutes != settings.interval_minutes
        ):
            return _persist_tick_transition(
                resolved_store,
                snapshot,
                state="idle",
                reason="scheduled",
                eligible_accounts=eligible_count,
                active_task_id=None,
                next_run_at=_utc_iso(wall_now + interval),
                scheduled_interval_minutes=settings.interval_minutes,
            )

        if wall_now < next_run_at:
            return _persist_tick_transition(
                resolved_store,
                snapshot,
                state="idle",
                reason="scheduled",
                eligible_accounts=eligible_count,
                active_task_id=None,
                next_run_at=_utc_iso(next_run_at),
                scheduled_interval_minutes=settings.interval_minutes,
            )

        try:
            decision = dict(try_enqueue(eligible_ids, settings.concurrency))
        except Exception:
            return _persist_tick_transition(
                resolved_store,
                snapshot,
                state="idle",
                reason="enqueue_failed",
                eligible_accounts=eligible_count,
                active_task_id=None,
                next_run_at=_utc_iso(next_run_at),
                scheduled_interval_minutes=settings.interval_minutes,
            )

        task_id = _optional_text(decision.get("task_id"))
        if not bool(decision.get("accepted")) or task_id is None:
            reason = _optional_text(decision.get("reason")) or "task_busy"
            if bool(decision.get("accepted")):
                reason = "enqueue_failed"
            return _persist_tick_transition(
                resolved_store,
                snapshot,
                state="idle",
                reason=reason,
                eligible_accounts=eligible_count,
                active_task_id=None,
                next_run_at=_utc_iso(next_run_at),
                scheduled_interval_minutes=settings.interval_minutes,
            )

        started_at = _utc_iso(wall_now)
        return _persist_tick_transition(
            resolved_store,
            snapshot,
            state="running",
            reason="task_running",
            eligible_accounts=eligible_count,
            active_task_id=task_id,
            last_task_id=task_id,
            last_started_at=started_at,
            next_run_at=None,
            scheduled_interval_minutes=settings.interval_minutes,
        )


def reconcile_chatgpt_auto_relogin_eligibility(
    *,
    store: ConfigStoreLike | None = None,
    eligible_account_ids: Iterable[int] | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    """Persist no-account pause/resume state without starting a task."""

    with _STATUS_TRANSITION_LOCK:
        return _reconcile_chatgpt_auto_relogin_eligibility_locked(
            store=store,
            eligible_account_ids=eligible_account_ids,
            now=now,
        )


def _reconcile_chatgpt_auto_relogin_eligibility_locked(
    *,
    store: ConfigStoreLike | None,
    eligible_account_ids: Iterable[int] | None,
    now: datetime | None,
) -> dict[str, object]:

    resolved_store = store or _get_config_store()
    snapshot = dict(resolved_store.get_all())
    settings = _settings_from_snapshot(snapshot)
    if not settings.enabled:
        return get_chatgpt_auto_relogin_status(resolved_store)

    if eligible_account_ids is None:
        from services.chatgpt_relogin import list_relogin_eligible_account_ids

        eligible_account_ids = list_relogin_eligible_account_ids()
    eligible_count = len(tuple(eligible_account_ids))
    current_state = _optional_text(
        snapshot.get(_STATUS_KEY_BY_FIELD["state"], "")
    )
    current_active_task_id = _optional_text(
        snapshot.get(_STATUS_KEY_BY_FIELD["active_task_id"], "")
    )
    current_eligible_count = _bounded_int(
        snapshot.get(_STATUS_KEY_BY_FIELD["eligible_accounts"], ""),
        0,
        0,
        2_147_483_647,
    )
    if current_state == "running" and current_active_task_id is not None:
        if current_eligible_count != eligible_count:
            update_chatgpt_auto_relogin_status(
                store=resolved_store,
                eligible_accounts=eligible_count,
            )
        return get_chatgpt_auto_relogin_status(resolved_store)
    if eligible_count == 0:
        update_chatgpt_auto_relogin_status(
            store=resolved_store,
            state="paused_no_accounts",
            reason="no_eligible_accounts",
            eligible_accounts=0,
            active_task_id=None,
            next_run_at=None,
            scheduled_interval_minutes=None,
        )
        return get_chatgpt_auto_relogin_status(resolved_store)

    current_next_run_at = _optional_text(
        snapshot.get(_STATUS_KEY_BY_FIELD["next_run_at"], "")
    )
    needs_schedule = current_state == "paused_no_accounts" or (
        current_state in {None, "disabled", "idle", "scheduled"}
        and current_next_run_at is None
    )
    if needs_schedule:
        current_time = now or datetime.now(timezone.utc)
        update_chatgpt_auto_relogin_status(
            store=resolved_store,
            state="idle",
            reason="scheduled",
            eligible_accounts=eligible_count,
            active_task_id=None,
            next_run_at=_utc_iso(
                current_time + timedelta(minutes=settings.interval_minutes)
            ),
            scheduled_interval_minutes=settings.interval_minutes,
        )
    elif current_eligible_count != eligible_count:
        update_chatgpt_auto_relogin_status(
            store=resolved_store,
            eligible_accounts=eligible_count,
        )
    return get_chatgpt_auto_relogin_status(resolved_store)
