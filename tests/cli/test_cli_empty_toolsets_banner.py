"""Regression tests for explicit classic-CLI toolset selections."""

from unittest.mock import MagicMock, patch

import pytest

import cli


def test_main_preserves_explicit_empty_toolsets_for_banner():
    """An explicit empty selection must not fall back to configured tools."""
    cli_instance = MagicMock()

    with patch.object(cli, "HermesCLI", return_value=cli_instance) as cli_class:
        with pytest.raises(SystemExit) as exc_info:
            cli.main(toolsets="", list_tools=True)

    assert exc_info.value.code == 0
    assert cli_class.call_args.kwargs["toolsets"] == []
    cli_instance.show_banner.assert_called_once_with()


def test_main_omitted_toolsets_still_uses_default_resolution():
    """Omitting the option must retain configured/coding default resolution."""
    cli_instance = MagicMock()

    with (
        patch.object(cli, "HermesCLI", return_value=cli_instance) as cli_class,
        patch("agent.coding_context.coding_selection", return_value=["coding"]),
    ):
        with pytest.raises(SystemExit) as exc_info:
            cli.main(list_tools=True)

    assert exc_info.value.code == 0
    assert cli_class.call_args.kwargs["toolsets"] == ["coding"]
