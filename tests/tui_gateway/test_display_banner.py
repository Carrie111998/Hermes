"""Behavioral coverage for the interactive banner preference."""

from unittest.mock import patch

from hermes_cli.config_defaults import DEFAULT_CONFIG
from tui_gateway import server


def test_banner_defaults_to_enabled_for_compatibility():
    assert DEFAULT_CONFIG["display"].get("banner", True) is True


def test_tui_gateway_resolves_disabled_banner_from_config():
    with patch.object(server, "_load_cfg", return_value={"display": {"banner": False}}):
        assert server.resolve_banner_enabled() is False


def test_tui_gateway_defaults_banner_on_for_missing_or_invalid_display():
    with patch.object(server, "_load_cfg", return_value={}):
        assert server.resolve_banner_enabled() is True

    with patch.object(server, "_load_cfg", return_value={"display": "invalid"}):
        assert server.resolve_banner_enabled() is True
