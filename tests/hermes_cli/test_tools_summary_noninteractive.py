"""``hermes tools --summary`` must work without an interactive TTY."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest


def test_cmd_tools_summary_skips_tty_requirement(capsys):
    """Summary mode is a read-only diagnostic; pipes/CI must not be refused."""
    from hermes_cli.main import cmd_tools

    args = SimpleNamespace(tools_action=None, summary=True)
    config = {"platform_toolsets": {"cli": ["hermes-cli", "terminal", "file"]}}

    with patch("hermes_cli.main._require_tty") as require_tty, patch(
        "hermes_cli.tools_config.load_config", return_value=config
    ), patch(
        "hermes_cli.tools_config._get_enabled_platforms", return_value=["cli"]
    ), patch(
        "sys.stdin.isatty", return_value=False
    ):
        cmd_tools(args)

    require_tty.assert_not_called()
    out = capsys.readouterr().out
    assert "Tool Summary" in out


def test_cmd_tools_interactive_still_requires_tty():
    """Bare ``hermes tools`` (curses UI) still refuses non-interactive stdin."""
    from hermes_cli.main import cmd_tools

    args = SimpleNamespace(tools_action=None, summary=False)

    with patch("hermes_cli.main._require_tty", side_effect=SystemExit(1)) as require_tty:
        with pytest.raises(SystemExit):
            cmd_tools(args)
    require_tty.assert_called_once_with("tools")
