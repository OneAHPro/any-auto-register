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
