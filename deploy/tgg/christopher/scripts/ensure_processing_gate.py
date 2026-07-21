#!/usr/bin/env python3
"""Create Christopher's processing gate once; preserve valid live state thereafter."""

from __future__ import annotations

import argparse
import datetime
import json
import os
from pathlib import Path


def ensure_processing_gate(path: Path) -> dict[str, object]:
    if path.exists():
        state = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(state, dict) or not isinstance(state.get("enabled"), bool):
            raise RuntimeError("processing gate enabled state must be boolean")
        if type(state.get("generation")) is not int or state["generation"] < 0:
            raise RuntimeError("processing gate generation must be a non-negative integer")
        return state

    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    state: dict[str, object] = {
        "version": 1,
        "enabled": False,
        "generation": 0,
        "initial_state": "disabled",
        "initial_disabled_boundary": now,
        "disabled_at": now,
        "last_transition": None,
        "source": "ClientAgentDeployment",
    }
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
    with os.fdopen(fd, "w") as handle:
        json.dump(state, handle, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    return state


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path")
    args = parser.parse_args()
    ensure_processing_gate(Path(args.path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
