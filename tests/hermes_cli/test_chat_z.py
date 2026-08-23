from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from hermes_cli.subcommands.chat_z import build_chat_z_parser, send_to_desktop


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    build_chat_z_parser(parser.add_subparsers(dest="command", required=True))
    return parser


def _accepting_launcher(user_data: Path, seen: list[dict]):
    def launch(_uri: str) -> None:
        request_path = next((user_data / "chat-z" / "requests").glob("*.json"))
        request = json.loads(request_path.read_text(encoding="utf-8"))
        seen.append(request)
        receipt_path = user_data / "chat-z" / "receipts" / request_path.name
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text(
            json.dumps(
                {
                    "requestId": request["requestId"],
                    "status": "accepted",
                    "storedSessionId": "stored-1",
                    "title": request.get("newTitle") or request.get("title"),
                    "created": bool(request.get("newSession")),
                    "cwd": request.get("cwd"),
                }
            ),
            encoding="utf-8",
        )

    return launch


def test_new_session_spools_fixed_title_and_workspace(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    args = _parser().parse_args(
        ["chat-z", "--new", "--cwd", str(project), "--title", "Knowledge receiver", "-q", "Wait"]
    )
    seen: list[dict] = []

    receipt = send_to_desktop(args, launch=_accepting_launcher(tmp_path, seen), user_data=tmp_path)

    assert receipt["storedSessionId"] == "stored-1"
    assert seen[0]["newSession"] is True
    assert seen[0]["newTitle"] == "Knowledge receiver"
    assert seen[0]["cwd"] == str(project.resolve())
    assert seen[0]["text"] == "Wait"


def test_existing_session_by_id_spools_durable_target(tmp_path: Path) -> None:
    args = _parser().parse_args(["chat-z", "--session-id", "stored-7", "-q", "Do work"])
    seen: list[dict] = []

    send_to_desktop(args, launch=_accepting_launcher(tmp_path, seen), user_data=tmp_path)

    assert seen[0]["sessionId"] == "stored-7"
    assert "title" not in seen[0]
    assert "newSession" not in seen[0]


def test_title_is_rejected_without_new(tmp_path: Path) -> None:
    args = _parser().parse_args(["chat-z", "-c", "Receiver", "--title", "Wrong", "-q", "Message"])

    with pytest.raises(ValueError, match="--title can only be used with --new"):
        send_to_desktop(args, launch=lambda _uri: None, user_data=tmp_path)


def test_new_requires_existing_directory(tmp_path: Path) -> None:
    args = _parser().parse_args(["chat-z", "--new", "--cwd", str(tmp_path / "missing"), "-q", "Message"])

    with pytest.raises(ValueError, match="cannot resolve --cwd"):
        send_to_desktop(args, launch=lambda _uri: None, user_data=tmp_path)
