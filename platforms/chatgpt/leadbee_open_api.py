"""Small signed client for LeadBee's Open API v1."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import time
import uuid
from collections.abc import Callable
from typing import Any
from urllib.parse import quote, urlsplit

import requests

LEADBEE_API_ORIGIN = "https://api.leadbee.cn"
LEADBEE_API_PREFIX = "/api/open/v1"
LEADBEE_API_BASE = f"{LEADBEE_API_ORIGIN}{LEADBEE_API_PREFIX}"

_SAFE_DIAGNOSTIC = re.compile(r"^[A-Za-z0-9_.:/-]{1,160}$")
_IDEMPOTENCY_KEY = re.compile(r"^[!-~]{16,128}$")
_SENSITIVE_DIAGNOSTIC_LABELS = (
    "authorization",
    "x-api-key",
    "x-signature",
    "api_secret",
    "api-secret",
)


class LeadBeeAPIError(RuntimeError):
    """A sanitized LeadBee failure with structured retry diagnostics."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "",
        request_id: str = "",
        retry_after: float | None = None,
        status_code: int = 0,
        error_code: str | None = None,
        http_status: int | None = None,
    ) -> None:
        self.message = message
        self.error_code = _safe_diagnostic(
            error_code if error_code is not None else code
        )
        self.request_id = _safe_diagnostic(request_id)
        self.retry_after = retry_after
        self.http_status = int(status_code if http_status is None else http_status)
        super().__init__(self.__str__())

    @property
    def code(self) -> str:
        return self.error_code

    @property
    def status_code(self) -> int:
        return self.http_status

    def __str__(self) -> str:
        diagnostics = [f"http_status={self.http_status}"]
        if self.error_code:
            diagnostics.append(f"error_code={self.error_code}")
        if self.request_id:
            diagnostics.append(f"request_id={self.request_id}")
        if self.retry_after is not None:
            diagnostics.append(f"retry_after={self.retry_after:g}")
        return f"{self.message} ({', '.join(diagnostics)})"

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self!s})"


class LeadBeeTransportError(LeadBeeAPIError):
    """The request did not produce an HTTP response."""


class LeadBeeHTTPError(LeadBeeAPIError):
    """LeadBee returned a non-successful HTTP status."""


class LeadBeeResponseError(LeadBeeAPIError):
    """LeadBee returned malformed JSON or an invalid response envelope."""


class LeadBeeOpenAPIClient:
    """Perform byte-exact signed requests without implicit retries."""

    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        base_url: str = LEADBEE_API_BASE,
        session: Any | None = None,
        request_timeout: float | tuple[float, float] = 20.0,
        clock: Callable[[], float] | None = None,
        nonce_factory: Callable[[], str] | None = None,
    ) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("api_key must be a non-empty string")
        if not isinstance(api_secret, str) or not api_secret.strip():
            raise ValueError("api_secret must be a non-empty string")
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("base_url must be a non-empty URL")

        normalized_base = base_url.rstrip("/")
        parsed_base = urlsplit(normalized_base)
        if (
            parsed_base.scheme not in {"http", "https"}
            or not parsed_base.netloc
            or parsed_base.query
            or parsed_base.fragment
        ):
            raise ValueError(
                "base_url must be an absolute URL without query or fragment"
            )

        self._api_key = api_key
        self._api_secret_text = api_secret
        self._api_secret = api_secret.encode("utf-8")
        self._base_url = normalized_base
        self._session = session if session is not None else requests.Session()
        self._request_timeout = _validate_request_timeout(request_timeout)
        self._clock = clock if clock is not None else time.time
        self._nonce_factory = (
            nonce_factory if nonce_factory is not None else lambda: uuid.uuid4().hex
        )

    def get_products(self) -> dict[str, Any]:
        return self._request("GET", "/products")

    def list_products(self) -> dict[str, Any]:
        """Compatibility alias matching the integration design."""
        return self.get_products()

    def get_balance(self) -> dict[str, Any]:
        return self._request("GET", "/balance")

    def create_order(
        self,
        client_order_id: str,
        product_id: str,
        quantity: int = 1,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        _require_non_empty("client_order_id", client_order_id)
        _require_non_empty("product_id", product_id)
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
            raise ValueError("quantity must be a positive integer")
        body = _json_bytes(
            {
                "client_order_id": client_order_id,
                "product_id": product_id,
                "quantity": quantity,
            }
        )
        return self._request(
            "POST",
            "/orders",
            body=body,
            idempotency_key=_validate_idempotency_key(idempotency_key),
        )

    def get_order(self, order_id: str) -> dict[str, Any]:
        return self._request("GET", f"/orders/{_path_segment(order_id)}")

    def replace_order(self, order_id: str, *, idempotency_key: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/orders/{_path_segment(order_id)}/replace",
            body=b"",
            idempotency_key=_validate_idempotency_key(idempotency_key),
        )

    def cancel_order(self, order_id: str, *, idempotency_key: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/orders/{_path_segment(order_id)}/cancel",
            body=b"",
            idempotency_key=_validate_idempotency_key(idempotency_key),
        )

    def _request(
        self,
        method: str,
        relative_path: str,
        *,
        body: bytes = b"",
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        url = f"{self._base_url}{relative_path}"
        request_path = urlsplit(url).path
        timestamp = str(int(self._clock()))
        nonce = self._nonce_factory()
        if not isinstance(nonce, str) or not nonce:
            raise ValueError("nonce_factory must return a non-empty string")

        canonical = "\n".join(
            (
                method,
                request_path,
                "",
                hashlib.sha256(body).hexdigest(),
                timestamp,
                nonce,
                idempotency_key,
            )
        ).encode("utf-8")
        signature = hmac.new(self._api_secret, canonical, hashlib.sha256).hexdigest()
        sensitive_values = _request_sensitive_values(
            api_key=self._api_key,
            api_secret=self._api_secret_text,
            signature=signature,
            body=body,
        )
        headers = {
            "Accept": "application/json",
            "X-API-Key": self._api_key,
            "X-Timestamp": timestamp,
            "X-Nonce": nonce,
            "X-Signature": signature,
        }
        if method != "GET":
            headers["Content-Type"] = "application/json"
            headers["Idempotency-Key"] = idempotency_key

        transport_error: LeadBeeTransportError | None = None
        try:
            response = self._session.request(
                method,
                url,
                headers=headers,
                data=body,
                allow_redirects=False,
                timeout=self._request_timeout,
            )
        except Exception:  # noqa: BLE001 - injected sessions may use other errors
            transport_error = LeadBeeTransportError(
                "LeadBee transport failure",
                code="TRANSPORT_ERROR",
            )
        if transport_error is not None:
            raise transport_error from None

        status_code = int(response.status_code)
        retry_after = _retry_after_seconds(getattr(response, "headers", {}))
        if not 200 <= status_code < 300:
            error_code, request_id = _response_diagnostics(
                response,
                sensitive_values=sensitive_values,
            )
            raise LeadBeeHTTPError(
                "LeadBee HTTP request failed",
                code=error_code or "HTTP_ERROR",
                request_id=request_id,
                retry_after=retry_after,
                status_code=status_code,
            )

        response_error: LeadBeeResponseError | None = None
        envelope: Any = None
        try:
            envelope = response.json()
        except Exception:  # noqa: BLE001 - response JSON decoders vary by session
            response_error = LeadBeeResponseError(
                "LeadBee response was not valid JSON",
                code="INVALID_JSON",
                status_code=status_code,
            )
        if response_error is not None:
            raise response_error from None

        if not isinstance(envelope, dict) or envelope.get("success") is not True:
            if isinstance(envelope, dict) and envelope.get("success") is False:
                error_code, request_id = _payload_diagnostics(
                    envelope,
                    sensitive_values=sensitive_values,
                )
                raise LeadBeeAPIError(
                    "LeadBee API rejected the request",
                    code=error_code or "API_ERROR",
                    request_id=request_id,
                    retry_after=retry_after,
                    status_code=status_code,
                )
            raise LeadBeeResponseError(
                "LeadBee response envelope was invalid",
                code="INVALID_ENVELOPE",
                status_code=status_code,
            )

        data = envelope.get("data")
        if not isinstance(data, dict):
            raise LeadBeeResponseError(
                "LeadBee response envelope was invalid",
                code="INVALID_ENVELOPE",
                status_code=status_code,
            )
        return data


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _require_non_empty(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _validate_idempotency_key(value: Any) -> str:
    if not isinstance(value, str) or not _IDEMPOTENCY_KEY.fullmatch(value):
        raise ValueError(
            "idempotency_key must contain 16 to 128 visible ASCII characters"
        )
    return value


def _validate_request_timeout(
    value: Any,
) -> float | tuple[float, float]:
    if _is_positive_finite_number(value):
        return value
    if (
        isinstance(value, tuple)
        and len(value) == 2
        and all(_is_positive_finite_number(part) for part in value)
    ):
        return value
    raise ValueError(
        "request_timeout must be a positive finite number or a two-number tuple"
    )


def _is_positive_finite_number(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and value > 0
    )


def _path_segment(value: Any) -> str:
    return quote(_require_non_empty("path segment", value), safe="")


def _safe_diagnostic(
    value: Any,
    *,
    sensitive_values: tuple[str, ...] = (),
) -> str:
    if not isinstance(value, str) or not _SAFE_DIAGNOSTIC.fullmatch(value):
        return ""
    folded_value = value.casefold()
    if any(label in folded_value for label in _SENSITIVE_DIAGNOSTIC_LABELS):
        return ""
    for sensitive_value in sensitive_values:
        folded_sensitive = sensitive_value.casefold()
        if folded_value in folded_sensitive or folded_sensitive in folded_value:
            return ""
    return value


def _request_sensitive_values(
    *,
    api_key: str,
    api_secret: str,
    signature: str,
    body: bytes,
) -> tuple[str, ...]:
    values = {api_key, api_secret, signature}
    if body:
        try:
            decoded_body = body.decode("utf-8")
        except UnicodeDecodeError:
            decoded_body = ""
        if decoded_body:
            values.add(decoded_body)
            try:
                payload = json.loads(decoded_body)
            except (TypeError, ValueError):
                payload = None
            values.update(_nested_string_values(payload))
    return tuple(value for value in values if value)


def _nested_string_values(value: Any) -> set[str]:
    if isinstance(value, str):
        return {value} if value else set()
    if isinstance(value, dict):
        nested_values: set[str] = set()
        for nested_value in value.values():
            nested_values.update(_nested_string_values(nested_value))
        return nested_values
    if isinstance(value, list):
        nested_values = set()
        for nested_value in value:
            nested_values.update(_nested_string_values(nested_value))
        return nested_values
    return set()


def _payload_diagnostics(
    payload: dict[str, Any],
    *,
    sensitive_values: tuple[str, ...] = (),
) -> tuple[str, str]:
    error = payload.get("error")
    error_data = error if isinstance(error, dict) else {}
    error_code = _safe_diagnostic(
        error_data.get("code") or payload.get("code"),
        sensitive_values=sensitive_values,
    )
    request_id = _safe_diagnostic(
        payload.get("request_id") or error_data.get("request_id"),
        sensitive_values=sensitive_values,
    )
    return error_code, request_id


def _response_diagnostics(
    response: Any,
    *,
    sensitive_values: tuple[str, ...] = (),
) -> tuple[str, str]:
    try:
        payload = response.json()
    except Exception:  # noqa: BLE001 - error parsing must not mask the HTTP error
        return "", ""
    if not isinstance(payload, dict):
        return "", ""
    return _payload_diagnostics(payload, sensitive_values=sensitive_values)


def _retry_after_seconds(headers: Any) -> float | None:
    try:
        raw_value = headers.get("Retry-After")
        if raw_value is None:
            raw_value = headers.get("retry-after")
    except Exception:  # noqa: BLE001 - tolerate non-mapping response header objects
        return None
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None
