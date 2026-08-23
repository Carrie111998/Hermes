"""Desktop Command Center must not inherit Kanban worker identity.

PO-0021 / live incident 2026-08-24: Hermes.app relaunched from a packaged
install running inside a dispatcher worker inherited HERMES_PROFILE=company-infra
and HERMES_KANBAN_TASK/RUN_ID/CLAIM_LOCK/WORKSPACE. Interactive Command Center
turns then received the kanban-worker protocol and acted as Infra.

This is a third isolation path, distinct from:
- PO-0015 cron Bot Chat strip (child env copy; parent os.environ untouched)
- BF-0016 nested ``hermes chat`` child isolation
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

import pytest


def _worker_desktop_env(**extra: str) -> dict[str, str]:
    """Env captured from the 2026-08-24 Hermes.app contamination."""
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/Users/shermanlye",
        "HERMES_PROFILE": "company-infra",
        "HERMES_HOME": "/Users/shermanlye/.hermes/profiles/company-infra",
        "HERMES_KANBAN_TASK": "t_22bb7847",
        "HERMES_KANBAN_RUN_ID": "45",
        "HERMES_KANBAN_CLAIM_LOCK": "Shermans-MacBook-Pro.local:20597",
        "HERMES_KANBAN_WORKSPACE": "/Users/shermanlye/.hermes/kanban/boards/personal/workspaces/t_22bb7847",
        "HERMES_KANBAN_WORKSPACES_ROOT": "/Users/shermanlye/.hermes/kanban/boards/personal/workspaces",
        "HERMES_KANBAN_BRANCH": "infra/desktop-group-room-owner-tag-20260823",
        "HERMES_KANBAN_BOARD": "personal",
        "HERMES_KANBAN_DB": "/Users/shermanlye/.hermes/kanban/boards/personal/kanban.db",
        "HERMES_SESSION_SOURCE": "kanban",
        "HERMES_SESSION_ID": "20260823_230728_ce516e",
        "HERMES_SINGLE_QUERY_SESSION": "1",
        "HERMES_INTERACTIVE": "1",
        "HERMES_AGENT": "true",
    }
    env.update(extra)
    return env


def test_sanitize_desktop_host_env_drops_lifecycle_ownership_and_worker_profile():
    from hermes_cli.desktop_identity import sanitize_desktop_host_env

    src = _worker_desktop_env()
    cleaned = sanitize_desktop_host_env(src)

    assert src["HERMES_KANBAN_TASK"] == "t_22bb7847"
    assert "HERMES_KANBAN_TASK" not in cleaned
    assert "HERMES_KANBAN_RUN_ID" not in cleaned
    assert "HERMES_KANBAN_CLAIM_LOCK" not in cleaned
    assert "HERMES_KANBAN_WORKSPACE" not in cleaned
    assert "HERMES_KANBAN_WORKSPACES_ROOT" not in cleaned
    assert "HERMES_KANBAN_BRANCH" not in cleaned
    assert cleaned.get("HERMES_KANBAN_BOARD") == "personal"
    assert cleaned.get("HERMES_KANBAN_DB", "").endswith("kanban.db")
    assert "HERMES_PROFILE" not in cleaned
    assert cleaned["HERMES_HOME"] == "/Users/shermanlye/.hermes"
    assert cleaned.get("HERMES_SESSION_SOURCE") != "kanban"
    assert "HERMES_SESSION_ID" not in cleaned
    assert "HERMES_SINGLE_QUERY_SESSION" not in cleaned
    assert cleaned.get("HERMES_INTERACTIVE") == "1"
    assert cleaned.get("PATH") == "/usr/bin:/bin"


def test_sanitize_desktop_host_env_keeps_explicit_desk_profile():
    from hermes_cli.desktop_identity import sanitize_desktop_host_env

    cleaned = sanitize_desktop_host_env(
        _worker_desktop_env(),
        explicit_profile="company-cpo",
    )
    assert cleaned["HERMES_PROFILE"] == "company-cpo"
    assert "HERMES_KANBAN_TASK" not in cleaned


def test_sanitize_desktop_host_env_is_noop_without_worker_identity():
    from hermes_cli.desktop_identity import sanitize_desktop_host_env

    src = {
        "PATH": "/usr/bin",
        "HERMES_HOME": "/Users/shermanlye/.hermes",
        "HERMES_KANBAN_BOARD": "personal",
    }
    cleaned = sanitize_desktop_host_env(src)
    assert cleaned["HERMES_HOME"] == "/Users/shermanlye/.hermes"
    assert cleaned["HERMES_KANBAN_BOARD"] == "personal"
    assert "HERMES_PROFILE" not in cleaned


def test_apply_host_isolation_mutates_only_the_given_mapping():
    from hermes_cli.desktop_identity import apply_desktop_host_env_isolation

    env = _worker_desktop_env()
    changed = apply_desktop_host_env_isolation(env)
    assert changed is True
    assert "HERMES_KANBAN_TASK" not in env
    assert env["HERMES_HOME"] == "/Users/shermanlye/.hermes"


def test_gui_launch_env_does_not_inherit_worker_lifecycle(tmp_path, monkeypatch):
    """``hermes desktop`` from a worker must not relaunch Hermes.app as that worker."""
    from hermes_cli import main as cli_main

    root = tmp_path / "hermes-agent"
    desktop_dir = root / "apps" / "desktop"
    desktop_dir.mkdir(parents=True)
    (desktop_dir / "package.json").write_text("{}", encoding="utf-8")
    exe = desktop_dir / "release" / "mac-arm64" / "Hermes.app" / "Contents" / "MacOS" / "Hermes"
    if __import__("sys").platform == "darwin":
        packaged = exe
    elif __import__("sys").platform == "win32":
        packaged = desktop_dir / "release" / "win-unpacked" / "Hermes.exe"
    else:
        packaged = desktop_dir / "release" / "linux-unpacked" / "hermes"
    packaged.parent.mkdir(parents=True, exist_ok=True)
    packaged.write_text("", encoding="utf-8")
    if __import__("sys").platform not in ("darwin", "win32"):
        (packaged.parent / "chrome-sandbox").write_text("", encoding="utf-8")

    monkeypatch.setattr(cli_main, "PROJECT_ROOT", root)
    for key, value in _worker_desktop_env().items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setenv("GNOME_KEYRING_CONTROL", "/run/user/1000/keyring")
    monkeypatch.delenv("KDE_SESSION_VERSION", raising=False)
    monkeypatch.delenv("KDE_FULL_SESSION", raising=False)
    monkeypatch.delenv("HERMES_DESKTOP_PASSWORD_STORE", raising=False)

    launch_envs: list[dict] = []

    def _capture_run(*args, **kwargs):
        env = kwargs.get("env")
        if env and args and args[0] and str(args[0][0]) == str(packaged):
            launch_envs.append(dict(env))
        return subprocess.CompletedProcess(args[0] if args else ["hermes"], 0)

    ns = argparse.Namespace(
        skip_build=True,
        build_only=False,
        force_build=False,
        source=False,
        fake_boot=False,
        ignore_existing=False,
        hermes_root=None,
        cwd=None,
    )
    from unittest.mock import patch

    with (
        patch("hermes_cli.main._desktop_linux_sandbox_fixup", return_value=True),
        patch("hermes_cli.main._register_linux_desktop_entry"),
        patch("hermes_cli.main.subprocess.run", side_effect=_capture_run),
        pytest.raises(SystemExit) as exc,
    ):
        cli_main.cmd_gui(ns)

    assert exc.value.code == 0
    assert launch_envs, "packaged desktop launch env was not captured"
    launched = launch_envs[0]
    assert "HERMES_KANBAN_TASK" not in launched
    assert "HERMES_KANBAN_RUN_ID" not in launched
    assert "HERMES_KANBAN_CLAIM_LOCK" not in launched
    assert "HERMES_KANBAN_WORKSPACE" not in launched
    assert "HERMES_KANBAN_BRANCH" not in launched
    assert launched.get("HERMES_PROFILE") != "company-infra"
    assert launched.get("HERMES_HOME") == "/Users/shermanlye/.hermes"
    # Parent worker env must stay intact so the dispatcher claim/heartbeat survives.
    assert __import__("os").environ["HERMES_KANBAN_TASK"] == "t_22bb7847"
    assert __import__("os").environ["HERMES_PROFILE"] == "company-infra"
