"""Synthetic subprocess fixture for dispatcher-owned worker supervision tests."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import time

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from hermes_cli.worker_lifecycle import (
    emit_start_identity_event,
    emit_terminal_event,
    process_birth_token,
)


def emit(path: Path, **payload: object) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(payload, sort_keys=True) + "\n")
        stream.flush()


def emit_terminal(
    args: argparse.Namespace,
    *,
    exit_code: int,
    classification: str,
) -> None:
    birth_token = process_birth_token(os.getpid())
    if birth_token is None:
        raise RuntimeError("could not obtain fixture process birth identity")
    failure_reason = "none" if classification == "success" else "transient_provider"
    emit(
        args.event_path,
        schema_version=3,
        kind="terminal",
        nonce=args.start_nonce,
        task_id=args.task_id,
        run_id=args.run_id,
        attempt=args.attempt,
        expected_session_id=args.session_id,
        observed_session_id=args.session_id,
        worktree=str(args.worktree.resolve()),
        root_pid=os.getpid(),
        process_birth_token=birth_token,
        exit_kind="code",
        exit_value=exit_code,
        failure_reason=failure_reason,
        classification=classification,
    )


def run_listener(event_path: Path) -> int:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    emit(
        event_path,
        kind="owned_process",
        pid=os.getpid(),
        role="listener",
        port=listener.getsockname()[1],
    )
    while True:
        time.sleep(1)


def run_attempt(args: argparse.Namespace) -> int:
    birth_token = process_birth_token(os.getpid())
    if birth_token is None:
        raise RuntimeError("could not obtain fixture process birth identity")
    emit(
        args.event_path,
        schema_version=3,
        kind="identity",
        nonce=args.start_nonce,
        attempt=args.attempt,
        task_id=args.task_id,
        run_id=args.run_id,
        expected_session_id=args.session_id,
        observed_session_id=args.session_id,
        worktree=str(args.worktree.resolve()),
        root_pid=os.getpid(),
        process_birth_token=birth_token,
    )
    if args.attempt == 1:
        subprocess.Popen(
            [sys.executable, __file__, "listener", "--event-path", str(args.event_path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if args.event_path.exists() and '"role": "listener"' in args.event_path.read_text(encoding="utf-8"):
                break
            time.sleep(0.02)
        else:
            emit(args.event_path, kind="failure", classification="fixture_timeout")
            emit_terminal(args, exit_code=70, classification="fixture_timeout")
            return 70
        emit(
            args.event_path,
            kind="failure",
            classification="transient_provider",
            provider="fixture",
        )
        emit_terminal(args, exit_code=75, classification="transient_provider")
        return 75

    if args.outcome == "fail":
        # Keep the interpreter live long enough for the supervisor to bind the
        # emitted identity to the retained launcher handle.
        time.sleep(0.1)
        emit(
            args.event_path,
            kind="failure",
            classification="transient_provider",
            provider="fixture",
        )
        emit_terminal(args, exit_code=76, classification="transient_provider")
        return 76

    bounded_file = args.worktree / "recovered.txt"
    bounded_file.write_text("recovered\n", encoding="utf-8")
    subprocess.run(["git", "add", "recovered.txt"], cwd=args.worktree, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Hermes Fixture",
            "-c",
            "user.email=fixture@invalid",
            "commit",
            "-m",
            "fixture: recovered worker",
        ],
        cwd=args.worktree,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    from hermes_cli import kanban_db as kb

    conn = kb.connect(args.board_db)
    try:
        completed = kb.complete_task(
            conn,
            args.task_id,
            result="worker lifecycle fixture recovered",
            expected_run_id=args.run_id if args.run_id is not None else None,
        )
    finally:
        conn.close()
    if not completed:
        emit(args.event_path, kind="failure", classification="completion_rejected")
        emit_terminal(args, exit_code=71, classification="completion_rejected")
        return 71
    emit(args.event_path, kind="task_done", task_id=args.task_id)
    emit_terminal(args, exit_code=0, classification="success")
    return 0


def run_owned_default() -> int:
    """Exercise the real default-spawn argv/env contract without a provider."""
    session_id = os.environ.get("HERMES_WORKER_SESSION_ID")
    if not emit_start_identity_event(session_id=session_id):
        return 72
    time.sleep(1)
    if not emit_terminal_event(
        {"failed": False},
        session_id=session_id,
        exit_code=0,
    ):
        return 73
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("attempt", "listener"))
    parser.add_argument("--event-path", type=Path, required=True)
    parser.add_argument("--attempt", type=int)
    parser.add_argument("--session-id")
    parser.add_argument("--worktree", type=Path)
    parser.add_argument("--board-db", type=Path)
    parser.add_argument("--task-id")
    parser.add_argument("--run-id", type=int)
    parser.add_argument("--start-nonce", required=False)
    parser.add_argument("--outcome", choices=("success", "fail"), default="success")
    return parser.parse_args()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "owned-default":
        raise SystemExit(run_owned_default())
    parsed = parse_args()
    raise SystemExit(run_listener(parsed.event_path) if parsed.mode == "listener" else run_attempt(parsed))
