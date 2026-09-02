"""Exec helper for cgroup-contained Kanban workers.

The parent passes one pipe descriptor.  This process performs no worker action
until the dispatcher has durably registered its cgroup identity and writes the
single release byte.
"""

from __future__ import annotations

import os
import sys

_ABORTED_EXIT = 125


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 3 or args[1] != "--":
        return _ABORTED_EXIT
    try:
        gate_fd = int(args[0])
    except (TypeError, ValueError):
        return _ABORTED_EXIT
    command = args[2:]
    if gate_fd < 0 or not command or any("\x00" in part for part in command):
        return _ABORTED_EXIT
    try:
        token = os.read(gate_fd, 1)
    except OSError:
        return _ABORTED_EXIT
    finally:
        try:
            os.close(gate_fd)
        except OSError:
            pass
    if token != b"1":
        return _ABORTED_EXIT
    try:
        os.execvpe(command[0], command, os.environ)
    except OSError:
        return 126


if __name__ == "__main__":
    raise SystemExit(main())
