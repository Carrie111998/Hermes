"""Deterministic, offline interactive-Claude stand-in used by registrar tests."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time


_BRACKETED_PASTE_OPEN = b"\x1b[200~"
_BRACKETED_PASTE_CLOSE = b"\x1b[201~"
_MAX_FRAME_BYTES = 65_536


def _record(**values: object) -> None:
    path = Path(os.environ["FAKE_CLAUDE_RECORD"])
    current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    current.append(values)
    path.write_text(json.dumps(current, sort_keys=True), encoding="utf-8")


def _read_frame() -> str:
    data = bytearray()
    while len(data) < len(_BRACKETED_PASTE_OPEN):
        byte = sys.stdin.buffer.read(1)
        if not byte:
            break
        data.extend(byte)
        if byte in (b"\r", b"\n"):
            return bytes(data).decode("utf-8", errors="replace")
    if data == _BRACKETED_PASTE_OPEN:
        while len(data) < _MAX_FRAME_BYTES and not data.endswith(
            _BRACKETED_PASTE_CLOSE
        ):
            byte = sys.stdin.buffer.read(1)
            if not byte:
                break
            data.extend(byte)
        if data.endswith(_BRACKETED_PASTE_CLOSE):
            terminator = sys.stdin.buffer.read(1)
            if terminator in (b"\r", b"\n"):
                data.extend(terminator)
        return bytes(data).decode("utf-8", errors="replace")
    while len(data) < _MAX_FRAME_BYTES:
        byte = sys.stdin.buffer.read(1)
        if not byte:
            break
        data.extend(byte)
        if byte in (b"\r", b"\n"):
            break
    return bytes(data).decode("utf-8", errors="replace")


def main() -> int:
    scenario = os.environ.get("FAKE_CLAUDE_SCENARIO", "registered")
    _record(event="spawn", argv=sys.argv[1:], cwd=os.getcwd())
    if scenario == "authentication_failure":
        sys.stdout.write("Authentication required\r\n")
        sys.stdout.flush()
        return _exit(scenario, 1)
    sys.stdout.write("\x1b[?2004h")
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
    exit_frame = ""
    while not exit_frame.strip():
        raw_exit = sys.stdin.buffer.readline()
        if not raw_exit:
            break
        exit_frame = raw_exit.decode("utf-8", errors="replace")
    _record(event="stdin", frame=exit_frame)
    if exit_frame.strip() != "/exit":
        return _exit(scenario, 7)
    sequence = 9 if scenario == "nonzero" else int(os.environ.get("FAKE_CLAUDE_EXIT", "0"))
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
