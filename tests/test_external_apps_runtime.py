from __future__ import annotations

import importlib
import sys


def test_external_app_storage_uses_runtime_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_RUNTIME_DIR", str(tmp_path))
    sys.modules.pop("services.external_apps", None)

    external_apps = importlib.import_module("services.external_apps")

    assert external_apps._EXT_ROOT == tmp_path / "external_apps"
    assert external_apps._LOG_ROOT == tmp_path / "logs" / "external_apps"
    assert external_apps._LOG_ROOT.is_dir()
