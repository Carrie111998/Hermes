"""CLI coverage for the restricted Hermes MCP server surface."""

import argparse

from hermes_cli.subcommands.mcp import build_mcp_parser


def test_mcp_serve_read_only_flag_is_parsed():
    parser = argparse.ArgumentParser(prog="hermes")
    subparsers = parser.add_subparsers(dest="command")
    build_mcp_parser(subparsers, cmd_mcp=lambda args: None)

    args = parser.parse_args(["mcp", "serve", "--read-only"])

    assert args.command == "mcp"
    assert args.mcp_action == "serve"
    assert args.read_only is True
