"""Tests for in-chat profile switching in the classic CLI."""

from types import SimpleNamespace
from unittest.mock import patch

from cli import HermesCLI


def _call(self_, command):
    return HermesCLI._handle_profile_command(self_, command)


def test_profile_without_name_shows_runtime_profile(capsys):
    self_ = SimpleNamespace(_pending_relaunch=None)

    with (
        patch("hermes_cli.profiles.get_active_profile_name", return_value="coder"),
        patch("hermes_constants.display_hermes_home", return_value="~/.hermes/profiles/coder"),
    ):
        result = _call(self_, "/profile")

    assert result is False
    assert self_._pending_relaunch is None
    output = capsys.readouterr().out
    assert "Profile: coder" in output
    assert "Home:    ~/.hermes/profiles/coder" in output


def test_profile_name_sets_sticky_profile_and_requests_clean_relaunch(capsys):
    self_ = SimpleNamespace(_pending_relaunch=None)

    with (
        patch("hermes_cli.profiles.get_active_profile_name", return_value="default"),
        patch("hermes_cli.profiles.set_active_profile") as set_active,
    ):
        result = _call(self_, "/profile Coder")

    assert result is True
    set_active.assert_called_once_with("Coder")
    assert self_._pending_relaunch == ["--profile", "coder", "--cli", "chat"]
    assert "Switching to profile 'coder'" in capsys.readouterr().out


def test_profile_name_error_keeps_current_chat(capsys):
    self_ = SimpleNamespace(_pending_relaunch=None)

    with patch(
        "hermes_cli.profiles.set_active_profile",
        side_effect=FileNotFoundError("Profile 'missing' does not exist"),
    ):
        result = _call(self_, "/profile missing")

    assert result is False
    assert self_._pending_relaunch is None
    assert "does not exist" in capsys.readouterr().out


def test_profile_switch_relaunch_argv_includes_selected_profile(tmp_path, monkeypatch, capsys):
    """E2E-ish: sticky write + relaunch argv resolve the selected profile name."""
    from hermes_cli.profiles import set_active_profile, get_active_profile

    profiles_root = tmp_path / "profiles"
    coder = profiles_root / "coder"
    coder.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "default-home"))
    # Point sticky active_profile file into tmp root via profiles helpers.
    monkeypatch.setattr(
        "hermes_cli.profiles._get_active_profile_path",
        lambda: tmp_path / "active_profile",
    )
    monkeypatch.setattr(
        "hermes_cli.profiles._get_profiles_root",
        lambda: profiles_root,
    )
    monkeypatch.setattr(
        "hermes_cli.profiles.profile_exists",
        lambda name: name == "coder",
    )

    self_ = SimpleNamespace(_pending_relaunch=None)
    with patch("hermes_cli.profiles.get_active_profile_name", return_value="default"):
        result = _call(self_, "/profile coder")

    assert result is True
    assert get_active_profile() == "coder"
    assert self_._pending_relaunch == ["--profile", "coder", "--cli", "chat"]
    assert "Switching to profile 'coder'" in capsys.readouterr().out
