"""Behavior tests for update/recovery venv layout resolution."""

from __future__ import annotations

import subprocess

from hermes_cli import main as cli_main
from hermes_cli import update_cmd


def test_resolve_project_venv_probes_dotvenv_then_venv(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_main, "PROJECT_ROOT", tmp_path)
    dotvenv = tmp_path / ".venv"
    legacy = tmp_path / "venv"
    dotvenv.mkdir()
    legacy.mkdir()

    assert cli_main._resolve_project_venv_dir() == dotvenv

    dotvenv.rmdir()
    assert cli_main._resolve_project_venv_dir() == legacy


def test_resolve_project_venv_keeps_legacy_creation_target(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_main, "PROJECT_ROOT", tmp_path)

    assert cli_main._resolve_project_venv_dir() == tmp_path / "venv"


def test_default_uv_install_targets_dotvenv(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_main, "PROJECT_ROOT", tmp_path)
    dotvenv = tmp_path / ".venv"
    dotvenv.mkdir()
    monkeypatch.setattr("hermes_cli.managed_uv.ensure_uv", lambda: "uv")
    monkeypatch.setattr(cli_main, "_is_termux_env", lambda *args: False)

    prefix, env = cli_main._default_venv_install_target()

    assert prefix == ["uv", "pip"]
    assert env is not None
    assert env["VIRTUAL_ENV"] == str(dotvenv)


def test_update_health_probe_uses_dotvenv_python(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_main, "PROJECT_ROOT", tmp_path)
    bin_dir = "Scripts" if cli_main._is_windows() else "bin"
    python_name = "python.exe" if cli_main._is_windows() else "python"
    python = tmp_path / ".venv" / bin_dir / python_name
    python.parent.mkdir(parents=True)
    python.write_bytes(b"")
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    # _venv_core_imports_healthy is defined in update_cmd and calls
    # subprocess.run via update_cmd's module global, so patch it there —
    # patching cli_main.subprocess has no effect on Linux CI.
    monkeypatch.setattr(update_cmd.subprocess, "run", fake_run)

    assert cli_main._venv_core_imports_healthy() == (True, "")
    assert calls[0][0] == str(python)
