#!/usr/bin/env python3
"""Validate or prepare a controlled Workflow v1 Phase 1/2 pilot manifest."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from hermes_cli import kanban_db as kb  # noqa: E402
from hermes_cli.kanban_pilot_runner import (  # noqa: E402
    PilotPlan,
    PilotSafetyError,
    assert_runner_source,
    prepare_pilot,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail-closed preparation for a disposable Workflow v1 Phase 1/2 pilot"
    )
    parser.add_argument(
        "--runner-source-tree",
        required=True,
        help="exact clean HEAD tree approved by immutable source review",
    )
    parser.add_argument("action", choices=("validate", "prepare"))
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args(argv)
    try:
        assert_runner_source(_REPO_ROOT, args.runner_source_tree)
        plan = PilotPlan.load(args.manifest)
        result: dict[str, object] = {
            "status": "valid",
            "schema": "hermes.workflow-pilot.v1",
            "board": plan.board,
            "pin_sha": plan.pin_sha,
            "concurrency": plan.concurrency,
            "leaves": [leaf.logical_key for leaf in plan.leaves],
            "dispatch_enabled": False,
            "workers_launched": False,
        }
        if args.action == "prepare":
            # Board selection is explicit and manifest-owned. Preparation still
            # fails unless both durable launch gates are currently off.
            os.environ["HERMES_KANBAN_BOARD"] = plan.board
            kb.init_db()
            with kb.connect() as conn:
                prepared = prepare_pilot(conn, plan)
            result.update(
                status="prepared",
                manifest_digest=prepared.manifest_digest,
                task_ids=dict(prepared.task_ids),
            )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (PilotSafetyError, PermissionError) as exc:
        print(
            json.dumps({"status": "rejected", "error": str(exc)}, sort_keys=True),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
