"""Tests for `hermes memory status` CLI command.

Covers:
- Status output shows config-aware indicators instead of hardcoded 'always active'
- memory_enabled, user_profile_enabled, and memory tool are each reflected
- Memory tool resolution uses the canonical _get_platform_tools resolver
- Original issue: 'Built-in: always active' was misleading when features were disabled
"""

import pytest
from unittest.mock import patch


def _run_cmd_status(capfd, mem_config=None, memory_tools=None):
    """Run cmd_status with a mocked config and return captured stdout.

    Args:
        mem_config: The "memory" section of config.
        memory_tools: Set of tool names returned by _get_platform_tools.
                      Defaults to {"memory"} (tool enabled).
    """
    from hermes_cli.memory_setup import cmd_status

    config = {"memory": mem_config or {}}
    if memory_tools is None:
        memory_tools = {"memory"}

    with patch("hermes_cli.config.load_config", return_value=config):
        with patch("hermes_cli.memory_setup._get_available_providers", return_value=[]):
            with patch(
                "hermes_cli.tools_config._get_platform_tools",
                return_value=memory_tools,
            ):
                cmd_status(args=None)

    captured = capfd.readouterr()
    return captured.out


class TestMemoryStatusLabels:
    """Status output should reflect actual config, not a hardcoded string."""


    def test_shows_memory_injection_enabled_by_default(self, capfd):
        """Memory injection defaults to enabled."""
        out = _run_cmd_status(capfd)
        assert "Memory injection:" in out
        assert "enabled ✓" in out

    def test_shows_memory_injection_disabled(self, capfd):
        """When memory_enabled is false, status reflects it."""
        out = _run_cmd_status(capfd, mem_config={"memory_enabled": False})
        assert "Memory injection:" in out
        assert "disabled ✗" in out


def _run_cmd_status_platforms(capfd, platform_toolsets):
    """Run cmd_status with a multi-platform toolset config.

    platform_toolsets: dict mapping platform name -> set of tools, used as
    the side_effect of _get_platform_tools(config, plat, ...).
    """
    from hermes_cli.memory_setup import cmd_status

    config = {"memory": {}, "platform_toolsets": platform_toolsets}

    def _side_effect(config, plat, include_default_mcp_servers=False):
        return platform_toolsets.get(plat, set())

    with patch("hermes_cli.config.load_config", return_value=config):
        with patch("hermes_cli.memory_setup._get_available_providers", return_value=[]):
            with patch(
                "hermes_cli.tools_config._get_platform_tools",
                side_effect=_side_effect,
            ):
                cmd_status(args=None)

    return capfd.readouterr().out


class TestMemoryStatusMultiPlatform:
    """Memory tool status must reflect the platform actually in use.

    Regression for #81430: cmd_status hardcoded the "cli" platform, so a
    gateway platform (Telegram/Discord) with memory enabled showed
    "disabled" when the cli toolset lacked it.
    """

    def test_enabled_on_gateway_platform_not_cli_shows_enabled(self, capfd):
        """The #81430 case: memory enabled for telegram, absent for cli."""
        out = _run_cmd_status_platforms(
            capfd,
            {"cli": {"terminal", "web"}, "telegram": {"terminal", "memory"}},
        )
        # Summary line must not misreport: the tool IS enabled somewhere.
        assert "Memory tool:        enabled ✓" in out
        # Per-platform detail shows exactly where.
        assert "Memory tool by platform:" in out
        assert "cli          disabled ✗" in out
        assert "telegram     enabled ✓" in out

    def test_disabled_everywhere_still_disabled(self, capfd):
        """When no configured platform enables memory, keep showing disabled."""
        out = _run_cmd_status_platforms(
            capfd,
            {"cli": {"terminal"}, "telegram": {"terminal"}},
        )
        assert "Memory tool:        disabled ✗" in out

    def test_no_platform_toolsets_falls_back_to_cli(self, capfd):
        """Without platform_toolsets, behavior matches the old cli-only check."""
        out = _run_cmd_status_platforms(capfd, {"cli": {"memory"}})
        assert "Memory tool:        enabled ✓" in out
        assert "Memory tool by platform:" not in out



