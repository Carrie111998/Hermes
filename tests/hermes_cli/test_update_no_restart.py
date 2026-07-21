"""Regression tests for externally orchestrated updates without auto-restart."""

import argparse
from types import SimpleNamespace

from hermes_cli.subcommands.update import build_update_parser


def _parser():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    build_update_parser(subparsers, cmd_update=lambda _args: None)
    return parser


def test_update_no_restart_flag_is_explicit_opt_in():
    args = _parser().parse_args(["update", "--no-restart"])
    assert args.no_restart is True


def test_update_restart_default_remains_enabled():
    args = _parser().parse_args(["update"])
    assert args.no_restart is False


def test_update_yes_and_no_restart_are_compatible():
    args = _parser().parse_args(["update", "--yes", "--no-restart"])
    assert args.yes is True
    assert args.no_restart is True


def test_legacy_argument_objects_keep_restart_default():
    args = SimpleNamespace()
    assert not bool(getattr(args, "no_restart", False))