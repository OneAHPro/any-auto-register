from __future__ import annotations

from decimal import Decimal


def test_estimate_account_quota_marks_one_hundred_percent_exhausted():
    from services.chatgpt_codex2api_quota import estimate_account_quota

    estimate = estimate_account_quota(
        {
            "usage_percent_7d": 100,
            "billed_7d": 81.42,
        }
    )

    assert estimate.state == "exhausted"
    assert estimate.remaining_usd == Decimal("0.00")


def test_estimate_account_quota_marks_over_one_hundred_percent_exhausted():
    from services.chatgpt_codex2api_quota import estimate_account_quota

    estimate = estimate_account_quota(
        {
            "usage_percent_7d": 105,
            "billed_7d": 120,
        }
    )

    assert estimate.state == "exhausted"
    assert estimate.remaining_usd == Decimal("0.00")


def test_estimate_account_quota_calculates_remaining_usd():
    from services.chatgpt_codex2api_quota import estimate_account_quota

    estimate = estimate_account_quota(
        {
            "usage_percent_7d": 68,
            "billed_7d": 81.42,
        }
    )

    assert estimate.state == "available"
    assert estimate.usage_percent == Decimal("68")
    assert estimate.billed_usd == Decimal("81.42")
    assert estimate.remaining_usd == Decimal("38.32")


def test_estimate_account_quota_rejects_missing_zero_or_invalid_values():
    from services.chatgpt_codex2api_quota import estimate_account_quota

    invalid_rows = (
        {"billed_7d": 10},
        {"usage_percent_7d": 0, "billed_7d": 0},
        {"usage_percent_7d": "nan", "billed_7d": 10},
        {"usage_percent_7d": 50, "billed_7d": -1},
        {"usage_percent_7d": True, "billed_7d": 10},
    )

    for row in invalid_rows:
        assert estimate_account_quota(row).state == "invalid"


def test_summarize_available_quota_filters_non_normal_accounts():
    from services.chatgpt_codex2api_quota import summarize_available_quota

    report = summarize_available_quota(
        [
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
            {
                "email": "full@example.com",
                "remote_status": "rate_limited",
                "usage_percent_7d": 100,
                "billed_7d": 120,
            },
            {
                "email": "bad@example.com",
                "remote_status": "unauthorized",
                "usage_percent_7d": 50,
                "billed_7d": 50,
            },
        ]
    )

    assert report.remote_account_count == 4
    assert report.account_count == 2
    assert report.estimated_remaining_usd == Decimal("98.85")
    assert [row.email for row in report.accounts] == [
        "a@example.com",
        "b@example.com",
    ]
    assert report.accounts[0].remaining_usd == Decimal("60.53")
    assert report.accounts[1].remaining_usd == Decimal("38.32")
