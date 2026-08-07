from __future__ import annotations

from decimal import Decimal
import json
import logging

import pytest

from services.chatgpt_codex2api_quota import AvailableQuotaReport
from services import chatgpt_bark_alerts as alerts


BARK_DEVICE_SECRET = "BARK_DEVICE_SECRET"
BARK_ENDPOINT = f"https://api.day.app/{BARK_DEVICE_SECRET}"
BASE_CONFIG = {
    "bark_enabled": "1",
    "bark_endpoint": BARK_ENDPOINT,
    "chatgpt_auto_relogin_alert_threshold": "5",
    "chatgpt_auto_relogin_quota_alert_threshold_usd": "120.00",
}
REPORT = AvailableQuotaReport(
    account_count=2,
    remote_account_count=7,
    estimated_remaining_usd=Decimal("98.85"),
    accounts=(),
)


class FakeResponse:
    def __init__(self, body: bytes, status: int = 200):
        self._body = body
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self, limit: int = -1) -> bytes:
        if limit < 0:
            return self._body
        return self._body[:limit]


def _success_urlopen(calls: list[tuple[object, float]]):
    def urlopen(request, timeout):
        calls.append((request, timeout))
        return FakeResponse(b'{"code":200,"message":"success","timestamp":1}')

    return urlopen


def test_relogin_alert_posts_critical_call_payload(monkeypatch):
    calls: list[tuple[object, float]] = []
    monkeypatch.setattr(alerts, "_open_bark_request", _success_urlopen(calls))

    result = alerts.send_bark_relogin_alert(
        task_id="task-relogin-1",
        quota_report=REPORT,
        quota_eligible_failure_count=5,
        quota_exhausted_failure_count=2,
        relogin_failed_count=7,
        deleted_account_count=3,
        config=BASE_CONFIG,
    )

    assert result == {"sent": True, "reason": "sent", "threshold": 5}
    assert len(calls) == 1
    outbound, timeout = calls[0]
    assert outbound.full_url == BARK_ENDPOINT
    assert outbound.get_method() == "POST"
    assert outbound.get_header("Content-type") == "application/json"
    assert timeout == 20
    payload = json.loads(outbound.data.decode("utf-8"))
    assert payload == {
        "title": "$98.85｜正常可用账号 2 个｜Codex 重登失败账号告警",
        "body": (
            "仍有额度的重登失败：5\n"
            "其中封禁或删除：3\n"
            "额度已用完的重登失败：2\n"
            "正常可用账号：2\n"
            "当前估算剩余额度：$98.85\n"
            "任务 ID：task-relogin-1"
        ),
        "group": "Any Auto Register · Codex",
        "level": "critical",
        "call": "1",
        "sound": "alarm",
    }


def test_relogin_alert_skips_when_below_threshold(monkeypatch):
    monkeypatch.setattr(alerts, "_open_bark_request", pytest.fail)

    result = alerts.send_bark_relogin_alert(
        task_id="task-relogin-2",
        quota_report=REPORT,
        quota_eligible_failure_count=4,
        quota_exhausted_failure_count=0,
        relogin_failed_count=4,
        deleted_account_count=0,
        config=BASE_CONFIG,
    )

    assert result == {"sent": False, "reason": "below_threshold", "threshold": 5}


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"bark_enabled": "0"}, "bark_disabled"),
        ({"bark_endpoint": ""}, "bark_not_configured"),
        ({"bark_endpoint": "file:///tmp/device"}, "invalid_bark_endpoint"),
        ({"bark_endpoint": "http://127.0.0.1/device"}, "invalid_bark_endpoint"),
        ({"bark_endpoint": "https://localhost/device"}, "invalid_bark_endpoint"),
        ({"bark_endpoint": "https://10.0.0.1/device"}, "invalid_bark_endpoint"),
        (
            {"bark_endpoint": "https://169.254.169.254/latest/meta-data"},
            "invalid_bark_endpoint",
        ),
        ({"bark_endpoint": "https://[::1]/device"}, "invalid_bark_endpoint"),
        ({"bark_endpoint": "https://evil.example/device"}, "invalid_bark_endpoint"),
        ({"bark_endpoint": "https://[::1"}, "invalid_bark_endpoint"),
    ],
)
def test_relogin_alert_reports_disabled_missing_and_invalid_config(
    monkeypatch,
    overrides,
    reason,
):
    monkeypatch.setattr(alerts, "_open_bark_request", pytest.fail)
    config = {**BASE_CONFIG, **overrides}

    result = alerts.send_bark_relogin_alert(
        task_id="task-relogin-config",
        quota_report=REPORT,
        quota_eligible_failure_count=5,
        quota_exhausted_failure_count=0,
        relogin_failed_count=5,
        deleted_account_count=0,
        config=config,
    )

    assert result == {"sent": False, "reason": reason, "threshold": 5}


def test_quota_alert_posts_every_time_remaining_is_below_threshold(monkeypatch):
    calls: list[tuple[object, float]] = []
    monkeypatch.setattr(alerts, "_open_bark_request", _success_urlopen(calls))

    first = alerts.send_bark_quota_threshold_alert(
        task_id="task-quota-1",
        quota_report=REPORT,
        config=BASE_CONFIG,
    )
    second = alerts.send_bark_quota_threshold_alert(
        task_id="task-quota-2",
        quota_report=REPORT,
        config=BASE_CONFIG,
    )

    expected = {
        "sent": True,
        "reason": "sent",
        "threshold_usd": "120.00",
        "estimated_remaining_usd": "98.85",
    }
    assert first == expected
    assert second == expected
    assert len(calls) == 2
    payload = json.loads(calls[0][0].data.decode("utf-8"))
    assert payload["title"] == "$98.85｜正常可用账号 2 个｜Codex 剩余额度不足告警"
    assert payload["body"] == (
        "当前估算剩余额度：$98.85\n"
        "告警阈值：$120.00\n"
        "正常可用账号：2\n"
        "账号总数：7\n"
        "任务 ID：task-quota-1"
    )


def test_quota_alert_skips_disabled_and_equal_threshold(monkeypatch):
    monkeypatch.setattr(alerts, "_open_bark_request", pytest.fail)
    equal_report = AvailableQuotaReport(
        account_count=2,
        remote_account_count=7,
        estimated_remaining_usd=Decimal("120.00"),
        accounts=(),
    )

    disabled = alerts.send_bark_quota_threshold_alert(
        task_id="task-quota-disabled",
        quota_report=REPORT,
        config={**BASE_CONFIG, "chatgpt_auto_relogin_quota_alert_threshold_usd": "0"},
    )
    equal = alerts.send_bark_quota_threshold_alert(
        task_id="task-quota-equal",
        quota_report=equal_report,
        config=BASE_CONFIG,
    )

    assert disabled["reason"] == "quota_alert_disabled"
    assert equal == {
        "sent": False,
        "reason": "quota_not_below_threshold",
        "threshold_usd": "120.00",
        "estimated_remaining_usd": "120.00",
    }


def test_test_notification_uses_same_critical_delivery(monkeypatch):
    calls: list[tuple[object, float]] = []
    monkeypatch.setattr(alerts, "_open_bark_request", _success_urlopen(calls))

    result = alerts.send_bark_test_notification(config=BASE_CONFIG)

    assert result == {"sent": True, "reason": "sent"}
    payload = json.loads(calls[0][0].data.decode("utf-8"))
    assert payload["title"] == "Any Auto Register · Bark 强提醒测试"
    assert "配置可用" in payload["body"]
    assert payload["level"] == "critical"
    assert payload["call"] == "1"
    assert payload["sound"] == "alarm"


@pytest.mark.parametrize(
    "response",
    [
        FakeResponse(b"not-json"),
        FakeResponse(b'{"code":500,"message":"failed"}'),
        FakeResponse(b'{"code":200}', status=500),
    ],
)
def test_transport_rejects_invalid_bark_responses(monkeypatch, response):
    monkeypatch.setattr(alerts, "_open_bark_request", lambda *args, **kwargs: response)

    result = alerts.send_bark_test_notification(config=BASE_CONFIG)

    assert result["sent"] is False
    assert result["reason"] == "send_failed"
    assert result["error_type"] in {
        "BarkResponseError",
        "JSONDecodeError",
    }


def test_transport_uses_an_opener_that_rejects_redirects(monkeypatch):
    handlers: list[object] = []

    class FakeOpener:
        def open(self, outbound, timeout):
            return FakeResponse(b'{"code":200,"message":"success"}')

    def build_opener(*args):
        handlers.extend(args)
        return FakeOpener()

    monkeypatch.setattr(alerts.request, "build_opener", build_opener)
    monkeypatch.setattr(alerts.request, "urlopen", pytest.fail)

    result = alerts.send_bark_test_notification(config=BASE_CONFIG)

    assert result == {"sent": True, "reason": "sent"}
    assert any(
        isinstance(handler, alerts._NoRedirectHandler)
        for handler in handlers
    )


def test_redirect_handler_rejects_redirect_targets():
    handler = alerts._NoRedirectHandler()

    with pytest.raises(alerts.BarkResponseError):
        handler.redirect_request(None, None, 302, "Found", {}, "https://evil.example")


def test_transport_rejects_oversized_response(monkeypatch):
    body = b'{"code":200,"padding":"' + (b"x" * 70_000) + b'"}'
    monkeypatch.setattr(
        alerts,
        "_open_bark_request",
        lambda *args, **kwargs: FakeResponse(body),
    )

    result = alerts.send_bark_test_notification(config=BASE_CONFIG)

    assert result == {
        "sent": False,
        "reason": "send_failed",
        "error_type": "BarkResponseError",
    }


def test_transport_failure_never_leaks_endpoint_or_payload(monkeypatch, caplog):
    def fail(*args, **kwargs):
        raise RuntimeError(f"failed {BARK_ENDPOINT} body-secret")

    monkeypatch.setattr(alerts, "_open_bark_request", fail)

    with caplog.at_level(logging.WARNING):
        result = alerts.send_bark_test_notification(config=BASE_CONFIG)

    rendered = f"{result}\n{caplog.text}"
    assert result == {
        "sent": False,
        "reason": "send_failed",
        "error_type": "RuntimeError",
    }
    assert BARK_DEVICE_SECRET not in rendered
    assert BARK_ENDPOINT not in rendered
    assert "body-secret" not in rendered
