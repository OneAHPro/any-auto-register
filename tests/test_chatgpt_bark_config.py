from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest

from api import config as config_api


BARK_DEVICE_SECRET = "BARK_DEVICE_SECRET"
BARK_ENDPOINT = f"https://api.day.app/{BARK_DEVICE_SECRET}"


def test_get_config_defaults_bark_off_and_never_returns_endpoint(monkeypatch):
    monkeypatch.setattr(
        config_api.config_store,
        "get_all",
        lambda: {"bark_endpoint": BARK_ENDPOINT},
    )

    response = config_api.get_config()

    assert response["bark_enabled"] == "0"
    assert response["bark_endpoint"] == ""
    assert BARK_DEVICE_SECRET not in str(response)


def test_update_config_normalizes_bark_and_preserves_empty_endpoint(monkeypatch):
    writes: list[dict[str, object]] = []
    monkeypatch.setattr(
        config_api.config_store,
        "set_many",
        lambda values: writes.append(dict(values)),
    )

    enabled = config_api.update_config(
        config_api.ConfigUpdate(
            data={
                "bark_enabled": True,
                "bark_endpoint": f"{BARK_ENDPOINT}/",
            }
        )
    )
    preserved = config_api.update_config(
        config_api.ConfigUpdate(
            data={
                "bark_enabled": False,
                "bark_endpoint": "",
            }
        )
    )

    assert writes == [
        {
            "bark_enabled": "1",
            "bark_endpoint": BARK_ENDPOINT,
        },
        {"bark_enabled": "0"},
    ]
    assert enabled["updated"] == ["bark_enabled", "bark_endpoint"]
    assert preserved["updated"] == ["bark_enabled"]


@pytest.mark.parametrize(
    "endpoint",
    [
        "file:///tmp/device",
        "api.day.app/device",
        "https:///missing-host",
        "http://api.day.app/device",
        "http://127.0.0.1/device",
        "https://localhost/device",
        "https://10.0.0.1/device",
        "https://169.254.169.254/latest/meta-data",
        "https://[::1]/device",
        "https://user:pass@api.day.app/device",
        "https://evil.example/device",
        "https://api.day.app:bad/device",
        "https://[::1",
    ],
)
def test_update_config_rejects_invalid_bark_endpoint_without_echoing_it(
    monkeypatch,
    endpoint,
):
    monkeypatch.setattr(config_api.config_store, "set_many", pytest.fail)

    with pytest.raises(HTTPException) as caught:
        config_api.update_config(
            config_api.ConfigUpdate(data={"bark_endpoint": endpoint})
        )

    assert caught.value.status_code == 400
    assert caught.value.detail == "Bark 推送地址必须使用 https://api.day.app/ 官方地址"
    assert endpoint not in str(caught.value.detail)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("put", "/config"),
        ("post", "/config/bark/test"),
    ],
)
def test_bark_routes_return_sanitized_400_for_malformed_url(
    monkeypatch,
    method,
    path,
):
    secret = "https://[BARK_ENDPOINT_SECRET"
    monkeypatch.setattr(config_api.config_store, "set_many", pytest.fail)
    monkeypatch.setattr(config_api.config_store, "get_all", lambda: {})
    app = FastAPI()
    app.include_router(config_api.router)

    with TestClient(app) as client:
        response = getattr(client, method)(
            path,
            json={"data": {"bark_enabled": True, "bark_endpoint": secret}},
        )

    assert response.status_code == 400
    assert response.json() == {
        "detail": "Bark 推送地址必须使用 https://api.day.app/ 官方地址"
    }
    assert secret not in response.text


def test_bark_test_uses_unsaved_form_values(monkeypatch):
    monkeypatch.setattr(
        config_api.config_store,
        "get_all",
        lambda: {
            "bark_enabled": "0",
            "bark_endpoint": "https://api.day.app/SAVED_DEVICE_SECRET",
        },
    )
    snapshots: list[dict[str, object]] = []

    def send(*, config):
        snapshots.append(dict(config))
        return {"sent": True, "reason": "sent"}

    monkeypatch.setattr(
        "services.chatgpt_bark_alerts.send_bark_test_notification",
        send,
    )

    response = config_api.test_bark_config(
        config_api.SMTPTestRequest(
            data={
                "bark_enabled": True,
                "bark_endpoint": BARK_ENDPOINT,
                "smtp_password": "must-be-ignored",
            }
        )
    )

    assert response == {"ok": True, "message": "测试 Bark 强提醒已发送"}
    assert snapshots == [
        {"bark_enabled": "1", "bark_endpoint": BARK_ENDPOINT}
    ]


def test_bark_test_empty_endpoint_uses_saved_secret(monkeypatch):
    saved_endpoint = "https://api.day.app/SAVED_DEVICE_SECRET"
    monkeypatch.setattr(
        config_api.config_store,
        "get_all",
        lambda: {"bark_enabled": "1", "bark_endpoint": saved_endpoint},
    )
    snapshots: list[dict[str, object]] = []

    def send(*, config):
        snapshots.append(dict(config))
        return {"sent": True, "reason": "sent"}

    monkeypatch.setattr(
        "services.chatgpt_bark_alerts.send_bark_test_notification",
        send,
    )

    response = config_api.test_bark_config(
        config_api.SMTPTestRequest(
            data={"bark_enabled": True, "bark_endpoint": ""}
        )
    )

    assert response["ok"] is True
    assert snapshots == [
        {"bark_enabled": "1", "bark_endpoint": saved_endpoint}
    ]


@pytest.mark.parametrize(
    ("result", "status_code", "detail"),
    [
        (
            {"sent": False, "reason": "bark_disabled"},
            400,
            "请先启用 Bark 强提醒",
        ),
        (
            {"sent": False, "reason": "bark_not_configured"},
            400,
            "请填写 Bark App 提供的完整推送地址",
        ),
        (
            {"sent": False, "reason": "invalid_bark_endpoint"},
            400,
            "Bark 推送地址必须使用 https://api.day.app/ 官方地址",
        ),
        (
            {
                "sent": False,
                "reason": "send_failed",
                "error_type": "RuntimeError",
            },
            502,
            "Bark 测试通知发送失败（RuntimeError）",
        ),
    ],
)
def test_bark_test_maps_sanitized_failures(
    monkeypatch,
    result,
    status_code,
    detail,
):
    monkeypatch.setattr(
        config_api.config_store,
        "get_all",
        lambda: {"bark_enabled": "1", "bark_endpoint": BARK_ENDPOINT},
    )
    monkeypatch.setattr(
        "services.chatgpt_bark_alerts.send_bark_test_notification",
        lambda **kwargs: result,
    )

    with pytest.raises(HTTPException) as caught:
        config_api.test_bark_config(config_api.SMTPTestRequest(data={}))

    assert caught.value.status_code == status_code
    assert caught.value.detail == detail
    assert BARK_DEVICE_SECRET not in str(caught.value.detail)
