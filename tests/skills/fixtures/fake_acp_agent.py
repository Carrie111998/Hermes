#!/usr/bin/env python3
"""Controllable ACP stdio peer for transport-boundary regression tests."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time


SESSION_ID = "88888888-8888-4888-8888-888888888888"


def frame(sequence: int, target_bytes: int) -> bytes:
    payload = {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "sessionId": SESSION_ID,
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": ""},
                "_meta": {"sequence": sequence},
            },
        },
    }
    empty = (json.dumps(payload, separators=(",", ":")) + "\n").encode()
    padding = target_bytes - len(empty)
    if padding < 0:
        raise ValueError(f"target frame is too small: {target_bytes}")
    payload["params"]["update"]["content"]["text"] = "x" * padding
    encoded = (json.dumps(payload, separators=(",", ":")) + "\n").encode()
    if len(encoded) != target_bytes:
        raise AssertionError((len(encoded), target_bytes))
    return encoded


def write_bytes(data: bytes, chunk_size: int, delay: float) -> None:
    size = chunk_size if chunk_size > 0 else len(data)
    for offset in range(0, len(data), size):
        os.write(sys.stdout.fileno(), data[offset : offset + size])
        if delay:
            time.sleep(delay)


def response(request_id: int, result: dict) -> None:
    write_bytes(
        (
            json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}) + "\n"
        ).encode(),
        0,
        0,
    )


def structured_report(*, block_reason: str = "", test_outcome: str = "passed") -> dict:
    return {
        "status": "completed",
        "summary": "PASS: Structured ACP report.",
        "changed_files": ["src/example.py"],
        "tests": [
            {
                "command": "pytest -q",
                "outcome": test_outcome,
                "details": "1 passed" if test_outcome == "passed" else "not run",
            }
        ],
        "risks": [],
        "evidence": ["pytest -q: 1 passed"],
        "block_reason": block_reason,
        "block_kind": None,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--frames", default="")
    parser.add_argument("--chunk-size", type=int, default=0)
    parser.add_argument("--delay", type=float, default=0)
    parser.add_argument(
        "--mode",
        choices=(
            "normal",
            "no-newline",
            "malformed",
            "stall-after-prefix",
            "exit-2",
        ),
        default="normal",
    )
    parser.add_argument("--pid-file")
    parser.add_argument("--prompt-log")
    parser.add_argument("--structured-report", action="store_true")
    parser.add_argument("--complete-structured-report", action="store_true")
    parser.add_argument("--invalid-first-report", action="store_true")
    parser.add_argument(
        "--invalid-completed-block-reason-first-report",
        action="store_true",
    )
    parser.add_argument(
        "--invalid-completed-block-reason-always",
        action="store_true",
    )
    parser.add_argument(
        "--invalid-completed-not-run-first-report",
        action="store_true",
    )
    parser.add_argument(
        "--invalid-completed-not-run-always",
        action="store_true",
    )
    parser.add_argument("--exit-on-prompt", type=int, default=1)
    parser.add_argument("--stall-on-prompt", type=int, default=0)
    parser.add_argument("--cancel-log")
    parser.add_argument("--detached-child-pid-file")
    parser.add_argument("--work-stop-reason", default="end_turn")
    parser.add_argument("--session-id")
    args = parser.parse_args()
    global SESSION_ID
    if args.session_id:
        SESSION_ID = args.session_id
    if args.pid_file:
        with open(args.pid_file, "w", encoding="utf-8") as handle:
            handle.write(str(os.getpid()))

    targets = [int(value) for value in args.frames.split(",") if value]
    prompt_count = 0
    for raw in sys.stdin.buffer:
        request = json.loads(raw)
        method = request.get("method")
        if method == "initialize":
            response(request["id"], {"protocolVersion": 1})
        elif method == "session/new":
            response(request["id"], {"sessionId": SESSION_ID})
        elif method in {"session/load", "session/resume"}:
            response(request["id"], {})
        elif method == "session/prompt":
            prompt_count += 1
            if args.detached_child_pid_file and prompt_count == 1:
                child = subprocess.Popen(
                    [sys.executable, "-c", "import time; time.sleep(60)"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                with open(
                    args.detached_child_pid_file, "w", encoding="utf-8"
                ) as handle:
                    handle.write(str(child.pid))
            if args.prompt_log:
                with open(args.prompt_log, "a", encoding="utf-8") as handle:
                    handle.write(
                        json.dumps({
                            "pid": os.getpid(),
                            "session_id": request["params"]["sessionId"],
                            "has_output_schema": "outputSchema"
                            in request["params"].get("_meta", {}),
                        })
                        + "\n"
                    )
            if prompt_count == args.stall_on_prompt:
                continue
            if args.mode == "exit-2" and prompt_count == args.exit_on_prompt:
                sys.stderr.write(
                    "PROVIDER_TOKEN=super-secret-value\n"
                    "Unicode diagnostic: 接続に失敗しました 🚫\n"
                    + ("long-diagnostic-" * 600)
                    + "\ntail-marker\n"
                )
                return 2
            if args.mode == "malformed":
                write_bytes(b'{"jsonrpc":\n', args.chunk_size, args.delay)
                time.sleep(60)
            if args.structured_report and prompt_count > 1:
                structured_output = {
                    "status": "completed",
                    "summary": "Structured ACP report.",
                }
                if args.complete_structured_report:
                    structured_output = structured_report()
                if (
                    args.invalid_completed_block_reason_first_report
                    or args.invalid_completed_block_reason_always
                    or args.invalid_completed_not_run_first_report
                    or args.invalid_completed_not_run_always
                ):
                    structured_output = structured_report()
                if args.invalid_first_report and prompt_count == 2:
                    structured_output = {"status": "completed"}
                if args.invalid_completed_block_reason_always or (
                    args.invalid_completed_block_reason_first_report
                    and prompt_count == 2
                ):
                    structured_output = structured_report(
                        block_reason="Unexpected non-empty blocker text."
                    )
                if args.invalid_completed_not_run_always or (
                    args.invalid_completed_not_run_first_report and prompt_count == 2
                ):
                    structured_output = structured_report(test_outcome="not_run")
                response(
                    request["id"],
                    {
                        "stopReason": "end_turn",
                        "_meta": {"structuredOutput": structured_output},
                    },
                )
                continue
            for sequence, target in enumerate(targets):
                encoded = frame(sequence, target)
                if args.mode in {"no-newline", "stall-after-prefix"}:
                    encoded = encoded[:-1]
                write_bytes(encoded, args.chunk_size, args.delay)
                if args.mode in {"no-newline", "stall-after-prefix"}:
                    time.sleep(60)
            response(
                request["id"],
                {
                    "stopReason": args.work_stop_reason
                    if prompt_count == 1
                    else "end_turn"
                },
            )
        elif method == "session/cancel":
            if args.cancel_log:
                with open(args.cancel_log, "w", encoding="utf-8") as handle:
                    handle.write(request["params"]["sessionId"])
            return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
