from __future__ import annotations

from pathlib import Path

import pytest

from harness_logging import harness_log_path, harness_state_dir, harness_state_path


def test_harness_log_path_uses_profile_home(monkeypatch, tmp_path: Path) -> None:
    profile_home = tmp_path / "profile"
    monkeypatch.setenv("HERMES_HOME", str(profile_home))

    log_path = harness_log_path("module.log")

    assert log_path == profile_home / "logs" / "module.log"
    assert log_path.parent.is_dir()


def test_harness_log_path_rejects_nested_paths(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    with pytest.raises(ValueError, match="must not contain a directory"):
        harness_log_path("nested/module.log")


def test_harness_state_paths_stay_under_profile_home(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    resonance_dir = harness_state_dir("resonance")
    graph_path = harness_state_path("knowledge", "graph.json")

    assert resonance_dir == tmp_path / "harness" / "resonance"
    assert resonance_dir.is_dir()
    assert graph_path == tmp_path / "harness" / "knowledge" / "graph.json"
    assert graph_path.parent.is_dir()


def test_harness_state_path_rejects_parent_traversal(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    with pytest.raises(ValueError, match="filename components only"):
        harness_state_path("..", "outside.json")
