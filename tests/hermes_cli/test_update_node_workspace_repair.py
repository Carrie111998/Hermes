"""Regression tests for zero-commit Node workspace repair."""

from unittest.mock import patch

import pytest

import hermes_constants
from hermes_cli import main as hm


def _prepare_project(tmp_path, monkeypatch):
    monkeypatch.setattr(hm, "PROJECT_ROOT", tmp_path)
    (tmp_path / "package.json").write_text("{}")
    monkeypatch.setattr(hermes_constants, "get_default_hermes_root", lambda: tmp_path)


def test_zero_commit_node_repair_skips_healthy_install(
    tmp_path, monkeypatch, capsys
):
    _prepare_project(tmp_path, monkeypatch)

    with (
        patch.object(hm, "_npm_lockfile_changed", return_value=False),
        patch.object(hm, "_update_node_dependencies") as update,
    ):
        assert hm._repair_node_dependencies_if_needed() is False

    update.assert_not_called()
    assert capsys.readouterr().out == ""


def test_zero_commit_node_repair_runs_when_workspace_install_is_unhealthy(
    tmp_path, monkeypatch, capsys
):
    _prepare_project(tmp_path, monkeypatch)

    with (
        patch.object(hm, "_npm_lockfile_changed", return_value=True),
        patch.object(hm, "_update_node_dependencies", return_value=[]) as update,
    ):
        assert hm._repair_node_dependencies_if_needed() is True

    update.assert_called_once_with()
    out = capsys.readouterr().out
    assert "Repairing Node.js workspace dependencies" in out
    assert "Node.js workspace dependencies repaired" in out


def test_zero_commit_node_repair_exits_nonzero_on_install_failure(
    tmp_path, monkeypatch, capsys
):
    _prepare_project(tmp_path, monkeypatch)

    with (
        patch.object(hm, "_npm_lockfile_changed", return_value=True),
        patch.object(
            hm,
            "_update_node_dependencies",
            return_value=["ui-tui, @hermes/ink, web workspaces"],
        ),
        pytest.raises(SystemExit) as exc,
    ):
        hm._repair_node_dependencies_if_needed()

    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "dependency repair failed" in out
    assert "re-run: hermes update" in out
