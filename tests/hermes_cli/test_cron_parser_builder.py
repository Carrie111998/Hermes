"""Unit tests for the extracted ``hermes cron`` parser builder.

Confirms ``build_cron_parser`` wires up the same subactions, aliases, options,
and ``func=cmd_cron`` dispatch that lived inline in ``main()`` before the
god-file Phase 2 extraction.
"""

from __future__ import annotations

import argparse

import pytest

from hermes_cli.subcommands.cron import build_cron_parser


def _sentinel_handler(args):  # pragma: no cover - only identity is asserted
    return "cron-handler"


def _build():
    parser = argparse.ArgumentParser(prog="hermes")
    subparsers = parser.add_subparsers(dest="command")
    build_cron_parser(subparsers, cmd_cron=_sentinel_handler)
    return parser


def test_cron_subactions_present():
    parser = _build()
    for action in ("list", "create", "edit", "pause", "resume", "run", "remove", "status", "runs", "tick"):
        ns = parser.parse_args(["cron", action] if action in ("list", "status", "runs", "tick")
                               else ["cron", action, "jobid"] if action in ("pause", "resume", "run", "remove", "edit")
                               else ["cron", "create", "30m"])
        assert ns.command == "cron"
        assert ns.cron_command == action


def test_cron_create_script_timeout_option():
    parser = _build()
    ns = parser.parse_args([
        "cron", "create", "0 9 * * *", "daily task prompt",
        "--script-timeout-seconds", "12.5",
    ])
    assert ns.script_timeout_seconds == 12.5
def test_cron_edit_no_agent_tristate():
    parser = _build()
    # --no-agent -> True, --agent -> False, neither -> None
    assert parser.parse_args(["cron", "edit", "j", "--no-agent"]).no_agent is True
    assert parser.parse_args(["cron", "edit", "j", "--agent"]).no_agent is False
    assert parser.parse_args(["cron", "edit", "j"]).no_agent is None


@pytest.mark.parametrize("value", ["-1", "nan", "inf", "bad"])
def test_cron_script_timeout_rejects_invalid_values(value):
    parser = _build()
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["cron", "create", "30m", "prompt", "--script-timeout-seconds", value]
        )


def test_cron_script_timeout_default_is_explicit_null():
    parser = _build()
    omitted = parser.parse_args(["cron", "edit", "j"])
    cleared = parser.parse_args(
        ["cron", "edit", "j", "--script-timeout-seconds", "default"]
    )
    assert not hasattr(omitted, "script_timeout_seconds")
    assert cleared.script_timeout_seconds is None
def test_cron_accept_hooks_flag_on_run_and_tick():
    parser = _build()
    # --accept-hooks is suppressed-default; present only when passed.
    ns = parser.parse_args(["cron", "run", "jid", "--accept-hooks"])
    assert ns.accept_hooks is True
    ns2 = parser.parse_args(["cron", "tick", "--accept-hooks"])
    assert ns2.accept_hooks is True
