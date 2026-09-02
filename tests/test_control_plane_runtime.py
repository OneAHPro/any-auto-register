from unittest import mock


class InlineExecutor:
    def submit(self, callback):
        callback()


def test_pending_migration_recovery_runs_when_scheduler_is_disabled(monkeypatch):
    from services import control_plane_runtime as runtime

    recover = mock.Mock(return_value=[])
    monkeypatch.setattr(runtime, "_EXECUTOR", InlineExecutor())
    monkeypatch.setattr(runtime, "_enabled", lambda: False)
    monkeypatch.setattr(runtime, "run_pending_migrations", recover)
    runtime._RUNNING_JOBS.clear()

    assert runtime.wake_pending_migrations() is True
    recover.assert_called_once_with()
    assert "pending_migrations" not in runtime._RUNNING_JOBS


def test_control_plane_router_is_registered_on_main_app():
    from main import app

    paths = set(app.openapi()["paths"])

    assert "/api/codex2api/targets" in paths
    assert "/api/scheduler/apply" in paths
