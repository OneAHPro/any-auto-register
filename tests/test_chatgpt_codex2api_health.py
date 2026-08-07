from __future__ import annotations

from dataclasses import dataclass
from unittest import mock

import pytest


@dataclass
class FakeResponse:
    payload: object
    status_code: int = 200
    text: str = ""

    def json(self):
        return self.payload


BASE_CONFIG = {
    "codex2api_api_url": "https://codex2api.example.test",
    "codex2api_admin_key": "admin-test-secret",
}


def test_health_snapshot_matches_accounts_and_only_marks_auth_failures():
    from services import chatgpt_codex2api_health as health

    local_accounts = {
        1: "healthy@example.com",
        2: "limited@example.com",
        3: "invalid@example.com",
        4: "error@example.com",
        5: "missing@example.com",
        6: "duplicate@example.com",
        7: "unknown-status@example.com",
    }
    responses = [
        FakeResponse({"usage_probe_responses_fallback_enabled": False}),
        FakeResponse({"probes": {"usage_probe_running": False}}),
        FakeResponse(
            {
                "accounts": [
                    {
                        "id": 101,
                        "email": "healthy@example.com",
                        "status": "active",
                        "usage_percent_7d": 53,
                        "billed_7d": 68.26,
                        "reset_7d_at": "2026-08-13T17:35:22+08:00",
                    },
                    {
                        "id": 102,
                        "name": "limited@example.com",
                        "status": "rate_limited",
                        "usage_percent_7d": 100,
                        "billed_7d": 120.0,
                    },
                    {
                        "id": 103,
                        "email": "invalid@example.com",
                        "status": "unauthorized",
                        "usage_percent_7d": 68,
                        "billed_7d": 81.42,
                    },
                    {
                        "id": 104,
                        "email": "error@example.com",
                        "status": "error",
                    },
                    {
                        "id": 105,
                        "email": "duplicate@example.com",
                        "status": "active",
                    },
                    {
                        "id": 106,
                        "name": "duplicate@example.com",
                        "status": "active",
                    },
                    {
                        "id": 107,
                        "email": "unknown-status@example.com",
                        "status": "future_status",
                    },
                    {
                        "id": 108,
                        "email": "remote-only@example.com",
                        "status": "active",
                        "usage_percent_7d": 25,
                        "billed_7d": 10.0,
                    },
                ]
            }
        ),
    ]
    quota_accounts = []

    with mock.patch.object(
        health.cffi_requests,
        "get",
        side_effect=responses,
    ) as request, mock.patch.object(
        health.cffi_requests,
        "post",
        return_value=FakeResponse(
            {"triggered": True, "mode": "wham_only", "concurrency": 16}
        ),
    ) as trigger:
        snapshot = health.inspect_codex2api_account_health(
            local_accounts.keys(),
            config=BASE_CONFIG,
            local_accounts=local_accounts,
            probe_poll_interval_seconds=0,
            quota_accounts=quota_accounts,
        )

    assert snapshot[1]["state"] == "healthy"
    assert snapshot[1]["usage_percent_7d"] == 53
    assert snapshot[1]["billed_7d"] == 68.26
    assert snapshot[2]["state"] == "healthy"
    assert snapshot[3] == {
        "account_id": 3,
        "email": "invalid@example.com",
        "state": "auth_failed",
        "remote_id": 103,
        "remote_status": "unauthorized",
        "probe_mode": "wham_only",
        "usage_percent_7d": 68,
        "billed_7d": 81.42,
        "message": "Codex2API 本轮 wham 探针明确标记账号鉴权失效",
    }
    assert snapshot[4]["state"] == "deferred"
    assert snapshot[5]["state"] == "remote_missing"
    assert snapshot[5]["message"] == (
        "Codex2API 未找到同邮箱账号，将执行一次完整登录确认"
    )
    assert snapshot[6]["state"] == "ambiguous"
    assert snapshot[7]["state"] == "deferred"
    assert {row["remote_id"] for row in quota_accounts} == set(range(101, 109))
    assert all("reset_7d_at" not in row for row in quota_accounts)
    assert next(
        row for row in quota_accounts if row["remote_id"] == 108
    ) == {
        "remote_id": 108,
        "email": "remote-only@example.com",
        "remote_status": "active",
        "usage_percent_7d": 25,
        "billed_7d": 10.0,
    }
    assert request.call_count == 3
    assert request.call_args_list[0].args[0].endswith("/api/admin/settings")
    assert request.call_args_list[1].args[0].endswith(
        "/api/admin/runtime-status"
    )
    assert request.call_args_list[2].args[0].endswith(
        "/api/admin/accounts?channel=codex"
    )
    trigger.assert_called_once()
    assert trigger.call_args.args[0].endswith(
        "/api/admin/accounts/usage/probe"
    )


def test_health_snapshot_requires_wham_only_remote_probe_mode():
    from services import chatgpt_codex2api_health as health

    with mock.patch.object(
        health.cffi_requests,
        "get",
        return_value=FakeResponse(
            {"usage_probe_responses_fallback_enabled": True}
        ),
    ) as request:
        with pytest.raises(
            health.Codex2APIHealthError,
            match="Responses 探针回退",
        ):
            health.inspect_codex2api_account_health(
                [1],
                config=BASE_CONFIG,
                local_accounts={1: "one@example.com"},
            )

    request.assert_called_once()


def test_health_snapshot_waits_for_remote_probe_to_finish():
    from services import chatgpt_codex2api_health as health

    sleep = mock.Mock()
    with mock.patch.object(
        health.cffi_requests,
        "get",
        side_effect=[
            FakeResponse(
                {"usage_probe_responses_fallback_enabled": False}
            ),
            FakeResponse({"probes": {"usage_probe_running": True}}),
            FakeResponse({"probes": {"usage_probe_running": False}}),
            FakeResponse(
                {
                    "accounts": [
                        {
                            "id": 101,
                            "email": "one@example.com",
                            "status": "active",
                        }
                    ]
                }
            ),
        ],
    ) as request, mock.patch.object(
        health.cffi_requests,
        "post",
        return_value=FakeResponse(
            {"triggered": True, "mode": "wham_only", "concurrency": 16}
        ),
    ):
        snapshot = health.inspect_codex2api_account_health(
            [1],
            config=BASE_CONFIG,
            local_accounts={1: "one@example.com"},
            probe_poll_attempts=3,
            probe_poll_interval_seconds=1,
            sleep_fn=sleep,
        )

    assert snapshot[1]["state"] == "healthy"
    assert request.call_count == 4
    sleep.assert_called_once_with(1)


def test_health_snapshot_stops_when_fresh_probe_does_not_finish():
    from services import chatgpt_codex2api_health as health

    with mock.patch.object(
        health.cffi_requests,
        "get",
        side_effect=[
            FakeResponse(
                {"usage_probe_responses_fallback_enabled": False}
            ),
            FakeResponse({"probes": {"usage_probe_running": True}}),
            FakeResponse({"probes": {"usage_probe_running": True}}),
        ],
    ), mock.patch.object(
        health.cffi_requests,
        "post",
        return_value=FakeResponse(
            {"triggered": True, "mode": "wham_only", "concurrency": 16}
        ),
    ):
        with pytest.raises(
            health.Codex2APIHealthError,
            match="探针执行超时",
        ):
            health.inspect_codex2api_account_health(
                [1],
                config=BASE_CONFIG,
                local_accounts={1: "one@example.com"},
                probe_poll_attempts=2,
                probe_poll_interval_seconds=0,
            )


def test_health_snapshot_never_exposes_admin_key_in_transport_errors():
    from services import chatgpt_codex2api_health as health

    with mock.patch.object(
        health.cffi_requests,
        "get",
        side_effect=RuntimeError("failed with admin-test-secret"),
    ):
        with pytest.raises(health.Codex2APIHealthError) as error:
            health.inspect_codex2api_account_health(
                [1],
                config=BASE_CONFIG,
                local_accounts={1: "one@example.com"},
            )

    assert "admin-test-secret" not in str(error.value)


def test_auth_failure_confirmation_skips_local_login_when_remote_refresh_recovers():
    from services import chatgpt_codex2api_health as health

    initial = {
        "account_id": 3,
        "email": "recover@example.com",
        "state": "auth_failed",
        "remote_id": 103,
        "remote_status": "unauthorized",
        "remote_updated_at": "2026-08-03T08:00:00+08:00",
        "message": "Codex2API 已明确标记账号鉴权失效",
    }
    with mock.patch.object(
        health.cffi_requests,
        "post",
        return_value=FakeResponse({"message": "refresh requested"}),
    ) as refresh, mock.patch.object(
        health.cffi_requests,
        "get",
        return_value=FakeResponse(
            {
                "accounts": [
                    {
                        "id": 103,
                        "email": "recover@example.com",
                        "status": "active",
                        "updated_at": "2026-08-03T08:00:01+08:00",
                    }
                ]
            }
        ),
    ) as read_accounts:
        result = health.confirm_codex2api_auth_failure(
            initial,
            config=BASE_CONFIG,
            poll_attempts=1,
            poll_interval_seconds=0,
        )

    assert result["state"] == "healthy"
    assert result["resolution"] == "remote_refresh_recovered"
    assert "无需本地重登" in result["message"]
    assert refresh.call_args.args[0].endswith("/api/admin/accounts/103/refresh")
    assert read_accounts.call_args.args[0].endswith(
        "/api/admin/accounts?channel=codex"
    )


def test_auth_failure_confirmation_requires_a_fresh_remote_result():
    from services import chatgpt_codex2api_health as health

    initial = {
        "account_id": 4,
        "email": "pending@example.com",
        "state": "auth_failed",
        "remote_id": 104,
        "remote_status": "unauthorized",
        "remote_updated_at": "2026-08-03T08:00:00+08:00",
        "message": "Codex2API 已明确标记账号鉴权失效",
    }
    unchanged = FakeResponse(
        {
            "accounts": [
                {
                    "id": 104,
                    "email": "pending@example.com",
                    "status": "unauthorized",
                    "updated_at": "2026-08-03T08:00:00+08:00",
                }
            ]
        }
    )
    sleep = mock.Mock()
    with mock.patch.object(
        health.cffi_requests,
        "post",
        return_value=FakeResponse({"message": "refresh requested"}),
    ), mock.patch.object(
        health.cffi_requests,
        "get",
        side_effect=[unchanged, unchanged],
    ):
        result = health.confirm_codex2api_auth_failure(
            initial,
            config=BASE_CONFIG,
            poll_attempts=2,
            poll_interval_seconds=1,
            sleep_fn=sleep,
        )

    assert result["state"] == "deferred"
    assert result["resolution"] == "remote_refresh_pending"
    sleep.assert_called_once_with(1)


def test_auth_failure_confirmation_accepts_updated_persistent_unauthorized():
    from services import chatgpt_codex2api_health as health

    initial = {
        "account_id": 5,
        "email": "invalid@example.com",
        "state": "auth_failed",
        "remote_id": 105,
        "remote_status": "unauthorized",
        "remote_updated_at": "2026-08-03T08:00:00+08:00",
        "message": "Codex2API 已明确标记账号鉴权失效",
    }
    with mock.patch.object(
        health.cffi_requests,
        "post",
        return_value=FakeResponse({"message": "refresh requested"}),
    ), mock.patch.object(
        health.cffi_requests,
        "get",
        return_value=FakeResponse(
            {
                "accounts": [
                    {
                        "id": 105,
                        "email": "invalid@example.com",
                        "status": "unauthorized",
                        "updated_at": "2026-08-03T08:00:02+08:00",
                    }
                ]
            }
        ),
    ):
        result = health.confirm_codex2api_auth_failure(
            initial,
            config=BASE_CONFIG,
            poll_attempts=1,
            poll_interval_seconds=0,
        )

    assert result["state"] == "auth_failed"
    assert result["resolution"] == "remote_refresh_confirmed_failure"
    assert "本地验证码重登" in result["message"]


def test_auth_failure_confirmation_accepts_refresh_500_after_fresh_wham_401():
    from services import chatgpt_codex2api_health as health

    initial = {
        "account_id": 6,
        "email": "invalid@example.com",
        "state": "auth_failed",
        "remote_id": 106,
        "remote_status": "unauthorized",
        "probe_mode": "wham_only",
        "message": "Codex2API 本轮 wham 探针明确标记账号鉴权失效",
    }
    with mock.patch.object(
        health.cffi_requests,
        "post",
        return_value=FakeResponse(
            {"error": "account refresh failed"},
            status_code=500,
        ),
    ):
        result = health.confirm_codex2api_auth_failure(
            initial,
            config=BASE_CONFIG,
            poll_attempts=1,
            poll_interval_seconds=0,
        )

    assert result["state"] == "auth_failed"
    assert result["resolution"] == "remote_refresh_confirmed_failure"
    assert "本地验证码重登" in result["message"]


def test_auth_failure_confirmation_defers_refresh_500_without_fresh_probe():
    from services import chatgpt_codex2api_health as health

    initial = {
        "account_id": 7,
        "email": "stale@example.com",
        "state": "auth_failed",
        "remote_id": 107,
        "remote_status": "unauthorized",
        "message": "Codex2API 旧状态为鉴权失效",
    }
    with mock.patch.object(
        health.cffi_requests,
        "post",
        return_value=FakeResponse(
            {"error": "temporary server error"},
            status_code=500,
        ),
    ):
        result = health.confirm_codex2api_auth_failure(
            initial,
            config=BASE_CONFIG,
            poll_attempts=1,
            poll_interval_seconds=0,
        )

    assert result["state"] == "deferred"
    assert result["resolution"] == "remote_refresh_unavailable"


def test_auth_failure_confirmation_defers_unknown_remote_status():
    from services import chatgpt_codex2api_health as health

    initial = {
        "account_id": 6,
        "email": "unknown@example.com",
        "state": "auth_failed",
        "remote_id": 106,
        "remote_status": "unauthorized",
        "remote_updated_at": "2026-08-03T08:00:00+08:00",
        "message": "Codex2API 已明确标记账号鉴权失效",
    }
    with mock.patch.object(
        health.cffi_requests,
        "post",
        return_value=FakeResponse({"message": "refresh requested"}),
    ), mock.patch.object(
        health.cffi_requests,
        "get",
        return_value=FakeResponse(
            {
                "accounts": [
                    {
                        "id": 106,
                        "email": "unknown@example.com",
                        "status": "future_status",
                        "updated_at": "2026-08-03T08:00:02+08:00",
                    }
                ]
            }
        ),
    ):
        result = health.confirm_codex2api_auth_failure(
            initial,
            config=BASE_CONFIG,
            poll_attempts=1,
            poll_interval_seconds=0,
        )

    assert result["state"] == "deferred"
    assert result["resolution"] == "remote_refresh_pending"
