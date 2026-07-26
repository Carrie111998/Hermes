"""Read-only lane doctor and fixture-backed dry-run CLI handlers."""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable

from hermes_cli.lanes import enable as lane_enable
from hermes_cli.lanes.doctor import run_lane_doctor
from hermes_cli.lanes.dry_run import run_lane_dry_run
from hermes_cli.lanes.harness import DryRunViolation


def run_doctor_command(args: argparse.Namespace) -> int:
    report = run_lane_doctor(
        args.lane_id,
        manifest_path=args.manifest,
        db_path=args.db_path,
    )
    print(report.to_json())
    return report.exit_code


def run_dry_run_command(args: argparse.Namespace) -> int:
    try:
        report = run_lane_dry_run(
            args.lane_id,
            stage=args.stage,
            manifest_path=args.manifest,
            db_path=args.db_path,
        )
    except DryRunViolation as exc:
        print(
            json.dumps(
                {
                    "error": f"DryRunViolation: {exc}",
                    "lane_id": args.lane_id,
                    "stage": args.stage,
                    "success": False,
                },
                sort_keys=True,
            )
        )
        return 1
    print(report.to_json())
    return report.exit_code


def run_mutation_command(args: argparse.Namespace) -> int:
    handlers = {
        "enable": lane_enable.enable_lane,
        "disable": lane_enable.disable_lane,
        "enable-publish": lane_enable.enable_publish,
        "disable-publish": lane_enable.disable_publish,
    }
    handler = handlers[args.lanes_command]
    result = handler(
        args.lane_id,
        bool(args.i_understand_this_is_live),
        manifest_path=args.manifest,
        db_path=args.db_path,
        notes=args.notes,
    )
    print(result.to_json())
    return result.exit_code


def run_audit_command(args: argparse.Namespace) -> int:
    rows = lane_enable.list_audit(
        args.lane_id,
        limit=args.limit,
        db_path=args.db_path,
    )
    if not rows:
        print(f"no audit rows for {args.lane_id}")
        return 0
    print(json.dumps(rows, ensure_ascii=False, sort_keys=True))
    return 0


def _add_mutation_parser(
    children: argparse._SubParsersAction,
    name: str,
    *,
    add_common: Callable[[argparse.ArgumentParser], None],
) -> None:
    parser = children.add_parser(
        name,
        help=f"Guarded live lane {name.replace('-', ' ')}",
    )
    parser.add_argument("lane_id", nargs="?")
    parser.add_argument("--i-understand-this-is-live", action="store_true")
    parser.add_argument("--notes")
    # Temporary-test compatibility only. hermes_cli.lanes.cli refuses this
    # retired syntax against either production path.
    if name in {"enable", "disable"}:
        parser.add_argument("--lane", dest="legacy_lane")
        parser.add_argument("--reason")
        parser.add_argument(
            "--publish-enabled",
            choices=("true", "false"),
        )
        parser.add_argument(
            "--i-understand-lane-will-write-external-side-effects",
            action="store_true",
        )
    add_common(parser)


def register_cli(
    children: argparse._SubParsersAction,
    *,
    add_common: Callable[[argparse.ArgumentParser], None],
) -> None:
    doctor = children.add_parser(
        "doctor",
        help="Run read-only structural checks for one lane",
    )
    doctor.add_argument("lane_id")
    add_common(doctor)

    dry_run = children.add_parser(
        "dry-run",
        help="Exercise one lane with fixtures and no durable writes",
    )
    dry_run.add_argument("lane_id")
    dry_run.add_argument(
        "--stage",
        choices=("ingest", "digest", "full"),
        default="full",
    )
    add_common(dry_run)

    for name in (
        "enable",
        "disable",
        "enable-publish",
        "disable-publish",
    ):
        _add_mutation_parser(
            children,
            name,
            add_common=add_common,
        )

    audit = children.add_parser(
        "audit",
        help="Read lane-manifest mutation history",
    )
    audit.add_argument("lane_id")
    audit.add_argument("--limit", type=int, default=50)
    add_common(audit)


__all__ = [
    "register_cli",
    "run_audit_command",
    "run_doctor_command",
    "run_dry_run_command",
    "run_mutation_command",
]
