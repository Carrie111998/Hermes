"""CLI for ``hermes htr …`` — read-only HTR observation."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from htr.observe import (
    EXIT_INVOCATION,
    ObserveInvocationError,
    build_run_snapshot,
    compute_exit_code,
)


def _print_summary(snapshot: dict[str, Any], *, stream: Any = None) -> None:
    import sys

    out = stream if stream is not None else sys.stderr
    integrity = snapshot.get("integrity", {})
    phase1 = snapshot.get("phase1_chain", {})
    print(
        f"run {snapshot.get('run_id')}  "
        f"integrity={integrity.get('status')}  "
        f"errors={integrity.get('error_count', 0)}  "
        f"chain_complete={phase1.get('chain_complete')}  "
        f"terminal_reached={phase1.get('terminal_reached')}",
        file=out,
    )


def htr_command(args) -> int:
    """Dispatch ``hermes htr`` subcommands."""
    if args.htr_command != "observe":
        print(f"unknown htr subcommand: {args.htr_command!r}", file=sys.stderr)
        return EXIT_INVOCATION

    base_dir = Path(args.runs_root) if args.runs_root else None
    try:
        snapshot = build_run_snapshot(args.run_id, base_dir=base_dir)
    except ObserveInvocationError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_INVOCATION

    if args.summary:
        _print_summary(snapshot)

    print(json.dumps(snapshot, indent=2, ensure_ascii=False))
    return compute_exit_code(snapshot, strict=bool(args.strict))
