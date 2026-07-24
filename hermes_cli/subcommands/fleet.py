"""CLI boundary for the opt-in, subscription-aware fleet router."""

from __future__ import annotations

import argparse
import hashlib
import json
import uuid
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import Any

from hermes_cli.fleet.config import LANE_ORDER
from hermes_cli.fleet.inspection import (
    DEFAULT_CAPABILITIES as _DEFAULT_CAPABILITIES,
    build_fleet_service as _default_service,
    build_inspection_payload,
    serialize_evaluations,
    serialize_selected,
)
from hermes_cli.fleet.service import FleetService
from hermes_cli.fleet.types import (
    ReasonCode,
    TaskPin,
    TaskSpec,
)


EXIT_OK = 0
EXIT_USAGE = 2
EXIT_DISABLED = 3
EXIT_NO_ROUTE = 4
EXIT_EXECUTION_FAILED = 5
_MAX_TASK_BYTES = 1024 * 1024


def _add_json_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="Emit JSON")


def _add_task_args(parser: argparse.ArgumentParser, *, task_id: bool) -> None:
    parser.add_argument("--task-file", required=True, help="UTF-8 task prompt file")
    parser.add_argument("--cwd", default=".", help="Worker working directory")
    if task_id:
        parser.add_argument("--task-id", help="Existing or caller-assigned task UUID")
    _add_json_flag(parser)


def build_fleet_parser(subparsers) -> argparse.ArgumentParser:
    """Register the bounded v1 ``hermes fleet`` command tree."""

    fleet = subparsers.add_parser(
        "fleet",
        help="Route bounded tasks across qualified subscription lanes",
    )
    commands = fleet.add_subparsers(dest="fleet_action", required=True)

    status = commands.add_parser("status", help="Show read-only fleet state")
    _add_json_flag(status)

    doctor = commands.add_parser(
        "doctor", help="Evaluate every fail-closed eligibility gate"
    )
    doctor.add_argument("--lane", choices=LANE_ORDER)
    _add_json_flag(doctor)

    plan = commands.add_parser(
        "plan", help="Select a lane without pinning, reserving, or writing state"
    )
    _add_task_args(plan, task_id=False)

    run = commands.add_parser("run", help="Pin, reserve, and execute one task")
    _add_task_args(run, task_id=True)

    audit = commands.add_parser("audit", help="Read the reason-coded audit log")
    audit.add_argument("--task-id")
    audit.add_argument("--reason")
    audit.add_argument("--jsonl", action="store_true", help="Emit one JSON event per line")

    release = commands.add_parser("release", help="Release a matching live task lease")
    release.add_argument("task_id")
    release.add_argument(
        "--outcome",
        choices=("completed", "failed", "cancelled"),
        default="cancelled",
    )
    _add_json_flag(release)

    fleet.set_defaults(func=fleet_command)
    return fleet


def _task_from_args(
    args: argparse.Namespace,
    *,
    reservation_pct: Decimal,
    plan: bool = False,
) -> tuple[TaskSpec, str]:
    path = Path(args.task_file)
    raw = path.read_bytes()
    if len(raw) > _MAX_TASK_BYTES:
        raise ValueError("task file exceeds 1 MiB")
    try:
        prompt = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("task file must be UTF-8") from exc
    if not prompt.strip():
        raise ValueError("task file must not be empty")
    cwd = Path(args.cwd).resolve()
    if not cwd.is_dir():
        raise ValueError("cwd must be an existing directory")
    task_id = (
        "read-only-plan"
        if plan
        else (getattr(args, "task_id", None) or str(uuid.uuid4()))
    )
    return (
        TaskSpec(
            task_id=task_id,
            cwd=cwd,
            required_capabilities=_DEFAULT_CAPABILITIES,
            reservation_pct=reservation_pct,
            prompt_fingerprint=f"sha256:{hashlib.sha256(raw).hexdigest()}",
        ),
        prompt,
    )


def _pin(pin: TaskPin | None) -> dict[str, Any] | None:
    if pin is None:
        return None
    payload = asdict(pin)
    payload["adapter_kind"] = pin.adapter_kind.value
    return payload


def _emit(payload: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(payload, sort_keys=True))
        return
    print(f"fleet {payload['command']}: {payload.get('reason', 'MET')}")
    selected = payload.get("selected")
    if selected:
        capacity = selected.get("capacity") or {}
        print(
            "selected "
            f"{selected['lane_id']} via {selected['adapter_kind']} "
            f"({capacity.get('source_kind', 'no-capacity')}, "
            f"{capacity.get('freshness', 'unknown')}, "
            f"{capacity.get('confidence', 'unknown')})"
        )
    purposes = payload.get("purposes")
    matrices = (
        [
            (purpose, purposes[purpose])
            for purpose in ("task_worker", "desktop_parent")
            if purpose in purposes
        ]
        if isinstance(purposes, dict)
        else [(None, {"evaluations": payload.get("evaluations", [])})]
    )
    for purpose, matrix in matrices:
        if purpose is not None:
            print(
                f"[{purpose}] enabled={str(bool(matrix.get('enabled'))).lower()} "
                f"eligible={str(bool(matrix.get('eligible'))).lower()}"
            )
        for item in matrix.get("evaluations", []):
            capacity = item.get("capacity") or {}
            comparability = "/".join(
                str(value)
                for value in (
                    capacity.get("comparability_group"),
                    capacity.get("quota_window_id"),
                )
                if value
            )
            print(
                f"{item['lane_id']}: {','.join(item['reasons'])} "
                f"adapter={item['adapter_kind']} "
                f"model={item.get('model_id') or 'none'} "
                f"effort={item.get('effort') or 'none'} "
                f"capacity={capacity.get('source_kind', 'none')} "
                f"hash={capacity.get('source_hash', 'none')} "
                f"captured={capacity.get('captured_at', 'unknown')} "
                f"freshness={capacity.get('freshness', 'unknown')} "
                f"confidence={capacity.get('confidence', 'unknown')} "
                f"comparability={comparability or 'none'}"
            )
    pin_state = payload.get("pin_state")
    if isinstance(pin_state, dict):
        worker_total = (pin_state.get("task_worker") or {}).get("total", 0)
        parent_total = (pin_state.get("desktop_parent") or {}).get("total", 0)
        print(
            f"pins task_worker={worker_total} "
            f"desktop_parent={parent_total}"
        )
    if payload.get("task_id"):
        print(f"task_id={payload['task_id']}")
    if payload.get("output"):
        print(payload["output"])


def fleet_command(
    args: argparse.Namespace, *, service: FleetService | None = None
) -> int:
    """Execute one fleet subcommand and return a stable process exit code."""

    service = service or _default_service()
    action = args.fleet_action

    if action in {"status", "doctor"}:
        payload = build_inspection_payload(
            service,
            command=action,
            lane=getattr(args, "lane", None),
        )
        _emit(payload, json_output=args.json)
        return EXIT_OK if action == "status" or payload["ok"] else EXIT_NO_ROUTE

    if action == "plan":
        try:
            task, _ = _task_from_args(
                args,
                reservation_pct=service.config.default_reservation_pct,
                plan=True,
            )
        except (OSError, ValueError) as exc:
            payload = {
                "schema_version": 1,
                "command": action,
                "ok": False,
                "reason": str(exc),
            }
            _emit(payload, json_output=args.json)
            return EXIT_USAGE
        decision = service.plan(task)
        payload = {
            "schema_version": 1,
            "command": action,
            "ok": decision.lane_id is not None,
            "reason": decision.reason.value,
            "selected": serialize_selected(decision),
            "evaluations": serialize_evaluations(decision.evaluations),
        }
        _emit(payload, json_output=args.json)
        return EXIT_OK if decision.lane_id is not None else EXIT_NO_ROUTE

    if action == "run":
        try:
            task, prompt = _task_from_args(
                args,
                reservation_pct=service.config.default_reservation_pct,
            )
        except (OSError, ValueError) as exc:
            payload = {
                "schema_version": 1,
                "command": action,
                "ok": False,
                "reason": str(exc),
            }
            _emit(payload, json_output=args.json)
            return EXIT_USAGE
        result = service.run(task, prompt=prompt)
        payload = {
            "schema_version": 1,
            "command": action,
            "ok": result.ok,
            "task_id": result.task_id,
            "reason": result.reason.value,
            "pin": _pin(result.pin),
            "output": (
                result.adapter_result.output if result.adapter_result is not None else ""
            ),
            "evaluations": serialize_evaluations(result.evaluations),
        }
        _emit(payload, json_output=args.json)
        if result.ok:
            return EXIT_OK
        if result.reason is ReasonCode.FLEET_DISABLED:
            return EXIT_DISABLED
        if result.reason in {
            ReasonCode.NO_ELIGIBLE_LANE,
            ReasonCode.PINNED_LANE_UNAVAILABLE,
        }:
            return EXIT_NO_ROUTE
        return EXIT_EXECUTION_FAILED

    if action == "audit":
        events = service.store.audit(task_id=args.task_id, reason=args.reason)
        if args.jsonl:
            for event in events:
                print(json.dumps(event, sort_keys=True))
        else:
            for event in events:
                print(
                    f"{event['at']} {event['event_type']} "
                    f"task={event['task_id'] or '-'} lane={event['lane_id'] or '-'} "
                    f"reason={event['reason_code']}"
                )
        return EXIT_OK

    if action == "release":
        released = service.release_task(args.task_id, outcome=args.outcome)
        payload = {
            "schema_version": 1,
            "command": action,
            "ok": released,
            "released": released,
            "task_id": args.task_id,
            "outcome": args.outcome,
            "reason": ReasonCode.MET.value if released else "LEASE_NOT_FOUND",
        }
        _emit(payload, json_output=args.json)
        return EXIT_OK if released else EXIT_NO_ROUTE

    raise ValueError(f"unknown fleet action: {action}")
