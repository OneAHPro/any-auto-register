import json
from unittest import mock

import pytest
from fastapi import HTTPException

from api import accounts as accounts_api


def _result(
    account_id,
    *,
    ok=True,
    status="deleted",
    local_deleted=True,
    remote_status="deleted",
    remote_id=71,
    error_code="",
    message="账号已删除",
):
    remote = {"enabled": remote_status not in {"skipped_disabled", "not_applicable"}, "status": remote_status}
    if remote_id is not None:
        remote["remote_id"] = remote_id
    return {
        "ok": ok,
        "account_id": account_id,
        "status": status,
        "local_deleted": local_deleted,
        "codex2api": remote,
        "error_code": error_code,
        "message": message,
    }


def _response_json(response):
    return json.loads(response.body.decode("utf-8"))


def test_single_delete_returns_structured_service_success():
    session = mock.Mock()
    bind = object()
    session.get_bind.return_value = bind
    expected = _result(17)

    with mock.patch(
        "api.accounts.remove_account",
        return_value=expected,
    ) as remove:
        result = accounts_api.delete_account(17, session=session)

    assert result == expected
    remove.assert_called_once_with(17, database_engine=bind)
    session.delete.assert_not_called()
    session.commit.assert_not_called()


@pytest.mark.parametrize(
    ("service_status", "expected_http"),
    [
        ("not_found", 404),
        ("busy", 409),
        ("local_delete_conflict", 409),
        ("remote_failed", 502),
        ("database_error", 500),
    ],
)
def test_single_delete_maps_service_failures_to_http_status(
    service_status,
    expected_http,
):
    session = mock.Mock()
    failed = _result(
        18,
        ok=False,
        status=service_status,
        local_deleted=False,
        remote_status="failed",
        remote_id=None,
        error_code="delete_failed",
        message="删除未完成",
    )
    with mock.patch("api.accounts.remove_account", return_value=failed):
        response = accounts_api.delete_account(18, session=session)

    assert response.status_code == expected_http
    payload = _response_json(response)
    assert payload["detail"] == "删除未完成"
    assert payload["status"] == service_status
    assert payload["local_deleted"] is False


def test_batch_delete_deduplicates_and_reports_partial_results():
    session = mock.Mock()
    bind = object()
    session.get_bind.return_value = bind
    by_id = {
        1: _result(1, remote_status="deleted", remote_id=71),
        2: _result(
            2,
            ok=False,
            status="remote_failed",
            local_deleted=False,
            remote_status="ambiguous",
            remote_id=None,
            error_code="remote_ambiguous",
            message="远端认证不唯一",
        ),
        3: _result(
            3,
            ok=False,
            status="not_found",
            local_deleted=False,
            remote_status="not_applicable",
            remote_id=None,
            error_code="not_found",
            message="账号不存在",
        ),
    }

    with mock.patch(
        "api.accounts.remove_account",
        side_effect=lambda account_id, **_kwargs: by_id[account_id],
    ) as remove:
        response = accounts_api.batch_delete_accounts(
            accounts_api.BatchDeleteRequest(ids=[1, 2, 1, 3]),
            session=session,
        )

    assert response == {
        "total_requested": 4,
        "total_unique": 3,
        "deleted": 1,
        "failed": 1,
        "not_found": [3],
        "remote_deleted": 1,
        "remote_already_absent": 0,
        "remote_skipped": 0,
        "items": [by_id[1], by_id[2], by_id[3]],
    }
    assert [call.args[0] for call in remove.call_args_list] == [1, 2, 3]
    assert all(call.kwargs == {"database_engine": bind} for call in remove.call_args_list)
    session.commit.assert_not_called()
    session.rollback.assert_not_called()


def test_batch_delete_counts_local_only_and_already_absent_remote_successes():
    session = mock.Mock()
    results = {
        4: _result(
            4,
            remote_status="skipped_disabled",
            remote_id=None,
        ),
        5: _result(
            5,
            remote_status="already_absent",
            remote_id=75,
        ),
        6: _result(
            6,
            remote_status="not_applicable",
            remote_id=None,
        ),
    }
    with mock.patch(
        "api.accounts.remove_account",
        side_effect=lambda account_id, **_kwargs: results[account_id],
    ):
        response = accounts_api.batch_delete_accounts(
            accounts_api.BatchDeleteRequest(ids=[4, 5, 6]),
            session=session,
        )

    assert response["deleted"] == 3
    assert response["failed"] == 0
    assert response["remote_deleted"] == 0
    assert response["remote_already_absent"] == 1
    assert response["remote_skipped"] == 2


def test_batch_unexpected_failure_does_not_rollback_prior_success():
    session = mock.Mock()

    def remove(account_id, **_kwargs):
        if account_id == 1:
            return _result(1)
        raise RuntimeError("database exploded with at-private-secret")

    with mock.patch("api.accounts.remove_account", side_effect=remove):
        response = accounts_api.batch_delete_accounts(
            accounts_api.BatchDeleteRequest(ids=[1, 2]),
            session=session,
        )

    assert response["deleted"] == 1
    assert response["failed"] == 1
    assert response["items"][1]["status"] == "database_error"
    assert "at-private-secret" not in json.dumps(response)
    session.rollback.assert_not_called()


@pytest.mark.parametrize("ids", [[], list(range(1001))])
def test_batch_delete_preserves_request_size_validation(ids):
    with pytest.raises(HTTPException) as error:
        accounts_api.batch_delete_accounts(
            accounts_api.BatchDeleteRequest(ids=ids),
            session=mock.Mock(),
        )

    assert error.value.status_code == 400
