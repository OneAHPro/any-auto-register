from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_logrotate_can_read_systemd_owned_application_logs():
    config = (
        PROJECT_ROOT / "deploy" / "logrotate" / "any-auto-register"
    ).read_text(encoding="utf-8")
    application_stanza = config.split(
        "/www/any-auto-register/shared/logs/nginx-access.log",
        maxsplit=1,
    )[0]

    assert "su root any-auto-register" in application_stanza


def test_nginx_terminates_https_for_accounts_domain():
    config = (
        PROJECT_ROOT / "deploy" / "nginx" / "accounts.anhepro.com.conf"
    ).read_text(encoding="utf-8")

    assert "return 301 https://$host$request_uri;" in config
    assert "listen 443 ssl http2;" in config
    assert (
        "ssl_certificate "
        "/etc/letsencrypt/live/accounts.anhepro.com/fullchain.pem;"
    ) in config
    assert (
        "ssl_certificate_key "
        "/etc/letsencrypt/live/accounts.anhepro.com/privkey.pem;"
    ) in config
