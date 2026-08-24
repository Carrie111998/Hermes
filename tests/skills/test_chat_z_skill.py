"""Integration contracts for the bundled Chat-Z skill."""

from __future__ import annotations

import argparse
from pathlib import Path

from hermes_cli.subcommands.chat_z import build_chat_z_parser


REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_PATH = REPO_ROOT / "skills" / "productivity" / "chat-z" / "SKILL.md"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    build_chat_z_parser(parser.add_subparsers(dest="command", required=True))
    return parser


def test_chat_z_is_a_bundled_skill() -> None:
    assert SKILL_PATH.is_file()


def test_cli_supports_the_skill_existing_session_target() -> None:
    args = _parser().parse_args([
        "chat-z",
        "--session-id",
        "stored-session",
        "-q",
        "Run the task",
    ])

    assert args.session_id == "stored-session"
    assert args.query == "Run the task"
    assert args.new_session is False


def test_cli_supports_the_skill_new_project_session_target(tmp_path: Path) -> None:
    args = _parser().parse_args([
        "chat-z",
        "--new",
        "--cwd",
        str(tmp_path),
        "--title",
        "Receiver",
        "-q",
        "Wait for work",
    ])

    assert args.new_session is True
    assert args.cwd == str(tmp_path)
    assert args.title == "Receiver"
