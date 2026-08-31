"""Durable managed-process wrapper.

The wrapper owns the child's output descriptors, drains them even after its own
parent dies, and persists only an authenticated session id / exit code receipt.
"""
from __future__ import annotations

import argparse
import codecs
import json
import os
import signal
import struct
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

if os.name != "nt":
    import fcntl
    import pty
    import termios
    import tty

def _write_result(path: Path, session_id: str, exit_code: int) -> None:
    """Atomically publish the exact safe result payload with private permissions."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = json.dumps({"session_id": session_id, "exit_code": exit_code}, sort_keys=True, separators=(",", ":")).encode("utf-8")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb", closefd=True) as stream:
            stream.write(payload); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try: os.fsync(directory_fd)
            finally: os.close(directory_fd)
    finally:
        try: os.unlink(temporary)
        except FileNotFoundError: pass


def _relay(source, target) -> None:
    """Drain a child stream; a dead parent's pipe must not kill the child."""
    while True:
        try: chunk = source.read(4096)
        except (OSError, ValueError): return
        if not chunk: return
        try:
            target.write(chunk); target.flush()
        except (BrokenPipeError, OSError):
            # EPIPE/EIO means the registry/UI vanished. Keep draining so the
            # child never blocks on a full pipe.
            target = open(os.devnull, "wb", buffering=0)


def _spawn_conpty(command: list[str], pty_process_cls, *, input_stream=None, output_stream=None):
    """Start a child behind ConPTY and relay the outer PTY transport to it.

    ``pywinpty`` owns the child stdin/stdout, retaining Windows TTY semantics
    (including ``isatty()``) while this durable sidecar continues to drain
    output and can publish an exit receipt after its registry parent dies.
    The injectable streams/factory keep the relay testable on non-Windows.
    """
    proc = pty_process_cls.spawn(command)
    input_stream = input_stream or sys.stdin.buffer
    output_stream = output_stream or sys.stdout.buffer

    def relay_output() -> None:
        stream = output_stream
        while proc.isalive():
            try:
                chunk = proc.read(4096)
            except (EOFError, OSError, ValueError):
                return
            if not chunk:
                continue
            if isinstance(chunk, str):
                chunk = chunk.encode("utf-8", errors="replace")
            try:
                stream.write(chunk)
                stream.flush()
            except (BrokenPipeError, OSError, ValueError):
                # The registry-side outer PTY may disappear. Keep draining the
                # inner PTY so a verbose child cannot block forever.
                stream = open(os.devnull, "wb", buffering=0)

    def relay_input() -> None:
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        try:
            while True:
                chunk = input_stream.read(1)
                if not chunk:
                    # Flush a trailing partial sequence once, then preserve
                    # close_stdin() behavior for programs which use Ctrl-D as
                    # terminal EOF; pywinpty accepts text only.
                    tail = decoder.decode(b"", final=True)
                    if tail:
                        proc.write(tail)
                    proc.write("\x04")
                    return
                if isinstance(chunk, bytes):
                    text = decoder.decode(chunk)
                    if text:
                        proc.write(text)
                else:
                    proc.write(str(chunk))
        except (EOFError, OSError, ValueError):
            return

    output_thread = threading.Thread(target=relay_output, daemon=True)
    input_thread = threading.Thread(target=relay_input, daemon=True)
    output_thread.start()
    input_thread.start()
    return proc, [output_thread, input_thread], None


def _spawn(command: list[str], use_pty: bool):
    if os.name == "nt" and use_pty:
        try:  # pywinpty is a Windows-only optional dependency.
            from winpty import PtyProcess
        except ImportError as exc:  # pragma: no cover - Windows packaging lane
            raise RuntimeError(
                "Windows PTY requested but pywinpty/ConPTY is unavailable; "
                "refusing non-interactive pipe fallback"
            ) from exc
        return _spawn_conpty(command, PtyProcess)
    if not use_pty:
        proc = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=(os.name != "nt"),
        )
        threads = [threading.Thread(target=_relay, args=(proc.stdout, sys.stdout.buffer), daemon=True), threading.Thread(target=_relay, args=(proc.stderr, sys.stderr.buffer), daemon=True)]
        for thread in threads: thread.start()
        return proc, threads, None
    # Bridge the registry-owned outer PTY to a sidecar-owned inner PTY. The
    # inner master must survive the registry parent, while the outer slave is
    # raw so only the inner terminal performs echo/canonical/output processing.
    outer_fd = sys.stdin.fileno()
    outer_attrs = None
    try:
        outer_attrs = termios.tcgetattr(outer_fd)
        tty.setraw(outer_fd, termios.TCSANOW)
    except (OSError, termios.error):
        # The registry parent may die immediately after spawn, hanging up the
        # outer PTY before Python reaches this point. Durability still requires
        # launching and draining the inner child without an input transport.
        pass
    master, slave = pty.openpty()

    def copy_winsize() -> bool:
        try:
            size = fcntl.ioctl(outer_fd, termios.TIOCGWINSZ, b"\0" * 8)
            fcntl.ioctl(master, termios.TIOCSWINSZ, size)
            return True
        except OSError:
            return False

    if not copy_winsize():
        try:
            default_size = struct.pack("HHHH", 30, 120, 0, 0)
            fcntl.ioctl(master, termios.TIOCSWINSZ, default_size)
        except OSError:
            pass
    if hasattr(signal, "SIGWINCH"):
        signal.signal(signal.SIGWINCH, lambda _signum, _frame: copy_winsize())
    proc = subprocess.Popen(
        command, stdin=slave, stdout=slave, stderr=slave,
        start_new_session=True, close_fds=True,
    )
    os.close(slave)
    master_stream = os.fdopen(master, "rb", buffering=0)
    output_thread = threading.Thread(
        target=_relay, args=(master_stream, sys.stdout.buffer), daemon=True,
    )
    output_thread.start()
    input_fd = os.dup(master_stream.fileno())

    def relay_stdin() -> None:
        try:
            while True:
                chunk = os.read(outer_fd, 1)
                if not chunk:
                    os.write(input_fd, b"\x04")
                    return
                os.write(input_fd, chunk)
        except OSError:
            return
        finally:
            try:
                os.close(input_fd)
            except OSError:
                pass

    input_thread = threading.Thread(target=relay_stdin, daemon=True)
    input_thread.start()

    def restore_outer_tty() -> None:
        if outer_attrs is None:
            return
        try:
            termios.tcsetattr(outer_fd, termios.TCSANOW, outer_attrs)
        except (OSError, termios.error):
            pass

    return proc, [output_thread, input_thread], restore_outer_tty


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--result", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--pty", action="store_true")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command: parser.error("a command is required")
    proc = None
    cleanup = None
    def forward(signum, _frame):
        if proc is not None:
            try:
                if os.name == "nt":
                    proc.send_signal(signum)
                else:
                    os.killpg(proc.pid, signum)
            except (AttributeError, OSError):
                pass
    # The outer PTY belongs to the spawning registry. Its disappearance sends
    # SIGHUP when that registry process dies; durability requires the wrapper
    # and its inner child to outlive that transport. Explicit process-tool
    # termination uses SIGTERM/SIGINT and is still forwarded below.
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, forward)
    try:
        proc, threads, cleanup = _spawn(command, args.pty)
        waited = proc.wait()
        # subprocess.Popen.wait() returns an int, while pywinpty records the
        # terminal status on ``exitstatus`` and may return None.
        exit_code = waited if isinstance(waited, int) else getattr(proc, "exitstatus", -1)
        if not isinstance(exit_code, int):
            exit_code = -1
        for thread in threads: thread.join(timeout=2)
    except (OSError, RuntimeError):
        exit_code = 127
    finally:
        if cleanup is not None:
            cleanup()
    _write_result(Path(args.result), args.session_id, int(exit_code))
    # OS process statuses cannot encode Python's negative signal convention.
    # Keep the exact negative value in the durable receipt and expose the
    # conventional shell transport status (128 + signal) to outer observers.
    if exit_code < 0:
        return 128 + min(-int(exit_code), 127)
    return int(exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
