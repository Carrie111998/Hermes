"""Optional dependency targeting at the canary transaction boundary."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import memory_setup, update_cmd
from tools import lazy_deps


def test_windows_external_coordinator_captures_with_project_venv(
    tmp_path: Path, monkeypatch
):
    project = tmp_path / "project"
    interpreter = project / ".venv" / "Scripts" / "python.exe"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_bytes(b"placeholder")
    external_python = tmp_path / "coordinator" / "python.exe"
    monkeypatch.setattr(sys, "executable", str(external_python))
    monkeypatch.setattr(update_cmd._m(), "_is_windows", lambda: True)
    monkeypatch.setenv("PYTHONHOME", "wrong-runtime")
    monkeypatch.setenv("PYTHONPATH", "wrong-imports")

    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "lazy_features": ["platform.telegram", "platform.discord"],
                    "tool_dependencies": ["langfuse"],
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(update_cmd.subprocess, "run", run)

    captured = update_cmd._capture_rollout_active_optional_dependencies(project)

    assert captured == (
        ["platform.telegram", "platform.discord"],
        ["langfuse"],
    )
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command[0] == str(interpreter)
    assert command[0] != str(external_python)
    assert command[1:4] == ["-I", "-B", "-c"]
    assert command[-1] == str(project.resolve())
    assert kwargs["cwd"] == project.resolve()
    assert "PYTHONHOME" not in kwargs["env"]
    assert "PYTHONPATH" not in kwargs["env"]
    assert kwargs["env"]["VIRTUAL_ENV"] == str(project / ".venv")


def test_absent_project_venv_has_empty_capture_and_forces_full_install(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(
        update_cmd.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("an absent venv must not be probed"),
    )

    assert update_cmd._capture_rollout_active_optional_dependencies(tmp_path) == (
        [],
        [],
    )
    assert not update_cmd._rollout_dependency_install_is_current(
        True,
        {"dependency_state": {"venv_present": False}},
        project_root=tmp_path,
    )


def test_existing_project_venv_probe_failure_refuses_empty_snapshot(
    tmp_path: Path, monkeypatch
):
    interpreter = tmp_path / "venv" / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_bytes(b"placeholder")
    monkeypatch.setattr(update_cmd._m(), "_is_windows", lambda: False)
    monkeypatch.setattr(
        update_cmd.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="probe import failed",
        ),
    )

    with pytest.raises(RuntimeError, match="probe import failed"):
        update_cmd._capture_rollout_active_optional_dependencies(tmp_path)


def test_explicit_lazy_restore_uses_exact_target_command_and_environment(
    tmp_path: Path, monkeypatch
):
    uv = tmp_path / "managed" / "uv.exe"
    target_venv = tmp_path / "project" / "venv"
    env = {"VIRTUAL_ENV": str(target_venv), "UV_PYTHON": "managed"}
    calls = []

    monkeypatch.setattr(lazy_deps, "_allow_lazy_installs", lambda: True)
    monkeypatch.setattr(
        lazy_deps,
        "_venv_pip_install",
        lambda *args, **kwargs: pytest.fail(
            "explicit restore must not select the coordinator interpreter"
        ),
    )

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    monkeypatch.setattr(lazy_deps.subprocess, "run", run)

    result = lazy_deps.restore_features(
        ["platform.telegram"],
        install_cmd_prefix=[str(uv), "pip"],
        env=env,
    )

    assert result == {"platform.telegram": "restored"}
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command == [
        str(uv),
        "pip",
        "install",
        *lazy_deps.LAZY_DEPS["platform.telegram"],
    ]
    assert kwargs["env"] is env


def test_explicit_lazy_restore_keeps_security_opt_out(monkeypatch):
    monkeypatch.delenv("HERMES_DISABLE_LAZY_INSTALLS", raising=False)
    monkeypatch.setattr(lazy_deps, "_allow_lazy_installs", lambda: False)
    monkeypatch.setattr(
        lazy_deps.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("security gate must run before pip"),
    )

    result = lazy_deps.restore_features(
        ["platform.discord"],
        install_cmd_prefix=["target-uv", "pip"],
        env={"VIRTUAL_ENV": "target-venv"},
    )

    assert result["platform.discord"].startswith("skipped:")
    assert "allow_lazy_installs=false" in result["platform.discord"]


def test_update_lazy_refresh_selects_explicit_installer(monkeypatch):
    calls = []

    def restore(features, *, install_cmd_prefix=None, env=None):
        calls.append((features, install_cmd_prefix, env))
        return {"platform.discord": "restored"}

    monkeypatch.setattr(lazy_deps, "restore_features", restore)
    env = {"VIRTUAL_ENV": "C:/Hermes/venv"}

    assert update_cmd._refresh_active_lazy_features(
        ["C:/Hermes/uv.exe", "pip"],
        env=env,
        features=["platform.discord"],
        explicit_target=True,
    )
    assert calls == [
        (
            ["platform.discord"],
            ["C:/Hermes/uv.exe", "pip"],
            env,
        )
    ]


def test_memory_provider_refresh_forwards_transaction_installer(
    monkeypatch,
):
    calls = []
    env = {"VIRTUAL_ENV": "C:/Hermes/venv"}
    prefix = ["C:/Hermes/uv.exe", "pip"]
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"memory": {"provider": "mem0"}},
    )

    def install(provider, **kwargs):
        calls.append((provider, kwargs))

    monkeypatch.setattr(memory_setup, "_install_dependencies", install)

    update_cmd._refresh_active_memory_provider_dependencies(prefix, env=env)

    assert calls == [
        (
            "mem0",
            {
                "force": True,
                "install_cmd_prefix": prefix,
                "env": env,
            },
        )
    ]


def test_memory_provider_install_uses_exact_target_command_and_environment(
    tmp_path: Path, monkeypatch
):
    import yaml

    plugin_dir = tmp_path / "mem0"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.yaml").write_text(
        yaml.safe_dump({"pip_dependencies": ["mem0ai>=2.0.10,<3"]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "plugins.memory.find_provider_dir", lambda provider: plugin_dir
    )
    monkeypatch.setattr(lazy_deps, "_allow_lazy_installs", lambda: True)
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    monkeypatch.setattr(lazy_deps.subprocess, "run", run)
    prefix = [str(tmp_path / "uv.exe"), "pip"]
    env = {"VIRTUAL_ENV": str(tmp_path / "project" / "venv")}

    memory_setup._install_dependencies(
        "mem0",
        force=True,
        install_cmd_prefix=prefix,
        env=env,
    )

    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command == prefix + ["install", "mem0ai>=2.0.10,<3"]
    assert kwargs["env"] is env
    assert str(sys.executable) not in command
