from decimal import Decimal

from platforms.chatgpt.leadbee_capacity import parse_leadbee_capacity


def test_capacity_prefers_specific_available_balance_and_falls_back_product_id():
    snapshot = parse_leadbee_capacity(
        {
            "data": {
                "results": [
                    {
                        "id": 123,
                        "product_id": "prod-selected",
                        "price": "2.50",
                        "status": "AVAILABLE",
                    }
                ]
            }
        },
        {
            "data": {
                "balance": "100.00",
                "available_balance": "12.50",
                "reserved_balance": "5.00",
                "currency": {"code": "cny"},
            }
        },
        product_id="prod-selected",
    )

    assert snapshot.configured_product_available is True
    assert snapshot.balance_available == Decimal("12.50")
    assert snapshot.balance_reserved == Decimal("5.00")
    assert snapshot.unit_price == Decimal("2.50")
    assert snapshot.estimated_order_capacity == 5
    assert snapshot.currency == "CNY"


def test_capacity_omits_invalid_amounts_and_unavailable_product():
    snapshot = parse_leadbee_capacity(
        {
            "products": [
                {
                    "id": "prod-disabled",
                    "price": "0",
                    "status": "UNAVAILABLE",
                }
            ]
        },
        {
            "available": "NaN",
            "reserved": -1,
            "currency": "not-a-currency",
        },
        product_id="prod-disabled",
    )

    assert snapshot.public_dict() == {
        "configured_product_available": False,
        "balance_available": None,
        "balance_reserved": None,
        "unit_price": None,
        "estimated_order_capacity": None,
        "currency": None,
    }
