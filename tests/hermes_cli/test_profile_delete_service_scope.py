"""Service cleanup during profile delete must survive an ambient home override.

``_cleanup_gateway_service`` scopes ``_profile_suffix()`` to the profile being
deleted by setting ``HERMES_HOME``. But ``get_hermes_home()`` resolves the
context-local override (``set_hermes_home_override``) BEFORE the env var, so
when the delete runs inside a context that already holds an override — the
dashboard's delete endpoint, or a profile-scoped gateway context — the env
trick is shadowed and the plist path resolves to the default (or the wrong
profile's) service. The orphaned plist then respawns the "deleted" profile
via launchd ``KeepAlive`` (#97897). These tests pin the fix: the override
layer is scoped alongside the env var, on both the happy path and the
swallowed-exception path.
"""

from __future__ import annotations

import os
import platform
import subprocess
from pathlib import Path

import pytest

from hermes_cli import gateway as gateway_mod
from hermes_cli import profiles as profiles_mod
from hermes_constants import (
    get_hermes_home,
    get_hermes_home_override,
    reset_hermes_home_override,
    set_hermes_home_override,
)


@pytest.fixture()
def profile_env(tmp_path, monkeypatch):
    """Isolate profile paths from the user's real Hermes installation."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    default_home = tmp_path / ".hermes"
    default_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(default_home))
    return default_home


@pytest.fixture()
def darwin_service_branch(monkeypatch):
    """Force the launchd branch regardless of the host OS.

    The Linux/systemd branch shells out to systemctl and the Darwin branch
    to launchctl; both subprocess calls are mocked in the tests below, but
    the plist assertions only make sense on the Darwin branch.
    """
    monkeypatch.setattr(platform, "system", lambda: "Darwin")


@pytest.fixture()
def launch_agents(tmp_path, monkeypatch):
    """Redirect launchd artifacts away from the real ~/Library/LaunchAgents.

    ``get_launchd_plist_path()`` appends ``Library/LaunchAgents`` itself, so
    the fake user home is the tmp root and the plist dir lives one level
    below it.
    """
    monkeypatch.setattr(gateway_mod, "_launchd_user_home", lambda: tmp_path)
    agents_dir = tmp_path / "Library" / "LaunchAgents"
    agents_dir.mkdir(parents=True)
    return agents_dir


def test_cleanup_removes_target_plist_under_ambient_home_override(
    profile_env, launch_agents, darwin_service_branch, monkeypatch
):
    """An ambient context-local override must not shadow the deleted profile.

    Before the fix, ``get_hermes_home()`` returned the caller's override, so
    ``_profile_suffix()`` derived the default profile's service name: the
    target plist survived (KeepAlive respawn) and the default's plist was
    unlinked instead.
    """
    profile_dir = profile_env / "profiles" / "rnobot"
    profile_dir.mkdir(parents=True)
    target_plist = launch_agents / "ai.hermes.gateway-rnobot.plist"
    target_plist.write_text("rnobot service")
    default_plist = launch_agents / "ai.hermes.gateway.plist"
    default_plist.write_text("default service")

    launchctl_calls: list[list[str]] = []
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **kwargs: launchctl_calls.append(list(cmd))
    )

    # Simulate the dashboard delete endpoint / profile-scoped gateway
    # context: a context-local override pointing at the default home is
    # active while the target profile's service is cleaned up.
    token = set_hermes_home_override(profile_env)
    try:
        profiles_mod._cleanup_gateway_service("rnobot", profile_dir)
    finally:
        reset_hermes_home_override(token)

    assert not target_plist.exists(), (
        "the deleted profile's plist must be unlinked, or launchd KeepAlive "
        "respawns it (#97897)"
    )
    assert default_plist.exists(), (
        "the shadowed-resolution bug unloads/unlinks the DEFAULT profile's "
        "plist instead of the deleted one"
    )
    assert launchctl_calls == [["launchctl", "unload", str(target_plist)]]


def test_cleanup_without_ambient_override_keeps_working(
    profile_env, launch_agents, darwin_service_branch, monkeypatch
):
    """Plain CLI deletes (no override active) still remove the plist."""
    profile_dir = profile_env / "profiles" / "coder"
    profile_dir.mkdir(parents=True)
    target_plist = launch_agents / "ai.hermes.gateway-coder.plist"
    target_plist.write_text("coder service")

    monkeypatch.setattr(subprocess, "run", lambda cmd, **kwargs: None)

    assert get_hermes_home_override() is None
    profiles_mod._cleanup_gateway_service("coder", profile_dir)

    assert not target_plist.exists()


def test_cleanup_restores_env_when_service_removal_raises(
    profile_env, launch_agents, darwin_service_branch, monkeypatch
):
    """A swallowed cleanup failure must not leak the temporary env scoping."""

    def _boom(cmd, **kwargs):
        raise RuntimeError("launchctl exploded")

    monkeypatch.setattr(subprocess, "run", _boom)

    profile_dir = profile_env / "profiles" / "rnobot"
    profile_dir.mkdir(parents=True)
    (launch_agents / "ai.hermes.gateway-rnobot.plist").write_text("service")

    token = set_hermes_home_override(profile_env)
    try:
        # The exception is caught and printed by the function under test;
        # the finally block must still restore the scoping layers.
        profiles_mod._cleanup_gateway_service("rnobot", profile_dir)
    finally:
        reset_hermes_home_override(token)

    assert os.environ.get("HERMES_HOME") == str(profile_env)
    assert get_hermes_home() == profile_env
