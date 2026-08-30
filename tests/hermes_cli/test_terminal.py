"""Tests for hermes_cli/terminal.py — terminal subcommand helpers."""


def test_terminal_module_imports():
    from hermes_cli.terminal import run_terminal_command
    assert callable(run_terminal_command)
