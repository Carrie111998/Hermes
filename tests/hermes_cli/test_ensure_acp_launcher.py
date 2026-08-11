"""`hermes update` must self-heal the ``hermes-acp`` launcher.

ACP hosts (Zed, JetBrains, Buzz Desktop) resolve the agent by the
``hermes-acp`` command name on the login-shell PATH. Fresh installs get the
launcher from ``scripts/install.sh``; existing installs get it from
``_ensure_acp_launcher()`` during ``hermes update``.
"""

import os
import stat
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from hermes_cli.main import _ensure_acp_launcher


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    bin_dir = tmp_path / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    return bin_dir


def test_does_not_follow_symlink_into_venv(fake_home, tmp_path):
    """#21454 failure mode: never write through a symlinked hermes-acp."""
    (fake_home / "hermes").write_text("#!/bin/sh\n", encoding="utf-8")
    console_script = tmp_path / "venv" / "bin" / "hermes-acp"
    console_script.parent.mkdir(parents=True)
    marker = "#!/usr/bin/env python\n# real console script\n"
    console_script.write_text(marker, encoding="utf-8")
    (fake_home / "hermes-acp").symlink_to(console_script)

    _ensure_acp_launcher()

    assert console_script.read_text(encoding="utf-8") == marker
    assert (fake_home / "hermes-acp").is_symlink()


def test_unwritable_bin_dir_is_skipped(fake_home):
    (fake_home / "hermes").write_text("#!/bin/sh\n", encoding="utf-8")
    if os.geteuid() == 0:
        pytest.skip("root ignores directory write permissions")
    fake_home.chmod(0o555)
    try:
        _ensure_acp_launcher()  # must not raise
        assert not (fake_home / "hermes-acp").exists()
    finally:
        fake_home.chmod(0o755)


# --- Windows shim path (#83797) -------------------------------------------
#
# install.ps1 keeps the venv Scripts dir OFF the user PATH (it hosts
# python.exe/pip.exe) and ships `hermes`/`hermes-acp` as .cmd shims in
# %LOCALAPPDATA%\hermes\bin instead. `hermes update` must keep that shim in
# place, so _ensure_acp_launcher's Windows branch re-creates it.


def _cmd_encoding():
    # mbcs (the Windows ANSI code page) is what cmd.exe expects for .cmd
    # files; Linux test runners have no mbcs, so fall back to utf-8.
    return "mbcs" if os.name == "nt" else "utf-8"


def _win_env(tmp_path, monkeypatch, with_target=True):
    local = tmp_path / "local"
    scripts = local / "hermes" / "hermes-agent" / "venv" / "Scripts"
    if with_target:
        scripts.mkdir(parents=True)
        (scripts / "hermes-acp.exe").write_bytes(b"dummy")
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setattr(sys, "platform", "win32")
    return local, scripts


def _shim_bytes(scripts):
    return (
        "@echo off\r\n@\"{}\" %*\r\n".format(scripts / "hermes-acp.exe")
    ).encode(_cmd_encoding())


def test_windows_acp_shim_created(tmp_path, monkeypatch):
    local, scripts = _win_env(tmp_path, monkeypatch)

    _ensure_acp_launcher()

    shim = local / "hermes" / "bin" / "hermes-acp.cmd"
    assert shim.read_bytes() == _shim_bytes(scripts)


def test_windows_acp_shim_idempotent(tmp_path, monkeypatch):
    local, _ = _win_env(tmp_path, monkeypatch)

    _ensure_acp_launcher()
    shim = local / "hermes" / "bin" / "hermes-acp.cmd"
    first_mtime = shim.stat().st_mtime_ns

    _ensure_acp_launcher()

    assert shim.stat().st_mtime_ns == first_mtime


def test_windows_acp_shim_missing_target_is_noop(tmp_path, monkeypatch):
    local, _ = _win_env(tmp_path, monkeypatch, with_target=False)

    _ensure_acp_launcher()

    assert not (local / "hermes" / "bin").exists()


def test_windows_acp_shim_rewrites_stale_content(tmp_path, monkeypatch):
    local, scripts = _win_env(tmp_path, monkeypatch)
    stale = local / "hermes" / "bin" / "hermes-acp.cmd"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"@echo off\r\n@old-path.exe %*\r\n")

    _ensure_acp_launcher()

    assert stale.read_bytes() == _shim_bytes(scripts)
