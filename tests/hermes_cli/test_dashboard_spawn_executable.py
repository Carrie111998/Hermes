"""Detached dashboard action runtime regression coverage (#90026/#90030)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import hermes_cli.web_server as web_server
from hermes_cli.runtime_launch import detached_python_env, resolve_project_python


def _touch_python(root: Path, *parts: str) -> Path:
    candidate = root.joinpath(*parts)
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.touch()
    return candidate


def test_same_project_interpreter_is_returned_verbatim(tmp_path):
    venv_python = _touch_python(tmp_path, "venv", "bin", "python")

    assert (
        resolve_project_python(tmp_path, current_executable=str(venv_python))
        == str(venv_python)
    )


def test_uv_base_interpreter_is_replaced_by_project_venv(tmp_path):
    venv_python = _touch_python(tmp_path, "venv", "bin", "python")
    base_python = tmp_path / "uv-base" / "python"

    assert resolve_project_python(
        tmp_path, current_executable=base_python
    ) == str(venv_python)


def test_windows_project_venv_layout_is_supported(tmp_path):
    venv_python = _touch_python(tmp_path, "venv", "Scripts", "python.exe")

    assert resolve_project_python(
        tmp_path, current_executable=tmp_path / "uv-base" / "python.exe"
    ) == str(venv_python)


def test_dot_venv_layout_is_supported(tmp_path):
    venv_python = _touch_python(tmp_path, ".venv", "bin", "python")

    assert resolve_project_python(
        tmp_path, current_executable=tmp_path / "uv-base" / "python"
    ) == str(venv_python)


def test_missing_project_venv_preserves_current_interpreter(tmp_path):
    current = tmp_path / "exotic-runtime" / "python"

    assert resolve_project_python(tmp_path, current_executable=current) == str(current)


def test_posix_venv_symlink_is_launched_without_resolving_it(tmp_path):
    if os.name == "nt":
        return

    base_python = _touch_python(tmp_path, "base", "python")
    venv_python = tmp_path / "venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.symlink_to(base_python)

    # Resolving this symlink would launch the base interpreter and lose the
    # adjacent pyvenv.cfg.  The venv path itself is the runtime contract.
    assert resolve_project_python(
        tmp_path, current_executable=base_python
    ) == str(venv_python)


def test_runtime_pythonpath_keeps_only_absolute_injected_roots(tmp_path):
    prefix = tmp_path / "base-python"
    baseline = prefix / "lib" / "python3.11"
    injected = tmp_path / "venv" / "lib" / "python3.11" / "site-packages"
    existing = str(tmp_path / "operator-path")

    env = detached_python_env(
        {"PYTHONPATH": existing, "KEEP": "yes"},
        runtime_paths=("", "relative", baseline, injected, injected),
        interpreter_prefixes=(prefix,),
    )

    assert env["KEEP"] == "yes"
    assert env["PYTHONPATH"].split(os.pathsep) == [str(injected), existing]


def test_spawn_uses_project_venv_and_preserves_runtime_paths(monkeypatch, tmp_path):
    venv_python = _touch_python(tmp_path, "venv", "bin", "python")
    injected = tmp_path / "injected-runtime"
    existing = str(tmp_path / "existing-pythonpath")
    captured = {}

    class FakeProc:
        pid = 1234

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs["env"]
        return FakeProc()

    monkeypatch.setattr(web_server, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(web_server, "_ACTION_LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(web_server, "_ACTION_PROCS", {})
    monkeypatch.setattr(web_server.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(web_server.sys, "executable", str(tmp_path / "uv-base" / "python"))
    monkeypatch.setattr(web_server.sys, "path", [str(injected), str(Path(sys.prefix) / "lib")])
    monkeypatch.setenv("PYTHONPATH", existing)

    web_server._spawn_hermes_action(["gateway", "restart"], "gateway-restart")

    assert captured["cmd"][:3] == [str(venv_python), "-m", "hermes_cli.main"]
    assert captured["env"]["PYTHONPATH"].split(os.pathsep) == [str(injected), existing]
