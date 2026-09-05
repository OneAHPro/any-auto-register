"""Target-aware client for the Codex2API admin API.

The control plane talks to each Codex2API instance through this module.  It
keeps the legacy single-target configuration compatible while making the
target explicit for all new orchestration code.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, replace
from typing import Any, Mapping
from urllib.parse import urlencode

from curl_cffi import CurlMime
from curl_cffi import requests as cffi_requests


logger = logging.getLogger(__name__)
MAX_ERROR_DETAIL_LENGTH = 240


@dataclass(frozen=True)
class TargetConfig:
    """Connection details for one Codex2API instance."""

    id: int = 0
    name: str = "default"
    base_url: str = ""
    admin_key: str = ""
    target_type: str = "public"
    server_label: str = ""
    default_pool_id: str = "PUBLIC_POOL"
    enabled: bool = True

    def normalized(self) -> "TargetConfig":
        return replace(
            self,
            base_url=str(self.base_url or "").strip().rstrip("/"),
            name=str(self.name or "").strip() or f"target-{self.id}",
            admin_key=str(self.admin_key or "").strip(),
            target_type=str(self.target_type or "public").strip().lower()
            or "public",
            server_label=str(self.server_label or "").strip(),
            default_pool_id=str(self.default_pool_id or "PUBLIC_POOL").strip()
            or "PUBLIC_POOL",
        )


class Codex2APITargetError(RuntimeError):
    """A target request failed with a credential-safe diagnostic."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 0,
        endpoint: str = "",
    ) -> None:
        self.status_code = int(status_code or 0)
        self.endpoint = str(endpoint or "")
        super().__init__(str(message)[:MAX_ERROR_DETAIL_LENGTH])


def _text(value: Any) -> str:
    return str(value or "").strip()


def _bool(value: Any, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    normalized = _text(value).lower()
    if normalized in {"1", "true", "yes", "on", "enabled"}:
        return True
    if normalized in {"0", "false", "no", "off", "disabled"}:
        return False
    return default


def _redact(value: Any, secrets: list[str]) -> str:
    result = _text(value)
    for secret in secrets:
        if secret:
            result = result.replace(secret, "***")
    return result[:MAX_ERROR_DETAIL_LENGTH]


def _collect_secrets(value: Any) -> list[str]:
    """Collect string leaves from a request payload for error redaction."""

    result: list[str] = []
    if isinstance(value, Mapping):
        for item in value.values():
            result.extend(_collect_secrets(item))
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            result.extend(_collect_secrets(item))
    elif isinstance(value, str) and value.strip():
        result.append(value.strip())
    return result


def _response_detail(response: Any, secrets: list[str]) -> str:
    try:
        payload = response.json()
    except Exception:
        payload = None
    if isinstance(payload, Mapping):
        value = payload.get("message") or payload.get("msg") or payload.get("error")
    else:
        value = getattr(response, "text", "")
    return _redact(value, secrets)


def _contains_marker(value: Any, markers: tuple[str, ...]) -> bool:
    normalized = _text(value).lower()
    return any(marker in normalized for marker in markers)


_AUTH_FAILURE_MARKERS = (
    "token_invalidated",
    "invalid_token",
    "invalid token",
    "unauthorized",
    "authentication token",
    "invalid_grant",
)
_USAGE_LIMIT_MARKERS = (
    "usage_limit",
    "usage limited",
    "usage limit",
    "quota_exhausted",
    "quota exhausted",
    "rate_limited",
    "rate limited",
)


def _remote_secret_key(key: object) -> bool:
    normalized = str(key or "").strip().lower().replace("-", "_")
    if normalized in {
        "allowed_api_key_ids",
        "api_key_ids",
        "group_ids",
        "api_key_id",
    }:
        return False
    return any(
        marker in normalized
        for marker in (
            "refresh_token",
            "access_token",
            "session_token",
            "id_token",
            "cookie",
            "password",
            "private_key",
            "admin_key",
            "api_key",
            "secret",
            "token",
            "credential",
            "bearer",
        )
    )


def _sanitize_remote_payload(value: Any, secrets: list[str]) -> Any:
    """Keep remote operational fields while removing credential-shaped data."""

    if isinstance(value, Mapping):
        return {
            str(key): _sanitize_remote_payload(child, secrets)
            for key, child in value.items()
            if not _remote_secret_key(key)
        }
    if isinstance(value, list):
        return [_sanitize_remote_payload(item, secrets) for item in value[:1000]]
    if isinstance(value, tuple):
        return [_sanitize_remote_payload(item, secrets) for item in value[:1000]]
    if isinstance(value, str):
        return _redact(value, secrets)
    return value


def _parse_sse_payload(text: str) -> dict[str, Any] | None:
    complete: dict[str, Any] | None = None
    for raw_line in _text(text).splitlines():
        line = raw_line.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            event = json.loads(data)
        except (TypeError, ValueError):
            continue
        if not isinstance(event, dict):
            continue
        event_type = _text(event.get("type")).lower()
        if event_type in {"complete", "test_complete", "result", "done"}:
            complete = event
        elif complete is None:
            # Some compatible versions emit one final object without a type.
            complete = event
    return complete


def _parse_response_payload(response: Any) -> dict[str, Any]:
    try:
        payload = response.json()
    except Exception:
        payload = None
    if isinstance(payload, dict):
        return payload
    parsed = _parse_sse_payload(getattr(response, "text", ""))
    if parsed is not None:
        return parsed
    if int(getattr(response, "status_code", 0) or 0) in (204,):
        return {}
    raise ValueError("response payload is not JSON or a complete SSE event")


def _coerce_target_entry(raw: Mapping[str, Any], index: int) -> TargetConfig | None:
    base_url = _text(raw.get("base_url") or raw.get("url") or raw.get("api_url"))
    admin_key = _text(raw.get("admin_key") or raw.get("key") or raw.get("token"))
    if not base_url or not admin_key:
        return None
    if raw.get("id") in (None, ""):
        raise ValueError("structured Codex2API target requires a stable positive id")
    try:
        target_id = int(raw.get("id"))
    except (TypeError, ValueError):
        raise ValueError("structured Codex2API target id must be an integer") from None
    if target_id <= 0:
        raise ValueError("structured Codex2API target id must be positive")
    return TargetConfig(
        id=target_id,
        name=_text(raw.get("name") or raw.get("server_label"))
        or f"target-{target_id}",
        base_url=base_url,
        admin_key=admin_key,
        target_type=_text(raw.get("target_type") or raw.get("type")) or "public",
        server_label=_text(raw.get("server_label")),
        default_pool_id=_text(raw.get("default_pool_id") or raw.get("pool_id"))
        or "PUBLIC_POOL",
        enabled=_bool(raw.get("enabled"), True),
    ).normalized()


def load_target_configs(config: Mapping[str, Any] | None = None) -> list[TargetConfig]:
    """Resolve structured targets, falling back to the legacy single target."""

    values = dict(config or {})
    raw_targets: Any = values.get("codex2api_targets")
    if raw_targets in (None, ""):
        raw_targets = values.get("codex2api_targets_json")
    if isinstance(raw_targets, str):
        try:
            raw_targets = json.loads(raw_targets)
        except (TypeError, ValueError):
            raw_targets = None

    entries: list[Mapping[str, Any]] = []
    if isinstance(raw_targets, Mapping):
        for key, item in raw_targets.items():
            if isinstance(item, Mapping):
                entry = dict(item)
                entry.setdefault("id", key)
                entries.append(entry)
    elif isinstance(raw_targets, list):
        entries = [item for item in raw_targets if isinstance(item, Mapping)]

    targets = [
        target
        for index, entry in enumerate(entries, start=1)
        if (target := _coerce_target_entry(entry, index)) is not None
    ]
    ids = [target.id for target in targets]
    names = [target.name.casefold() for target in targets]
    if len(ids) != len(set(ids)) or len(names) != len(set(names)):
        raise ValueError("Codex2API target IDs and names must be unique")
    if targets:
        return targets

    legacy_url = _text(values.get("codex2api_api_url"))
    legacy_key = _text(values.get("codex2api_admin_key"))
    if not legacy_url or not legacy_key:
        return []
    return [
        TargetConfig(
            id=1,
            name="default",
            base_url=legacy_url,
            admin_key=legacy_key,
            target_type="public",
            default_pool_id="PUBLIC_POOL",
        ).normalized()
    ]


def _config_snapshot() -> dict[str, Any]:
    try:
        from core.config_store import config_store

        return dict(config_store.get_all() or {})
    except Exception:
        return {}


def default_target_config(config: Mapping[str, Any] | None = None) -> TargetConfig | None:
    targets = load_target_configs(config if config is not None else _config_snapshot())
    return targets[0] if targets else None


def target_config_from_model(model: Any, secrets: Mapping[str, Any] | None = None) -> TargetConfig:
    """Convert a persisted target row to a client config.

    ``secrets`` is a mapping of references to actual keys supplied by the
    control-plane secret resolver.  The model itself never contains the key.
    """

    values = dict(secrets or {})
    ref = _text(getattr(model, "admin_key_ref", ""))
    raw_admin_key = _text(values.get(ref))
    admin_key = ""
    if raw_admin_key:
        from services.secret_store import open_secret

        admin_key = open_secret(raw_admin_key, allow_legacy_plaintext=True)
    if not admin_key:
        # A caller may pass a mapping keyed by the numeric target id while
        # bootstrapping an installation.  This remains an in-memory fallback;
        # the persisted row still stores only the reference.
        admin_key = _text(values.get(str(getattr(model, "id", ""))))
    if not admin_key and int(getattr(model, "id", 0) or 0) == 1:
        # Installations upgraded from the original single-target settings keep
        # their key under this legacy name until the settings UI saves a
        # structured target secret.
        raw_legacy_key = _text(values.get("codex2api_admin_key"))
        if raw_legacy_key:
            from services.secret_store import open_secret

            admin_key = open_secret(raw_legacy_key, allow_legacy_plaintext=True)
    return TargetConfig(
        id=int(getattr(model, "id", 0) or 0),
        name=_text(getattr(model, "name", "")),
        base_url=_text(getattr(model, "base_url", "")),
        admin_key=admin_key,
        target_type=_text(getattr(model, "target_type", "")) or "public",
        server_label=_text(getattr(model, "server_label", "")),
        default_pool_id=_text(getattr(model, "default_pool_id", ""))
        or "PUBLIC_POOL",
        enabled=bool(getattr(model, "enabled", True)),
    ).normalized()


def load_db_target_configs(database_engine=None) -> list[TargetConfig]:
    """Load enabled/disabled target rows and resolve their secret references."""

    from core.db import Codex2APITargetModel, engine as default_engine
    from sqlmodel import Session, select

    target_engine = database_engine or default_engine
    values = _config_snapshot()
    with Session(target_engine) as session:
        from core.config_store import ConfigItem

        ConfigItem.__table__.create(bind=target_engine, checkfirst=True)
        for item in session.exec(select(ConfigItem)).all():
            values.setdefault(str(item.key), str(item.value or ""))
        rows = session.exec(
            select(Codex2APITargetModel).order_by(Codex2APITargetModel.id)
        ).all()
    return [target_config_from_model(row, values) for row in rows]


def get_target_client(target_id: int, database_engine=None) -> "Codex2APITargetClient":
    """Resolve one persisted target and return a ready client."""

    from core.db import Codex2APITargetModel, engine as default_engine
    from sqlmodel import Session

    target_engine = database_engine or default_engine
    with Session(target_engine) as session:
        from core.config_store import ConfigItem

        row = session.get(Codex2APITargetModel, int(target_id))
        if row is None:
            raise Codex2APITargetError(
                "Codex2API 目标不存在",
                endpoint="target",
            )
        if not bool(getattr(row, "enabled", True)):
            raise Codex2APITargetError(
                "Codex2API 目标已停用",
                endpoint="target",
            )
        values = _config_snapshot()
        secret = session.get(ConfigItem, str(row.admin_key_ref or ""))
        if secret is not None:
            values[str(row.admin_key_ref)] = str(secret.value or "")
        target = target_config_from_model(row, values)
    return Codex2APITargetClient(target)


def ensure_default_target(database_engine=None, config: Mapping[str, Any] | None = None):
    """Materialize and return the first configured target."""

    targets = ensure_configured_targets(database_engine, config)
    return targets[0] if targets else None


def _persist_target_secret(database_engine, key: str, value: str) -> None:
    from core.config_store import ConfigItem
    from sqlmodel import Session

    from services.secret_store import seal_secret

    ConfigItem.__table__.create(bind=database_engine, checkfirst=True)
    sealed = seal_secret(value)
    with Session(database_engine) as session:
        item = session.get(ConfigItem, key)
        if item is None:
            item = ConfigItem(key=key, value=sealed)
        else:
            item.value = sealed
        session.add(item)
        session.commit()


def ensure_configured_targets(
    database_engine=None,
    config: Mapping[str, Any] | None = None,
) -> list[Any]:
    """Materialize all configured targets and persist their keys by reference."""

    from core.db import Codex2APITargetModel, engine as default_engine
    from sqlmodel import Session, select

    target_engine = database_engine or default_engine
    from core.config_store import ConfigItem

    ConfigItem.__table__.create(bind=target_engine, checkfirst=True)
    source_values = dict(config if config is not None else _config_snapshot())
    raw_structured = source_values.get("codex2api_targets")
    if raw_structured in (None, ""):
        raw_structured = source_values.get("codex2api_targets_json")
    structured_present = raw_structured not in (None, "")
    parsed_structured: Any = raw_structured
    if isinstance(parsed_structured, str):
        try:
            parsed_structured = json.loads(parsed_structured)
        except (TypeError, ValueError):
            parsed_structured = None
    if isinstance(parsed_structured, Mapping):
        structured_entries = [
            item for item in parsed_structured.values() if isinstance(item, Mapping)
        ]
    elif isinstance(parsed_structured, list):
        structured_entries = [
            item for item in parsed_structured if isinstance(item, Mapping)
        ]
    else:
        structured_entries = []
    try:
        targets = load_target_configs(source_values)
    except ValueError:
        # A malformed structured setting must never turn every persisted
        # target off. Leave the database projection untouched until an
        # operator supplies a valid complete configuration.
        targets = []
    structured_source = bool(
        structured_present
        and structured_entries
        and len(targets) == len(structured_entries)
    )
    if structured_present and not structured_source:
        with Session(target_engine) as session:
            return session.exec(
                select(Codex2APITargetModel).order_by(Codex2APITargetModel.id)
            ).all()
    materialized: list[Any] = []
    from services.secret_store import seal_secret

    sealed_secrets = (
        {int(target.id): seal_secret(target.admin_key) for target in targets}
        if structured_source
        else {}
    )
    with Session(target_engine) as session:
        configured_ids = {int(target.id) for target in targets}
        for target in targets:
            existing = session.get(Codex2APITargetModel, int(target.id))
            if existing is None:
                existing = session.exec(
                    select(Codex2APITargetModel).where(
                        Codex2APITargetModel.name == target.name
                    )
                ).first()
                if existing is not None and int(existing.id or 0) != int(target.id):
                    raise ValueError(
                        "Codex2API target name is already bound to a different id"
                    )
            row_id = int(existing.id) if existing is not None else int(target.id)
            ref = (
                f"codex2api_target_{row_id}_admin_key"
                if structured_source
                else "codex2api_admin_key"
            )
            if existing is None:
                existing = Codex2APITargetModel(
                    id=row_id,
                    name=target.name,
                    target_type=target.target_type,
                    server_label=target.server_label,
                    base_url=target.base_url,
                    admin_key_ref=ref,
                    default_pool_id=target.default_pool_id,
                    enabled=target.enabled,
                    health_status="unknown",
                )
            else:
                existing.name = target.name
                existing.target_type = target.target_type
                existing.server_label = target.server_label
                existing.base_url = target.base_url
                existing.admin_key_ref = ref
                existing.default_pool_id = target.default_pool_id
                existing.enabled = target.enabled
                existing.updated_at = __import__("datetime").datetime.now(
                    __import__("datetime").timezone.utc
                )
            session.add(existing)
            session.flush()
            materialized.append(existing)
            if structured_source:
                secret_item = session.get(ConfigItem, ref)
                if secret_item is None:
                    secret_item = ConfigItem(
                        key=ref,
                        value=sealed_secrets[int(target.id)],
                    )
                else:
                    secret_item.value = sealed_secrets[int(target.id)]
                session.add(secret_item)
        if structured_source:
            for stale in session.exec(select(Codex2APITargetModel)).all():
                if int(stale.id or 0) not in configured_ids:
                    stale.enabled = False
                    stale.updated_at = __import__("datetime").datetime.now(
                        __import__("datetime").timezone.utc
                    )
                    session.add(stale)
        session.commit()
        for row in materialized:
            session.refresh(row)
    return materialized


class Codex2APITargetClient:
    """Small, redacting wrapper around one Codex2API admin API."""

    def __init__(self, target: TargetConfig):
        self.target = target.normalized()
        if not self.target.base_url:
            raise ValueError("Codex2API target URL is required")
        if not self.target.admin_key:
            raise ValueError("Codex2API target Admin Key is required")

    @property
    def _secrets(self) -> list[str]:
        return [self.target.admin_key]

    def _url(self, path: str) -> str:
        return f"{self.target.base_url}/{str(path or '').lstrip('/')}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, Any] | None = None,
        multipart: CurlMime | None = None,
        accept: str = "application/json",
        timeout: int = 20,
        allowed_statuses: tuple[int, ...] = (200, 201, 202, 204),
        redaction_secrets: list[str] | None = None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "headers": {
                "X-Admin-Key": self.target.admin_key,
                "Accept": accept,
            },
            "proxies": None,
            "verify": True,
            "allow_redirects": False,
            "timeout": timeout,
            "impersonate": "chrome110",
        }
        if json_body is not None:
            kwargs["json"] = dict(json_body)
        if multipart is not None:
            kwargs["multipart"] = multipart
        method_name = str(method or "GET").upper()
        request_fn = getattr(cffi_requests, method_name.lower(), None)
        if request_fn is None:
            raise Codex2APITargetError("Codex2API 请求方法不受支持", endpoint=path)
        try:
            response = request_fn(self._url(path), **kwargs)
        except Exception as exc:
            detail = _redact(exc, [*self._secrets, *_collect_secrets(json_body), *(redaction_secrets or [])])
            raise Codex2APITargetError(
                f"Codex2API 请求异常: {detail or type(exc).__name__}",
                endpoint=path,
            ) from None
        status_code = int(getattr(response, "status_code", 0) or 0)
        if status_code not in allowed_statuses:
            detail = _response_detail(
                response,
                [*self._secrets, *_collect_secrets(json_body), *(redaction_secrets or [])],
            )
            if (
                status_code == 429
                and path.rstrip("/").endswith("/test")
                and not _contains_marker(detail, _AUTH_FAILURE_MARKERS)
                and _contains_marker(detail, _USAGE_LIMIT_MARKERS)
            ):
                return {
                    "success": True,
                    "usage_limited": True,
                    "message": "目标账号已鉴权，但当前处于用量限制",
                }
            if status_code in (401, 403):
                message = "Codex2API Admin Key 无效或无权限"
            else:
                message = f"Codex2API 请求失败（HTTP {status_code}）"
                if detail:
                    message += f": {detail}"
            if path.rstrip("/").endswith("/test"):
                return {
                    "success": False,
                    "verified": False,
                    "auth_failed": _contains_marker(detail, _AUTH_FAILURE_MARKERS),
                    "status_code": status_code,
                    "message": (
                        "目标账号鉴权失败"
                        if status_code in (401, 403)
                        else "目标账号测试未确认"
                    ),
                }
            raise Codex2APITargetError(
                message,
                status_code=status_code,
                endpoint=path,
            )
        try:
            payload = _parse_response_payload(response)
            return _sanitize_remote_payload(
                payload,
                [*self._secrets, *_collect_secrets(json_body), *(redaction_secrets or [])],
            )
        except ValueError:
            if (
                status_code == 429
                and path.rstrip("/").endswith("/test")
            ):
                detail = _response_detail(
                    response,
                    [*self._secrets, *_collect_secrets(json_body), *(redaction_secrets or [])],
                )
                if _contains_marker(detail, _AUTH_FAILURE_MARKERS):
                    return {
                        "success": False,
                        "auth_failed": True,
                        "message": "目标账号鉴权失败",
                    }
                if _contains_marker(detail, _USAGE_LIMIT_MARKERS):
                    return {
                        "success": True,
                        "usage_limited": True,
                        "message": "目标账号已鉴权，但当前处于用量限制",
                    }
            raise Codex2APITargetError(
                "Codex2API 响应无法解析",
                status_code=status_code,
                endpoint=path,
            ) from None

    def health(self) -> dict[str, Any]:
        try:
            return self._request("GET", "/api/admin/health")
        except Codex2APITargetError as exc:
            if exc.status_code != 404:
                raise
            return self._request("GET", "/health")

    def capabilities(self) -> dict[str, Any]:
        settings = self._request("GET", "/api/admin/settings")
        # The pinned upstream contract uses soft delete and exposes restore.
        # Older nodes that return 404 are downgraded by the operation error and
        # the migration fallback re-imports the saved credential.
        restore_supported = not bool(settings.get("restore_endpoint_disabled"))
        capabilities = {
            "settings": settings,
            "list_accounts": True,
            "usage_probe": True,
            "account_test": True,
            "enable_toggle": True,
            "lock_toggle": True,
            "delete": True,
            # Delete is a soft-delete in supported Codex2API versions.  The
            # restore route is version-dependent and is only advertised when
            # the target explicitly reports it.
            "restore": restore_supported,
            "soft_delete": True,
            # If the target does not advertise a restore endpoint, rollback
            # can still re-import the saved credential after a soft delete.
            "migratable": True,
            "rollback_strategy": "restore_or_reimport",
        }
        return capabilities

    def list_accounts(self) -> list[dict[str, Any]]:
        payload = self._request("GET", "/api/admin/accounts?channel=codex")
        rows = payload.get("accounts") if isinstance(payload, Mapping) else payload
        if not isinstance(rows, list):
            raise Codex2APITargetError("Codex2API 账号清单格式无效", endpoint="accounts")
        return [dict(row) for row in rows if isinstance(row, Mapping)]

    def trigger_usage_probe(self) -> dict[str, Any]:
        return self._request("POST", "/api/admin/accounts/usage/probe")

    def runtime_status(self) -> dict[str, Any]:
        return self._request("GET", "/api/admin/runtime-status")

    def api_key_usage(
        self,
        *,
        start: Any,
        end: Any,
    ) -> list[dict[str, Any]]:
        query = urlencode(
            {
                "start": start.isoformat() if hasattr(start, "isoformat") else str(start),
                "end": end.isoformat() if hasattr(end, "isoformat") else str(end),
            }
        )
        payload = self._request("GET", f"/api/admin/usage/api-keys?{query}")
        items = payload.get("items") if isinstance(payload, Mapping) else None
        if not isinstance(items, list):
            raise Codex2APITargetError(
                "Codex2API API Key 用量格式无效",
                endpoint="usage/api-keys",
            )
        return [dict(item) for item in items if isinstance(item, Mapping)]

    def import_refresh_token(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/api/admin/accounts", json_body=payload)

    def import_access_token(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/api/admin/accounts/at", json_body=payload)

    def import_full_json(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        mime = CurlMime()
        try:
            body = json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
            mime.addpart(name="format", data=b"json")
            mime.addpart(
                name="file",
                data=body,
                filename="chatgpt-account.json",
                content_type="application/json",
            )
            return self._request(
                "POST",
                "/api/admin/accounts/import",
                multipart=mime,
                accept="text/event-stream, application/json",
                timeout=30,
                redaction_secrets=_collect_secrets(payload),
            )
        finally:
            mime.close()

    def import_agent_identity(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Import one Codex Agent Identity through its dedicated endpoint."""
        auth_json = json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":"))
        request = {
            "auth_json": auth_json,
            "name": _text(payload.get("name") or payload.get("email")),
        }
        return self._request(
            "POST",
            "/api/admin/accounts/codex/agent-identity",
            json_body=request,
            redaction_secrets=_collect_secrets(payload),
        )

    def test_account(self, remote_id: int) -> dict[str, Any]:
        try:
            result = self._request(
                "GET",
                f"/api/admin/accounts/{int(remote_id)}/test",
                accept="text/event-stream, application/json",
                timeout=45,
                allowed_statuses=(200, 201, 202, 204, 429),
            )
        except Codex2APITargetError as exc:
            if exc.status_code == 429 and _contains_marker(
                str(exc), _AUTH_FAILURE_MARKERS
            ):
                return {
                    "success": False,
                    "auth_failed": True,
                    "message": "目标账号鉴权失败",
                }
            raise
        status_text = json.dumps(result, ensure_ascii=False).lower()
        if _contains_marker(status_text, _AUTH_FAILURE_MARKERS):
            return {**result, "success": False, "auth_failed": True}
        if _contains_marker(status_text, _USAGE_LIMIT_MARKERS):
            return {**result, "success": True, "usage_limited": True}
        if not bool(result.get("success")):
            return {
                **result,
                "success": False,
                "verified": False,
            }
        if not result:
            return {"success": False, "verified": False, "message": "目标账号未返回测试结果"}
        return result

    def set_enabled(self, remote_id: int, enabled: bool) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/admin/accounts/{int(remote_id)}/enable",
            json_body={"enabled": bool(enabled)},
        )

    def set_locked(self, remote_id: int, locked: bool) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/admin/accounts/{int(remote_id)}/lock",
            json_body={"locked": bool(locked)},
        )

    def refresh_account(self, remote_id: int) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/admin/accounts/{int(remote_id)}/refresh",
        )

    def delete_account(self, remote_id: int) -> dict[str, Any]:
        return self._request("DELETE", f"/api/admin/accounts/{int(remote_id)}")

    def restore_account(self, remote_id: int) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/admin/accounts/{int(remote_id)}/restore",
        )

    def update_scheduler(self, remote_id: int, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self._request(
            "PATCH",
            f"/api/admin/accounts/{int(remote_id)}/scheduler",
            json_body=payload,
        )

    def wait_for_zero_active_requests(
        self,
        remote_id: int,
        *,
        timeout_seconds: float = 600,
        poll_interval_seconds: float = 2,
        sleep_fn=time.sleep,
    ) -> bool:
        deadline = time.monotonic() + max(float(timeout_seconds), 0)
        while True:
            rows = self.list_accounts()
            row = next(
                (
                    item
                    for item in rows
                    if int(item.get("id") or 0) == int(remote_id)
                ),
                None,
            )
            if row is None:
                raise Codex2APITargetError(
                    "Codex2API 排空时未找到目标账号",
                    endpoint="accounts",
                )
            try:
                if "active_requests" not in row:
                    raise ValueError("missing")
                active = int(row.get("active_requests"))
            except (TypeError, ValueError):
                raise Codex2APITargetError(
                    "Codex2API 未提供可验证的活动请求数",
                    endpoint="accounts",
                ) from None
            if active <= 0:
                return True
            if time.monotonic() >= deadline:
                return False
            sleep_fn(max(float(poll_interval_seconds), 0))


__all__ = [
    "Codex2APITargetClient",
    "Codex2APITargetError",
    "TargetConfig",
    "default_target_config",
    "ensure_default_target",
    "get_target_client",
    "load_db_target_configs",
    "load_target_configs",
    "target_config_from_model",
]
