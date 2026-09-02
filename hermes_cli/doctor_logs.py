"""``hermes doctor logs <path>`` — one-shot log rotation (t_57aac3e7 fix 1).

A small, self-contained rotator so an operator or host-ops automation can
rotate a *foreign* log on demand instead of hand-truncating it. For ordinary
external logs it performs bounded rename-rotation. The oMLX application log is
different: its server process can retain an open descriptor indefinitely, so
we refuse to rotate it unless the caller supplies a controlled reopen/restart
command and the fresh path gains a writer afterwards.

Exit codes:
    0  rotated (or nothing to do: file absent/empty), reported cleanly
    2  usage error (bad path / unreadable argument)
    3  oMLX rotation refused because no reopen command was supplied
    4  reopen command failed, or no writer opened the fresh oMLX log
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path


def _human(n: int) -> str:
    """Compact byte count for reporting."""
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} TiB"


def _is_omlx_managed_server_log(log_path: Path) -> bool:
    """True only for oMLX's app-managed ``Application Support`` server log."""
    parts = tuple(part.casefold() for part in log_path.parts)
    return (
        log_path.name == "server.log"
        and "omlx" in parts
        and "application support" in parts
    )


def _writer_pids(log_path: Path) -> list[int] | None:
    """Return processes with *log_path* open, or ``None`` if lsof is unavailable."""
    lsof = shutil.which("lsof")
    if not lsof:
        return None
    try:
        result = subprocess.run(
            [lsof, "-t", str(log_path)],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return [int(line) for line in result.stdout.splitlines() if line.isdigit()]


def _run_reopen_command(command: list[str], timeout: float) -> tuple[bool, str]:
    """Run an explicit operator-provided reopen command without a shell."""
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if result.returncode == 0:
        return True, ""
    detail = (result.stderr or result.stdout).strip().splitlines()
    return False, detail[-1] if detail else f"exit {result.returncode}"


def _wait_for_writer(log_path: Path, timeout: float) -> list[int] | None:
    """Wait for a process to open the replacement log after a controlled restart."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pids = _writer_pids(log_path)
        if pids:
            return pids
        time.sleep(0.1)
    return _writer_pids(log_path)


def run_doctor_logs(args) -> int:
    """Rotate ``args.path`` on demand. Returns a process exit code."""
    from hermes_cli.stderr_timestamp import (
        _get_external_log_rotation_config,
        _rotate_error_log_if_needed,
    )

    raw = getattr(args, "path", None)
    if not raw:
        print("error: no log path given", file=sys.stderr)
        return 2
    log_path = Path(raw).expanduser()

    # Infer caps from the path unless the operator overrode them.
    inferred_max, inferred_backups = _get_external_log_rotation_config(log_path)
    max_bytes = getattr(args, "max_bytes", None)
    backups = getattr(args, "backups", None)
    if max_bytes is None:
        max_bytes = inferred_max
    if backups is None:
        backups = inferred_backups
    force = bool(getattr(args, "force", False))
    reopen_command = list(getattr(args, "reopen_command", None) or [])
    reopen_timeout = float(getattr(args, "reopen_timeout", 30.0))

    if max_bytes < 0 or backups < 0 or reopen_timeout <= 0:
        print(
            "error: --max-bytes and --backups must be >= 0; --reopen-timeout must be > 0",
            file=sys.stderr,
        )
        return 2

    if not log_path.exists():
        print(f"nothing to rotate: {log_path} does not exist")
        return 0

    managed_omlx_log = _is_omlx_managed_server_log(log_path)
    if managed_omlx_log and not reopen_command:
        print(
            "refusing to rotate oMLX server.log without --reopen-command: "
            "the active writer would keep appending to the backup",
            file=sys.stderr,
        )
        return 3

    before_size = log_path.stat().st_size
    # Snapshot existing backups so we can report the shift accurately.
    def _backup_exists(gen: int) -> bool:
        return log_path.with_suffix(log_path.suffix + f".{gen}").exists()

    had_backups = [g for g in range(1, backups + 1) if _backup_exists(g)]

    _rotate_error_log_if_needed(log_path, max_bytes, backups, force=force)

    # Determine outcome. A successful rotation leaves a fresh ``.1`` in place
    # (and the main file either gone or re-created empty by the writer).
    rotated = _backup_exists(1) and (not log_path.exists() or log_path.stat().st_size < before_size)
    if force and not rotated:
        # force should always rotate an existing non-empty file; if .1 is
        # missing the file was empty (rename of an empty file still yields .1,
        # so this only happens on a permission error swallowed upstream).
        rotated = _backup_exists(1)

    now_backups = [g for g in range(1, backups + 1) if _backup_exists(g)]

    if managed_omlx_log and rotated:
        # Make the replacement pathname visible immediately. It only becomes a
        # successful handoff after the controlled restart opens it.
        log_path.touch(exist_ok=True)
        reopened, detail = _run_reopen_command(reopen_command, reopen_timeout)
        if not reopened:
            print(f"status:          rotation incomplete; reopen command failed: {detail}")
            return 4
        writer_pids = _wait_for_writer(log_path, reopen_timeout)
        if not writer_pids:
            print(
                "status:          rotation incomplete; no writer opened fresh log",
                file=sys.stderr,
            )
            return 4
        after = log_path.stat().st_size
        print(f"log:            {log_path}")
        print(f"size before:     {_human(before_size)}")
        print(f"size after:      {_human(after)}")
        print(f"backups kept:    {now_backups}")
        print(f"active writers:  {writer_pids}")
        print("status:          rotated and writer handoff verified")
        return 0

    print(f"log:            {log_path}")
    print(f"size before:     {_human(before_size)}")
    if rotated:
        after = log_path.stat().st_size if log_path.exists() else 0
        print(f"size after:      {_human(after)}")
        print(f"backups kept:    {now_backups}")
        print("status:          rotated")
    else:
        print(
            f"status:          not rotated "
            f"(under cap {_human(max_bytes)}; use --force to rotate anyway)"
        )
    return 0
