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


def test_summarize_mixes_plus_current_5h_with_pro_weekly_quota():
    from services.chatgpt_codex2api_quota import summarize_available_quota

    report = summarize_available_quota(
        [
            {
                "email": "plus@example.com",
                "plan_type": "plus",
                "remote_status": "active",
                "usage_percent_5h": 50,
                "billed_5h": 10,
                "usage_percent_7d": 25,
                "billed_7d": 20,
            },
            {
                "email": "pro@example.com",
                "plan_type": "pro",
                "remote_status": "active",
                "usage_percent_7d": 50,
                "billed_7d": 50,
            },
            {
                "email": "plus-pending@example.com",
                "plan_type": "plus",
                "remote_status": "active",
                "usage_percent_7d": 50,
                "billed_7d": 10,
            },
        ]
    )

    # Plus current = 5h (10), Pro current = weekly (50), while total is
    # always the weekly sum (60 + 50 + 10).
    assert report.current_remaining_usd == Decimal("60.00")
    assert report.total_remaining_usd == Decimal("120.00")
    assert report.estimated_remaining_usd == Decimal("120.00")
    assert report.eligible_account_count == 3
    assert report.current_data_count == 2
    assert report.total_data_count == 3
    assert not report.current_data_complete
    assert report.total_data_complete


def test_incomplete_window_does_not_reuse_previous_quota_value():
    from services.chatgpt_codex2api_quota import (
        summarize_available_quota,
    )

    report = summarize_available_quota(
        [{
            "email": "plus@example.com",
            "plan_type": "plus",
            "remote_status": "active",
            "usage_percent_7d": 50,
            "billed_7d": 120,
        }]
    )
    assert report.current_remaining_usd == Decimal("0.00")
    assert report.total_remaining_usd == Decimal("120.00")
    assert not report.current_data_complete
    assert report.total_data_complete


def test_exhausted_current_window_is_valid_zero_not_missing_data():
    from services.chatgpt_codex2api_quota import summarize_available_quota

    report = summarize_available_quota([
        {
            "email": "plus@example.com",
            "plan_type": "plus",
            "has_5h_window": True,
            "remote_status": "active",
            "usage_percent_5h": 100,
            "billed_5h": 10,
            "usage_percent_7d": 50,
            "billed_7d": 20,
        },
    ])

    assert report.current_remaining_usd == Decimal("0.00")
    assert report.total_remaining_usd == Decimal("20.00")
    assert report.current_data_count == 1
    assert report.total_data_count == 1
    assert report.current_data_complete
    assert report.total_data_complete
    assert report.available


def test_usage_percent_without_billed_cost_is_incomplete():
    from services.chatgpt_codex2api_quota import summarize_available_quota

    report = summarize_available_quota([
        {
            "email": "plus@example.com",
            "plan_type": "plus",
            "has_5h_window": True,
            "remote_status": "active",
            "usage_percent_5h": 50,
            "billed_5h": 10,
            "usage_percent_7d": 50,
        },
    ])

    assert report.current_data_complete
    assert not report.total_data_complete
    assert not report.available


def test_api_accounts_do_not_make_subscription_quota_incomplete():
    from services.chatgpt_codex2api_quota import summarize_available_quota

    report = summarize_available_quota([
        {
            "email": "api@example.com",
            "plan_type": "api",
            "remote_status": "active",
        },
        {
            "email": "plus@example.com",
            "plan_type": "plus",
            "has_5h_window": True,
            "remote_status": "active",
            "usage_percent_5h": 50,
            "billed_5h": 10,
            "usage_percent_7d": 50,
            "billed_7d": 20,
        },
        {
            "email": "unlimited@example.com",
            "plan_type": "pro",
            "has_5h_window": False,
            "remote_status": "active",
            "usage_percent_7d": 50,
            "billed_7d": 30,
        },
    ])

    assert report.current_remaining_usd == Decimal("40.00")
    assert report.total_remaining_usd == Decimal("50.00")
    assert report.current_data_complete
    assert report.total_data_complete
