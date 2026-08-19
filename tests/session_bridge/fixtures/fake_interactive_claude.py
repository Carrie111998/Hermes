"""Deterministic, offline interactive-Claude stand-in used by registrar tests.

ConPTY may collapse multiline bracketed-paste input before this fixture records it.
That transport normalization is fixture-only: production registration still verifies
the exact canonical prompt parsed from Claude's native JSONL transcript.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time
from typing import BinaryIO


_BRACKETED_PASTE_OPEN = b"\x1b[200~"
_BRACKETED_PASTE_CLOSE = b"\x1b[201~"
_MAX_FRAME_BYTES = 65_536


def _record(**values: object) -> None:
    path = Path(os.environ["FAKE_CLAUDE_RECORD"])
    current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    current.append(values)
    path.write_text(json.dumps(current, sort_keys=True), encoding="utf-8")


def _read_frame(stream: BinaryIO | None = None) -> str:
    input_stream = stream or sys.stdin.buffer
    data = bytearray()
    while len(data) < len(_BRACKETED_PASTE_OPEN):
        byte = input_stream.read(1)
        if not byte:
            break
        data.extend(byte)
        if byte in (b"\r", b"\n"):
            return bytes(data).decode("utf-8", errors="replace")
    if data == _BRACKETED_PASTE_OPEN:
        while len(data) < _MAX_FRAME_BYTES and not data.endswith(
            _BRACKETED_PASTE_CLOSE
        ):
            byte = input_stream.read(1)
            if not byte:
                break
            data.extend(byte)
        if data.endswith(_BRACKETED_PASTE_CLOSE):
            terminator = input_stream.read(1)
            if terminator in (b"\r", b"\n"):
                data.extend(terminator)
        return bytes(data).decode("utf-8", errors="replace")
    while len(data) < _MAX_FRAME_BYTES:
        byte = input_stream.read(1)
        if not byte:
            break
        data.extend(byte)
        if byte in (b"\r", b"\n"):
            break
    return bytes(data).decode("utf-8", errors="replace")


def main() -> int:
    scenario = os.environ.get("FAKE_CLAUDE_SCENARIO", "registered")
    spawn = {"event": "spawn", "argv": sys.argv[1:], "cwd": os.getcwd()}
    entrypoint = os.environ.get("CLAUDE_CODE_ENTRYPOINT")
    if entrypoint:
        spawn["entrypoint"] = entrypoint
    _record(**spawn)
    if scenario == "authentication_failure":
        sys.stdout.write("Authentication required\r\n")
        sys.stdout.flush()
        return _exit(scenario, 1)
    # Match the main-only footer emitted by the production argv's dontAsk mode.
    sys.stdout.write("\x1b[?2004h\x1b[2m⏵⏵ don't ask on\x1b[0m")
    sys.stdout.flush()
    frame = _read_frame()
    _record(event="stdin", frame=frame)
    _record(event="native_created", session_id=_session_id())
    if scenario == "delayed_transcript_indexing":
        time.sleep(float(os.environ.get("FAKE_CLAUDE_INDEX_DELAY", "0.05")))
        _record(event="index_ready", session_id=_session_id())
    sys.stdout.write("\x1b[32mClaude>\x1b[0m ")
    if scenario == "timeout_after_native_creation":
        sys.stdout.flush()
        time.sleep(3600)
    elif scenario == "malformed_response":
        sys.stdout.write("NOT REGISTERED\r\n")
    else:
        sys.stdout.write("REGISTERED\r\n")
    sys.stdout.flush()
    if scenario == "delayed_extra":
        time.sleep(float(os.environ.get("FAKE_CLAUDE_EXTRA_DELAY", "0.05")))
        sys.stdout.write("extra\r\n")
        sys.stdout.flush()
        # Recorded AFTER the flush, so a reader that observes this event knows the
        # trailing line is already in the PTY stream.  That lets the test wait on
        # the effect instead of betting the line lands inside the reader's
        # _RESPONSE_SETTLE_SECONDS window -- a bet it loses on a loaded host.
        _record(event="extra")
    exit_frame = ""
    while not exit_frame.strip():
        raw_exit = sys.stdin.buffer.readline()
        if not raw_exit:
            break
        exit_frame = raw_exit.decode("utf-8", errors="replace")
    _record(event="stdin", frame=exit_frame)
    if exit_frame.strip() != "/exit":
        return _exit(scenario, 7)
    sequence = (
        9 if scenario == "nonzero" else int(os.environ.get("FAKE_CLAUDE_EXIT", "0"))
    )
    return _exit(scenario, sequence)


def _exit(scenario: str, sequence: int) -> int:
    _record(event="exit", sequence=sequence, scenario=scenario)
    return sequence


def _session_id() -> str | None:
    try:
        return sys.argv[sys.argv.index("--session-id") + 1]
    except (ValueError, IndexError):
        return None


if __name__ == "__main__":
    raise SystemExit(main())
