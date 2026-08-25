"""Tests for argv-first execution of user-configured commands.

``run_configured_command`` is the shared runner for config-derived command
strings (quick_commands, goal gates, MCP bootstrap). It must execute simple
commands as argv (no shell, so metacharacters in arguments are inert) and fall
back to the shell only for explicit shell syntax.
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from hermes_cli._subprocess_compat import (
    command_needs_shell,
    run_configured_command,
)


# ──────────────────────────────────────────────────────────────────────
# command_needs_shell
# ──────────────────────────────────────────────────────────────────────


class TestCommandNeedsShell:
    @pytest.mark.parametrize(
        "command",
        [
            "echo daily-note",
            "npm install",
            "git status --porcelain",
            "ls -la",
        ],
    )
    def test_simple_commands_do_not_need_shell(self, command):
        assert command_needs_shell(command) is False

    @pytest.mark.parametrize(
        "command",
        [
            "echo a && echo b",
            "ls | grep foo",
            "echo a; echo b",
            "cat file > out.txt",
            "echo $HOME",
            "ls *.py",
            "echo `pwd`",
            "cd /tmp || exit 1",
            "echo hello # comment",
        ],
    )
    def test_shell_syntax_needs_shell(self, command):
        assert command_needs_shell(command) is True

    def test_empty_command_does_not_need_shell(self):
        assert command_needs_shell("") is False
        assert command_needs_shell("   ") is False

    def test_metacharacter_in_quoted_argument_does_not_need_shell(self):
        # A `;` inside quotes is data, not a separator — argv is safe here and
        # keeps the `;` inert (the doc's core requirement: a filename with `;`
        # must not spawn a second command).
        assert command_needs_shell('echo "a;b"') is False
        assert command_needs_shell("echo 'a|b'") is False

    def test_unquoted_operator_still_needs_shell(self):
        assert command_needs_shell("echo a; echo b") is True
        assert command_needs_shell("ls | grep foo") is True
        assert command_needs_shell("cd /tmp && ls") is True

    def test_unquoted_operator_string_runs_through_shell_completely(self):
        # Contract boundary (not a sandbox): the strings are operator-authored
        # config, so an unquoted `;` is a real operator choice and the WHOLE
        # string — both parts — runs through the shell. This is the scenario
        # an earlier draft mislabeled as "fixed"; the honest contract is that
        # a simple command with no shell syntax never spawns a shell, while
        # operator-written shell syntax is preserved exactly.
        with patch("hermes_cli._subprocess_compat.subprocess.run") as mock_run:
            run_configured_command("echo hello; id")
        args, kwargs = mock_run.call_args
        assert args[0] == "echo hello; id"
        assert kwargs.get("shell") is True


# ──────────────────────────────────────────────────────────────────────
# run_configured_command
# ──────────────────────────────────────────────────────────────────────


class TestRunConfiguredCommand:
    def test_simple_command_runs_as_argv(self):
        # No shell metacharacters → split into argv, no /bin/sh involved.
        with patch("hermes_cli._subprocess_compat.subprocess.run") as mock_run:
            run_configured_command("echo hello world", capture_output=True, text=True)
        args, kwargs = mock_run.call_args
        assert args[0] == ["echo", "hello", "world"]
        assert kwargs.get("shell") is None  # shell=False default

    def test_shell_syntax_runs_through_shell(self):
        with patch("hermes_cli._subprocess_compat.subprocess.run") as mock_run:
            run_configured_command("echo a && echo b")
        args, kwargs = mock_run.call_args
        assert args[0] == "echo a && echo b"
        assert kwargs.get("shell") is True

    def test_metacharacter_in_argument_is_not_a_second_command(self):
        """A `;` inside an argument must NOT spawn a second command.

        ``split_command_line`` treats ``touch "/tmp/safe;x"`` as one argument
        of ``touch`` (no shell to interpret the ``;``), so nothing else runs.
        """
        with patch("hermes_cli._subprocess_compat.subprocess.run") as mock_run:
            run_configured_command('touch "/tmp/safe;x"', capture_output=True, text=True)
        args, kwargs = mock_run.call_args
        assert args[0] == ["touch", "/tmp/safe;x"]
        assert kwargs.get("shell") is None

    def test_empty_command_is_noop_shell(self):
        with patch("hermes_cli._subprocess_compat.subprocess.run") as mock_run:
            run_configured_command("")
        args, kwargs = mock_run.call_args
        assert kwargs.get("shell") is True

    def test_unbalanced_quotes_fall_back_to_shell(self):
        with patch("hermes_cli._subprocess_compat.subprocess.run") as mock_run:
            run_configured_command('echo "unclosed')
        args, kwargs = mock_run.call_args
        assert kwargs.get("shell") is True


# ──────────────────────────────────────────────────────────────────────
# Integration: the runner actually executes (real subprocess)
# ──────────────────────────────────────────────────────────────────────


class TestRunConfiguredCommandIntegration:
    def test_echo_runs(self):
        result = run_configured_command("echo hello", capture_output=True, text=True)
        assert result.returncode == 0
        assert result.stdout.strip() == "hello"

    def test_semicolon_in_quoted_filename_does_not_chain(self):
        """Behavior contract: argv-first means a `;` inside a quoted argument
        is inert — ``echo "safe;ls"`` prints the literal text, never runs ls.
        """
        result = run_configured_command('echo "safe;ls"', capture_output=True, text=True)
        assert result.returncode == 0
        assert result.stdout.strip() == "safe;ls"

    def test_shell_syntax_still_works(self):
        result = run_configured_command("echo a && echo b", capture_output=True, text=True)
        assert result.returncode == 0
        assert result.stdout.strip() == "a\nb"
