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


def test_ready_remote_status_is_treated_as_healthy_for_auth_decisions():
    from services import chatgpt_codex2api_health as health

    assert "ready" in health.HEALTHY_STATUSES


def test_fetch_quota_accounts_reads_latest_rows_without_triggering_probe():
    from services import chatgpt_codex2api_health as health

    with mock.patch.object(
        health.cffi_requests,
        "get",
        return_value=FakeResponse(
            {
                "accounts": [
                    {
                        "id": 101,
                        "email": "one@example.com",
                        "status": "active",
                        "usage_percent_7d": 53,
                        "billed_7d": 68.26,
                        "reset_7d_at": "ignored",
                    }
                ]
            }
        ),
    ) as request, mock.patch.object(health.cffi_requests, "post") as post:
        rows = health.fetch_codex2api_quota_accounts(config=BASE_CONFIG)

    assert rows == [
        {
            "remote_id": 101,
            "email": "one@example.com",
            "remote_status": "active",
            "usage_percent_7d": 53,
            "billed_7d": 68.26,
        }
    ]
    request.assert_called_once()
    assert request.call_args.args[0].endswith(
        "/api/admin/accounts?channel=codex"
    )
    post.assert_not_called()


def test_fetch_quota_accounts_preserves_independent_5h_and_weekly_fields():
    from services import chatgpt_codex2api_health as health

    with mock.patch.object(
        health.cffi_requests,
        "get",
        return_value=FakeResponse(
            {
                "accounts": [
                    {
                        "id": 101,
                        "email": "one@example.com",
                        "status": "active",
                        "plan_type": "plus",
                        "usage_percent_5h": 20,
                        "billed_5h": 10,
                        "usage_percent_7d": 40,
                        "billed_7d": 20,
                    }
                ]
            }
        ),
    ):
        rows = health.fetch_codex2api_quota_accounts(config=BASE_CONFIG)

    assert rows[0]["plan_type"] == "plus"
    assert rows[0]["usage_percent_5h"] == 20
    assert rows[0]["billed_5h"] == 10
    assert rows[0]["usage_percent_7d"] == 40
    assert rows[0]["billed_7d"] == 20


def test_fetch_quota_accounts_exposes_display_fields_only_when_requested():
    from services import chatgpt_codex2api_health as health

    payload = {
        "accounts": [
            {
                "id": 101,
                "email": "one@example.com",
                "status": "active",
                "plan_type": "pro",
                "chatgpt_account_id": "acct-101",
                "effective_workspace_id": "workspace-101",
                "subscription_expires_at": "2026-10-01T00:00:00Z",
                "usage_percent_7d": 40,
                "billed_7d": 20,
                "usage_7d_detail": {"requests": 123, "account_billed": 999},
            }
        ]
    }
    with mock.patch.object(
        health.cffi_requests,
        "get",
        return_value=FakeResponse(payload),
    ):
        legacy_rows = health.fetch_codex2api_quota_accounts(config=BASE_CONFIG)
    with mock.patch.object(
        health.cffi_requests,
        "get",
        return_value=FakeResponse(payload),
    ):
        display_rows = health.fetch_codex2api_quota_accounts(
            config=BASE_CONFIG,
            include_display_fields=True,
        )

    assert "chatgpt_account_id" not in legacy_rows[0]
    assert display_rows[0]["chatgpt_account_id"] == "acct-101"
    assert display_rows[0]["effective_workspace_id"] == "workspace-101"
    assert display_rows[0]["subscription_expires_at"] == "2026-10-01T00:00:00Z"
    assert display_rows[0]["usage_7d_requests"] == 123


def test_fetch_quota_accounts_preserves_window_reset_times():
    from services import chatgpt_codex2api_health as health

    with mock.patch.object(
        health.cffi_requests,
        "get",
        return_value=FakeResponse(
            {
                "accounts": [
                    {
                        "id": 101,
                        "email": "one@example.com",
                        "status": "active",
                        "usage_percent_5h": 20,
                        "billed_5h": 10,
                        "reset_5h_at": "2026-09-02T15:00:00Z",
                        "usage_percent_7d": 40,
                        "billed_7d": 20,
                        "reset_7d_at": "2026-09-07T15:00:00Z",
                    }
                ]
            }
        ),
    ):
        rows = health.fetch_codex2api_quota_accounts(config=BASE_CONFIG)

    assert rows[0]["reset_5h_at"] == "2026-09-02T15:00:00Z"
    assert rows[0]["reset_7d_at"] == "2026-09-07T15:00:00Z"


def test_fetch_quota_accounts_does_not_use_rolling_detail_for_missing_summary():
    from services import chatgpt_codex2api_health as health

    with mock.patch.object(
        health.cffi_requests,
        "get",
        return_value=FakeResponse(
            {
                "accounts": [
                    {
                        "id": 101,
                        "email": "one@example.com",
                        "status": "active",
                        "plan_type": "plus",
                        "usage_percent_5h": 20,
                        "usage_percent_7d": 40,
                        "usage_5h_detail": {"account_billed": 10},
                        "usage_7d_detail": {"account_billed": 20},
                    }
                ]
            }
        ),
    ):
        rows = health.fetch_codex2api_quota_accounts(config=BASE_CONFIG)

    assert "billed_5h" not in rows[0]
    assert rows[0]["billed_7d"] is None


def test_fetch_quota_accounts_keeps_zero_summary_over_rolling_detail():
    from services import chatgpt_codex2api_health as health

    with mock.patch.object(
        health.cffi_requests,
        "get",
        return_value=FakeResponse(
            {
                "accounts": [
                    {
                        "id": 101,
                        "email": "one@example.com",
                        "status": "active",
                        "plan_type": "team",
                        "usage_percent_5h": 0,
                        "billed_5h": 0,
                        "usage_percent_7d": 0,
                        "billed_7d": 0,
                        "usage_5h_detail": {"account_billed": 0},
                        "usage_7d_detail": {"account_billed": 94.22},
                    }
                ]
            }
        ),
    ):
        rows = health.fetch_codex2api_quota_accounts(config=BASE_CONFIG)

    assert rows[0]["billed_5h"] == 0
    assert rows[0]["billed_7d"] == 0


def test_fetch_quota_accounts_keeps_positive_summary_over_rolling_detail():
    from services import chatgpt_codex2api_health as health

    with mock.patch.object(
        health.cffi_requests,
        "get",
        return_value=FakeResponse(
            {
                "accounts": [
                    {
                        "id": 101,
                        "email": "one@example.com",
                        "status": "active",
                        "plan_type": "plus",
                        "usage_percent_5h": 40,
                        "billed_5h": 10,
                        "usage_percent_7d": 5,
                        "billed_7d": 10,
                        "usage_5h_detail": {"account_billed": 10},
                        "usage_7d_detail": {"account_billed": 200},
                    }
                ]
            }
        ),
    ):
        rows = health.fetch_codex2api_quota_accounts(config=BASE_CONFIG)

    assert rows[0]["billed_5h"] == 10
    assert rows[0]["billed_7d"] == 10


def test_fetch_quota_accounts_keeps_reset_aligned_summary_over_rolling_detail():
    from services import chatgpt_codex2api_health as health

    with mock.patch.object(
        health.cffi_requests,
        "get",
        return_value=FakeResponse(
            {
                "accounts": [
                    {
                        "id": 6049,
                        "email": "pro@example.com",
                        "status": "active",
                        "plan_type": "pro",
                        "usage_percent_7d": 1,
                        "billed_7d": 12.7424096,
                        "usage_7d_detail": {
                            "account_billed": 3624.34587308,
                        },
                    }
                ]
            }
        ),
    ):
        rows = health.fetch_codex2api_quota_accounts(config=BASE_CONFIG)

    assert rows[0]["billed_7d"] == 12.7424096


def test_fetch_quota_accounts_keeps_rows_while_quota_fields_are_refreshing():
    from services import chatgpt_codex2api_health as health

    with mock.patch.object(
        health.cffi_requests,
        "get",
        return_value=FakeResponse(
            {
                "accounts": [
                    {
                        "id": 101,
                        "email": "one@example.com",
                        "status": "active",
                        "account_type": "oauth",
                        "usage_percent_7d": 53,
                    }
                ]
            }
        ),
    ):
        rows = health.fetch_codex2api_quota_accounts(config=BASE_CONFIG)

    assert len(rows) == 1


def test_fetch_quota_accounts_ignores_missing_quota_on_unauthorized_oauth_rows():
    from services import chatgpt_codex2api_health as health

    with mock.patch.object(
        health.cffi_requests,
        "get",
        return_value=FakeResponse(
            {
                "accounts": [
                    {
                        "id": 101,
                        "email": "one@example.com",
                        "status": "active",
                        "account_type": "oauth",
                        "usage_percent_7d": 53,
                        "billed_7d": 68.26,
                    },
                    {
                        "id": 102,
                        "email": "two@example.com",
                        "status": "unauthorized",
                        "account_type": "oauth",
                    },
                ]
            }
        ),
    ):
        rows = health.fetch_codex2api_quota_accounts(config=BASE_CONFIG)

    assert len(rows) == 2


def test_fetch_quota_accounts_marks_token_import_placeholders_for_summary():
    from services import chatgpt_codex2api_health as health
    from services.chatgpt_codex2api_quota import summarize_available_quota

    with mock.patch.object(
        health.cffi_requests,
        "get",
        return_value=FakeResponse(
            {
                "accounts": [
                    {
                        "id": 100,
                        "email": "",
                        "name": "at-import-1",
                        "status": "active",
                    },
                    {
                        "id": 101,
                        "email": "ready@example.com",
                        "status": "active",
                        "plan_type": "pro",
                        "usage_percent_7d": 50,
                        "billed_7d": 20,
                    },
                ]
            }
        ),
    ):
        rows = health.fetch_codex2api_quota_accounts(config=BASE_CONFIG)

    report = summarize_available_quota(rows)

    assert rows[0]["quota_placeholder"] is True
    assert report.remote_account_count == 2
    assert report.account_count == 1
    assert report.total_data_complete
    assert report.available


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
        8: "invalidated-error@example.com",
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
                    {
                        "id": 109,
                        "email": "invalidated-error@example.com",
                        "status": "error",
                        "error_message": (
                            "刷新失败 (status 401): "
                            '{"error":{"code":"refresh_token_invalidated"}}'
                        ),
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
    assert snapshot[8]["state"] == "auth_failed"
    assert snapshot[8]["remote_status"] == "error"
    assert snapshot[8]["probe_mode"] == "wham_only"
    assert {row["remote_id"] for row in quota_accounts} == set(range(101, 110))
    assert next(
        row for row in quota_accounts if row["remote_id"] == 101
    )["reset_7d_at"] == "2026-08-13T17:35:22+08:00"
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


def test_auth_failure_confirmation_trusts_explicit_refresh_token_invalidation():
    from services import chatgpt_codex2api_health as health

    initial = {
        "account_id": 8,
        "email": "invalidated@example.com",
        "state": "auth_failed",
        "remote_id": 108,
        "remote_status": "error",
        "remote_updated_at": "2026-08-21T03:32:40+08:00",
        "probe_mode": "wham_only",
        "auth_failure_source": "error_message",
        "message": "Codex2API 明确返回 Refresh Token 已失效",
    }

    with mock.patch.object(health.cffi_requests, "post") as refresh:
        result = health.confirm_codex2api_auth_failure(
            initial,
            config=BASE_CONFIG,
        )

    assert result["state"] == "auth_failed"
    assert result["resolution"] == "remote_error_confirmed_failure"
    assert "本地验证码重登" in result["message"]
    refresh.assert_not_called()


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


def test_assigned_accounts_are_probed_against_their_persisted_targets(monkeypatch):
    from sqlalchemy.pool import StaticPool
    from sqlmodel import Session, create_engine

    from core import db
    from services import chatgpt_codex2api_health as health

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    db.init_account_pool_schema(engine)
    clients = {}
    with Session(engine) as session:
        for account_id, target_id, email in (
            (1, 11, "one@example.com"),
            (2, 22, "two@example.com"),
        ):
            session.add(
                db.AccountModel(
                    id=account_id,
                    platform="chatgpt",
                    email=email,
                    password="fixture",
                    identity_id=f"identity-{account_id}",
                )
            )
            session.add(
                db.AccountAssignmentModel(
                    identity_id=f"identity-{account_id}",
                    local_account_id=account_id,
                    pool_id="PUBLIC_POOL",
                    target_id=target_id,
                    state="active",
                )
            )
        session.commit()

    class AssignedClient:
        def __init__(self, target_id, email, remote_id):
            self.target_id = target_id
            self.email = email
            self.remote_id = remote_id
            self.calls = []

        def capabilities(self):
            self.calls.append("capabilities")
            return {"settings": {"usage_probe_responses_fallback_enabled": False}}

        def trigger_usage_probe(self):
            self.calls.append("probe")
            return {"mode": "wham_only"}

        def runtime_status(self):
            self.calls.append("runtime")
            return {"probes": {"usage_probe_running": False}}

        def list_accounts(self):
            self.calls.append("list")
            return [{
                "id": self.remote_id,
                "email": self.email,
                "status": "active",
                "usage_percent_7d": 20,
                "billed_7d": 10,
            }]

        def refresh_account(self, remote_id):
            self.calls.append(("refresh", remote_id))
            return {"ok": True}

    clients[11] = AssignedClient(11, "one@example.com", 111)
    clients[22] = AssignedClient(22, "two@example.com", 222)
    monkeypatch.setattr(
        "services.codex2api_target_client.get_target_client",
        lambda target_id, database_engine: clients[int(target_id)],
    )

    quota = []
    result = health.inspect_codex2api_account_health(
        [1, 2],
        database_engine=engine,
        probe_poll_interval_seconds=0,
        quota_accounts=quota,
    )

    assert result[1]["state"] == "healthy"
    assert result[1]["target_id"] == 11
    assert result[2]["target_id"] == 22
    assert clients[11].calls.count("probe") == 1
    assert clients[22].calls.count("probe") == 1
    assert {row["target_id"] for row in quota} == {11, 22}


def test_auth_failure_confirmation_uses_the_health_target_client(monkeypatch):
    from services import chatgpt_codex2api_health as health

    class Client:
        def __init__(self):
            self.calls = []

        def refresh_account(self, remote_id):
            self.calls.append(("refresh", remote_id))

        def list_accounts(self):
            self.calls.append("list")
            return [{
                "id": 222,
                "email": "two@example.com",
                "status": "active",
                "updated_at": "2026-09-03T00:00:01+00:00",
            }]

    client = Client()
    monkeypatch.setattr(
        "services.codex2api_target_client.get_target_client",
        lambda target_id, database_engine: client,
    )
    result = health.confirm_codex2api_auth_failure(
        {
            "account_id": 2,
            "email": "two@example.com",
            "state": "auth_failed",
            "target_id": 22,
            "remote_id": 222,
            "remote_status": "unauthorized",
            "remote_updated_at": "2026-09-03T00:00:00+00:00",
        },
        poll_attempts=1,
        poll_interval_seconds=0,
    )

    assert result["state"] == "healthy"
    assert client.calls == [("refresh", 222), "list"]


def test_final_quota_reader_includes_remote_only_accounts_on_assigned_targets(monkeypatch):
    from sqlalchemy.pool import StaticPool
    from sqlmodel import Session, create_engine

    from core import db
    from services import chatgpt_codex2api_health as health

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    db.init_account_pool_schema(engine)
    with Session(engine) as session:
        for account_id, target_id, email in (
            (1, 11, "one@example.com"),
            (2, 22, "two@example.com"),
        ):
            session.add(
                db.AccountModel(
                    id=account_id,
                    platform="chatgpt",
                    email=email,
                    password="fixture",
                    identity_id=f"identity-{account_id}",
                )
            )
            session.add(
                db.AccountAssignmentModel(
                    identity_id=f"identity-{account_id}",
                    local_account_id=account_id,
                    pool_id="PUBLIC_POOL",
                    target_id=target_id,
                    state="active",
                )
            )
        session.commit()

    class Client:
        def __init__(self, email, remote_id, remote_only_email, remote_only_id):
            self.email = email
            self.remote_id = remote_id
            self.remote_only_email = remote_only_email
            self.remote_only_id = remote_only_id

        def list_accounts(self):
            return [
                {
                    "id": self.remote_id,
                    "email": self.email,
                    "status": "active",
                    "usage_percent_7d": 10,
                    "billed_7d": 2,
                },
                {
                    "id": self.remote_only_id,
                    "email": self.remote_only_email,
                    "status": "active",
                    "usage_percent_7d": 20,
                    "billed_7d": 4,
                },
            ]

    clients = {
        11: Client("one@example.com", 111, "manual-one@example.com", 112),
        22: Client("two@example.com", 222, "manual-two@example.com", 223),
    }
    monkeypatch.setattr(
        "services.codex2api_target_client.get_target_client",
        lambda target_id, database_engine: clients[int(target_id)],
    )

    rows = health.fetch_codex2api_quota_accounts(database_engine=engine)

    assert {(row["email"], row["target_id"]) for row in rows} == {
        ("one@example.com", 11),
        ("manual-one@example.com", 11),
        ("two@example.com", 22),
        ("manual-two@example.com", 22),
    }


def test_final_quota_reader_uses_enabled_targets_and_deduplicates_by_assignment(
    monkeypatch,
):
    from sqlalchemy.pool import StaticPool
    from sqlmodel import Session, create_engine

    from core import db
    from services import chatgpt_codex2api_health as health

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    db.init_account_pool_schema(engine)
    with Session(engine) as session:
        session.add_all(
            [
                db.Codex2APITargetModel(
                    id=11,
                    name="target-one",
                    base_url="https://one.example.test",
                    admin_key_ref="target-one-key",
                    enabled=True,
                ),
                db.Codex2APITargetModel(
                    id=22,
                    name="target-two",
                    base_url="https://two.example.test",
                    admin_key_ref="target-two-key",
                    enabled=True,
                ),
                db.Codex2APITargetModel(
                    id=33,
                    name="target-disabled",
                    base_url="https://disabled.example.test",
                    admin_key_ref="target-disabled-key",
                    enabled=False,
                ),
                db.AccountModel(
                    id=1,
                    platform="chatgpt",
                    email="shared@example.com",
                    password="fixture",
                    identity_id="identity-shared",
                ),
                db.AccountAssignmentModel(
                    identity_id="identity-shared",
                    local_account_id=1,
                    pool_id="PUBLIC_POOL",
                    target_id=22,
                    state="active",
                ),
            ]
        )
        session.commit()

    class Client:
        def __init__(self, rows):
            self.rows = rows

        def list_accounts(self):
            return list(self.rows)

    clients = {
        11: Client(
            [
                {
                    "id": 111,
                    "email": "shared@example.com",
                    "status": "active",
                    "usage_percent_7d": 10,
                    "billed_7d": 1,
                },
                {
                    "id": 112,
                    "email": "manual@example.com",
                    "status": "active",
                    "usage_percent_7d": 25,
                    "billed_7d": 5,
                },
                {
                    "id": 113,
                    "email": "",
                    "name": "managed-account",
                    "status": "active",
                    "usage_percent_7d": 30,
                    "billed_7d": 6,
                },
            ]
        ),
        22: Client(
            [
                {
                    "id": 221,
                    "email": "shared@example.com",
                    "status": "active",
                    "usage_percent_7d": 20,
                    "billed_7d": 4,
                },
                {
                    "id": 222,
                    "email": "",
                    "name": "managed-account",
                    "status": "active",
                    "usage_percent_7d": 35,
                    "billed_7d": 7,
                },
            ]
        ),
    }
    requested_targets = []

    def get_client(target_id, database_engine):
        requested_targets.append(int(target_id))
        return clients[int(target_id)]

    monkeypatch.setattr(
        "services.codex2api_target_client.get_target_client",
        get_client,
    )

    rows = health.fetch_codex2api_quota_accounts(database_engine=engine)

    assert requested_targets == [11, 22]
    assert {(row["email"], row["target_id"]) for row in rows} == {
        ("managed-account", 11),
        ("managed-account", 22),
        ("manual@example.com", 11),
        ("shared@example.com", 22),
    }
    shared = next(row for row in rows if row["email"] == "shared@example.com")
    assert shared["remote_id"] == 221
    assert shared["billed_7d"] == 4
    assert {
        row["remote_id"]
        for row in rows
        if row["email"] == "managed-account"
    } == {113, 222}


def test_final_quota_reader_never_falls_back_to_legacy_when_registry_is_disabled(
    monkeypatch,
):
    from sqlalchemy.pool import StaticPool
    from sqlmodel import Session, create_engine

    from core import db
    from services import chatgpt_codex2api_health as health

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    db.init_account_pool_schema(engine)
    with Session(engine) as session:
        session.add(
            db.Codex2APITargetModel(
                id=11,
                name="disabled-target",
                base_url="https://disabled.example.test",
                admin_key_ref="disabled-target-key",
                enabled=False,
            )
        )
        session.commit()

    monkeypatch.setattr(health, "_get_config", lambda: dict(BASE_CONFIG))
    legacy_request = mock.Mock(
        return_value=FakeResponse(
            {
                "accounts": [
                    {
                        "id": 999,
                        "email": "legacy@example.com",
                        "status": "active",
                        "usage_percent_7d": 50,
                        "billed_7d": 20,
                    }
                ]
            }
        )
    )
    monkeypatch.setattr(health.cffi_requests, "get", legacy_request)

    rows = health.fetch_codex2api_quota_accounts(database_engine=engine)

    assert rows == []
    legacy_request.assert_not_called()


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
