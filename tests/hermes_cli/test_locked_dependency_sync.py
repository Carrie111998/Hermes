"""Locked dependency-sync coverage for update and recovery paths."""

from __future__ import annotations

import contextlib
import subprocess


def _prepare_main(tmp_path, monkeypatch, *, lock: bool = True):
    from hermes_cli import main as hm

    monkeypatch.setattr(hm, "PROJECT_ROOT", tmp_path)
    if lock:
        (tmp_path / "uv.lock").write_text("# lock\n")
    monkeypatch.setattr(hm, "_is_termux_env", lambda env=None: False)
    monkeypatch.setattr(
        hm, "_verify_core_dependencies_installed", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        hm, "_verify_console_scripts_installed", lambda *args, **kwargs: None
    )
    return hm


def test_uv_installer_prefers_locked_inexact_sync(tmp_path, monkeypatch):
    hm = _prepare_main(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(
        hm,
        "_run_quarantined_install",
        lambda cmd, **kwargs: calls.append((cmd, kwargs["env"])),
    )

    hm._install_python_dependencies_with_optional_fallback(["/usr/bin/uv", "pip"])

    assert len(calls) == 1
    cmd, env = calls[0]
    assert cmd == [
        "/usr/bin/uv",
        "sync",
        "--locked",
        "--inexact",
        "--extra",
        "all",
    ]
    assert env["UV_PROJECT_ENVIRONMENT"] == str(tmp_path / "venv")
    assert env["VIRTUAL_ENV"] == str(tmp_path / "venv")


def test_sync_failure_uses_editable_fallback(tmp_path, monkeypatch):
    hm = _prepare_main(tmp_path, monkeypatch)
    calls = []

    def fake_install(cmd, **kwargs):
        calls.append(cmd)
        if cmd[1] == "sync":
            raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(hm, "_run_quarantined_install", fake_install)

    hm._install_python_dependencies_with_optional_fallback(["/usr/bin/uv", "pip"])

    assert calls[0][1] == "sync"
    assert calls[1] == ["/usr/bin/uv", "pip", "install", "-e", ".[all]"]


def test_uv_environment_uses_existing_canonical_venv(tmp_path, monkeypatch):
    hm = _prepare_main(tmp_path, monkeypatch)
    python = tmp_path / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!fake\n")

    env = hm._project_uv_install_env({"UV_PYTHON": "/host/python"})

    assert env["UV_PROJECT_ENVIRONMENT"] == str(tmp_path / ".venv")
    assert env["VIRTUAL_ENV"] == str(tmp_path / ".venv")
    assert env["UV_PYTHON"] == str(python)


def test_uv_environment_drops_host_python_when_creating_venv(tmp_path, monkeypatch):
    hm = _prepare_main(tmp_path, monkeypatch)

    env = hm._project_uv_install_env({"UV_PYTHON": "/host/python"})

    assert "UV_PYTHON" not in env


def test_termux_keeps_curated_pip_tier(tmp_path, monkeypatch):
    hm = _prepare_main(tmp_path, monkeypatch)
    monkeypatch.setattr(hm, "_is_termux_env", lambda env=None: True)
    calls = []
    monkeypatch.setattr(
        hm,
        "_run_quarantined_install",
        lambda cmd, **kwargs: calls.append((cmd, kwargs["env"])),
    )

    hm._install_python_dependencies_with_optional_fallback(
        ["/usr/bin/uv", "pip"],
        env={
            "PREFIX": "/data/data/com.termux/files/usr",
            "UV_PYTHON": "/host/python",
        },
        group="termux-all",
    )

    cmd, env = calls[0]
    assert cmd == [
        "/usr/bin/uv",
        "pip",
        "install",
        "-e",
        ".[termux-all]",
    ]
    assert env["PREFIX"] == "/data/data/com.termux/files/usr"
    assert "UV_PYTHON" not in env


def test_plain_pip_skips_lockfile_sync(tmp_path, monkeypatch):
    hm = _prepare_main(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(
        hm, "_run_quarantined_install", lambda cmd, **kwargs: calls.append(cmd)
    )

    hm._install_python_dependencies_with_optional_fallback(
        ["/venv/python", "-m", "pip"]
    )

    assert calls == [["/venv/python", "-m", "pip", "install", "-e", ".[all]"]]


def test_early_recovery_prefers_locked_sync(tmp_path, monkeypatch):
    from hermes_cli import _install_repair as ir

    (tmp_path / "uv.lock").write_text("# lock\n")
    env = {
        "UV_PROJECT_ENVIRONMENT": str(tmp_path / "venv"),
        "VIRTUAL_ENV": str(tmp_path / "venv"),
    }
    monkeypatch.setattr(ir, "_resolve_install_target", lambda root: (["/uv", "pip"], env))
    monkeypatch.setattr(ir, "_stdout_to_stderr", contextlib.nullcontext)
    monkeypatch.setattr(ir.subprocess, "run", lambda *args, **kwargs: None)
    calls = []
    monkeypatch.setattr(
        ir,
        "_run_install_cmd",
        lambda cmd, **kwargs: calls.append((cmd, kwargs["env"])),
    )

    ir.run_core_install(tmp_path)

    assert calls == [
        (
            ["/uv", "sync", "--locked", "--inexact", "--extra", "all"],
            env,
        )
    ]


def test_early_recovery_sync_failure_falls_back(tmp_path, monkeypatch):
    from hermes_cli import _install_repair as ir

    (tmp_path / "uv.lock").write_text("# lock\n")
    monkeypatch.setattr(ir, "_resolve_install_target", lambda root: (["/uv", "pip"], {}))
    monkeypatch.setattr(ir, "_stdout_to_stderr", contextlib.nullcontext)
    monkeypatch.setattr(ir.subprocess, "run", lambda *args, **kwargs: None)
    calls = []

    def fake_install(cmd, **kwargs):
        calls.append(cmd)
        if cmd[1] == "sync":
            raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(ir, "_run_install_cmd", fake_install)

    ir.run_core_install(tmp_path)

    assert calls[:2] == [
        ["/uv", "sync", "--locked", "--inexact", "--extra", "all"],
        ["/uv", "pip", "install", "-e", ".[all]"],
    ]
