"""CLI for ``hermes htr …`` — read-only HTR observation and planning."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from htr.action_plan import (
    EXIT_INVOCATION as PLAN_EXIT_INVOCATION,
    PlanningIntent,
    build_action_plan,
    compute_plan_exit_code,
    make_invocation_error,
)
from htr.observe import (
    EXIT_INVOCATION,
    ObserveInvocationError,
    build_run_snapshot,
    compute_exit_code,
)


def _print_observe_summary(snapshot: dict[str, Any], *, stream: Any = None) -> None:
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


def _print_plan_summary(plan: dict[str, Any], *, stream: Any = None) -> None:
    out = stream if stream is not None else sys.stderr
    print(
        f"run {plan.get('run_id')}  "
        f"state={plan.get('plan_state')}  "
        f"action={(plan.get('requested_intent') or {}).get('requested_action')}  "
        f"execution_eligible={(plan.get('automation_eligibility') or {}).get('execution_eligible')}",
        file=out,
    )


def _load_inputs_file(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read inputs file: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"inputs file is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("inputs file must contain a JSON object")
    return data


def _handle_plan(args) -> int:
    base_dir = Path(args.runs_root) if args.runs_root else None

    if getattr(args, "inputs_file", None) and not getattr(args, "action", None):
        error = make_invocation_error(
            "inputs_without_action",
            "--inputs-file requires --action",
        )
        print(json.dumps(error, indent=2, ensure_ascii=False))
        return PLAN_EXIT_INVOCATION

    action_inputs = None
    if getattr(args, "inputs_file", None):
        try:
            action_inputs = _load_inputs_file(Path(args.inputs_file))
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            error = make_invocation_error("invalid_inputs_file", str(exc))
            print(json.dumps(error, indent=2, ensure_ascii=False))
            return PLAN_EXIT_INVOCATION

    try:
        snapshot = build_run_snapshot(args.run_id, base_dir=base_dir)
    except ObserveInvocationError as exc:
        print(str(exc), file=sys.stderr)
        error = make_invocation_error("observe_failed", str(exc))
        print(json.dumps(error, indent=2, ensure_ascii=False))
        return PLAN_EXIT_INVOCATION

    intent = PlanningIntent(
        requested_action=getattr(args, "action", None),
        action_inputs=action_inputs,
        project_repository_checkpoint=getattr(args, "project_checkpoint", None),
        htr_runs_root=str(base_dir) if base_dir is not None else None,
        remediation_oriented=bool(getattr(args, "remediation_intent", False)),
    )
    plan = build_action_plan(snapshot, intent)

    if args.summary:
        _print_plan_summary(plan)

    print(json.dumps(plan, indent=2, ensure_ascii=False))
    return compute_plan_exit_code(plan)


def htr_command(args) -> int:
    """Dispatch ``hermes htr`` subcommands."""
    if args.htr_command == "observe":
        base_dir = Path(args.runs_root) if args.runs_root else None
        try:
            snapshot = build_run_snapshot(args.run_id, base_dir=base_dir)
        except ObserveInvocationError as exc:
            print(str(exc), file=sys.stderr)
            return EXIT_INVOCATION

        if args.summary:
            _print_observe_summary(snapshot)

        print(json.dumps(snapshot, indent=2, ensure_ascii=False))
        return compute_exit_code(snapshot, strict=bool(args.strict))

    if args.htr_command == "plan":
        return _handle_plan(args)

    print(f"unknown htr subcommand: {args.htr_command!r}", file=sys.stderr)
    return EXIT_INVOCATION
