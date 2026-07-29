"""Non-interactive (scripted/CI) behavior of `hermes plugins enable`.

With stdin not a TTY, enabling a plugin must never block on a prompt:
the tool-override grant defaults to deny (grantable later via
`--allow-tool-override`), and missing `requires_env` secrets are reported
as a hint instead of prompted for.
"""

from unittest.mock import patch

import pytest


class FakeConsole:
    def __init__(self):
        self.printed = []

    def print(self, *args, **kwargs):
        self.printed.append(" ".join(str(a) for a in args))

    def input(self, *args, **kwargs):
        raise AssertionError("console.input must not be called without a TTY")


@pytest.fixture
def no_tty(monkeypatch):
    import sys

    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)


class TestToolOverrideGrantNonInteractive:
    def test_defaults_to_deny_without_prompting(self, no_tty):
        from hermes_cli.plugins_cmd import _resolve_tool_override_grant

        console = FakeConsole()
        with patch("hermes_cli.plugins_cmd._set_plugin_entry_flag") as set_flag:
            _resolve_tool_override_grant(console, "compresr", None)
        set_flag.assert_called_once_with("compresr", "allow_tool_override", False)
        assert any("may not override" in line for line in console.printed)

    def test_explicit_grant_still_respected(self, no_tty):
        from hermes_cli.plugins_cmd import _resolve_tool_override_grant

        console = FakeConsole()
        with patch("hermes_cli.plugins_cmd._set_plugin_entry_flag") as set_flag:
            _resolve_tool_override_grant(console, "compresr", True)
        set_flag.assert_called_once_with("compresr", "allow_tool_override", True)


class TestRequiresEnvNonInteractive:
    def test_missing_secret_hints_instead_of_prompting(self, no_tty):
        from hermes_cli.plugins_cmd import _prompt_plugin_env_vars

        console = FakeConsole()
        manifest = {
            "name": "compresr",
            "requires_env": [{"name": "COMPRESR_API_KEY", "secret": True}],
        }
        with patch("hermes_cli.config.get_env_value", return_value=None):
            _prompt_plugin_env_vars(manifest, console)
        assert any("COMPRESR_API_KEY" in line for line in console.printed)

    def test_already_set_var_prints_nothing(self, no_tty):
        from hermes_cli.plugins_cmd import _prompt_plugin_env_vars

        console = FakeConsole()
        manifest = {
            "name": "compresr",
            "requires_env": [{"name": "COMPRESR_API_KEY", "secret": True}],
        }
        with patch("hermes_cli.config.get_env_value", return_value="cmp_set"):
            _prompt_plugin_env_vars(manifest, console)
        assert console.printed == []
