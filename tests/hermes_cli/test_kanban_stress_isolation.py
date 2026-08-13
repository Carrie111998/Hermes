from __future__ import annotations

import importlib.util
import os
from pathlib import Path


def _load_isolation_helper():
    helper_path = Path(__file__).parents[1] / "stress" / "_kanban_isolation.py"
    spec = importlib.util.spec_from_file_location("kanban_stress_isolation", helper_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_stress_isolation_replaces_inherited_live_worker_pins(
    tmp_path: Path, monkeypatch
) -> None:
    live = tmp_path / "live" / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(live))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "sdlc-base")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_live")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "42")
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", "live:claim")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACES_ROOT", str(tmp_path / "live-ws"))
    disposable = tmp_path / "disposable"

    helper = _load_isolation_helper()
    helper.isolate_kanban_env(disposable)

    assert os.environ["HERMES_HOME"] == str(disposable)
    assert os.environ["HOME"] == str(disposable)
    assert os.environ["HERMES_KANBAN_HOME"] == str(disposable)
    assert os.environ["HERMES_KANBAN_DB"] == str(disposable / "kanban.db")
    assert os.environ["HERMES_KANBAN_BOARD"] == "default"
    assert os.environ["HERMES_KANBAN_WORKSPACES_ROOT"] == str(
        disposable / "kanban" / "boards" / "default" / "workspaces"
    )
    assert "HERMES_KANBAN_TASK" not in os.environ
    assert "HERMES_KANBAN_RUN_ID" not in os.environ
    assert "HERMES_KANBAN_CLAIM_LOCK" not in os.environ
