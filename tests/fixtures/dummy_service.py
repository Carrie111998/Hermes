#!/usr/bin/env python3
"""Small external process used only by coordinated-restart tests."""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from pathlib import Path


def _append(path: Path | None, message: str) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(message + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid-file", required=True)
    parser.add_argument("--ready-file")
    parser.add_argument("--log-file")
    parser.add_argument("--ignore-term-seconds", type=float, default=0.0)
    parser.add_argument("--crash", action="store_true")
    parser.add_argument("--exit-after", type=float)
    args = parser.parse_args()

    if args.crash:
        return 17

    pid_file = Path(args.pid_file)
    ready_file = Path(args.ready_file) if args.ready_file else None
    log_file = Path(args.log_file) if args.log_file else None
    started = time.monotonic()

    def _term(_signum, _frame) -> None:
        if time.monotonic() - started < max(
            0.0,
            args.ignore_term_seconds,
        ):
            _append(log_file, "sigterm_ignored")
            return
        _append(log_file, "sigterm_exit")
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _term)
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(f"{os.getpid()}\n", encoding="utf-8")
    if ready_file is not None:
        ready_file.parent.mkdir(parents=True, exist_ok=True)
        ready_file.write_text("ready\n", encoding="utf-8")
    _append(log_file, f"started:{os.getpid()}")

    while True:
        if (
            args.exit_after is not None
            and time.monotonic() - started >= args.exit_after
        ):
            _append(log_file, "timed_exit")
            return 0
        time.sleep(0.02)


if __name__ == "__main__":
    sys.exit(main())
