from __future__ import annotations

import importlib
from pathlib import Path
import sys

from camoufox.addons import DefaultAddons

from services import solver_manager


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOLVER_DIR = PROJECT_ROOT / "services" / "turnstile_solver"


def _load_api_solver(monkeypatch):
    monkeypatch.syspath_prepend(str(SOLVER_DIR))
    sys.modules.pop("api_solver", None)
    return importlib.import_module("api_solver")


def test_camoufox_solver_excludes_optional_default_addon(monkeypatch):
    api_solver = _load_api_solver(monkeypatch)

    options = api_solver._camoufox_launch_options(headless=True)

    assert options["headless"] is True
    assert options["exclude_addons"] == [DefaultAddons.UBO]


def test_solver_log_defaults_to_runtime_data_directory(monkeypatch, tmp_path):
    monkeypatch.delenv("SOLVER_LOG_PATH", raising=False)
    monkeypatch.setenv("APP_RUNTIME_DIR", str(tmp_path))

    assert solver_manager._solver_log_path() == tmp_path / "logs" / "solver.log"


def test_solver_log_path_can_be_overridden(monkeypatch, tmp_path):
    expected = tmp_path / "custom" / "solver.log"
    monkeypatch.setenv("SOLVER_LOG_PATH", str(expected))

    assert solver_manager._solver_log_path() == expected
