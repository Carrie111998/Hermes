"""``hermes doctor logs <path>`` — one-shot log rotation (t_57aac3e7 fix 1).

A small, self-contained rotator so an operator or host-ops automation can
rotate a *foreign* log on demand instead of hand-truncating it. Foreign
writers (oMLX's ``server.log``, launchd-supervised ``dashboard.error.log``)
hold the file open; rename-rotation moves the inode aside (``<path>`` ->
``<path>.1``, shifting ``.1 -> .2 -> ...`` up to ``--backups``) so the writer's
open fd keeps pointing at the old inode while a fresh file is created for the
next write. This reuses :func:`hermes_cli.stderr_timestamp._rotate_error_log_if_needed`
via import rather than re-implementing the shift logic.

Exit codes:
    0  rotated (or nothing to do: file absent/empty), reported cleanly
    2  usage error (bad path / unreadable argument)
"""

from __future__ import annotations

import sys
from pathlib import Path


def _human(n: int) -> str:
    """Compact byte count for reporting."""
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} TiB"


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

    if max_bytes < 0 or backups < 0:
        print("error: --max-bytes and --backups must be >= 0", file=sys.stderr)
        return 2

    if not log_path.exists():
        print(f"nothing to rotate: {log_path} does not exist")
        return 0

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

    print(f"log:            {log_path}")
    print(f"size before:     {_human(before_size)}")
    if rotated:
        after = log_path.stat().st_size if log_path.exists() else 0
        print(f"size after:      {_human(after)} (fresh file created)")
        print(f"backups kept:    {now_backups}")
        print("status:          rotated")
    else:
        print(
            f"status:          not rotated "
            f"(under cap {_human(max_bytes)}; use --force to rotate anyway)"
        )
    return 0
