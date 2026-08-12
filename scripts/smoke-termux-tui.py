#!/usr/bin/env python3
"""Native narrow-terminal startup smoke for the Hermes TUI.

Runs the installed ``hermes`` launcher inside a real PTY, at a phone-like
terminal size, long enough to catch import/gateway/layout startup failures.
The smoke intentionally does not need model credentials: an onboarding/setup
screen is a valid interactive state. It fails only when the process crashes,
produces a known fatal startup signature, or never paints anything.
"""

from __future__ import annotations

import argparse
import os
import select
import shutil
import signal
import struct
import subprocess
import sys
import time

if os.name != "posix":
    raise SystemExit("smoke-termux-tui.py requires a POSIX/Termux runtime")

import fcntl  # noqa: E402
import pty  # noqa: E402
import termios  # noqa: E402

FATAL_MARKERS = (
    b"Traceback (most recent call last)",
    b"Cannot find module",
    b"ERR_MODULE_NOT_FOUND",
    b"SyntaxError:",
    b"ENOTTY",
    b"gateway.start_timeout",
)


def _resize(fd: int, cols: int, rows: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _signal_process_group(proc: subprocess.Popen[bytes], sig: int) -> None:
    kill_group = getattr(os, "killpg", None)
    if callable(kill_group):
        kill_group(proc.pid, sig)
    else:  # Defensive fallback if the script is ever imported on a non-POSIX host.
        proc.terminate()


def _drain(master: int, proc: subprocess.Popen[bytes], deadline: float) -> bytes:
    chunks: list[bytes] = []
    while time.monotonic() < deadline:
        ready, _, _ = select.select([master], [], [], 0.1)
        if ready:
            try:
                chunk = os.read(master, 65536)
            except OSError:
                break
            if not chunk:
                break
            chunks.append(chunk)
        if proc.poll() is not None and not ready:
            break
    return b"".join(chunks)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--command", default="hermes")
    parser.add_argument("--cols", type=int, default=48)
    parser.add_argument("--rows", type=int, default=18)
    parser.add_argument("--observe-seconds", type=float, default=6.0)
    args = parser.parse_args(argv)

    executable = shutil.which(args.command)
    if not executable:
        raise SystemExit(f"TUI smoke command not found: {args.command}")
    if args.cols < 20 or args.rows < 8:
        raise SystemExit("TUI smoke dimensions are unrealistically small")

    master, slave = pty.openpty()
    _resize(slave, args.cols, args.rows)
    env = os.environ.copy()
    env.update(
        {
            "TERM": "xterm-256color",
            "COLORTERM": "truecolor",
            "HERMES_TUI_DISABLE_MOUSE": "1",
            "HERMES_TUI_STARTUP_TIMEOUT_MS": "12000",
        }
    )
    proc = subprocess.Popen(
        [executable],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env=env,
        start_new_session=True,
    )
    os.close(slave)
    output = b""
    try:
        output += _drain(master, proc, time.monotonic() + args.observe_seconds)
        early_rc = proc.poll()
        if early_rc is not None:
            raise RuntimeError(f"Hermes TUI exited during startup with code {early_rc}")
        if len(output) < 32:
            raise RuntimeError("Hermes TUI did not paint meaningful terminal output")
        for marker in FATAL_MARKERS:
            if marker in output:
                raise RuntimeError(f"Hermes TUI emitted fatal startup marker: {marker.decode(errors='replace')}")

        # A terminal Ctrl-C is the normal interactive exit path. Give Hermes a
        # short grace period to reap its gateway child before escalating.
        os.write(master, b"\x03")
        output += _drain(master, proc, time.monotonic() + 4.0)
        try:
            rc = proc.wait(timeout=4.0)
        except subprocess.TimeoutExpired as exc:
            _signal_process_group(proc, signal.SIGTERM)
            proc.wait(timeout=3.0)
            raise RuntimeError("Hermes TUI did not exit after terminal Ctrl-C") from exc
        if rc not in (0, 130, -signal.SIGINT):
            raise RuntimeError(f"Hermes TUI did not shut down cleanly after Ctrl-C (code {rc})")
    except Exception as exc:
        preview = output[-8000:].decode("utf-8", errors="replace")
        print(preview, file=sys.stderr)
        print(f"termux-tui-smoke failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if proc.poll() is None:
            try:
                _signal_process_group(proc, getattr(signal, "SIGKILL", signal.SIGTERM))
            except OSError:
                pass
        os.close(master)

    print(
        f"termux-tui-smoke-ok cols={args.cols} rows={args.rows} bytes={len(output)} rc={rc}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
