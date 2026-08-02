import logging
from unittest import mock

import pytest

from platforms.chatgpt.token_refresh import TokenRefreshManager


class _Response:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


def _refresh(response=None, error=None):
    session = mock.Mock()
    if error is not None:
        session.post.side_effect = error
    else:
        session.post.return_value = response
    manager = TokenRefreshManager()
    manager._create_session = mock.Mock(return_value=session)
    return manager.refresh_by_oauth_token("old-refresh-token")


def test_oauth_refresh_success_returns_valid_and_rotated_tokens():
    result = _refresh(
        _Response(
            200,
            {
                "access_token": "new-access-token",
                "refresh_token": "new-refresh-token",
                "expires_in": 3600,
            },
        )
    )

    assert result.success is True
    assert result.state == "valid"
    assert result.http_status == 200
    assert result.error_code == ""
    assert result.access_token == "new-access-token"
    assert result.refresh_token == "new-refresh-token"
    assert result.expires_at is not None


@pytest.mark.parametrize(
    "payload, expected_code",
    [
        (
            {
                "error": "invalid_grant",
                "error_description": "refresh token has expired",
            },
            "invalid_grant",
        ),
        (
            {
                "error": {
                    "code": "token_invalidated",
                    "message": "refresh token was revoked",
                }
            },
            "token_invalidated",
        ),
        (
            {
                "error": "refresh_token_invalidated",
                "error_description": "refresh token is no longer valid",
            },
            "refresh_token_invalidated",
        ),
    ],
)
def test_explicit_refresh_token_rejection_returns_invalid(payload, expected_code):
    status_code = 401 if expected_code == "refresh_token_invalidated" else 400
    result = _refresh(_Response(status_code, payload))

    assert result.success is False
    assert result.state == "invalid"
    assert result.http_status == status_code
    assert result.error_code == expected_code


@pytest.mark.parametrize(
    "status_code, payload",
    [
        (429, {"error": "rate_limit_exceeded"}),
        (500, {"error": "server_error"}),
        (503, {"error": "temporarily_unavailable"}),
        (400, {"error": "unsupported_response"}),
    ],
)
def test_unconfirmed_refresh_failures_are_transient(status_code, payload):
    result = _refresh(_Response(status_code, payload))

    assert result.success is False
    assert result.state == "transient_error"
    assert result.http_status == status_code


def test_network_exception_is_transient():
    result = _refresh(error=TimeoutError("upstream timed out"))

    assert result.success is False
    assert result.state == "transient_error"
    assert result.http_status == 0
    assert result.error_code == "network_error"


def test_success_response_without_access_token_is_transient():
    result = _refresh(_Response(200, {"refresh_token": "rotated-only"}))

    assert result.success is False
    assert result.state == "transient_error"
    assert result.http_status == 200
    assert result.error_code == "missing_access_token"


def test_refresh_error_log_does_not_include_upstream_response_body(caplog):
    caplog.set_level(logging.WARNING)
    result = _refresh(
        _Response(
            400,
            {
                "error": "invalid_grant",
                "error_description": "secret-upstream-detail",
            },
        )
    )

    assert result.state == "invalid"
    assert "secret-upstream-detail" not in caplog.text
