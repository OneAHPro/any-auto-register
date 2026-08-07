from __future__ import annotations

import logging
from datetime import datetime, timezone

import pytest


BASE_CONFIG = {
    "chatgpt_auto_relogin_alert_threshold": "20",
    "smtp_host": "smtp.example.test",
    "smtp_port": "587",
    "smtp_username": "sender@example.test",
    "smtp_password": "smtp-test-credential",
    "smtp_sender_email": "alerts@example.test",
    "smtp_recipient_email": "owner@example.test",
    "smtp_use_ssl": "1",
    "smtp_force_auth_login": "0",
}

QUOTA_ROWS = [
    {
        "email": "a@example.com",
        "remote_status": "active",
        "usage_percent_7d": 53,
        "billed_7d": 68.26,
    },
    {
        "email": "b@example.com",
        "remote_status": "rate_limited",
        "usage_percent_7d": 68,
        "billed_7d": 81.42,
    },
]


class FakeSMTP:
    instances: list["FakeSMTP"] = []

    def __init__(self, host, port, **kwargs):
        self.host = host
        self.port = port
        self.kwargs = kwargs
        self.calls: list[tuple] = []
        self.message = None
        type(self).instances.append(self)

    def __enter__(self):
        self.calls.append(("enter",))
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.calls.append(("exit", exc_type))
        return False

    def ehlo(self):
        self.calls.append(("ehlo",))

    def starttls(self, **kwargs):
        self.calls.append(("starttls", kwargs))

    def login(self, username, password):
        self.calls.append(("login", username, password))

    def auth(self, mechanism, authobject, **kwargs):
        self.calls.append(("auth", mechanism, kwargs))
        self.calls.append(("auth_username", authobject(b"Username:")))
        self.calls.append(("auth_password", authobject(b"Password:")))

    def send_message(self, message, **kwargs):
        self.message = message
        self.calls.append(("send_message", kwargs))


@pytest.fixture(autouse=True)
def reset_fake_smtp():
    FakeSMTP.instances = []
    yield
    FakeSMTP.instances = []


def test_invalid_rt_count_alone_does_not_open_smtp(monkeypatch):
    from services import chatgpt_auto_relogin_alerts as alerts

    smtp = pytest.fail
    monkeypatch.setattr(alerts.smtplib, "SMTP", smtp)
    monkeypatch.setattr(alerts.smtplib, "SMTP_SSL", smtp)

    result = alerts.send_auto_relogin_alert(
        task_id="task-invalid-only",
        total_accounts=64,
        successful_accounts=43,
        invalid_rt_count=100,
        relogin_failed_count=19,
        deleted_account_count=19,
        config=BASE_CONFIG,
    )

    assert result == {
        "sent": False,
        "reason": "below_threshold",
        "threshold": 20,
    }


def test_exhausted_failures_never_open_smtp(monkeypatch):
    from services import chatgpt_auto_relogin_alerts as alerts

    monkeypatch.setattr(alerts.smtplib, "SMTP", pytest.fail)
    monkeypatch.setattr(alerts.smtplib, "SMTP_SSL", pytest.fail)
    result = alerts.send_auto_relogin_alert(
        task_id="task-exhausted",
        total_accounts=30,
        successful_accounts=0,
        invalid_rt_count=30,
        relogin_failed_count=30,
        deleted_account_count=30,
        quota_eligible_failure_count=0,
        quota_exhausted_failure_count=30,
        quota_accounts=[],
        config={**BASE_CONFIG, "chatgpt_auto_relogin_alert_threshold": "5"},
    )

    assert result == {
        "sent": False,
        "reason": "below_threshold",
        "threshold": 5,
    }


def test_available_failure_alert_contains_remaining_usd(monkeypatch):
    from services import chatgpt_auto_relogin_alerts as alerts

    monkeypatch.setattr(alerts.smtplib, "SMTP", FakeSMTP)
    result = alerts.send_auto_relogin_alert(
        task_id="task-quota",
        total_accounts=8,
        successful_accounts=2,
        invalid_rt_count=8,
        relogin_failed_count=8,
        deleted_account_count=3,
        quota_eligible_failure_count=5,
        quota_exhausted_failure_count=3,
        quota_accounts=QUOTA_ROWS,
        config={**BASE_CONFIG, "chatgpt_auto_relogin_alert_threshold": "5"},
    )

    assert result["sent"] is True
    message = FakeSMTP.instances[0].message
    plain = message.get_body(preferencelist=("plain",)).get_content()
    html = message.get_body(preferencelist=("html",)).get_content()
    assert "仍有额度的重登失败：5" in plain
    assert "额度已用完的重登失败：3" in plain
    assert "正常可用账号：2" in plain
    assert "Codex2API 账号总数：2" in plain
    assert "当前估算剩余额度：$98.85" in plain
    assert message["Subject"] == (
        "$98.85｜正常可用账号 2 个｜ChatGPT 重登失败账号告警"
    )
    for account_detail in (
        "a@example.com",
        "b@example.com",
        "正常账号明细",
        "正常账号额度明细",
        "7d 使用率",
        "$60.53",
    ):
        assert account_detail not in plain
        assert account_detail not in html
    assert "重置时间" not in message.as_string()


def test_quota_threshold_alert_sends_below_threshold_every_call(monkeypatch):
    from services import chatgpt_auto_relogin_alerts as alerts
    from services.chatgpt_codex2api_quota import summarize_available_quota

    report = summarize_available_quota(QUOTA_ROWS)
    monkeypatch.setattr(alerts.smtplib, "SMTP", FakeSMTP)
    config = {
        **BASE_CONFIG,
        "chatgpt_auto_relogin_quota_alert_threshold_usd": "120.00",
    }

    results = [
        alerts.send_quota_threshold_alert(
            task_id=f"task-{index}",
            quota_report=report,
            quota_eligible_failure_count=0,
            quota_exhausted_failure_count=0,
            relogin_failed_count=0,
            deleted_account_count=0,
            config=config,
        )
        for index in (1, 2)
    ]

    assert [result["sent"] for result in results] == [True, True]
    assert [result["reason"] for result in results] == ["sent", "sent"]
    assert len(FakeSMTP.instances) == 2
    message = FakeSMTP.instances[0].message
    assert message["Subject"] == (
        "$98.85｜正常可用账号 2 个｜Codex2API 剩余额度不足告警"
    )
    plain = message.get_body(preferencelist=("plain",)).get_content()
    html = message.get_body(preferencelist=("html",)).get_content()
    assert "额度告警阈值：$120.00" in plain
    assert "当前估算剩余额度：$98.85" in plain
    assert "Codex2API 账号总数：2" in plain
    assert "a@example.com" not in plain
    assert "a@example.com" not in html


@pytest.mark.parametrize(
    ("threshold", "expected_reason"),
    [
        ("0.00", "quota_alert_disabled"),
        ("98.85", "quota_not_below_threshold"),
        ("90.00", "quota_not_below_threshold"),
    ],
)
def test_quota_threshold_alert_skips_disabled_equal_or_above_remaining(
    monkeypatch,
    threshold,
    expected_reason,
):
    from services import chatgpt_auto_relogin_alerts as alerts
    from services.chatgpt_codex2api_quota import summarize_available_quota

    report = summarize_available_quota(QUOTA_ROWS)
    monkeypatch.setattr(alerts.smtplib, "SMTP", pytest.fail)
    monkeypatch.setattr(alerts.smtplib, "SMTP_SSL", pytest.fail)

    result = alerts.send_quota_threshold_alert(
        task_id="task-no-quota-alert",
        quota_report=report,
        quota_eligible_failure_count=0,
        quota_exhausted_failure_count=0,
        relogin_failed_count=0,
        config={
            **BASE_CONFIG,
            "chatgpt_auto_relogin_quota_alert_threshold_usd": threshold,
        },
    )

    assert result == {
        "sent": False,
        "reason": expected_reason,
        "threshold_usd": threshold,
        "estimated_remaining_usd": "98.85",
    }


@pytest.mark.parametrize("configured_threshold", ["", "not-a-number", "0", "-1"])
def test_invalid_threshold_falls_back_to_twenty(monkeypatch, configured_threshold):
    from services import chatgpt_auto_relogin_alerts as alerts

    config = dict(BASE_CONFIG)
    config["chatgpt_auto_relogin_alert_threshold"] = configured_threshold
    monkeypatch.setattr(alerts.smtplib, "SMTP", pytest.fail)

    result = alerts.send_auto_relogin_alert(
        task_id="task-invalid-threshold",
        total_accounts=64,
        successful_accounts=43,
        invalid_rt_count=100,
        relogin_failed_count=19,
        config=config,
    )

    assert result == {
        "sent": False,
        "reason": "below_threshold",
        "threshold": 20,
    }


@pytest.mark.parametrize("relogin_failed_count", [20, 21])
def test_default_threshold_relogin_failure_sends_one_starttls_message(
    monkeypatch,
    relogin_failed_count,
):
    from services import chatgpt_auto_relogin_alerts as alerts

    monkeypatch.setattr(alerts.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(
        alerts.smtplib,
        "SMTP_SSL",
        lambda *args, **kwargs: pytest.fail("587 should use STARTTLS"),
    )

    result = alerts.send_auto_relogin_alert(
        task_id="task-threshold",
        total_accounts=64,
        successful_accounts=43,
        invalid_rt_count=100,
        relogin_failed_count=relogin_failed_count,
        deleted_account_count=17,
        config=BASE_CONFIG,
    )

    assert result == {"sent": True, "reason": "sent", "threshold": 20}
    assert len(FakeSMTP.instances) == 1
    smtp = FakeSMTP.instances[0]
    assert smtp.host == "smtp.example.test"
    assert smtp.port == 587
    assert any(call[0] == "starttls" for call in smtp.calls)
    assert ("login", "sender@example.test", "smtp-test-credential") in smtp.calls
    send_call = next(call for call in smtp.calls if call[0] == "send_message")
    assert send_call[1]["from_addr"] == "alerts@example.test"
    assert send_call[1]["to_addrs"] == ["owner@example.test"]
    body = smtp.message.get_body(preferencelist=("plain",)).get_content()
    assert "task-threshold" in body
    assert "账号总数：64" in body
    assert "成功账号：43" in body
    assert "鉴权失败：100" in body
    assert f"重登失败：{relogin_failed_count}" in body
    assert "其中已删除或停用账号：17" in body
    assert "smtp-test-credential" not in smtp.message.as_string()


def test_deleted_subset_is_clamped_to_relogin_failures_in_plain_and_html(monkeypatch):
    from services import chatgpt_auto_relogin_alerts as alerts

    monkeypatch.setattr(alerts.smtplib, "SMTP", FakeSMTP)

    result = alerts.send_auto_relogin_alert(
        task_id="task-clamped-deleted-subset",
        total_accounts=20,
        successful_accounts=0,
        invalid_rt_count=20,
        relogin_failed_count=20,
        deleted_account_count=999,
        config=BASE_CONFIG,
    )

    assert result == {"sent": True, "reason": "sent", "threshold": 20}
    message = FakeSMTP.instances[0].message
    plain = message.get_body(preferencelist=("plain",)).get_content()
    html = message.get_body(preferencelist=("html",)).get_content()
    assert "其中已删除或停用账号：20" in plain
    assert "其中已删除或停用账号：20" in html
    assert "已删除或停用账号属于重登失败账号的子集" in html
    assert "999" not in plain
    assert "999" not in html


@pytest.mark.parametrize(
    ("relogin_failed_count", "expected_sent"),
    [(6, False), (7, True)],
)
def test_custom_threshold_only_sends_when_relogin_failure_reaches_it(
    monkeypatch,
    relogin_failed_count,
    expected_sent,
):
    from services import chatgpt_auto_relogin_alerts as alerts

    config = dict(BASE_CONFIG)
    config["chatgpt_auto_relogin_alert_threshold"] = "7"
    monkeypatch.setattr(alerts.smtplib, "SMTP", FakeSMTP)

    result = alerts.send_auto_relogin_alert(
        task_id="task-custom-threshold",
        total_accounts=64,
        successful_accounts=43,
        invalid_rt_count=100,
        relogin_failed_count=relogin_failed_count,
        config=config,
    )

    assert result["sent"] is expected_sent
    assert result["threshold"] == 7
    assert len(FakeSMTP.instances) == int(expected_sent)


def test_port_465_uses_smtp_ssl_and_recipient_falls_back_to_username(monkeypatch):
    from services import chatgpt_auto_relogin_alerts as alerts

    config = dict(BASE_CONFIG)
    config.update({"smtp_port": "465", "smtp_recipient_email": ""})
    monkeypatch.setattr(alerts.smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setattr(
        alerts.smtplib,
        "SMTP",
        lambda *args, **kwargs: pytest.fail("465 should use SMTP_SSL"),
    )

    result = alerts.send_auto_relogin_alert(
        task_id="task-ssl",
        total_accounts=64,
        successful_accounts=43,
        invalid_rt_count=100,
        relogin_failed_count=20,
        config=config,
    )

    assert result["sent"] is True
    smtp = FakeSMTP.instances[0]
    assert smtp.port == 465
    assert not any(call[0] == "starttls" for call in smtp.calls)
    send_call = next(call for call in smtp.calls if call[0] == "send_message")
    assert send_call[1]["to_addrs"] == ["sender@example.test"]


def test_force_auth_login_uses_login_mechanism(monkeypatch):
    from services import chatgpt_auto_relogin_alerts as alerts

    config = dict(BASE_CONFIG)
    config["smtp_force_auth_login"] = "1"
    monkeypatch.setattr(alerts.smtplib, "SMTP", FakeSMTP)

    result = alerts.send_auto_relogin_alert(
        task_id="task-auth-login",
        total_accounts=64,
        successful_accounts=43,
        invalid_rt_count=100,
        relogin_failed_count=20,
        config=config,
    )

    assert result["sent"] is True
    calls = FakeSMTP.instances[0].calls
    assert any(call[:2] == ("auth", "LOGIN") for call in calls)
    assert not any(call[0] == "login" for call in calls)


def test_missing_smtp_configuration_returns_without_connecting(monkeypatch):
    from services import chatgpt_auto_relogin_alerts as alerts

    monkeypatch.setattr(alerts.smtplib, "SMTP", pytest.fail)

    result = alerts.send_auto_relogin_alert(
        task_id="task-not-configured",
        total_accounts=64,
        successful_accounts=43,
        invalid_rt_count=100,
        relogin_failed_count=20,
        config={"chatgpt_auto_relogin_alert_threshold": "20"},
    )

    assert result == {
        "sent": False,
        "reason": "smtp_not_configured",
        "threshold": 20,
    }


def test_send_failure_is_sanitized_and_does_not_raise(monkeypatch, caplog):
    from services import chatgpt_auto_relogin_alerts as alerts

    class FailingSMTP(FakeSMTP):
        def login(self, username, password):
            raise RuntimeError(f"login rejected for {username} with {password}")

    monkeypatch.setattr(alerts.smtplib, "SMTP", FailingSMTP)

    with caplog.at_level(logging.WARNING):
        result = alerts.send_auto_relogin_alert(
            task_id="task-failed-send",
            total_accounts=64,
            successful_accounts=43,
            invalid_rt_count=100,
            relogin_failed_count=20,
            config=BASE_CONFIG,
        )

    assert result == {
        "sent": False,
        "reason": "send_failed",
        "threshold": 20,
        "error_type": "RuntimeError",
    }
    assert "smtp-test-credential" not in caplog.text
    assert "smtp-test-credential" not in str(result)


def test_smtp_test_email_uses_dedicated_subject_without_alert_threshold(
    monkeypatch,
):
    from services import chatgpt_auto_relogin_alerts as alerts

    config = dict(BASE_CONFIG)
    config["chatgpt_auto_relogin_alert_threshold"] = "999"
    monkeypatch.setattr(alerts.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(alerts, "_format_beijing_time", lambda: "2026-08-04 20:34:56（北京时间）")

    result = alerts.send_smtp_test_email(config=config)

    assert result == {
        "sent": True,
        "reason": "sent",
        "recipient_count": 1,
    }
    assert len(FakeSMTP.instances) == 1
    smtp = FakeSMTP.instances[0]
    assert smtp.message["Subject"] == "[Any Auto Register] SMTP 测试成功"
    assert "SMTP 邮件配置可用" in smtp.message.get_content()
    assert "2026-08-04 20:34:56（北京时间）" in smtp.message.get_content()
    assert "鉴权失败" not in smtp.message.get_content()
    assert "smtp-test-credential" not in smtp.message.as_string()


def test_format_beijing_time_treats_naive_datetime_as_utc():
    from services import chatgpt_auto_relogin_alerts as alerts

    assert alerts._format_beijing_time(datetime(2026, 8, 4, 12, 34, 56)) == (
        "2026-08-04 20:34:56（北京时间）"
    )
    assert alerts._format_beijing_time(
        datetime(2026, 8, 4, 12, 34, 56, tzinfo=timezone.utc)
    ) == "2026-08-04 20:34:56（北京时间）"


def test_alert_message_has_escaped_html_and_fixed_metrics_order(monkeypatch):
    from services import chatgpt_auto_relogin_alerts as alerts

    monkeypatch.setattr(alerts.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(alerts, "_format_beijing_time", lambda: "2026-08-04 20:34:56（北京时间）")

    result = alerts.send_auto_relogin_alert(
        task_id="<script>alert('x')</script>",
        total_accounts=-64,
        successful_accounts="bad",
        invalid_rt_count=-1,
        relogin_failed_count=20,
        config=BASE_CONFIG,
    )

    assert result == {"sent": True, "reason": "sent", "threshold": 20}
    message = FakeSMTP.instances[0].message
    plain = message.get_body(preferencelist=("plain",)).get_content()
    html = message.get_body(preferencelist=("html",)).get_content()
    assert message["Subject"] == (
        "$0.00｜正常可用账号 0 个｜ChatGPT 重登失败账号告警"
    )
    assert plain.index("账号总数：0") < plain.index("成功账号：0")
    assert plain.index("成功账号：0") < plain.index("鉴权失败：0")
    assert plain.index("鉴权失败：0") < plain.index("重登失败：20")
    assert "鉴权失败数仅用于展示；仍有额度的重登失败数是本邮件的触发依据。" in plain
    assert "两项为过程指标，可能包含同一账号，四项统计不应相加核对总数。" in plain
    assert "2026-08-04 20:34:56（北京时间）" in plain
    assert "<script>" not in html
    assert "&lt;script&gt;alert(&#x27;x&#x27;)&lt;/script&gt;" in html
    assert "本轮自动鉴权已完成，重登失败账号数已达到告警阈值。" in html
    assert "请在 Any Auto Register 的“任务运行”页面查看本轮详细记录。" in html
    assert html.count('width="25%"') == 4
    assert "@media only screen and (max-width: 600px)" in html
    assert "smtp-test-credential" not in plain
    assert "smtp-test-credential" not in html
