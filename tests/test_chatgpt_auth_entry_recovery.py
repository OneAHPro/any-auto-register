import base64
import json
from unittest.mock import Mock, patch

import pytest

from core.task_runtime import StopTaskRequested, RegisterTaskControl, bind_task_attempt_context
from platforms.chatgpt.auth_entry_errors import auth_entry_error, response_retry_after
from platforms.chatgpt.chatgpt_client import ChatGPTClient
from platforms.chatgpt.oauth_client import OAuthClient
from platforms.chatgpt.utils import FlowState


def error_url(code='rate_limit_exceeded'):
    payload = base64.b64encode(json.dumps({
        'kind': 'AuthApiFailure', 'errorCode': code,
        'retryUrl': 'https://chatgpt.com/auth/login_with?callback_path=/',
    }).encode()).decode()
    return 'https://auth.openai.com/error?payload=' + payload + '&session_id=SECRET'


def client_for_entry(urls, *, cookie='login-session'):
    client = ChatGPTClient(verbose=False)
    client.visit_homepage = Mock(return_value=True)
    client.get_csrf_token = Mock(return_value='csrf')
    client.signin = Mock(return_value='https://auth.openai.com/api/accounts/authorize')
    client.authorize = Mock(side_effect=urls)
    client.last_authorize_status = 200
    client._get_cookie_value = Mock(return_value=cookie)
    client.fetch_chatgpt_session = Mock(return_value=(True, {'accessToken': 'fixture-at'}))
    client.get_next_auth_session_token = Mock(return_value='fixture-session')
    return client


def login(client):
    return client.login_existing_account_and_get_session(
        'fixture@example.com', None, password='fixture-password', prepare_phone_oauth=False,
    )


def test_password_page_without_legacy_cookie_continues_existing_transaction():
    client = client_for_entry(['https://auth.openai.com/log-in/password'] * 6, cookie='')
    with patch.object(OAuthClient, '_submit_password_verify', return_value=FlowState(
        page_type='add_phone', current_url='https://auth.openai.com/add-phone',
    )) as password, patch.object(OAuthClient, '_bootstrap_oauth_session', return_value='') as bootstrap:
        ok, result = login(client)
    assert ok and result['access_token'] == 'fixture-at'
    password.assert_called_once()
    bootstrap.assert_not_called()
    assert client.authorize.call_count == 1


def test_rate_limit_redirect_waits_then_recovers_without_resetting_browser():
    client = client_for_entry([error_url(), 'https://auth.openai.com/add-phone'])
    with patch.object(client, '_wait_login_entry_retry', create=True) as wait, patch.object(client, '_reset_session') as reset:
        ok, result = login(client)
    assert ok and result['access_token'] == 'fixture-at'
    wait.assert_called_once_with(30)
    reset.assert_not_called()


def test_repeated_rate_limit_is_bounded_and_reports_real_error():
    client = client_for_entry([error_url()] * 6)
    with patch.object(client, '_wait_login_entry_retry', create=True) as wait:
        ok, message = login(client)
    assert not ok
    assert 'rate_limit_exceeded' in message and '限流' in message
    assert 'SECRET' not in message and 'payload=' not in message
    assert client.authorize.call_count == 3
    assert [x.args[0] for x in wait.call_args_list] == [30, 60]
    assert str(client.last_auth_outcome.domain) == 'rate_limit'
    assert not client.last_auth_outcome.credential_rejected


@pytest.mark.parametrize('retry_after,expected_calls', [(90, 2), (600, 1)])
def test_retry_after_is_respected_without_waiting_unboundedly(retry_after, expected_calls):
    client = client_for_entry([error_url(), 'https://auth.openai.com/add-phone'])
    client.last_authorize_retry_after = retry_after
    with patch.object(client, '_wait_login_entry_retry', create=True) as wait:
        login(client)
    assert client.authorize.call_count == expected_calls
    if expected_calls == 2:
        wait.assert_called_once_with(90)
    else:
        wait.assert_not_called()


@pytest.mark.parametrize('url', [error_url('account_deactivated'), 'https://auth.openai.com/error?payload=bad'])
def test_non_transient_error_does_not_restart_or_expose_query(url):
    client = client_for_entry([url])
    ok, message = login(client)
    assert not ok and client.authorize.call_count == 1
    assert 'payload=' not in message and 'SECRET' not in message
    assert '未支持' not in message


def test_stop_during_entry_cooldown_prevents_another_request():
    client = client_for_entry([error_url()])
    control = RegisterTaskControl()
    attempt_id = control.start_attempt()
    with bind_task_attempt_context(control, attempt_id), patch(
        'platforms.chatgpt.chatgpt_client.time.sleep', side_effect=lambda _: control.request_stop(),
    ) as sleep:
        with pytest.raises(StopTaskRequested):
            login(client)
    assert client.authorize.call_count == 1
    sleep.assert_called_once_with(0.25)


def test_oauth_bootstrap_does_not_fallback_after_rate_limit():
    helper = OAuthClient({}, verbose=False)
    helper.session.get = Mock(return_value=Mock(
        status_code=200, url=error_url(), headers={'Retry-After': '90'}, history=[],
    ))
    assert not helper._bootstrap_oauth_session('https://auth.openai.com/oauth/authorize', {})
    assert helper.session.get.call_count == 1
    assert 'rate_limit_exceeded' in helper.last_error


def test_fallback_password_page_becomes_the_current_flow_state():
    client = client_for_entry(['https://auth.openai.com/api/accounts/authorize'], cookie='')
    with patch.object(OAuthClient, '_bootstrap_oauth_session', return_value='https://auth.openai.com/log-in/password'), patch.object(
        OAuthClient, '_get_cookie_value', return_value='login-session',
    ), patch.object(OAuthClient, '_submit_password_verify', return_value=FlowState(
        page_type='add_phone', current_url='https://auth.openai.com/add-phone',
    )) as password, patch.object(OAuthClient, '_submit_authorize_continue', return_value=FlowState(page_type='add_phone')) as email:
        ok, _ = login(client)
    assert ok
    password.assert_called_once()
    email.assert_not_called()


@pytest.mark.parametrize('url,status,code', [
    (error_url(), 200, 'rate_limit_exceeded'),
    ('https://auth.openai.com/oauth/authorize', 429, 'rate_limit_exceeded'),
    ('https://auth.openai.com/error?payload=W10=', 200, 'unknown_auth_error'),
    ('https://auth.openai.com/error?payload=invalid', 200, 'unknown_auth_error'),
    ('https://auth.openai.com/log-in/password', 503, 'temporarily_unavailable'),
    ('https://example.com/error?payload=invalid', 200, None),
])
def test_error_classification_handles_http_errors_and_malformed_payload(url, status, code):
    result = auth_entry_error(url, status)
    assert (result.code if result else None) == code


def test_authorize_captures_redirect_retry_after_and_redacts_error_query():
    client = ChatGPTClient(verbose=False)
    logs = []
    client._log = logs.append
    client.session.get = Mock(return_value=Mock(
        status_code=200, url=error_url(), headers={},
        history=[Mock(headers={'Retry-After': '90'})],
    ))
    client.authorize('https://auth.openai.com/oauth/authorize')
    assert client.last_authorize_retry_after == 90
    assert all('payload=' not in x and 'SECRET' not in x for x in logs)


@pytest.mark.parametrize('raw', ['nan', 'inf', '-10', 'invalid'])
def test_invalid_retry_after_is_ignored(raw):
    assert response_retry_after(Mock(headers={'Retry-After': raw}, history=[])) == 0


def test_web_login_recovers_when_bootstrap_fallback_is_rate_limited():
    client = client_for_entry([
        'https://auth.openai.com/api/accounts/authorize', 'https://auth.openai.com/add-phone',
    ], cookie='')

    def bootstrap(helper, *args, **kwargs):
        helper.last_entry_error = auth_entry_error(error_url())
        helper.last_entry_retry_after = 90
        client._get_cookie_value.return_value = 'login-session'
        return ''

    with patch.object(OAuthClient, '_bootstrap_oauth_session', autospec=True, side_effect=bootstrap), patch.object(
        client, '_wait_login_entry_retry',
    ) as wait:
        ok, _ = login(client)
    assert ok
    wait.assert_called_once_with(90)


def test_oauth_bootstrap_preserves_password_page_without_legacy_cookie():
    helper = OAuthClient({}, verbose=False)
    helper.session.get = Mock(return_value=Mock(
        status_code=200, url='https://auth.openai.com/log-in/password', headers={}, history=[],
    ))
    assert helper._bootstrap_oauth_session(
        'https://auth.openai.com/oauth/authorize', {}, allow_password_entry=True,
    ) == 'https://auth.openai.com/log-in/password'
    assert helper.session.get.call_count == 1
