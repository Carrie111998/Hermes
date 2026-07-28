"""Desktop session profile-plugin activation (#73230).

The process-global PluginManager is discovered once at process start against
the launch profile's config. A profile-neutral Desktop backend serving a
different selected profile must re-activate so the hook registry reflects
that profile's ``plugins.enabled`` — otherwise profile-only plugins (e.g.
Langfuse) silently no-op for Desktop sessions.

These tests pin the behavioral contract of
``PluginManager.activate_for_profile``: after activating a profile, only that
profile's hooks are active; activating a different profile swaps them out;
re-activating the same profile is a no-op.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from hermes_constants import (
    set_hermes_home_override,
    reset_hermes_home_override,
    get_hermes_home_override,
)
from hermes_cli.plugins import PluginManager, activate_profile_plugins


def _profile_scoped_discover(self: PluginManager, force: bool = False) -> None:
    """A fake ``discover_and_load`` that registers a hook tagged by the
    currently-active profile home.

    Stands in for real plugin discovery: the tag lets the test assert *which*
    profile's hooks are active without locking down plugin names or counts.
    """
    home = get_hermes_home_override() or "launch"
    # force=True semantics: clear and repopulate, just like the real sweep.
    self._hooks.clear()
    self._hooks.setdefault("pre_tool_call", []).append(lambda **kw: home)


def _activate_under(profile_home: str | Path | None, manager: PluginManager) -> bool:
    """Bind ``profile_home``'s HERMES_HOME override around activation, mirroring
    how ``_make_agent``'s caller scopes the build."""
    token = set_hermes_home_override(str(profile_home) if profile_home else None)
    try:
        return manager.activate_for_profile(profile_home)
    finally:
        reset_hermes_home_override(token)


def test_activate_profile_swaps_active_hooks(tmp_path: Path) -> None:
    """After activate(profile_A), only A's hooks are active; after B, only B's."""
    manager = PluginManager()
    profile_a = tmp_path / "profiles" / "a"
    profile_b = tmp_path / "profiles" / "b"
    profile_a.mkdir(parents=True)
    profile_b.mkdir(parents=True)

    with patch.object(PluginManager, "discover_and_load", _profile_scoped_discover):
        # Activate profile A → its hook is the only one active.
        assert _activate_under(profile_a, manager) is True
        results_a = manager.invoke_hook("pre_tool_call")
        assert results_a == [str(profile_a)]

        # Activate profile B → A's hook is gone, only B's remains.
        assert _activate_under(profile_b, manager) is True
        results_b = manager.invoke_hook("pre_tool_call")
        assert results_b == [str(profile_b)]
        assert str(profile_a) not in results_b

        # Re-activating B is a no-op (already active) — no redundant reload.
        assert _activate_under(profile_b, manager) is False
        assert manager.invoke_hook("pre_tool_call") == [str(profile_b)]


def test_launch_profile_activation_is_noop_on_fresh_manager() -> None:
    """A fresh manager is already discovered against the launch profile at
    process start, so activating the launch profile (None) must not force a
    redundant reload."""
    manager = PluginManager()
    with patch.object(PluginManager, "discover_and_load") as mock_discover:
        assert _activate_under(None, manager) is False
        mock_discover.assert_not_called()


def test_returning_to_launch_profile_reloads() -> None:
    """Switching from a selected profile back to the launch profile must
    re-discover (the launch profile's hooks are no longer active after a
    profile switch cleared them)."""
    manager = PluginManager()
    tmp = Path("/tmp/hermes_73230_profile")  # noqa: S108 - never created on disk
    with patch.object(PluginManager, "discover_and_load", _profile_scoped_discover):
        assert _activate_under(tmp, manager) is True
        assert manager.invoke_hook("pre_tool_call") == [str(tmp)]

        # Back to launch profile — not a no-op, because the manager's active
        # profile is currently ``tmp``, not the launch profile.
        with patch.object(PluginManager, "discover_and_load") as mock_discover:
            assert _activate_under(None, manager) is True
            mock_discover.assert_called_once_with(force=True)


def test_activate_profile_plugins_delegates_to_global_manager() -> None:
    """The module-level convenience delegates to the singleton manager."""
    manager = PluginManager()
    manager._active_profile_home = ""  # ensure launch profile is "active"
    with patch("hermes_cli.plugins.get_plugin_manager", return_value=manager):
        with patch.object(PluginManager, "discover_and_load", _profile_scoped_discover):
            token = set_hermes_home_override("/tmp/hermes_73230_global")  # noqa: S108
            try:
                assert activate_profile_plugins("/tmp/hermes_73230_global") is True
            finally:
                reset_hermes_home_override(token)
            assert manager.invoke_hook("pre_tool_call") == [
                "/tmp/hermes_73230_global"
            ]
