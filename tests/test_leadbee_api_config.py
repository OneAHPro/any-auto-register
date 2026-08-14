from __future__ import annotations

import pytest
from fastapi import HTTPException
from platforms.chatgpt.leadbee_open_api import LeadBeeAPIError


class FakeConfigStore:
    def __init__(self, values=None):
        self.values = dict(values or {})
        self.writes = []

    def get_all(self):
        return dict(self.values)

    def set_many(self, data):
        payload = dict(data)
        self.writes.append(payload)
        self.values.update(payload)


def test_leadbee_config_is_allowlisted_normalized_and_write_only(monkeypatch):
    from api import config as config_api

    store = FakeConfigStore(
        {
            "leadbee_api_enabled": "1",
            "leadbee_api_key": "stored_fixture_key",
            "leadbee_api_secret": "stored_fixture_secret",
            "leadbee_api_product_id": " old-product ",
        }
    )
    monkeypatch.setattr(config_api, "config_store", store)

    public = config_api.get_config()
    assert public["leadbee_api_key"] == ""
    assert public["leadbee_api_secret"] == ""
    assert public["leadbee_api_enabled"] == "1"

    result = config_api.update_config(
        config_api.ConfigUpdate(
            data={
                "leadbee_api_enabled": " YES ",
                "leadbee_api_key": " new_fixture_key ",
                "leadbee_api_secret": " new_fixture_secret ",
                "leadbee_api_product_id": " product-2 ",
            }
        )
    )
    assert result["ok"] is True
    assert store.values["leadbee_api_enabled"] == "1"
    assert store.values["leadbee_api_key"] == "new_fixture_key"
    assert store.values["leadbee_api_secret"] == "new_fixture_secret"
    assert store.values["leadbee_api_product_id"] == "product-2"


def test_enable_validation_rejects_without_writing(monkeypatch):
    from api import config as config_api

    store = FakeConfigStore({"leadbee_api_enabled": "0"})
    monkeypatch.setattr(config_api, "config_store", store)
    with pytest.raises(HTTPException) as exc:
        config_api.update_config(
            config_api.ConfigUpdate(data={"leadbee_api_enabled": True})
        )
    assert exc.value.status_code == 400
    assert store.writes == []


@pytest.mark.parametrize("persisted_enabled", ["true", "YES", True])
def test_historical_enabled_values_still_validate_unrelated_updates(
    monkeypatch, persisted_enabled
):
    from api import config as config_api

    store = FakeConfigStore({"leadbee_api_enabled": persisted_enabled})
    monkeypatch.setattr(config_api, "config_store", store)
    with pytest.raises(HTTPException) as exc:
        config_api.update_config(
            config_api.ConfigUpdate(data={"leadbee_api_product_id": "prod-1"})
        )
    assert exc.value.status_code == 400
    assert store.writes == []


def test_blank_leadbee_credentials_preserve_existing_values(monkeypatch):
    from api import config as config_api

    store = FakeConfigStore(
        {
            "leadbee_api_enabled": "0",
            "leadbee_api_key": "stored_fixture_key",
            "leadbee_api_secret": "stored_fixture_secret",
        }
    )
    monkeypatch.setattr(config_api, "config_store", store)
    config_api.update_config(
        config_api.ConfigUpdate(
            data={
                "leadbee_api_key": "   ",
                "leadbee_api_secret": "",
                "leadbee_api_enabled": False,
            }
        )
    )
    assert store.values["leadbee_api_key"] == "stored_fixture_key"
    assert store.values["leadbee_api_secret"] == "stored_fixture_secret"
    assert store.writes == [{"leadbee_api_enabled": "0"}]


def test_connection_test_merges_unsaved_credentials_without_persisting(monkeypatch):
    from api import config as config_api

    store = FakeConfigStore(
        {
            "leadbee_api_key": "stored_fixture_key",
            "leadbee_api_secret": "stored_fixture_secret",
            "leadbee_api_product_id": "prod-1",
        }
    )
    calls = []

    class FakeClient:
        def __init__(self, **kwargs):
            calls.append(("init", kwargs))

        def get_products(self):
            calls.append("products")
            return {
                "data": {
                    "results": [
                        {"id": 123, "product_id": "prod-1"},
                        {"id": "prod-2"},
                    ]
                }
            }

        def get_balance(self):
            calls.append("balance")
            return {"data": [{"balance": "12.5", "currency": {"code": "usd"}}]}

    monkeypatch.setattr(config_api, "config_store", store)
    monkeypatch.setattr(config_api, "LeadBeeOpenAPIClient", FakeClient)
    result = config_api.test_leadbee_config(
        config_api.LeadBeeTestRequest(
            data={"leadbee_api_key": "", "leadbee_api_product_id": "prod-2"}
        )
    )
    assert result == {
        "ok": True,
        "product_ids": ["prod-1", "prod-2"],
        "configured_product_available": True,
        "balance_available": "12.50",
        "currency": "USD",
    }
    assert calls == [
        ("init", {"api_key": "stored_fixture_key", "api_secret": "stored_fixture_secret"}),
        "products",
        "balance",
    ]
    assert store.writes == []


def test_connection_test_redacts_sensitive_product_metadata(monkeypatch):
    from api import config as config_api

    api_key = "FixtureKey-Alpha987"
    api_secret = "FixtureSecret-Beta654"
    store = FakeConfigStore(
        {
            "leadbee_api_key": api_key,
            "leadbee_api_secret": api_secret,
            "leadbee_api_product_id": "prod-1",
        }
    )

    class FakeClient:
        def __init__(self, **kwargs):
            assert kwargs == {"api_key": api_key, "api_secret": api_secret}

        def get_products(self):
            return {
                "data": {
                    "results": [
                        {"id": api_key},
                        {"id": f"prefix-{api_key.swapcase()}-suffix"},
                        {"id": "KEY-ALPHA987"},
                        {"id": api_secret.upper()},
                        {"id": f"prefix-{api_secret.lower()}-suffix"},
                        {"id": "secret-beta654"},
                        {"id": "13800138000"},
                        {"id": "1234"},
                        {"id": "12345678"},
                        {"id": "a3" * 32},
                        {"id": "Authorization"},
                        {"id": "X-API-Key"},
                        {"id": "api_secret"},
                        {"id": "prod-1"},
                        {"id": "sku_A.B/9:blue-2"},
                        {"id": "123"},
                        {"id": "123456789"},
                    ]
                }
            }

        def get_balance(self):
            return {"balance": "12.5", "currency": "USD"}

    monkeypatch.setattr(config_api, "config_store", store)
    monkeypatch.setattr(config_api, "LeadBeeOpenAPIClient", FakeClient)

    result = config_api.test_leadbee_config(
        config_api.LeadBeeTestRequest(data={})
    )

    assert result == {
        "ok": True,
        "product_ids": ["prod-1", "sku_A.B/9:blue-2", "123", "123456789"],
        "configured_product_available": True,
        "balance_available": "12.50",
        "currency": "USD",
    }


def test_connection_test_redacts_provider_errors_and_payload_secrets(monkeypatch):
    from api import config as config_api

    store = FakeConfigStore(
        {
            "leadbee_api_key": "stored_fixture_key",
            "leadbee_api_secret": "stored_fixture_secret",
            "leadbee_api_product_id": "prod-1",
        }
    )

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def get_products(self):
            return {"products": [{"id": "prod-1", "key": "fake_key", "phone": "13800138000"}]}

        def get_balance(self):
            raise RuntimeError("provider secret stored_fixture_secret phone 13800138000")

    monkeypatch.setattr(config_api, "config_store", store)
    monkeypatch.setattr(config_api, "LeadBeeOpenAPIClient", FakeClient)
    with pytest.raises(HTTPException) as exc:
        config_api.test_leadbee_config(config_api.LeadBeeTestRequest(data={}))
    assert exc.value.status_code == 502
    detail = str(exc.value.detail)
    assert "stored_fixture_secret" not in detail
    assert "13800138000" not in detail


def test_connection_test_logs_no_provider_diagnostics(monkeypatch, caplog):
    from api import config as config_api

    store = FakeConfigStore(
        {
            "leadbee_api_key": "stored_fixture_key",
            "leadbee_api_secret": "stored_fixture_secret",
            "leadbee_api_product_id": "prod-1",
        }
    )

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def get_products(self):
            raise LeadBeeAPIError(
                "provider detail stored_fixture_secret",
                code="13800138000",
            )

        def get_balance(self):
            pytest.fail("balance must not be called after products failure")

    monkeypatch.setattr(config_api, "config_store", store)
    monkeypatch.setattr(config_api, "LeadBeeOpenAPIClient", FakeClient)
    with pytest.raises(HTTPException):
        config_api.test_leadbee_config(config_api.LeadBeeTestRequest(data={}))
    logs = caplog.text
    assert "13800138000" not in logs
    assert "stored_fixture_secret" not in logs
