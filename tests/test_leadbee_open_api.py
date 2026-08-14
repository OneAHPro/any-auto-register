from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar

import pytest

from platforms.chatgpt.leadbee_open_api import (
    LEADBEE_API_BASE,
    LeadBeeAPIError,
    LeadBeeHTTPError,
    LeadBeeOpenAPIClient,
)

API_KEY = "ak_test_fixture_only"
API_SECRET = "secret_test_fixture_only"
TIMESTAMP = 1_785_686_400
NONCE = "request_nonce_000001"


@dataclass
class FakeResponse:
    payload: Any = field(default_factory=lambda: {"success": True, "data": {}})
    status_code: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    json_error: Exception | None = None

    def json(self) -> Any:
        if self.json_error is not None:
            raise self.json_error
        return self.payload


@dataclass
class RequestCall:
    method: str
    url: str
    headers: dict[str, str]
    body: bytes
    allow_redirects: bool | None = None
    timeout: Any = None


class FakeSession:
    def __init__(self, *results: FakeResponse | Exception):
        self.results = list(results) or [FakeResponse()]
        self.calls: list[RequestCall] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        data: bytes,
        allow_redirects: bool | None = None,
        timeout: Any = None,
    ) -> FakeResponse:
        self.calls.append(
            RequestCall(method, url, headers, data, allow_redirects, timeout)
        )
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class EchoDiagnosticSession:
    def __init__(self, *, field_name: str, value_source: str):
        self.field_name = field_name
        self.value_source = value_source
        self.calls: list[RequestCall] = []
        self.echoed_value = ""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        data: bytes,
        allow_redirects: bool | None = None,
        timeout: Any = None,
    ) -> FakeResponse:
        self.calls.append(
            RequestCall(method, url, headers, data, allow_redirects, timeout)
        )
        values = {
            "api_key": API_KEY,
            "api_secret": API_SECRET,
            "signature": headers["X-Signature"],
            "body_value": "body_fixture_private_value",
            "wrapped_body_value": "echo_body_fixture_private_value_suffix",
            "body_fragment": "fixture_private",
            "header_name": "X-Signature",
        }
        self.echoed_value = values[self.value_source]
        error = {"code": "NORMAL_ERROR", "message": "ignored"}
        payload = {
            "success": False,
            "error": error,
            "request_id": "normal_request_id",
        }
        if self.field_name == "error_code":
            error["code"] = self.echoed_value
        else:
            payload["request_id"] = self.echoed_value
        return FakeResponse(status_code=400, payload=payload)


class RedirectHandler(BaseHTTPRequestHandler):
    redirect_status = 302
    redirect_url = ""

    def _redirect(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length:
            self.rfile.read(content_length)
        self.send_response(self.redirect_status)
        self.send_header("Location", self.redirect_url)
        self.end_headers()

    def do_GET(self) -> None:
        self._redirect()

    def do_POST(self) -> None:
        self._redirect()

    def log_message(self, _format: str, *args: Any) -> None:
        return None


class RedirectTargetHandler(BaseHTTPRequestHandler):
    received_requests: ClassVar[list[tuple[str, dict[str, str], bytes]]] = []

    def _respond(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length) if content_length else b""
        self.received_requests.append((self.command, dict(self.headers), body))
        response_body = b'{"success":true,"data":{}}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response_body)))
        self.end_headers()
        self.wfile.write(response_body)

    def do_GET(self) -> None:
        self._respond()

    def do_POST(self) -> None:
        self._respond()

    def log_message(self, _format: str, *args: Any) -> None:
        return None


@contextmanager
def running_server(
    handler: type[BaseHTTPRequestHandler],
) -> Iterator[ThreadingHTTPServer]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def make_client(session: FakeSession) -> LeadBeeOpenAPIClient:
    return LeadBeeOpenAPIClient(
        api_key=API_KEY,
        api_secret=API_SECRET,
        session=session,
        clock=lambda: TIMESTAMP,
        nonce_factory=lambda: NONCE,
    )


def test_get_products_uses_default_base_and_byte_exact_signature():
    session = FakeSession(
        FakeResponse(payload={"success": True, "data": {"products": []}})
    )

    result = make_client(session).get_products()

    assert result == {"products": []}
    assert len(session.calls) == 1
    call = session.calls[0]
    assert call.method == "GET"
    assert call.url == f"{LEADBEE_API_BASE}/products"
    assert call.body == b""
    assert call.headers == {
        "Accept": "application/json",
        "X-API-Key": API_KEY,
        "X-Timestamp": str(TIMESTAMP),
        "X-Nonce": NONCE,
        "X-Signature": (
            "379f8291d634ad5e1b2acca2bb1c43983eff682c164d6d14f6f6153d35079af0"
        ),
    }
    assert "Idempotency-Key" not in call.headers
    assert call.allow_redirects is False
    assert call.timeout == 20.0


def test_create_order_signs_and_sends_the_exact_serialized_json_once():
    session = FakeSession(
        FakeResponse(status_code=201, payload={"success": True, "data": {"id": "1"}})
    )

    result = make_client(session).create_order(
        "customer_order_0001",
        "sms_verification_us",
        idempotency_key="order_20260803_customer_0001",
    )

    assert result == {"id": "1"}
    call = session.calls[0]
    assert call.method == "POST"
    assert call.url == f"{LEADBEE_API_BASE}/orders"
    assert call.body == (
        b'{"client_order_id":"customer_order_0001",'
        b'"product_id":"sms_verification_us","quantity":1}'
    )
    assert call.headers["Content-Type"] == "application/json"
    assert call.headers["Idempotency-Key"] == "order_20260803_customer_0001"
    assert call.headers["X-Signature"] == (
        "6b8f94f38eaf950bd863a3c64dbcde697695b086cb58a659476199826e06b875"
    )


def test_endpoint_methods_paths_bodies_and_idempotency_headers():
    session = FakeSession(
        *[
            FakeResponse(payload={"success": True, "data": {"call": index}})
            for index in range(5)
        ]
    )
    client = make_client(session)

    assert client.get_balance() == {"call": 0}
    assert client.get_order("order/with spaces?#") == {"call": 1}
    assert client.replace_order(
        "order/with spaces?#", idempotency_key="replace_fixture_001"
    ) == {"call": 2}
    assert client.cancel_order(
        "order/with spaces?#", idempotency_key="cancel_fixture_0001"
    ) == {"call": 3}
    assert client.list_products() == {"call": 4}

    encoded_order = "order%2Fwith%20spaces%3F%23"
    assert [(call.method, call.url) for call in session.calls] == [
        ("GET", f"{LEADBEE_API_BASE}/balance"),
        ("GET", f"{LEADBEE_API_BASE}/orders/{encoded_order}"),
        ("POST", f"{LEADBEE_API_BASE}/orders/{encoded_order}/replace"),
        ("POST", f"{LEADBEE_API_BASE}/orders/{encoded_order}/cancel"),
        ("GET", f"{LEADBEE_API_BASE}/products"),
    ]
    assert [call.body for call in session.calls] == [b"", b"", b"{}", b"{}", b""]
    assert "Idempotency-Key" not in session.calls[0].headers
    assert "Idempotency-Key" not in session.calls[1].headers
    assert session.calls[2].headers["Idempotency-Key"] == "replace_fixture_001"
    assert session.calls[3].headers["Idempotency-Key"] == "cancel_fixture_0001"
    assert "Idempotency-Key" not in session.calls[4].headers


def test_cancel_and_replace_send_json_object_body_for_provider_contract():
    """Write endpoints must send valid JSON even when they have no options."""

    class JsonObjectRequiredSession(FakeSession):
        def request(
            self, method, url, *, headers, data, allow_redirects=None, timeout=None
        ):
            self.calls.append(
                RequestCall(method, url, headers, data, allow_redirects, timeout)
            )
            if (
                method == "POST"
                and url.endswith(("/replace", "/cancel"))
                and data != b"{}"
            ):
                return FakeResponse(
                    status_code=400,
                    payload={
                        "success": False,
                        "error": {"code": "INVALID_JSON", "message": "fixture"},
                    },
                )
            return FakeResponse(payload={"success": True, "data": {"status": "ok"}})

    session = JsonObjectRequiredSession()
    client = make_client(session)

    assert client.replace_order("order-1", idempotency_key="replace_fixture_001") == {
        "status": "ok"
    }
    assert client.cancel_order("order-1", idempotency_key="cancel_fixture_0001") == {
        "status": "ok"
    }
    assert [call.body for call in session.calls] == [b"{}", b"{}"]


@pytest.mark.parametrize("status_code", [200, 201, 202, 204, 299])
def test_any_2xx_success_envelope_returns_data_with_unknown_fields(status_code: int):
    data = {"known": "value", "future_provider_field": {"nested": [1, 2, 3]}}
    session = FakeSession(
        FakeResponse(status_code=status_code, payload={"success": True, "data": data})
    )

    assert make_client(session).get_balance() == data


def test_success_false_raises_typed_error_with_safe_diagnostics():
    session = FakeSession(
        FakeResponse(
            status_code=200,
            payload={
                "success": False,
                "error": {"code": "PRODUCT_NOT_FOUND", "message": "ignored"},
                "request_id": "req_fixture_001",
            },
        )
    )

    with pytest.raises(LeadBeeAPIError) as captured:
        make_client(session).get_products()

    error = captured.value
    assert error.http_status == 200
    assert error.status_code == 200
    assert error.error_code == "PRODUCT_NOT_FOUND"
    assert error.code == "PRODUCT_NOT_FOUND"
    assert error.request_id == "req_fixture_001"
    assert error.retry_after is None


@pytest.mark.parametrize("status_code", [401, 403, 404, 409, 503])
def test_non_2xx_raises_http_error_with_parsed_diagnostics(status_code: int):
    session = FakeSession(
        FakeResponse(
            status_code=status_code,
            payload={
                "success": False,
                "error": {"code": f"HTTP_{status_code}", "message": "ignored"},
                "request_id": f"req_{status_code}",
            },
        )
    )

    with pytest.raises(LeadBeeAPIError) as captured:
        make_client(session).get_balance()

    assert captured.value.http_status == status_code
    assert captured.value.error_code == f"HTTP_{status_code}"
    assert captured.value.request_id == f"req_{status_code}"


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"success": "true", "data": {}},
        {"success": True},
        {"success": True, "data": []},
    ],
)
def test_invalid_success_envelope_raises_typed_response_error(payload: Any):
    session = FakeSession(FakeResponse(payload=payload))

    with pytest.raises(LeadBeeAPIError) as captured:
        make_client(session).get_products()

    assert captured.value.http_status == 200
    assert captured.value.error_code == "INVALID_ENVELOPE"


def test_invalid_json_raises_typed_response_error():
    session = FakeSession(FakeResponse(json_error=ValueError("fixture invalid json")))

    with pytest.raises(LeadBeeAPIError) as captured:
        make_client(session).get_products()

    assert captured.value.http_status == 200
    assert captured.value.error_code == "INVALID_JSON"


def test_429_parses_integer_retry_after_seconds_case_insensitively():
    session = FakeSession(
        FakeResponse(
            status_code=429,
            headers={"retry-after": "17"},
            payload={
                "success": False,
                "error": {"code": "RATE_LIMITED", "message": "ignored"},
                "request_id": "req_rate_fixture",
            },
        )
    )

    with pytest.raises(LeadBeeAPIError) as captured:
        make_client(session).get_products()

    assert captured.value.http_status == 429
    assert captured.value.error_code == "RATE_LIMITED"
    assert captured.value.request_id == "req_rate_fixture"
    assert captured.value.retry_after == 17.0


def test_error_str_and_repr_redact_credentials_signature_and_request_body():
    sensitive_body_value = "body_fixture_private_value"
    session = FakeSession(
        RuntimeError(
            f"{API_KEY} {API_SECRET} Authorization X-Signature {sensitive_body_value}"
        )
    )
    client = make_client(session)

    with pytest.raises(LeadBeeAPIError) as captured:
        client.create_order(
            sensitive_body_value,
            "product_fixture",
            idempotency_key="create_fixture_0001",
        )

    rendered = f"{captured.value!s}\n{captured.value!r}"
    assert API_KEY not in rendered
    assert API_SECRET not in rendered
    assert sensitive_body_value not in rendered
    assert "Authorization" not in rendered
    assert "X-Signature" not in rendered
    assert "transport" in rendered.lower()
    assert captured.value.__cause__ is None
    assert captured.value.__suppress_context__ is True
    assert len(session.calls) == 1


def test_remote_error_message_cannot_leak_secrets_or_body_values():
    body_value = "body_fixture_private_value"
    session = FakeSession(
        FakeResponse(
            status_code=400,
            payload={
                "success": False,
                "error": {
                    "code": "BAD_REQUEST",
                    "message": f"{API_KEY} {API_SECRET} {body_value} X-Signature",
                },
            },
        )
    )

    with pytest.raises(LeadBeeAPIError) as captured:
        make_client(session).create_order(
            body_value,
            "product_fixture",
            idempotency_key="create_fixture_0001",
        )

    rendered = f"{captured.value!s}\n{captured.value!r}"
    assert API_KEY not in rendered
    assert API_SECRET not in rendered
    assert body_value not in rendered
    assert "X-Signature" not in rendered


@pytest.mark.parametrize(
    ("field_name", "value_source"),
    [
        ("error_code", "api_key"),
        ("error_code", "api_secret"),
        ("request_id", "signature"),
        ("error_code", "body_value"),
        ("error_code", "wrapped_body_value"),
        ("request_id", "body_fragment"),
        ("error_code", "header_name"),
    ],
)
def test_echoed_request_values_are_removed_from_structured_diagnostics(
    field_name: str,
    value_source: str,
):
    session = EchoDiagnosticSession(
        field_name=field_name,
        value_source=value_source,
    )
    client = make_client(session)

    with pytest.raises(LeadBeeAPIError) as captured:
        client.create_order(
            "body_fixture_private_value",
            "product_fixture",
            idempotency_key="create_fixture_0001",
        )

    rendered = f"{captured.value!s}\n{captured.value!r}"
    assert session.echoed_value not in rendered


@pytest.mark.parametrize(
    ("api_key", "api_secret"),
    [
        ("", API_SECRET),
        ("   ", API_SECRET),
        (API_KEY, ""),
        (API_KEY, "   "),
    ],
)
def test_constructor_rejects_empty_credentials(api_key: str, api_secret: str):
    with pytest.raises(ValueError):
        LeadBeeOpenAPIClient(api_key=api_key, api_secret=api_secret)


@pytest.mark.parametrize(
    "idempotency_key",
    [None, "", "x" * 15, "x" * 129, "x" * 15 + "\n", 123],
)
def test_writes_reject_invalid_idempotency_keys(idempotency_key: Any):
    client = make_client(FakeSession())

    with pytest.raises(ValueError):
        client.create_order(
            "customer_order_0001",
            "product_fixture",
            idempotency_key=idempotency_key,
        )


def test_write_call_makes_one_attempt_and_keeps_caller_idempotency_key():
    session = FakeSession(RuntimeError("fixture transport failure"))
    client = make_client(session)

    with pytest.raises(LeadBeeAPIError):
        client.create_order(
            "customer_order_0001",
            "product_fixture",
            idempotency_key="stable_fixture_key",
        )

    assert len(session.calls) == 1
    assert session.calls[0].headers["Idempotency-Key"] == "stable_fixture_key"


@pytest.mark.parametrize(
    ("redirect_status", "operation"),
    [(302, "read"), (307, "write")],
)
def test_real_session_does_not_follow_cross_origin_redirects(
    redirect_status: int,
    operation: str,
):
    RedirectTargetHandler.received_requests = []
    with running_server(RedirectTargetHandler) as target_server:
        target_port = target_server.server_address[1]
        RedirectHandler.redirect_status = redirect_status
        RedirectHandler.redirect_url = f"http://127.0.0.1:{target_port}/capture"
        with running_server(RedirectHandler) as redirect_server:
            redirect_port = redirect_server.server_address[1]
            client = LeadBeeOpenAPIClient(
                api_key=API_KEY,
                api_secret=API_SECRET,
                base_url=f"http://127.0.0.1:{redirect_port}/api/open/v1",
                clock=lambda: TIMESTAMP,
                nonce_factory=lambda: NONCE,
            )

            with pytest.raises(LeadBeeHTTPError) as captured:
                if operation == "read":
                    client.get_products()
                else:
                    client.create_order(
                        "body_fixture_private_value",
                        "product_fixture",
                        idempotency_key="create_fixture_0001",
                    )

    assert captured.value.http_status == redirect_status
    assert RedirectTargetHandler.received_requests == []


@pytest.mark.parametrize("request_timeout", [3, 2.5, (1, 4.5)])
def test_request_timeout_is_forwarded_to_session(request_timeout: Any):
    session = FakeSession()
    client = LeadBeeOpenAPIClient(
        api_key=API_KEY,
        api_secret=API_SECRET,
        session=session,
        request_timeout=request_timeout,
        clock=lambda: TIMESTAMP,
        nonce_factory=lambda: NONCE,
    )

    client.get_products()

    assert session.calls[0].timeout == request_timeout
    assert session.calls[0].allow_redirects is False


def test_each_public_endpoint_forwards_its_request_timeout_override():
    session = FakeSession(
        *[
            FakeResponse(payload={"success": True, "data": {"call": index}})
            for index in range(7)
        ]
    )
    client = make_client(session)

    assert client.get_products(request_timeout=0.11) == {"call": 0}
    assert client.list_products(request_timeout=(0.12, 0.22)) == {"call": 1}
    assert client.get_balance(request_timeout=0.13) == {"call": 2}
    assert client.create_order(
        "customer_order_timeout",
        "product_timeout",
        idempotency_key="create_timeout_fixture",
        request_timeout=0.14,
    ) == {"call": 3}
    assert client.get_order("order_timeout", request_timeout=0.15) == {"call": 4}
    assert client.replace_order(
        "order_timeout",
        idempotency_key="replace_timeout_fixture",
        request_timeout=0.16,
    ) == {"call": 5}
    assert client.cancel_order(
        "order_timeout",
        idempotency_key="cancel_timeout_fixture",
        request_timeout=0.17,
    ) == {"call": 6}

    assert [call.timeout for call in session.calls] == [
        0.11,
        (0.12, 0.22),
        0.13,
        0.14,
        0.15,
        0.16,
        0.17,
    ]


def test_endpoint_timeout_override_does_not_mutate_the_client_default():
    session = FakeSession(
        *[FakeResponse(payload={"success": True, "data": {}}) for _index in range(3)]
    )
    client = LeadBeeOpenAPIClient(
        api_key=API_KEY,
        api_secret=API_SECRET,
        session=session,
        request_timeout=(4, 6),
        clock=lambda: TIMESTAMP,
        nonce_factory=lambda: NONCE,
    )

    client.get_products(request_timeout=(0.2, 0.8))
    client.get_products()
    client.get_products(request_timeout=None)

    assert [call.timeout for call in session.calls] == [
        (0.2, 0.8),
        (4, 6),
        (4, 6),
    ]


@pytest.mark.parametrize(
    "operation",
    [
        lambda client: client.get_products(request_timeout=0),
        lambda client: client.list_products(request_timeout=True),
        lambda client: client.get_balance(request_timeout=float("inf")),
        lambda client: client.create_order(
            "customer_order_timeout",
            "product_timeout",
            idempotency_key="create_timeout_fixture",
            request_timeout=(1, 0),
        ),
        lambda client: client.get_order("order_timeout", request_timeout=[]),
        lambda client: client.replace_order(
            "order_timeout",
            idempotency_key="replace_timeout_fixture",
            request_timeout=(1,),
        ),
        lambda client: client.cancel_order(
            "order_timeout",
            idempotency_key="cancel_timeout_fixture",
            request_timeout=-1,
        ),
    ],
)
def test_each_public_endpoint_rejects_invalid_request_timeout_override(operation):
    session = FakeSession()

    with pytest.raises(ValueError, match="request_timeout"):
        operation(make_client(session))

    assert session.calls == []


@pytest.mark.parametrize(
    "request_timeout",
    [
        None,
        True,
        0,
        -1,
        float("inf"),
        float("nan"),
        (),
        (1,),
        (0, 1),
        (1, 0),
        (1, float("inf")),
        (1, "2"),
        [1, 2],
    ],
)
def test_constructor_rejects_invalid_request_timeout(request_timeout: Any):
    with pytest.raises(ValueError):
        LeadBeeOpenAPIClient(
            api_key=API_KEY,
            api_secret=API_SECRET,
            request_timeout=request_timeout,
        )


def test_transport_exception_discards_sensitive_exception_context():
    sensitive_body_value = "body_fixture_private_value"
    session = FakeSession(
        RuntimeError(f"{API_KEY} {API_SECRET} {sensitive_body_value}")
    )
    client = make_client(session)

    with pytest.raises(LeadBeeAPIError) as captured:
        client.create_order(
            sensitive_body_value,
            "product_fixture",
            idempotency_key="create_fixture_0001",
        )

    rendered = f"{captured.value!s}\n{captured.value!r}"
    assert API_KEY not in rendered
    assert API_SECRET not in rendered
    assert sensitive_body_value not in rendered
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_json_decoder_exception_discards_sensitive_exception_context():
    sensitive_body_value = "body_fixture_private_value"
    session = FakeSession(
        FakeResponse(
            status_code=201,
            json_error=ValueError(
                f"{API_KEY} {API_SECRET} X-Signature {sensitive_body_value}"
            ),
        )
    )
    client = make_client(session)

    with pytest.raises(LeadBeeAPIError) as captured:
        client.create_order(
            sensitive_body_value,
            "product_fixture",
            idempotency_key="create_fixture_0001",
        )

    rendered = f"{captured.value!s}\n{captured.value!r}"
    assert API_KEY not in rendered
    assert API_SECRET not in rendered
    assert sensitive_body_value not in rendered
    assert "X-Signature" not in rendered
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
