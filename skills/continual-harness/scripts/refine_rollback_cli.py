#!/usr/bin/env python3
"""Standalone inspector for ``/refine`` rollback snapshots.

Thin CLI over the runtime module :mod:`agent.refine_rollback` so a user can
audit and manage review snapshots without booting the agent. Behavior is
identical to what ``/refine undo`` uses internally.

Usage:
    python skills/continual-harness/scripts/refine_rollback_cli.py list
    python skills/continual-harness/scripts/refine_rollback_cli.py restore <id>
    python skills/continual-harness/scripts/refine_rollback_cli.py delete <id>
    python skills/continual-harness/scripts/refine_rollback_cli.py latest <session_id>
"""
from __future__ import annotations

import sys
from pathlib import Path

# Import the runtime module by path so this script works from a checkout
# without the package being importable in the bare interpreter.
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[2]  # skills/continual-harness/scripts -> repo root
sys.path.insert(0, str(_ROOT))

from hermes_constants import get_hermes_home  # noqa: E402

from agent.refine_rollback import (  # noqa: E402
    delete_snapshot,
    latest_snapshot_id,
    list_snapshots,
    restore_snapshot,
)


def _home() -> Path:
    return get_hermes_home()


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    cmd = argv[0]
    home = _home()
    if cmd == "list":
        ids = list_snapshots(home)
        if not ids:
            print("No /refine snapshots.")
            return 0
        for i in ids:
            print(i)
        return 0
    if cmd == "latest":
        if len(argv) < 2:
            print("usage: latest <session_id>")
            return 2
        sid = latest_snapshot_id(home, argv[1])
        print(sid or "(none)")
        return 0
    if cmd == "restore":
        if len(argv) < 2:
            print("usage: restore <id>")
            return 2
        res = restore_snapshot(home, argv[1])
        if not res["applied"]:
            print(f"Snapshot {argv[1]} not found.")
            return 1
        print(f"Restored {argv[1]}.")
        if res["skipped"]:
            print(f"Skipped (data-loss guard): {', '.join(res['skipped'])}")
        return 0
    if cmd == "delete":
        if len(argv) < 2:
            print("usage: delete <id>")
            return 2
        ok = delete_snapshot(home, argv[1])
        print("Deleted." if ok else "Not found.")
        return 0 if ok else 1
    print(f"Unknown command: {cmd}")
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
