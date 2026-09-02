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

from curl_cffi import CurlMime
from curl_cffi import requests as cffi_requests


logger = logging.getLogger(__name__)
MAX_ERROR_DETAIL_LENGTH = 240


@dataclass(frozen=True)
class TargetConfig:
    """Connection details for one Codex2API instance."""

    id: int
    name: str
    base_url: str
    admin_key: str
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
    try:
        target_id = int(raw.get("id") or index)
    except (TypeError, ValueError):
        target_id = index
    if target_id <= 0:
        target_id = index
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
    admin_key = _text(values.get(ref))
    if not admin_key:
        # A caller may pass a mapping keyed by the numeric target id while
        # bootstrapping an installation.  This remains an in-memory fallback;
        # the persisted row still stores only the reference.
        admin_key = _text(values.get(str(getattr(model, "id", ""))))
    if not admin_key and int(getattr(model, "id", 0) or 0) == 1:
        # Installations upgraded from the original single-target settings keep
        # their key under this legacy name until the settings UI saves a
        # structured target secret.
        admin_key = _text(values.get("codex2api_admin_key"))
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
        rows = session.exec(
            select(Codex2APITargetModel).order_by(Codex2APITargetModel.id)
        ).all()
    result: list[TargetConfig] = []
    for row in rows:
        target = target_config_from_model(row, values)
        if target.admin_key:
            result.append(target)
    return result


def get_target_client(target_id: int, database_engine=None) -> "Codex2APITargetClient":
    """Resolve one persisted target and return a ready client."""

    from core.db import Codex2APITargetModel, engine as default_engine
    from sqlmodel import Session

    target_engine = database_engine or default_engine
    with Session(target_engine) as session:
        row = session.get(Codex2APITargetModel, int(target_id))
        if row is None:
            raise Codex2APITargetError(
                "Codex2API 目标不存在",
                endpoint="target",
            )
        target = target_config_from_model(row, _config_snapshot())
    return Codex2APITargetClient(target)


def ensure_default_target(database_engine=None, config: Mapping[str, Any] | None = None):
    """Materialize the legacy target in the structured target table once."""

    from core.db import Codex2APITargetModel, engine as default_engine
    from sqlmodel import Session, select

    target_engine = database_engine or default_engine
    target = default_target_config(config)
    if target is None:
        return None
    with Session(target_engine) as session:
        existing = session.exec(
            select(Codex2APITargetModel).where(
                Codex2APITargetModel.name == target.name
            )
        ).first()
        if existing is not None:
            return existing
        row = Codex2APITargetModel(
            name=target.name,
            target_type=target.target_type,
            server_label=target.server_label,
            base_url=target.base_url,
            admin_key_ref=f"codex2api_target_{target.id}_admin_key",
            default_pool_id=target.default_pool_id,
            enabled=target.enabled,
            health_status="unknown",
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return row


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
            detail = _redact(exc, self._secrets)
            raise Codex2APITargetError(
                f"Codex2API 请求异常: {detail or type(exc).__name__}",
                endpoint=path,
            ) from None
        status_code = int(getattr(response, "status_code", 0) or 0)
        if status_code not in allowed_statuses:
            detail = _response_detail(response, self._secrets)
            if status_code in (401, 403):
                message = "Codex2API Admin Key 无效或无权限"
            else:
                message = f"Codex2API 请求失败（HTTP {status_code}）"
                if detail:
                    message += f": {detail}"
            raise Codex2APITargetError(
                message,
                status_code=status_code,
                endpoint=path,
            )
        try:
            return _parse_response_payload(response)
        except ValueError:
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
        return {
            "settings": settings,
            "list_accounts": True,
            "usage_probe": True,
            "account_test": True,
            "enable_toggle": True,
            "lock_toggle": True,
            "delete": True,
            "restore": True,
            "migratable": True,
        }

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
            )
        finally:
            mime.close()

    def test_account(self, remote_id: int) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/api/admin/accounts/{int(remote_id)}/test",
            accept="text/event-stream, application/json",
            timeout=45,
        )

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
                return True
            try:
                active = int(row.get("active_requests") or 0)
            except (TypeError, ValueError):
                active = 0
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
