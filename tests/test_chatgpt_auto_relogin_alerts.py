from __future__ import annotations

import logging

import pytest


BASE_CONFIG = {
    "chatgpt_auto_relogin_alert_threshold": "5",
    "smtp_host": "smtp.example.test",
    "smtp_port": "587",
    "smtp_username": "sender@example.test",
    "smtp_password": "smtp-test-credential",
    "smtp_sender_email": "alerts@example.test",
    "smtp_recipient_email": "owner@example.test",
    "smtp_use_ssl": "1",
    "smtp_force_auth_login": "0",
}


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


def test_below_threshold_does_not_open_smtp(monkeypatch):
    from services import chatgpt_auto_relogin_alerts as alerts

    smtp = pytest.fail
    monkeypatch.setattr(alerts.smtplib, "SMTP", smtp)
    monkeypatch.setattr(alerts.smtplib, "SMTP_SSL", smtp)

    result = alerts.send_auto_relogin_alert(
        task_id="task-below",
        total_accounts=64,
        invalid_rt_count=4,
        relogin_failed_count=4,
        config=BASE_CONFIG,
    )

    assert result == {
        "sent": False,
        "reason": "below_threshold",
        "threshold": 5,
    }


@pytest.mark.parametrize(
    ("invalid_rt_count", "relogin_failed_count"),
    [(5, 0), (0, 5), (7, 2)],
)
def test_reaching_either_threshold_sends_one_starttls_message(
    monkeypatch,
    invalid_rt_count,
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
        invalid_rt_count=invalid_rt_count,
        relogin_failed_count=relogin_failed_count,
        config=BASE_CONFIG,
    )

    assert result == {"sent": True, "reason": "sent", "threshold": 5}
    assert len(FakeSMTP.instances) == 1
    smtp = FakeSMTP.instances[0]
    assert smtp.host == "smtp.example.test"
    assert smtp.port == 587
    assert any(call[0] == "starttls" for call in smtp.calls)
    assert ("login", "sender@example.test", "smtp-test-credential") in smtp.calls
    send_call = next(call for call in smtp.calls if call[0] == "send_message")
    assert send_call[1]["from_addr"] == "alerts@example.test"
    assert send_call[1]["to_addrs"] == ["owner@example.test"]
    body = smtp.message.get_content()
    assert "task-threshold" in body
    assert f"RT 明确失效：{invalid_rt_count}" in body
    assert f"完整重登失败：{relogin_failed_count}" in body
    assert "smtp-test-credential" not in smtp.message.as_string()


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
        invalid_rt_count=5,
        relogin_failed_count=0,
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
        invalid_rt_count=5,
        relogin_failed_count=0,
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
        invalid_rt_count=5,
        relogin_failed_count=0,
        config={"chatgpt_auto_relogin_alert_threshold": "5"},
    )

    assert result == {
        "sent": False,
        "reason": "smtp_not_configured",
        "threshold": 5,
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
            invalid_rt_count=5,
            relogin_failed_count=0,
            config=BASE_CONFIG,
        )

    assert result == {
        "sent": False,
        "reason": "send_failed",
        "threshold": 5,
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
    assert "RT 明确失效" not in smtp.message.get_content()
    assert "smtp-test-credential" not in smtp.message.as_string()
