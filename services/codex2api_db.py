"""Read-only PostgreSQL adapter for a Codex2API instance.

This module deliberately has no dependency on the application's SQLModel
engine and is safe to import when a PostgreSQL driver is not installed.  It
uses one configured DSN for one Codex2API instance (the caller supplies the
local ``target_id``) and always addresses remote accounts by the stable
``(target_id, remote_id)`` pair.

Schema contract (Codex2API v2.9.x / PostgreSQL 18):

* ``accounts.id`` is the remote account id and ``accounts.credentials`` is a
  projected JSONB object when the recommended reader view/search path is used.
  The adapter reads email, ``account_id``, ``workspace_id``, plan and Codex
  quota fields from that projection.
* ``usage_logs.account_id`` references ``accounts.id``.  Billing is read from
  ``account_billed`` (and ``user_billed``), never from ``account_daily_usage``
  ``credits``.  Rows with status code 499 are excluded, matching Codex2API's
  account-usage endpoint.
* ``created_at`` is ``TIMESTAMPTZ``.  Daily history is grouped in the
  configured IANA timezone (default ``Asia/Shanghai``).

The SQL only relies on columns present since the v2.6 schema.  Newer columns
are intentionally not required, so a read-only account can be used against a
rolling deployment.  A missing driver, missing DSN, connection failure, or
schema error is represented in ``last_status`` and yields an empty result;
callers can retain their last local snapshot.
"""

from __future__ import annotations

import json
import math
import os
import re
from decimal import Decimal, InvalidOperation
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from importlib import import_module
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import unquote, urlsplit, urlunsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from services.codex2api_remote_accounts import remote_bool


AccountKey = tuple[int, int]
ConnectFactory = Callable[..., Any]


_POSTGRES_SCHEMES = {"postgres", "postgresql", "postgresql+psycopg", "postgresql+psycopg2"}
_PASSWORD_KW_RE = re.compile(r"(?i)(password\s*=\s*)([^\s]+)")

# Keep the database process from ever receiving the credential-bearing JSONB
# document. Only fields needed for identity, display, and quota projection are
# selected; token-shaped keys are excluded at the SQL boundary as well as in
# the Python sanitizer.
_SAFE_CREDENTIALS_PROJECTION = """
jsonb_strip_nulls(jsonb_build_object(
    'email', a.credentials->>'email',
    'user_email', a.credentials->>'user_email',
    'account_id', a.credentials->>'account_id',
    'chatgpt_account_id', a.credentials->>'chatgpt_account_id',
    'user_id', a.credentials->>'user_id',
    'workspace_id', a.credentials->>'workspace_id',
    'effective_workspace_id', a.credentials->>'effective_workspace_id',
    'plan_type', a.credentials->>'plan_type',
    'upstream_type', a.credentials->>'upstream_type',
    'subscription_expires_at', a.credentials->>'subscription_expires_at',
    'expires_at', a.credentials->>'expires_at',
    'codex_5h_used_percent', a.credentials->>'codex_5h_used_percent',
    'codex_7d_used_percent', a.credentials->>'codex_7d_used_percent',
    'codex_5h_reset_at', a.credentials->>'codex_5h_reset_at',
    'codex_7d_reset_at', a.credentials->>'codex_7d_reset_at',
    'codex_usage_updated_at', a.credentials->>'codex_usage_updated_at',
    'codex_5h_usage_updated_at', a.credentials->>'codex_5h_usage_updated_at',
    'codex_7d_usage_updated_at', a.credentials->>'codex_7d_usage_updated_at',
    'workspace_name', a.credentials->>'workspace_name',
    'codex_credits', jsonb_strip_nulls(jsonb_build_object(
        'balance', a.credentials->'codex_credits'->>'balance',
        'has_credits', a.credentials->'codex_credits'->>'has_credits'
    ))
))
"""


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).decode("utf-8", errors="replace").strip()
    return str(value).strip()


def _finite_number(value: Any, *, integer: bool = False) -> int | float | None:
    if value is None or isinstance(value, bool):
        return None
    if integer:
        try:
            precise = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            return None
        if not precise.is_finite():
            return None
        return max(0, int(precise))
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number):
        return None
    return max(0.0, number)


def _int_value(value: Any) -> int:
    parsed = _finite_number(value, integer=True)
    return int(parsed or 0)


def _float_value(value: Any) -> float:
    parsed = _finite_number(value)
    return float(parsed or 0.0)


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        value = bytes(value).decode("utf-8", errors="replace")
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def _credential_value(credentials: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in credentials and credentials[key] not in (None, ""):
            return credentials[key]
    return None


def _redact_dsn(value: str) -> str:
    """Return a display-safe DSN without password material."""

    dsn = _text(value)
    if not dsn:
        return ""
    # Keyword libpq DSNs (``host=... password=...``).
    dsn = _PASSWORD_KW_RE.sub(r"\1***", dsn)
    try:
        parsed = urlsplit(dsn)
    except ValueError:
        return dsn
    if parsed.scheme and parsed.netloc:
        username = unquote(parsed.username or "")
        host = parsed.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        if parsed.port:
            host = f"{host}:{parsed.port}"
        netloc = ""
        if username:
            netloc = f"{username}:***@"
        netloc += host
        # Passwords occasionally arrive as a query parameter in generated
        # libpq URLs; redact that form as well.
        query = re.sub(r"(?i)(^|&)password=[^&]*", r"\1password=***", parsed.query)
        return urlunsplit((parsed.scheme, netloc, parsed.path, query, parsed.fragment))
    return dsn


def _is_postgres_dsn(dsn: str) -> bool:
    value = _text(dsn)
    if not value:
        return False
    if "=" in value and "://" not in value:
        # Keyword DSNs are accepted when they identify the postgres driver.
        return any(token in value.lower() for token in ("host=", "dbname=", "user=", "password="))
    try:
        return (urlsplit(value).scheme or "").lower() in _POSTGRES_SCHEMES
    except ValueError:
        return False


@dataclass(frozen=True)
class Codex2APIDBConfig:
    """Connection settings for one Codex2API PostgreSQL database."""

    dsn: str = ""
    connect_timeout: int = 5
    statement_timeout_ms: int = 15_000
    timezone_name: str = "Asia/Shanghai"
    target_id: int | None = None

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, Any] | None = None,
        *,
        dsn: str | None = None,
        connect_timeout: int | None = None,
        statement_timeout_ms: int | None = None,
        timezone_name: str | None = None,
        target_id: int | None = None,
    ) -> "Codex2APIDBConfig":
        values = os.environ if environ is None else environ
        selected = _text(dsn) or _text(values.get("CODEX2API_DATABASE_URL"))
        def _bounded(value: Any, fallback: int, minimum: int, maximum: int) -> int:
            try:
                return min(maximum, max(minimum, int(value)))
            except (TypeError, ValueError):
                return fallback

        selected_target: int | None = target_id
        if selected_target is None:
            raw_target = values.get("CODEX2API_DATABASE_TARGET_ID")
            if raw_target not in (None, ""):
                try:
                    selected_target = int(raw_target)
                except (TypeError, ValueError):
                    selected_target = None
        return cls(
            dsn=selected,
            connect_timeout=_bounded(
                connect_timeout if connect_timeout is not None else values.get("CODEX2API_DATABASE_CONNECT_TIMEOUT", 5),
                5,
                1,
                60,
            ),
            statement_timeout_ms=_bounded(
                statement_timeout_ms if statement_timeout_ms is not None else values.get("CODEX2API_DATABASE_STATEMENT_TIMEOUT_MS", 15_000),
                15_000,
                100,
                120_000,
            ),
            timezone_name=_text(timezone_name) or _text(values.get("CODEX2API_DATABASE_TIMEZONE")) or "Asia/Shanghai",
            target_id=selected_target,
        )

    @property
    def configured(self) -> bool:
        return bool(_text(self.dsn)) and _is_postgres_dsn(self.dsn)

    @property
    def redacted_dsn(self) -> str:
        return _redact_dsn(self.dsn)


def load_codex2api_db_config(
    dsn: str | None = None,
    *,
    environ: Mapping[str, Any] | None = None,
    **kwargs: Any,
) -> Codex2APIDBConfig:
    """Build config from an explicit DSN or ``CODEX2API_DATABASE_URL``."""

    return Codex2APIDBConfig.from_env(environ, dsn=dsn, **kwargs)


def database_dsn_for_target(
    target_id: int,
    environ: Mapping[str, Any] | None = None,
) -> str:
    """Resolve a DSN for one target without silently sharing another target.

    Target-specific variables (``CODEX2API_DATABASE_URL_<id>``) and the JSON
    mapping take precedence. The unsuffixed variable is treated as a legacy
    single-target setting and is usable for target 1, or for the target named
    by ``CODEX2API_DATABASE_TARGET_ID``.
    """
    values = os.environ if environ is None else environ
    try:
        normalized_target = int(target_id)
    except (TypeError, ValueError):
        return ""
    if normalized_target <= 0:
        return ""
    specific = _text(values.get(f"CODEX2API_DATABASE_URL_{normalized_target}"))
    if specific:
        return specific
    raw_mapping = _text(values.get("CODEX2API_DATABASE_URLS_JSON"))
    if raw_mapping:
        try:
            mapping = json.loads(raw_mapping)
        except (TypeError, ValueError, json.JSONDecodeError):
            mapping = {}
        if isinstance(mapping, Mapping):
            selected = _text(
                mapping.get(str(normalized_target))
                or mapping.get(normalized_target)
            )
            if selected:
                return selected
    shared = _text(values.get("CODEX2API_DATABASE_URL"))
    if not shared:
        return ""
    pinned = _text(values.get("CODEX2API_DATABASE_TARGET_ID"))
    if pinned:
        try:
            return shared if int(pinned) == normalized_target else ""
        except (TypeError, ValueError):
            return ""
    return shared if normalized_target == 1 else ""


def get_codex2api_db_adapter(
    target_id: int,
    *,
    environ: Mapping[str, Any] | None = None,
    connect_factory: ConnectFactory | None = None,
) -> "Codex2APIDBAdapter | None":
    """Return a configured reader for one target, or ``None`` if disabled."""
    dsn = database_dsn_for_target(target_id, environ)
    if not dsn:
        return None
    config = Codex2APIDBConfig.from_env(
        environ,
        dsn=dsn,
        target_id=int(target_id),
    )
    if not config.configured:
        return None
    if connect_factory is None:
        return Codex2APIDBAdapter(config)
    return Codex2APIDBAdapter(config, connect_factory=connect_factory)


def _load_driver() -> tuple[str, Any]:
    """Load psycopg3 first, then psycopg2, without importing at module load."""

    errors: list[str] = []
    for name in ("psycopg", "psycopg2"):
        try:
            return name, import_module(name)
        except ImportError as exc:
            errors.append(str(exc))
    raise ImportError("PostgreSQL driver is not installed (install psycopg[binary] or psycopg2)")


def _column_name(column: Any) -> str:
    if hasattr(column, "name"):
        return _text(column.name)
    if isinstance(column, Sequence) and column:
        return _text(column[0])
    return _text(column)


def _rows_as_dict(cursor: Any) -> list[dict[str, Any]]:
    description = list(getattr(cursor, "description", None) or ())
    names = [_column_name(item) for item in description]
    rows = cursor.fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        if isinstance(row, Mapping):
            result.append(dict(row))
            continue
        values = list(row) if isinstance(row, (tuple, list)) else [row]
        if names:
            result.append({name: values[index] if index < len(values) else None for index, name in enumerate(names)})
    return result


def _today_bounds(as_of: datetime | date | None, timezone_name: str) -> tuple[datetime, datetime, str]:
    try:
        tz = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        tz = timezone.utc
    if as_of is None:
        current = datetime.now(tz)
    elif isinstance(as_of, date) and not isinstance(as_of, datetime):
        current = datetime.combine(as_of, time.min, tzinfo=tz)
    else:
        current = as_of
        if current.tzinfo is None:
            current = current.replace(tzinfo=tz)
        else:
            current = current.astimezone(tz)
    start = current.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1), getattr(tz, "key", "UTC")


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = _text(value)
    return text or None


def _quota(credentials: Mapping[str, Any]) -> dict[str, Any]:
    def percent(*keys: str) -> float | None:
        value = _finite_number(_credential_value(credentials, *keys))
        if value is None:
            return None
        return min(100.0, max(0.0, float(value)))

    def text(*keys: str) -> str | None:
        value = _credential_value(credentials, *keys)
        result = _text(value)
        return result or None

    credits = _credential_value(credentials, "codex_credits", "credits")
    # Keep the credits object useful for diagnostics while ensuring it is a
    # detached JSON-safe value.
    if isinstance(credits, Mapping):
        credits = _public_json_value(credits)
    elif credits not in (None, ""):
        credits = _text(credits)
    else:
        credits = None
    return {
        "5h_used_percent": percent("codex_5h_used_percent", "usage_percent_5h", "codex_5h_usage_percent"),
        "7d_used_percent": percent("codex_7d_used_percent", "usage_percent_7d", "codex_7d_usage_percent"),
        "5h_reset_at": text("codex_5h_reset_at", "reset_5h_at", "codex_5h_reset"),
        "7d_reset_at": text("codex_7d_reset_at", "reset_7d_at", "codex_7d_reset"),
        "usage_updated_at": text("codex_usage_updated_at", "codex_5h_usage_updated_at", "codex_7d_usage_updated_at"),
        "credits": credits,
    }


_SECRET_FIELD_MARKERS = (
    "token",
    "password",
    "secret",
    "cookie",
    "credential",
    "private_key",
    "admin_key",
    "api_key",
    "bearer",
)


def _public_json_value(value: Any) -> Any:
    """Copy JSON data while dropping credential-shaped mapping keys."""

    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, child in value.items():
            normalized = str(key or "").strip().lower().replace("-", "_")
            if any(marker in normalized for marker in _SECRET_FIELD_MARKERS):
                continue
            result[str(key)] = _public_json_value(child)
        return result
    if isinstance(value, (list, tuple)):
        return [_public_json_value(item) for item in list(value)[:1000]]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _metric(requests: Any, tokens: Any, billed: Any, user_billed: Any) -> dict[str, Any]:
    return {
        "requests": _int_value(requests),
        "tokens": _int_value(tokens),
        "billed_usd": _float_value(billed),
        "user_billed_usd": _float_value(user_billed),
    }


def _account_metadata(row: Mapping[str, Any], target_id: int) -> dict[str, Any] | None:
    """Project one ``accounts`` row into the inventory-safe public shape."""

    remote_id = _int_value(row.get("remote_id"))
    if remote_id <= 0:
        return None
    credentials = _json_object(row.get("credentials"))
    email = _text(_credential_value(credentials, "email", "user_email"))
    account_id = _text(
        _credential_value(credentials, "account_id", "chatgpt_account_id", "user_id")
    )
    chatgpt_account_id = _text(
        _credential_value(credentials, "chatgpt_account_id", "account_id", "user_id")
    )
    workspace_id = _text(
        _credential_value(credentials, "workspace_id", "effective_workspace_id")
    )
    plan_type = _text(_credential_value(credentials, "plan_type", "planType"))
    status = _text(row.get("status")) or "unknown"
    cooldown_reason = _text(row.get("cooldown_reason"))
    cooldown_until = _iso(row.get("cooldown_until"))
    remote_status = status.lower()
    if cooldown_reason and any(
        marker in cooldown_reason.lower() for marker in ("rate", "limit")
    ):
        try:
            cooldown_at = datetime.fromisoformat(
                str(cooldown_until or "").replace("Z", "+00:00")
            )
            if cooldown_at.tzinfo is None:
                cooldown_at = cooldown_at.replace(tzinfo=timezone.utc)
            if cooldown_at > datetime.now(timezone.utc):
                remote_status = "rate_limited"
        except (TypeError, ValueError):
            # A malformed expiry must not promote an account to a schedulable
            # state; retain the persisted status and let the normal health
            # reconciliation decide what to do.
            pass
    quota_values = _quota(credentials)
    if remote_status in {"active", "ready"} and any(
        value is not None and value >= 100
        for value in (
            quota_values.get("5h_used_percent"),
            quota_values.get("7d_used_percent"),
        )
    ):
        remote_status = "rate_limited"
    result: dict[str, Any] = {
        "target_id": int(target_id),
        "remote_id": remote_id,
        "remote_account_id": remote_id,
        "name": _text(row.get("name")),
        "email": email,
        "account_id": account_id,
        "chatgpt_account_id": chatgpt_account_id,
        "user_id": _text(_credential_value(credentials, "user_id")),
        "workspace_id": workspace_id,
        "effective_workspace_id": workspace_id,
        "plan_type": plan_type,
        "subscription_expires_at": _iso(
            _credential_value(credentials, "subscription_expires_at", "subscriptionExpiresAt")
        ),
        "platform": _text(row.get("platform")),
        "account_type": _text(row.get("account_type")),
        "status": status,
        "remote_status": remote_status,
        "enabled": remote_bool(row.get("enabled"), True),
        "locked": remote_bool(row.get("locked"), False),
        "cooldown_reason": cooldown_reason,
        "cooldown_until": cooldown_until,
        "created_at": _iso(row.get("created_at")),
        "updated_at": _iso(row.get("updated_at")),
        "deleted_at": _iso(row.get("deleted_at")),
        "quota": quota_values,
        "source": "codex2api_postgresql",
    }

    # Keep the API-backed inventory contract: quota values are available at
    # the top level as well as in the diagnostic ``quota`` object.
    aliases = {
        "usage_percent_5h": ("codex_5h_used_percent", "usage_percent_5h", "codex_5h_usage_percent"),
        "usage_percent_7d": ("codex_7d_used_percent", "usage_percent_7d", "codex_7d_usage_percent"),
        "reset_5h_at": ("codex_5h_reset_at", "reset_5h_at", "codex_5h_reset"),
        "reset_7d_at": ("codex_7d_reset_at", "reset_7d_at", "codex_7d_reset"),
        "quota_5h_updated_at": ("codex_5h_usage_updated_at", "quota_5h_updated_at"),
        "quota_7d_updated_at": ("codex_7d_usage_updated_at", "quota_7d_updated_at", "codex_usage_updated_at"),
        "codex_5h_usage_updated_at": ("codex_5h_usage_updated_at",),
        "codex_usage_updated_at": ("codex_usage_updated_at", "codex_7d_usage_updated_at"),
        "workspace_name": ("workspace_name",),
    }
    for output_key, keys in aliases.items():
        value = _credential_value(credentials, *keys)
        if output_key.startswith("usage_percent_"):
            value = _finite_number(value)
            if value is not None:
                value = min(100.0, max(0.0, float(value)))
        else:
            value = _iso(value) if output_key.endswith("_at") or output_key.endswith("updated_at") else _text(value)
            if not value:
                value = None
        if value is not None:
            result[output_key] = value
    if "usage_percent_5h" in result:
        result["has_5h_window"] = True
    return result


class Codex2APIDBAdapter:
    """Batch reader for one Codex2API PostgreSQL database.

    ``connect_factory`` is intentionally injectable for tests and for callers
    that already own a connection pool.  A normal deployment leaves it unset;
    the adapter lazily imports psycopg3/psycopg2 and opens a short-lived,
    read-only transaction per batch.
    """

    def __init__(
        self,
        config: Codex2APIDBConfig | None = None,
        *,
        connect_factory: ConnectFactory | None = None,
        target_id: int | None = None,
    ) -> None:
        self.config = config or Codex2APIDBConfig.from_env()
        if target_id is not None:
            self.config = Codex2APIDBConfig(
                dsn=self.config.dsn,
                connect_timeout=self.config.connect_timeout,
                statement_timeout_ms=self.config.statement_timeout_ms,
                timezone_name=self.config.timezone_name,
                target_id=int(target_id),
            )
        self._connect_factory = connect_factory
        self.last_status: dict[str, Any] = {
            "available": False,
            "status": "not_configured" if not self.config.configured else "idle",
            "source": "postgresql",
            "error": "" if not self.config.configured else None,
        }

    def _set_status(self, status: str, *, available: bool, error: str = "", **extra: Any) -> None:
        payload = {
            "available": bool(available),
            "status": status,
            "source": "postgresql",
            "error": self._safe_error(error),
        }
        payload.update(extra)
        self.last_status = payload

    def _safe_error(self, error: Any) -> str:
        """Redact DSN credentials before exposing adapter diagnostics."""
        value = _text(error)
        if not value:
            return ""
        raw_dsn = _text(self.config.dsn)
        if raw_dsn:
            value = value.replace(raw_dsn, self.config.redacted_dsn)
        return _redact_dsn(value)[:500]

    def _connect(self) -> Any:
        if not self.config.configured:
            raise ValueError("Codex2API PostgreSQL DSN is not configured")
        if self._connect_factory is not None:
            factory = self._connect_factory
            try:
                connection = factory(self.config.dsn, self.config)
            except TypeError:
                connection = factory(self.config.dsn)
        else:
            driver_name, driver = _load_driver()
            connect = getattr(driver, "connect")
            connect_dsn = self.config.dsn
            # SQLAlchemy-style URLs are common in application settings, while
            # psycopg expects the plain libpq scheme.
            connect_dsn = re.sub(r"^postgresql\+(?:psycopg2?|asyncpg)://", "postgresql://", connect_dsn, flags=re.I)
            if driver_name == "psycopg":
                connection = connect(
                    connect_dsn,
                    connect_timeout=self.config.connect_timeout,
                    autocommit=False,
                )
            else:
                connection = connect(
                    connect_dsn,
                    connect_timeout=self.config.connect_timeout,
                )
        set_session = getattr(connection, "set_session", None)
        if callable(set_session):
            try:
                set_session(readonly=True, autocommit=False)
            except TypeError:
                # A few lightweight DBAPI wrappers expose only readonly.
                set_session(readonly=True)
        else:
            cursor = connection.cursor()
            try:
                cursor.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
            finally:
                cursor.close()
        return connection

    def _execute(self, cursor: Any, query: str, params: Sequence[Any]) -> None:
        cursor.execute(query, tuple(params))

    @staticmethod
    def _id_placeholders(ids: Sequence[int]) -> str:
        return ", ".join("%s" for _ in ids)

    def _internal_reason_clause(self, cursor: Any) -> str:
        """Return the maintenance-log filter when the column exists."""
        try:
            self._execute(
                cursor,
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM pg_catalog.pg_attribute AS attr
                    WHERE attr.attrelid = to_regclass('usage_logs')
                      AND attr.attname = 'internal_reason'
                      AND NOT attr.attisdropped
                ) AS column_exists
                """,
                (),
            )
            row = cursor.fetchone()
            if isinstance(row, (tuple, list)):
                exists = bool(row[0]) if row else False
            else:
                exists = bool(row)
            if not exists:
                return ""
            self._execute(
                cursor,
                """
                SELECT has_column_privilege(
                    current_user,
                    to_regclass('usage_logs'),
                    'internal_reason',
                    'SELECT'
                )
                """,
                (),
            )
            privilege_row = cursor.fetchone()
            readable = bool(
                privilege_row[0]
                if isinstance(privilege_row, (tuple, list))
                else privilege_row
            )
            if not readable:
                raise RuntimeError("usage_logs.internal_reason is not readable")
            return "AND TRIM(COALESCE(l.internal_reason, '')) = ''"
        except Exception as exc:
            # A metadata/privilege failure is different from a schema that
            # explicitly lacks the column.  Failing closed avoids silently
            # counting maintenance/probe logs when the filter capability is
            # unknown; the caller can fall back to the admin API.
            raise RuntimeError("cannot verify usage_logs internal_reason filter") from exc

    def probe(self) -> dict[str, Any]:
        """Check DSN, driver, connectivity, and read-only session setup."""

        if not self.config.configured:
            self._set_status("not_configured", available=False, error="DSN is not configured")
            return dict(self.last_status)
        connection = None
        select_one_ok = False
        try:
            connection = self._connect()
            cursor = connection.cursor()
            try:
                self._execute(cursor, "SELECT 1", ())
                row = cursor.fetchone()
                if not row or _int_value(row[0] if isinstance(row, (tuple, list)) else row) != 1:
                    raise RuntimeError("probe returned an unexpected result")
                select_one_ok = True
                # Verify the actual relations/columns used by the reader while
                # keeping the probe result set empty.  A role with CONNECT but
                # no table/view privilege must report a schema error before an
                # inventory run treats an empty response as authoritative.
                self._execute(
                    cursor,
                    "SELECT id, name, platform, type, credentials, status, enabled, "
                    "locked, cooldown_reason, cooldown_until, created_at, updated_at, "
                    "deleted_at, error_message FROM accounts LIMIT 0",
                    (),
                )
                self._execute(
                    cursor,
                    "SELECT account_id, status_code, total_tokens, account_billed, "
                    "user_billed, created_at FROM usage_logs LIMIT 0",
                    (),
                )
                self._execute(
                    cursor,
                    "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_attribute AS attr "
                    "WHERE attr.attrelid = to_regclass('usage_logs') "
                    "AND attr.attname = 'internal_reason' "
                    "AND NOT attr.attisdropped)",
                    (),
                )
                internal_reason_row = cursor.fetchone()
                if isinstance(internal_reason_row, (tuple, list)):
                    has_internal_reason = bool(internal_reason_row[0])
                else:
                    has_internal_reason = bool(internal_reason_row)
                if has_internal_reason:
                    self._execute(
                        cursor,
                        "SELECT has_column_privilege(current_user, "
                        "to_regclass('usage_logs'), "
                        "'internal_reason', 'SELECT')",
                        (),
                    )
                    privilege_row = cursor.fetchone()
                    readable = bool(
                        privilege_row[0]
                        if isinstance(privilege_row, (tuple, list))
                        else privilege_row
                    )
                    if not readable:
                        raise RuntimeError("usage_logs.internal_reason is not readable")
                if has_internal_reason:
                    self._execute(
                        cursor,
                        "SELECT internal_reason FROM usage_logs LIMIT 0",
                        (),
                    )
            finally:
                cursor.close()
            self._set_status("available", available=True, read_only=True)
        except ImportError as exc:
            self._set_status("dependency_missing", available=False, error=str(exc))
        except Exception as exc:
            self._set_status(
                "schema_error" if select_one_ok else "connection_error",
                available=False,
                error=str(exc),
            )
        finally:
            if connection is not None:
                try:
                    connection.rollback()
                except Exception:
                    pass
                try:
                    connection.close()
                except Exception:
                    pass
        return dict(self.last_status)

    def _validate_keys(self, keys: Iterable[AccountKey]) -> tuple[list[AccountKey], int]:
        unique: list[AccountKey] = []
        seen: set[AccountKey] = set()
        for raw in keys:
            try:
                if not isinstance(raw, (tuple, list)) or len(raw) != 2:
                    raise ValueError
                target_id, remote_id = raw
                # IDs are database primary keys.  Do not silently truncate
                # floats (1.5 -> 1) or accept booleans (True -> 1), since
                # either can make a caller read a different target/account
                # pair than it requested.
                if isinstance(target_id, bool) or isinstance(remote_id, bool):
                    raise ValueError
                if isinstance(target_id, float) and not target_id.is_integer():
                    raise ValueError
                if isinstance(remote_id, float) and not remote_id.is_integer():
                    raise ValueError
                key = (int(target_id), int(remote_id))
            except (TypeError, ValueError):
                raise ValueError("account keys must be (target_id, remote_id) integer pairs") from None
            if key[0] <= 0 or key[1] <= 0:
                raise ValueError("target_id and remote_id must be positive")
            if key not in seen:
                seen.add(key)
                unique.append(key)
        if not unique:
            return [], self.config.target_id or 0
        target_ids = {key[0] for key in unique}
        if self.config.target_id is not None and target_ids != {int(self.config.target_id)}:
            raise ValueError("account keys do not match configured target_id")
        if len(target_ids) != 1:
            raise ValueError("one database adapter can read one target at a time")
        return unique, next(iter(target_ids))

    def fetch_account_metadata(
        self,
        target_id: int | None = None,
        *,
        channel: str = "codex",
    ) -> list[dict[str, Any]]:
        """List every remote account using an accounts-only read query.

        This deliberately does not touch ``usage_logs``.  It is used by the
        inventory synchronizer, which needs a complete remote list and can
        fetch usage separately when required.  An unavailable database returns
        an empty list and records a non-available status so callers can fall
        back to the HTTP client without mistaking the result for an empty
        remote account set.
        """

        selected_target = target_id if target_id is not None else self.config.target_id
        try:
            selected_target = int(selected_target or 0)
        except (TypeError, ValueError):
            selected_target = 0
        if selected_target <= 0:
            self._set_status(
                "invalid_target",
                available=False,
                error="target_id is required for account metadata",
            )
            return []
        if (
            self.config.target_id is not None
            and selected_target != int(self.config.target_id)
        ):
            self._set_status(
                "invalid_target",
                available=False,
                error="target_id does not match configured database target",
            )
            return []
        if not self.config.configured:
            self._set_status("not_configured", available=False, error="DSN is not configured")
            return []
        channel_name = _text(channel).lower() or "codex"
        if channel_name not in {"codex", "grok", "all", "*"}:
            self._set_status(
                "invalid_channel",
                available=False,
                error="unsupported account channel",
            )
            return []
        if channel_name == "grok":
            channel_clause = (
                "AND LOWER(COALESCE(a.credentials->>'upstream_type', '')) "
                "IN ('grok', 'xai')"
            )
        elif channel_name in {"all", "*"}:
            channel_clause = ""
        else:
            channel_clause = (
                "AND LOWER(COALESCE(a.credentials->>'upstream_type', '')) "
                "NOT IN ('grok', 'antigravity', 'claude')"
            )

        connection = None
        try:
            connection = self._connect()
            cursor = connection.cursor()
            try:
                self._execute(
                    cursor,
                    f"SET LOCAL statement_timeout = {int(self.config.statement_timeout_ms)}",
                    (),
                )
                self._execute(
                    cursor,
                    f"""
                    SELECT a.id AS remote_id, a.name, a.platform,
                           a.type AS account_type,
                           {_SAFE_CREDENTIALS_PROJECTION} AS credentials,
                           a.status,
                           a.enabled, a.locked, a.cooldown_reason,
                           a.cooldown_until, a.created_at, a.updated_at,
                           a.deleted_at
                    FROM accounts AS a
                    WHERE a.status <> 'deleted'
                      AND COALESCE(a.error_message, '') <> 'deleted'
                      AND a.deleted_at IS NULL
                      {channel_clause}
                    ORDER BY a.id
                    """,
                    (),
                )
                account_rows = _rows_as_dict(cursor)
            finally:
                cursor.close()

            result = []
            for row in account_rows:
                projected = _account_metadata(row, selected_target)
                if projected is not None:
                    result.append(projected)
            self._set_status(
                "available",
                available=True,
                read_only=True,
                rows=len(result),
            )
            return result
        except ImportError as exc:
            self._set_status("dependency_missing", available=False, error=str(exc))
            return []
        except Exception as exc:
            self._set_status("query_error", available=False, error=str(exc))
            return []
        finally:
            if connection is not None:
                try:
                    connection.rollback()
                except Exception:
                    pass
                try:
                    connection.close()
                except Exception:
                    pass

    # Keep a descriptive alias for callers that use the list-oriented name.
    list_account_metadata = fetch_account_metadata

    def fetch_account_snapshots(
        self,
        keys: Iterable[AccountKey],
        *,
        as_of: datetime | date | None = None,
        include_history: bool = True,
    ) -> dict[AccountKey, dict[str, Any]]:
        """Fetch account metadata and aggregated usage in two or three queries.

        The result is keyed by ``(target_id, remote_id)``.  A missing account
        still gets no row, allowing callers to distinguish a remote deletion
        from a real zero-usage account. ``include_history=False`` skips the
        expensive daily group-by used by card summary reads. Database/driver
        errors return an empty mapping and update :attr:`last_status`.
        """

        requested, target_id = self._validate_keys(keys)
        if not requested:
            return {}
        if not self.config.configured:
            self._set_status("not_configured", available=False, error="DSN is not configured")
            return {}
        remote_ids = [key[1] for key in requested]
        placeholders = self._id_placeholders(remote_ids)
        today_start, tomorrow_start, tz_name = _today_bounds(as_of, self.config.timezone_name)
        connection = None
        try:
            connection = self._connect()
            cursor = connection.cursor()
            try:
                # Setting a local statement timeout is safe in a read-only
                # transaction and keeps a large usage_logs scan bounded.
                self._execute(
                    cursor,
                    f"SET LOCAL statement_timeout = {int(self.config.statement_timeout_ms)}",
                    (),
                )
                internal_reason_clause = self._internal_reason_clause(cursor)

                self._execute(
                    cursor,
                    f"""
                    SELECT a.id AS remote_id, a.name, a.platform,
                           a.type AS account_type,
                           {_SAFE_CREDENTIALS_PROJECTION} AS credentials,
                           a.status,
                           a.enabled, a.locked, a.cooldown_reason,
                           a.cooldown_until, a.created_at, a.updated_at,
                           a.deleted_at
                    FROM accounts AS a
                    WHERE a.id IN ({placeholders})
                      AND a.status <> 'deleted'
                      AND COALESCE(a.error_message, '') <> 'deleted'
                      AND a.deleted_at IS NULL
                      AND LOWER(COALESCE(a.credentials->>'upstream_type', ''))
                          NOT IN ('grok', 'antigravity', 'claude')
                    """,
                    remote_ids,
                )
                account_rows = _rows_as_dict(cursor)

                self._execute(
                    cursor,
                    f"""
                    SELECT l.account_id,
                           COUNT(*) FILTER (WHERE l.status_code <> 499) AS total_requests,
                           COALESCE(SUM(l.total_tokens) FILTER (WHERE l.status_code <> 499), 0) AS total_tokens,
                           COALESCE(SUM(l.account_billed) FILTER (WHERE l.status_code <> 499), 0) AS total_account_billed,
                           COALESCE(SUM(l.user_billed) FILTER (WHERE l.status_code <> 499), 0) AS total_user_billed,
                           COUNT(*) FILTER (WHERE l.created_at >= %s AND l.created_at < %s AND l.status_code <> 499) AS today_requests,
                           COALESCE(SUM(l.total_tokens) FILTER (WHERE l.created_at >= %s AND l.created_at < %s AND l.status_code <> 499), 0) AS today_tokens,
                           COALESCE(SUM(l.account_billed) FILTER (WHERE l.created_at >= %s AND l.created_at < %s AND l.status_code <> 499), 0) AS today_account_billed,
                           COALESCE(SUM(l.user_billed) FILTER (WHERE l.created_at >= %s AND l.created_at < %s AND l.status_code <> 499), 0) AS today_user_billed,
                           MAX(l.created_at) FILTER (WHERE l.status_code <> 499) AS last_request_at
                    FROM usage_logs AS l
                    WHERE l.account_id IN ({placeholders})
                      AND l.created_at < %s
                      {internal_reason_clause}
                    GROUP BY account_id
                    """,
                    [today_start, tomorrow_start, today_start, tomorrow_start,
                     today_start, tomorrow_start, today_start, tomorrow_start,
                     *remote_ids, tomorrow_start],
                )
                summary_rows = _rows_as_dict(cursor)

                if include_history:
                    self._execute(
                        cursor,
                        f"""
                        SELECT l.account_id,
                               (l.created_at AT TIME ZONE %s)::date AS day,
                               COUNT(*) FILTER (WHERE l.status_code <> 499) AS requests,
                               COALESCE(SUM(l.total_tokens) FILTER (WHERE l.status_code <> 499), 0) AS tokens,
                               COALESCE(SUM(l.account_billed) FILTER (WHERE l.status_code <> 499), 0) AS account_billed,
                               COALESCE(SUM(l.user_billed) FILTER (WHERE l.status_code <> 499), 0) AS user_billed
                        FROM usage_logs AS l
                        WHERE l.account_id IN ({placeholders})
                          AND l.created_at < %s
                          {internal_reason_clause}
                        GROUP BY account_id, day
                        ORDER BY account_id, day
                        """,
                        [tz_name, *remote_ids, tomorrow_start],
                    )
                    history_rows = _rows_as_dict(cursor)
                else:
                    history_rows = []
            finally:
                cursor.close()

            by_id: dict[int, dict[str, Any]] = {}
            for row in account_rows:
                remote_id = _int_value(row.get("remote_id"))
                if remote_id <= 0:
                    continue
                credentials = _json_object(row.get("credentials"))
                quota = _quota(credentials)
                email = _text(_credential_value(credentials, "email", "user_email"))
                account_id = _text(_credential_value(credentials, "account_id", "chatgpt_account_id"))
                workspace_id = _text(_credential_value(credentials, "workspace_id"))
                plan_type = _text(_credential_value(credentials, "plan_type", "planType"))
                subscription_expires_at = _iso(
                    _credential_value(credentials, "subscription_expires_at", "subscriptionExpiresAt")
                )
                expires_at = _iso(_credential_value(credentials, "expires_at", "expiresAt"))
                total = _metric(0, 0, 0, 0)
                today = _metric(0, 0, 0, 0)
                by_id[remote_id] = {
                    "target_id": target_id,
                    "remote_id": remote_id,
                    "name": _text(row.get("name")),
                    "email": email,
                    "account_id": account_id,
                    "workspace_id": workspace_id,
                    "user_id": _text(_credential_value(credentials, "user_id")),
                    "plan_type": plan_type,
                    "subscription_expires_at": subscription_expires_at,
                    "expires_at": expires_at,
                    "platform": _text(row.get("platform")),
                    "account_type": _text(row.get("account_type")),
                    "status": _text(row.get("status")) or "unknown",
                    "enabled": remote_bool(row.get("enabled"), True),
                    "locked": remote_bool(row.get("locked"), False),
                    "cooldown_reason": _text(row.get("cooldown_reason")),
                    "cooldown_until": _iso(row.get("cooldown_until")),
                    "created_at": _iso(row.get("created_at")),
                    "updated_at": _iso(row.get("updated_at")),
                    "deleted_at": _iso(row.get("deleted_at")),
                    "quota": quota,
                    "total": total,
                    "today": {
                        **today,
                        # Keep an explicit zero-usage day distinguishable from
                        # a missing today window in the billing/overview
                        # contract.
                        "date": today_start.date().isoformat(),
                    },
                    "history": [],
                    "last_request_at": None,
                    "source": "codex2api_postgresql",
                }
                # Keep naming compatible with the API-backed display adapter.
                by_id[remote_id]["remote_status"] = by_id[remote_id]["status"]

            for row in summary_rows:
                remote_id = _int_value(row.get("account_id"))
                item = by_id.get(remote_id)
                if item is None:
                    continue
                item["total"] = _metric(
                    row.get("total_requests"), row.get("total_tokens"),
                    row.get("total_account_billed"), row.get("total_user_billed"),
                )
                item["today"] = _metric(
                    row.get("today_requests"), row.get("today_tokens"),
                    row.get("today_account_billed"), row.get("today_user_billed"),
                )
                item["last_request_at"] = _iso(row.get("last_request_at"))

            for row in history_rows:
                remote_id = _int_value(row.get("account_id"))
                item = by_id.get(remote_id)
                if item is None:
                    continue
                item["history"].append({
                    "date": _iso(row.get("day")),
                    "requests": _int_value(row.get("requests")),
                    "tokens": _int_value(row.get("tokens")),
                    "billed_usd": _float_value(row.get("account_billed")),
                    "user_billed_usd": _float_value(row.get("user_billed")),
                })

            # Convenient aliases mirror the existing API's usage payload.
            for item in by_id.values():
                item["today"]["date"] = today_start.date().isoformat()
                item["total_account_billed"] = item["total"]["billed_usd"]
                item["today_account_billed"] = item["today"]["billed_usd"]
                item["total_requests"] = item["total"]["requests"]
                item["today_requests"] = item["today"]["requests"]

            result = {
                (target_id, remote_id): by_id[remote_id]
                for remote_id in remote_ids
                if remote_id in by_id
            }
            self._set_status("available", available=True, read_only=True, rows=len(result))
            return result
        except ImportError as exc:
            self._set_status("dependency_missing", available=False, error=str(exc))
            return {}
        except Exception as exc:
            self._set_status("query_error", available=False, error=str(exc))
            return {}
        finally:
            if connection is not None:
                try:
                    connection.rollback()
                except Exception:
                    pass
                try:
                    connection.close()
                except Exception:
                    pass


def fetch_codex2api_account_snapshots(
    keys: Iterable[AccountKey],
    *,
    dsn: str | None = None,
    config: Codex2APIDBConfig | None = None,
    as_of: datetime | date | None = None,
) -> dict[AccountKey, dict[str, Any]]:
    """Convenience wrapper using environment configuration."""

    adapter = Codex2APIDBAdapter(config or Codex2APIDBConfig.from_env(dsn=dsn))
    return adapter.fetch_account_snapshots(keys, as_of=as_of)


__all__ = [
    "AccountKey",
    "Codex2APIDBAdapter",
    "Codex2APIDBConfig",
    "database_dsn_for_target",
    "fetch_codex2api_account_snapshots",
    "get_codex2api_db_adapter",
    "load_codex2api_db_config",
]
