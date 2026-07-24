"""CLI boundary for the opt-in, subscription-aware fleet router."""

from __future__ import annotations

import argparse
import hashlib
import json
import uuid
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

from hermes_cli.config import load_config_readonly
from hermes_cli.fleet.capacity import BridgeUsageAdapter
from hermes_cli.fleet.adapters.live_routes import live_adapters
from hermes_cli.fleet.config import LANE_ORDER, parse_fleet_config
from hermes_cli.fleet.live import FleetQualificationDoctor
from hermes_cli.fleet.profiles import ordered_profiles
from hermes_cli.fleet.service import FleetService
from hermes_cli.fleet.state import FleetStore
from hermes_cli.fleet.types import (
    CapacitySnapshot,
    LaneEvaluation,
    ReasonCode,
    RouteDecision,
    TaskPin,
    TaskSpec,
)
from hermes_constants import get_hermes_home


EXIT_OK = 0
EXIT_USAGE = 2
EXIT_DISABLED = 3
EXIT_NO_ROUTE = 4
EXIT_EXECUTION_FAILED = 5
_MAX_TASK_BYTES = 1024 * 1024
_DEFAULT_CAPABILITIES = frozenset({"workspace_write", "shell"})


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


def _default_service(
    *,
    config_data: Mapping[str, Any] | None = None,
    doctor: FleetQualificationDoctor | None = None,
    adapters: Mapping[str, object] | None = None,
    store_path: Path | None = None,
    now=None,
) -> FleetService:
    """Build the live service from read-only, attributable qualifications."""

    config = parse_fleet_config(
        load_config_readonly() if config_data is None else config_data
    )
    profiles = ordered_profiles()
    qualifications = (doctor or FleetQualificationDoctor()).qualify(profiles)
    return FleetService(
        config=config,
        store=FleetStore(
            store_path or (get_hermes_home() / "fleet" / "state.db")
        ),
        profiles=profiles,
        qualifications=qualifications,
        adapters=dict(
            live_adapters(qualifications=qualifications)
            if adapters is None
            else adapters
        ),
        capacity_source=BridgeUsageAdapter(config.bridge_usage_file),
        now=now,
    )


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


def _capacity(snapshot: CapacitySnapshot | None) -> dict[str, Any] | None:
    if snapshot is None:
        return None
    source_hash = snapshot.source_id.rpartition("#")[2]
    return {
        "lane_id": snapshot.lane_id,
        "source_kind": snapshot.source_kind,
        "source_id": snapshot.source_id,
        "source_hash": source_hash,
        "captured_at": snapshot.captured_at.isoformat(),
        "read_at": snapshot.read_at.isoformat(),
        "expires_at": snapshot.expires_at.isoformat(),
        "freshness": snapshot.freshness.value,
        "confidence": snapshot.confidence.value,
        "schema_version": snapshot.schema_version,
        "used_pct": str(snapshot.used_pct),
        "remaining_pct": str(snapshot.remaining_pct),
        "reserved_pct": str(snapshot.reserved_pct),
        "effective_remaining_pct": str(snapshot.effective_remaining_pct),
        "overage_disabled": snapshot.overage_disabled,
    }


def _evaluation(item: LaneEvaluation) -> dict[str, Any]:
    return {
        "lane_id": item.lane_id,
        "eligible": item.eligible,
        "reasons": [reason.value for reason in item.reasons],
        "adapter_kind": item.profile.adapter_kind.value,
        "provider_id": item.profile.provider_id,
        "model_id": item.selected_model,
        "effort": item.selected_effort,
        "qualification_evidence_id": item.qualification_evidence_id,
        "qualification_detail": item.qualification_detail,
        "capacity": _capacity(item.capacity),
    }


def _evaluations(
    items: Sequence[LaneEvaluation], *, lane: str | None = None
) -> list[dict[str, Any]]:
    return [
        _evaluation(item)
        for item in items
        if lane is None or item.lane_id == lane
    ]


def _selected(decision: RouteDecision) -> dict[str, Any] | None:
    match = next(
        (item for item in decision.evaluations if item.lane_id == decision.lane_id),
        None,
    )
    return _evaluation(match) if match is not None else None


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
    for item in payload.get("evaluations", []):
        capacity = item.get("capacity") or {}
        print(
            f"{item['lane_id']}: {','.join(item['reasons'])} "
            f"adapter={item['adapter_kind']} "
            f"capacity={capacity.get('source_kind', 'none')} "
            f"freshness={capacity.get('freshness', 'unknown')} "
            f"confidence={capacity.get('confidence', 'unknown')}"
        )
    if payload.get("task_id"):
        print(f"task_id={payload['task_id']}")
    if payload.get("output"):
        print(payload["output"])


def _inspection_task(command: str, *, reservation_pct: Decimal) -> TaskSpec:
    return TaskSpec(
        task_id=f"read-only-{command}",
        cwd=Path.cwd(),
        required_capabilities=_DEFAULT_CAPABILITIES,
        reservation_pct=reservation_pct,
    )


def fleet_command(
    args: argparse.Namespace, *, service: FleetService | None = None
) -> int:
    """Execute one fleet subcommand and return a stable process exit code."""

    service = service or _default_service()
    action = args.fleet_action

    if action in {"status", "doctor"}:
        evaluations = service.inspect(
            _inspection_task(
                action,
                reservation_pct=service.config.default_reservation_pct,
            )
        )
        visible = tuple(
            item
            for item in evaluations
            if getattr(args, "lane", None) is None
            or item.lane_id == getattr(args, "lane", None)
        )
        has_route = any(item.eligible for item in visible)
        payload = {
            "schema_version": 1,
            "command": action,
            "ok": True if action == "status" else has_route,
            "enabled": service.config.enabled,
            "reason": (
                ReasonCode.MET.value
                if has_route
                else ReasonCode.NO_ELIGIBLE_LANE.value
            ),
            "evaluations": _evaluations(visible),
        }
        _emit(payload, json_output=args.json)
        return EXIT_OK if action == "status" or has_route else EXIT_NO_ROUTE

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
            "selected": _selected(decision),
            "evaluations": _evaluations(decision.evaluations),
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
            "evaluations": _evaluations(result.evaluations),
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
