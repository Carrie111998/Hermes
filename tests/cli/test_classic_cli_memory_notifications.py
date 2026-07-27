"""Classic CLI coverage for background-review notification configuration."""

from __future__ import annotations

import inspect

import pytest

from hermes_cli.cli_agent_setup_mixin import (
    CLIAgentSetupMixin,
    _normalize_memory_notifications,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, "on"),
        (False, "off"),
        (True, "on"),
        ("off", "off"),
        (" OFF ", "off"),
        ("on", "on"),
        ("verbose", "verbose"),
        ("invalid", "on"),
    ],
)
def test_normalize_memory_notifications(raw, expected):
    assert _normalize_memory_notifications(raw) == expected


def test_classic_cli_applies_memory_notifications_to_agent():
    """The classic CLI must not leave AIAgent's hard-coded default in place."""
    src = inspect.getsource(CLIAgentSetupMixin._init_agent)
    assert "self.agent.memory_notifications = _normalize_memory_notifications(" in src
    assert 'CLI_CONFIG.get("display")' in src
