"""Behavior tests for the Desktop build's cross-process lock."""

from __future__ import annotations

import argparse
import subprocess
import sys
from unittest.mock import patch

import pytest

from hermes_cli import main as cli_main
from hermes_cli.desktop_build_lock import DesktopBuildLock


def test_desktop_build_lock_is_exclusive_and_reacquirable(tmp_path):
    first = DesktopBuildLock(tmp_path)
    contender = DesktopBuildLock(tmp_path)

    assert first.acquire() is True
    try:
        assert contender.acquire() is False
    finally:
        first.release()

    assert contender.acquire() is True
    contender.release()


def test_desktop_build_lock_excludes_another_process(tmp_path):
    holder = DesktopBuildLock(tmp_path)
    assert holder.acquire() is True

    probe = (
        "import sys\n"
        "from pathlib import Path\n"
        "from hermes_cli.desktop_build_lock import DesktopBuildLock\n"
        "lock = DesktopBuildLock(Path(sys.argv[1]))\n"
        "raise SystemExit(0 if lock.acquire() else 23)\n"
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", probe, str(tmp_path)],
            check=False,
        )
    finally:
        holder.release()

    assert result.returncode == 23


def test_desktop_build_lock_releases_after_exception(tmp_path):
    try:
        with DesktopBuildLock(tmp_path):
            raise ValueError("build failed")
    except ValueError:
        pass

    successor = DesktopBuildLock(tmp_path)
    assert successor.acquire() is True
    successor.release()


def test_gui_reports_missing_source_before_constructing_build_lock(tmp_path, monkeypatch, capsys):
    root = tmp_path / "broken-hermes-agent"
    monkeypatch.setattr(cli_main, "PROJECT_ROOT", root)
    args = argparse.Namespace()

    with patch(
        "hermes_cli.desktop_build_lock.DesktopBuildLock",
        side_effect=AssertionError("build lock constructed before source validation"),
    ), pytest.raises(SystemExit) as exc:
        cli_main.cmd_gui(args)

    assert exc.value.code == 1
    assert capsys.readouterr().out.strip() == (
        f"Desktop GUI source not found at: {root / 'apps' / 'desktop'}"
    )


def test_gui_refuses_contended_build_before_checking_freshness(tmp_path, monkeypatch):
    root = tmp_path / "hermes-agent"
    desktop_dir = root / "apps" / "desktop"
    desktop_dir.mkdir(parents=True)
    (desktop_dir / "package.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(cli_main, "PROJECT_ROOT", root)
    # Keep this lock-order test out of the unrelated Linux keyring bootstrap.
    monkeypatch.setenv("GNOME_KEYRING_CONTROL", "/run/user/1000/keyring")

    holder = DesktopBuildLock(root)
    assert holder.acquire() is True
    args = argparse.Namespace(
        build_only=True,
        cwd=None,
        fake_boot=False,
        force_build=False,
        hermes_root=None,
        ignore_existing=False,
        skip_build=False,
        source=False,
    )

    try:
        with patch(
            "hermes_cli.main._desktop_build_needed",
            side_effect=AssertionError("freshness check ran without the build lock"),
        ), pytest.raises(SystemExit) as exc:
            cli_main.cmd_gui(args)
    finally:
        holder.release()

    assert exc.value.code == 2


def test_gui_releases_lock_before_packaged_electron_handoff(tmp_path, monkeypatch):
    root = tmp_path / "hermes-agent"
    desktop_dir = root / "apps" / "desktop"
    desktop_dir.mkdir(parents=True)
    (desktop_dir / "package.json").write_text("{}", encoding="utf-8")

    if sys.platform == "darwin":
        executable = desktop_dir / "release" / "mac-arm64" / "Hermes.app" / "Contents" / "MacOS" / "Hermes"
    elif sys.platform == "win32":
        executable = desktop_dir / "release" / "win-unpacked" / "Hermes.exe"
    else:
        executable = desktop_dir / "release" / "linux-unpacked" / "hermes"
    executable.parent.mkdir(parents=True)
    executable.write_text("", encoding="utf-8")

    monkeypatch.setattr(cli_main, "PROJECT_ROOT", root)
    monkeypatch.setenv("GNOME_KEYRING_CONTROL", "/run/user/1000/keyring")
    args = argparse.Namespace(
        build_only=False,
        cwd=None,
        fake_boot=False,
        force_build=False,
        hermes_root=None,
        ignore_existing=False,
        skip_build=True,
        source=False,
    )

    def launch_after_lock_release(*_args, **_kwargs):
        handoff_probe = DesktopBuildLock(root)
        assert handoff_probe.acquire() is True
        handoff_probe.release()
        return subprocess.CompletedProcess([], 0)

    with patch("hermes_cli.main._desktop_launch_options", return_value=([], "auto", "auto", "auto")), \
         patch("hermes_cli.main._register_linux_desktop_entry"), \
         patch("hermes_cli.main._desktop_linux_sandbox_fixup", return_value=True), \
         patch("hermes_cli.main.subprocess.run", side_effect=launch_after_lock_release), \
         pytest.raises(SystemExit) as exc:
        cli_main.cmd_gui(args)

    assert exc.value.code == 0
