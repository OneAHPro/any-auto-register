import json
from unittest.mock import Mock, patch

import pytest

from core.applemail_pool import parse_applemail_pool_content
from core.base_mailbox import AppleMailMailbox
from core.base_platform import RegisterConfig
from platforms.chatgpt.plugin import ChatGPTPlatform
from services.mail_imports.auto_detection import detect_mail_import_content
from services.mail_imports.auto_import import AutoMailImportService
from services.mail_imports.schemas import MailImportExecuteRequest
from services.mail_imports.registry import mail_import_registry


SECRET = 'JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP'


@pytest.mark.parametrize('password', ['P' * 23, 'P' * 24, 'Ab3!x' * 8, 'a' * 40, 'P' * 64])
@pytest.mark.parametrize('delimiter', ['----', '---', '\t'])
def test_password_length_does_not_turn_totp_into_mailbox_oauth(password, delimiter):
    text = delimiter.join(['fixture@gmail.com', password, SECRET])
    detection = detect_mail_import_content(text)
    assert detection.rows[0].account_type == 'chatgpt_password_totp'
    record = parse_applemail_pool_content(text)[0]
    assert record['account_type'] == detection.rows[0].account_type
    assert record['password'] == password
    assert record['totp_secret'] == SECRET
    assert 'refresh_token' not in record


def test_explicit_oauth_json_is_not_reinterpreted_as_totp():
    record = parse_applemail_pool_content(json.dumps({
        'email': 'fixture@example.com', 'client_id': 'a' * 40, 'refresh_token': SECRET,
    }))[0]
    assert record['refresh_token'] == SECRET
    assert 'totp_secret' not in record


def test_imported_long_password_reaches_login_engine_without_mailbox_wait(tmp_path):
    password = 'Ab3!x' * 8
    AutoMailImportService(mail_import_registry).execute(MailImportExecuteRequest(
        type='auto', content=f'fixture@gmail.com----{password}----{SECRET}',
        pool_dir=str(tmp_path), filename='long-password.json', bind_to_config=False,
    ))
    mailbox = AppleMailMailbox(pool_dir=str(tmp_path), pool_file='long-password.json')
    mailbox.get_current_ids = Mock(side_effect=AssertionError('Unexpected mailbox read'))
    mailbox.wait_for_code = Mock(side_effect=AssertionError('Unexpected mailbox OTP wait'))
    platform = ChatGPTPlatform(config=RegisterConfig(extra={
        'chatgpt_registration_mode': 'refresh_token',
        'chatgpt_existing_account_login_only': True,
        'chatgpt_subscription_gate_enabled': False,
    }), mailbox=mailbox)
    oauth = Mock()
    oauth.config = {}
    oauth.login_and_get_tokens.return_value = {
        'access_token': 'fixture-at', 'refresh_token': 'fixture-rt', 'account_id': 'fixture-account',
    }
    oauth.last_workspace_id = 'fixture-workspace'
    oauth._get_cookie_value.return_value = 'fixture-session'
    with patch('platforms.chatgpt.refresh_token_registration_engine.RefreshTokenRegistrationEngine._build_oauth_client', return_value=oauth), patch(
        'platforms.chatgpt.refresh_token_registration_engine.probe_chatgpt_subscription',
        return_value={'plan': 'plus', 'http_status': 200},
    ):
        account = platform.register()
    args = oauth.login_and_get_tokens.call_args
    assert args.args[1] == password
    assert args.kwargs['totp_secret'] == SECRET
    assert args.kwargs['force_password_login'] is True
    assert args.kwargs['prefer_passwordless_login'] is False
    assert account.password == password
    mailbox.get_current_ids.assert_not_called()
    mailbox.wait_for_code.assert_not_called()
