"""Tests for Google Workspace setup.py --check / --check-live in gws-native mode.

Relocated from the removed ``tests/skills/test_google_oauth_setup.py``.
Low-level parsing of ``gws auth status`` lives in ``test_gws_native_auth.py``.
"""

import importlib.util
from pathlib import Path

import pytest


SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "skills/productivity/google-workspace/scripts/setup.py"
)


@pytest.fixture
def setup_module(monkeypatch, tmp_path):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    spec = importlib.util.spec_from_file_location("google_workspace_setup_test", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    monkeypatch.setattr(module, "_ensure_deps", lambda: None)
    return module


class TestGwsNativeAuth:
    """setup.py --check / --check-live behavior in gws-native mode."""

    def test_check_auth_returns_true_when_gws_native(self, setup_module, monkeypatch, capsys):
        """No Hermes token but gws authed on its own → AUTHENTICATED via gws."""
        assert not setup_module.TOKEN_PATH.exists()
        monkeypatch.setattr(setup_module, "gws_native_authed", lambda: True)
        assert setup_module.check_auth() is True
        out = capsys.readouterr().out
        assert "AUTHENTICATED: via gws CLI" in out

    def test_check_auth_false_when_no_token_and_no_gws(self, setup_module, monkeypatch, capsys):
        """No token and gws not authed → NOT_AUTHENTICATED."""
        assert not setup_module.TOKEN_PATH.exists()
        monkeypatch.setattr(setup_module, "gws_native_authed", lambda: False)
        assert setup_module.check_auth() is False
        out = capsys.readouterr().out
        assert "NOT_AUTHENTICATED" in out

    def test_check_live_uses_gws_when_native(self, setup_module, monkeypatch, capsys):
        """In gws-native mode, --check-live validates via a real gws call, not the token client."""
        assert not setup_module.TOKEN_PATH.exists()
        monkeypatch.setattr(setup_module, "gws_native_authed", lambda: True)
        monkeypatch.setattr(setup_module, "gws_live_check", lambda: (True, "ok"))
        assert setup_module.check_auth_live() is True
        assert "LIVE_CHECK_OK" in capsys.readouterr().out

    def test_check_live_reports_disabled_client_from_gws(self, setup_module, monkeypatch, capsys):
        """A disabled_client error surfaced by gws is reported, not swallowed."""
        assert not setup_module.TOKEN_PATH.exists()
        monkeypatch.setattr(setup_module, "gws_native_authed", lambda: True)
        monkeypatch.setattr(
            setup_module, "gws_live_check", lambda: (False, "disabled_client: the client is disabled")
        )
        assert setup_module.check_auth_live() is False
        out = capsys.readouterr().out
        assert "LIVE_CHECK_FAILED" in out
        assert "disabled" in out.lower()
