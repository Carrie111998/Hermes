"""Operator CLI for business-lane contracts."""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
from typing import Any

import yaml

from hermes_constants import get_default_hermes_root
from hermes_cli.cost.ledger import ensure_migrated as ensure_cost_migrated
from hermes_cli.lanes import approvals, metrics, schema
from hermes_cli.lanes.errors import LaneError, LaneModuleNotFound
from hermes_cli.lanes.harness import LaneHarness
from hermes_cli.lanes.manifest import (
    LaneManifestError,
    default_path,
    load_manifest,
)
from hermes_cli.lanes.registry import LaneRegistry
from hermes_cli.programme.gate import get_state

EXIT_SUCCESS = 0
EXIT_LANE_ERROR = 1
EXIT_PROGRAMME_REFUSED = 2
EXIT_INVALID_ARGUMENTS = 3
EXIT_INTERNAL_ERROR = 4


def _paths(args: argparse.Namespace) -> tuple[Path, Path]:
    return Path(args.manifest).expanduser(), Path(args.db_path).expanduser()


def _cmd_list(args: argparse.Namespace) -> int:
    manifest_path, db_path = _paths(args)
    registry = LaneRegistry(manifest_path=manifest_path, db_path=db_path)
    conn = schema.connect(db_path)
    try:
        print("lane  enabled  publish  channel  last_activity")
        for lane in registry.list():
            row = conn.execute(
                """SELECT MAX(ingested_at) AS last_activity FROM lane_task
                   WHERE lane_id=?""",
                (lane.lane_id,),
            ).fetchone()
            print(
                f"{lane.lane_id}  {'ENABLED' if lane.enabled else 'DISABLED'}"
                f"  {'ON' if lane.publish_enabled else 'OFF'}"
                f"  {lane.approval_channel}"
                f"  {row['last_activity'] or '-'}"
            )
    finally:
        conn.close()
    return EXIT_SUCCESS


def _cmd_describe(args: argparse.Namespace) -> int:
    manifest_path, db_path = _paths(args)
    registry = LaneRegistry(manifest_path=manifest_path, db_path=db_path)
    config = registry.config(args.lane)
    ensure_cost_migrated(db_path)
    conn = schema.connect(db_path)
    try:
        tasks = conn.execute(
            """SELECT id,external_id,status,ingested_at FROM lane_task
               WHERE lane_id=? ORDER BY id DESC LIMIT 10""",
            (config.lane_id,),
        ).fetchall()
        approvals_row = conn.execute(
            """SELECT COUNT(*) AS count FROM lane_approval_queue
               WHERE lane_id=? AND status='pending'""",
            (config.lane_id,),
        ).fetchone()
        cost_row = conn.execute(
            """SELECT COALESCE(SUM(aud_amount),0) AS total
                 FROM cost_ledger WHERE lane=?""",
            (config.lane_id,),
        ).fetchone()
    finally:
        conn.close()
    print(f"lane_id: {config.lane_id}")
    print(f"enabled: {str(config.enabled).lower()}")
    print(f"publish_enabled: {str(config.publish_enabled).lower()}")
    print(f"module: {config.module}")
    try:
        module_status = (
            "RESOLVABLE"
            if importlib.util.find_spec(config.module) is not None
            else "ABSENT"
        )
    except (ImportError, ModuleNotFoundError):
        module_status = "ABSENT"
    print(f"module_status: {module_status}")
    print(f"approval_channel: {config.approval_channel}")
    print(f"pending_approvals: {int(approvals_row['count'])}")
    print(f"aggregate_cost_aud: {float(cost_row['total']):.4f}")
    print(f"recent_tasks: {len(tasks)}")
    for row in tasks:
        print(
            f"  {row['id']} {row['external_id']} "
            f"{row['status']} {row['ingested_at']}"
        )
    aggregate = metrics.aggregate(config.lane_id, db_path=db_path)
    print(f"metrics: {json.dumps(aggregate, sort_keys=True)}")
    return EXIT_SUCCESS


def _cmd_approvals_list(args: argparse.Namespace) -> int:
    _, db_path = _paths(args)
    rows = approvals.list_pending(lane_id=args.lane, db_path=db_path)
    if not rows:
        print("No pending lane approvals.")
        return EXIT_SUCCESS
    print("token  lane  task  channel  expires_at")
    for row in rows:
        print(
            f"{row['approval_token']}  {row['lane_id']}  "
            f"{row['lane_task_id']}  {row['channel']}  {row['expires_at']}"
        )
    return EXIT_SUCCESS


def _cmd_approvals_grant(args: argparse.Namespace) -> int:
    _, db_path = _paths(args)
    result = approvals.grant(args.token, note=args.note, db_path=db_path)
    print(f"Approval {result.token}: granted")
    return EXIT_SUCCESS


def _cmd_approvals_reject(args: argparse.Namespace) -> int:
    _, db_path = _paths(args)
    result = approvals.reject(
        args.token,
        reason=args.reason,
        db_path=db_path,
    )
    print(f"Approval {result.token}: rejected")
    return EXIT_SUCCESS


def _read_raw_manifest(path: Path) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise LaneManifestError(f"cannot load lane manifest: {exc}") from exc
    if not isinstance(raw, dict):
        raise LaneManifestError("lane manifest must be a mapping")
    return raw


def _write_raw_manifest(path: Path, raw: dict[str, Any]) -> None:
    encoded = yaml.safe_dump(raw, sort_keys=False)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(encoded, encoding="utf-8")
    temporary.replace(path)


def _set_enabled(args: argparse.Namespace, enabled: bool) -> int:
    manifest_path, db_path = _paths(args)
    raw = _read_raw_manifest(manifest_path)
    target = None
    for item in raw.get("lanes") or []:
        if item.get("lane_id") == args.lane:
            target = item
            break
    if target is None:
        raise LaneManifestError(f"unknown lane: {args.lane}")
    if args.publish_enabled is not None:
        target["publish_enabled"] = args.publish_enabled == "true"
    if (
        enabled
        and bool(target.get("publish_enabled"))
        and not args.i_understand_lane_will_write_external_side_effects
    ):
        raise LaneManifestError(
            "enabling a publish-capable lane requires "
            "--i-understand-lane-will-write-external-side-effects"
        )
    if enabled:
        module_name = str(target.get("module"))
        try:
            module_spec = importlib.util.find_spec(module_name)
        except (ImportError, ModuleNotFoundError):
            module_spec = None
        if module_spec is None:
            raise LaneModuleNotFound(
                f"lane module is not installed: {module_name}"
            )
    target["enabled"] = bool(enabled)
    _write_raw_manifest(manifest_path, raw)
    load_manifest(
        manifest_path,
        db_path=db_path,
        record_state=True,
        applied_by=(
            f"hermes lanes {'enable' if enabled else 'disable'}: "
            f"{args.reason}"
        ),
    )
    print(
        f"{args.lane}: {'enabled' if enabled else 'disabled'} "
        f"({args.reason})"
    )
    return EXIT_SUCCESS


def _cmd_run(args: argparse.Namespace) -> int:
    manifest_path, db_path = _paths(args)
    if not args.dry_run:
        state = get_state(db_path=db_path, migrate_if_missing=False)
        if state.state != "RUNNING":
            print(
                f"LANE RUN REFUSED: programme is {state.state}; "
                "use --dry-run for simulation"
            )
            return EXIT_PROGRAMME_REFUSED
    registry = LaneRegistry(manifest_path=manifest_path, db_path=db_path)
    config = registry.config(args.lane)
    lane_impl = None
    if not args.dry_run:
        lane_impl = registry.activate(args.lane)
    harness = LaneHarness(
        lane_id=config.lane_id,
        db_path=db_path,
        dry_run=bool(args.dry_run),
        manifest_path=manifest_path,
    )
    outcome = None
    if lane_impl is not None and not args.full_cycle:
        run_stage = getattr(lane_impl, "run_stage", None)
        if callable(run_stage):
            outcome = run_stage(stage=args.stage, harness=harness)
    if args.full_cycle:
        print(
            f"{config.lane_id}: ingest -> draft -> approve "
            f"({'dry-run' if args.dry_run else 'manual'}); "
            "publish intentionally skipped"
        )
    else:
        suffix = f"; result={outcome}" if outcome is not None else ""
        print(
            f"{config.lane_id}: {args.stage} "
            f"({'dry-run' if args.dry_run else 'manual'}){suffix}"
        )
    return EXIT_SUCCESS


def dispatch(args: argparse.Namespace) -> int:
    try:
        command = args.lanes_command
        if command == "list":
            return _cmd_list(args)
        if command == "describe":
            return _cmd_describe(args)
        if command in {"enable", "disable"} and not getattr(
            args,
            "lane_id",
            None,
        ):
            # Preserve the pre-CS-20 parser only for existing tests and other
            # explicitly non-production fixtures. The old surface is refused
            # for either real path because it lacks the live guardrails.
            manifest_path, db_path = _paths(args)
            production_root = get_default_hermes_root()
            if (
                manifest_path == default_path()
                or db_path == production_root / "kanban.db"
            ):
                print(
                    "LANE ERROR: retired syntax refused for production; use "
                    f"`hermes lanes {command} <lane_id> "
                    "--i-understand-this-is-live`"
                )
                return 2
            args.lane = args.legacy_lane
            if not args.lane or not args.reason:
                print("LANE ERROR: legacy fixture syntax requires --lane and --reason")
                return 3
            return _set_enabled(args, command == "enable")
        if command == "run":
            return _cmd_run(args)
        if command in {
            "doctor",
            "dry-run",
            "enable",
            "disable",
            "enable-publish",
            "disable-publish",
            "audit",
        }:
            from hermes_cli.subcommands import lanes as safety_cli

            if command == "doctor":
                return safety_cli.run_doctor_command(args)
            if command == "dry-run":
                return safety_cli.run_dry_run_command(args)
            if command == "audit":
                return safety_cli.run_audit_command(args)
            return safety_cli.run_mutation_command(args)
        if command == "approvals":
            action = args.approvals_command
            if action == "list":
                return _cmd_approvals_list(args)
            if action == "grant":
                return _cmd_approvals_grant(args)
            if action == "reject":
                return _cmd_approvals_reject(args)
        return EXIT_INVALID_ARGUMENTS
    except (LaneError, LaneManifestError, ValueError, OSError) as exc:
        print(f"LANE ERROR: {exc}")
        return EXIT_LANE_ERROR
    except Exception as exc:
        print(f"LANE INTERNAL ERROR: {type(exc).__name__}: {exc}")
        return EXIT_INTERNAL_ERROR


def _entry(args: argparse.Namespace) -> None:
    code = dispatch(args)
    if code:
        raise SystemExit(code)


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--manifest",
        default=str(default_path()),
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--db-path",
        default=str(get_default_hermes_root() / "kanban.db"),
        help=argparse.SUPPRESS,
    )


def register_cli(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "lanes",
        help="Inspect and operate business-lane contracts",
        epilog=(
            "Exit codes: 0 success; 1 lane error; 2 programme refusal; "
            "3 invalid arguments; 4 internal error."
        ),
    )
    children = parser.add_subparsers(dest="lanes_command", required=True)

    listing = children.add_parser("list", help="List registered lanes")
    _common(listing)

    describe = children.add_parser("describe", help="Describe one lane")
    describe.add_argument("--lane", required=True)
    _common(describe)

    approvals_parser = children.add_parser(
        "approvals",
        help="List or decide owner approvals",
    )
    approval_children = approvals_parser.add_subparsers(
        dest="approvals_command",
        required=True,
    )
    approval_list = approval_children.add_parser("list")
    approval_list.add_argument("--lane")
    _common(approval_list)
    approval_grant = approval_children.add_parser("grant")
    approval_grant.add_argument("--token", required=True)
    approval_grant.add_argument("--note")
    _common(approval_grant)
    approval_reject = approval_children.add_parser("reject")
    approval_reject.add_argument("--token", required=True)
    approval_reject.add_argument("--reason", required=True)
    _common(approval_reject)

    run = children.add_parser("run", help="Manually exercise one lane stage")
    run.add_argument("--lane", required=True)
    modes = run.add_mutually_exclusive_group(required=True)
    modes.add_argument(
        "--stage",
        choices=(
            "ingest",
            "draft",
            "digest",
            "approve",
            "publish",
            "cleanup",
        ),
    )
    modes.add_argument("--full-cycle", action="store_true")
    run.add_argument("--dry-run", action="store_true")
    _common(run)

    from hermes_cli.subcommands.lanes import (
        register_cli as register_safety_cli,
    )

    register_safety_cli(children, add_common=_common)

    parser.set_defaults(func=_entry)


__all__ = [
    "EXIT_INTERNAL_ERROR",
    "EXIT_INVALID_ARGUMENTS",
    "EXIT_LANE_ERROR",
    "EXIT_PROGRAMME_REFUSED",
    "EXIT_SUCCESS",
    "dispatch",
    "register_cli",
]
