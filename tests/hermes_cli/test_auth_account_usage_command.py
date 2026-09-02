from __future__ import annotations

import argparse
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli.auth_commands import auth_command
from hermes_cli.subcommands.auth import build_auth_parser


def test_auth_token_usage_parser_exposes_provider_and_json_flag():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    handler = MagicMock()
    build_auth_parser(subparsers, cmd_auth=handler)

    args = parser.parse_args(["auth", "token-usage", "openai-codex", "--json"])

    assert args.auth_action == "token-usage"
    assert args.provider == "openai-codex"
    assert args.json is True
    assert args.func is handler


def test_auth_token_usage_command_routes_to_read_only_usage_handler():
    args = argparse.Namespace(
        auth_action="token-usage", provider="openai-codex", json=False
    )
    with patch("hermes_cli.auth_commands.auth_token_usage_command") as handler:
        auth_command(args)
    handler.assert_called_once_with(args)


def test_auth_token_usage_rejects_unsupported_provider():
    from hermes_cli.auth_commands import auth_token_usage_command

    args = argparse.Namespace(provider="anthropic", json=False)
    with pytest.raises(SystemExit, match="supports only openai-codex"):
        auth_token_usage_command(args)
