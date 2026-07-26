"""Operator CLI for planned, audited service restarts."""

from __future__ import annotations

import argparse
from pathlib import Path

from hermes_constants import get_default_hermes_root
from hermes_cli.programme.gate import get_state
from hermes_cli.service import schema
from hermes_cli.service.manifest import (
    ManifestError,
    compute_restart_order,
    default_manifest_path,
    load_manifest,
)
from hermes_cli.service.runner import (
    ProgrammeRefused,
    RestartRunner,
    latest_status,
)


EXIT_SUCCESS = 0
EXIT_OPERATION_FAILED = 1
EXIT_PROGRAMME_REFUSED = 3
EXIT_STRUCTURAL_ERROR = 4


def _print_plan(manifest) -> None:
    print("Coordinated restart plan")
    if manifest.operator_review_required:
        print("BLOCKED FOR EXECUTION: operator review is still required.")
        if manifest.operator_review_note:
            print(f"Review note: {manifest.operator_review_note}")
    for index, service in enumerate(
        compute_restart_order(manifest),
        start=1,
    ):
        dependencies = ",".join(service.depends_on) or "-"
        tags = ",".join(service.tags) or "-"
        print(
            f"{index}. {service.id} "
            f"(depends_on={dependencies}; tags={tags})"
        )
    print("Plan only: no health checks, signals, or process launches.")


def _print_verification(results, *, heading: str) -> int:
    print(heading)
    failed = False
    for service, result in results:
        label = "PASS" if result.healthy else "FAIL"
        print(
            f"{label}  {service.id}  "
            f"{result.outcome}  {result.output}"
        )
        failed = failed or not result.healthy
    return EXIT_OPERATION_FAILED if failed else EXIT_SUCCESS


def _cmd_restart(args: argparse.Namespace) -> int:
    db_path = Path(args.db_path).expanduser()
    try:
        schema.ensure_migrated(db_path)
        if args.status:
            rows = latest_status(db_path=db_path)
            if not rows:
                print("No service restart runs recorded.")
                return EXIT_SUCCESS
            print("service  phase  outcome  pid  checked_at")
            for row in rows:
                pid = row.get("new_pid") or row.get("old_pid") or "-"
                print(
                    f"{row['service_id']}  {row['phase']}  "
                    f"{row['outcome']}  {pid}  "
                    f"{row.get('phase_ended_at') or '-'}"
                )
            return EXIT_SUCCESS

        manifest = load_manifest(
            args.manifest,
            db_path=db_path,
            record_state=True,
            applied_by="hermes restart",
        )
        if args.plan:
            _print_plan(manifest)
            return EXIT_SUCCESS

        runner = RestartRunner(db_path=db_path)
        if args.verify:
            return _print_verification(
                runner.verify(manifest),
                heading="Service verification",
            )
        if args.dry_run_execute:
            return _print_verification(
                runner.dry_run_execute(manifest),
                heading=(
                    "Dry-run execute: order and health only; "
                    "no process touched"
                ),
            )

        if manifest.operator_review_required:
            note = manifest.operator_review_note or (
                "review the manifest and set operator_review_required=false"
            )
            raise ManifestError(
                "manifest is blocked for execution pending operator review: "
                f"{note}"
            )
        if args.allow_active:
            programme_state = str(
                get_state(
                    db_path,
                    migrate_if_missing=False,
                ).state
            )
            if programme_state == "RUNNING":
                print(
                    "WARNING: programme is RUNNING; proceeding only because "
                    "--allow-active was explicitly supplied."
                )
        result = runner.execute(
            manifest,
            allow_active=bool(args.allow_active),
            reason=args.reason,
        )
        print(
            f"Restart run {result.run_id}: {result.overall_outcome}; "
            f"programme remained {result.programme_state_at_start}."
        )
        return (
            EXIT_SUCCESS
            if result.overall_outcome == "success"
            else EXIT_OPERATION_FAILED
        )
    except ProgrammeRefused as exc:
        print(f"RESTART REFUSED: {exc}")
        return exc.exit_code
    except (ManifestError, OSError, ValueError) as exc:
        print(f"RESTART STRUCTURAL ERROR: {exc}")
        return EXIT_STRUCTURAL_ERROR
    except Exception as exc:
        print(f"RESTART FAILED: {type(exc).__name__}: {exc}")
        return EXIT_OPERATION_FAILED


def register_cli(subparsers: argparse._SubParsersAction) -> None:
    """Register the compact `hermes restart --<action>` surface."""
    # The service schema follows the existing programme bootstrap convention:
    # parser construction is idempotent, so `hermes doctor` and the first
    # `hermes restart` invocation both guarantee the additive tables exist.
    schema.ensure_migrated()
    parser = subparsers.add_parser(
        "restart",
        help="Plan, verify, execute, or inspect coordinated service restarts.",
        epilog=(
            "Exit codes: 0 success; 1 operation/health failure; "
            "3 programme-state refusal; 4 structural manifest/state error."
        ),
    )
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument(
        "--plan",
        action="store_true",
        help="Print dependency order only; no health or process actions.",
    )
    actions.add_argument(
        "--verify",
        action="store_true",
        help="Run health checks only; never touch processes.",
    )
    actions.add_argument(
        "--status",
        action="store_true",
        help="Read the latest restart audit row per service.",
    )
    actions.add_argument(
        "--execute",
        action="store_true",
        help="Execute the manifest restart protocol.",
    )
    actions.add_argument(
        "--dry-run-execute",
        action="store_true",
        help="Exercise order and health only; never touch processes.",
    )
    parser.add_argument(
        "--allow-active",
        action="store_true",
        help="Explicitly allow execution while programme state is RUNNING.",
    )
    parser.add_argument("--reason", help="Operator reason stored on the run.")
    parser.add_argument(
        "--manifest",
        default=str(default_manifest_path()),
        help="Manifest path.",
    )
    parser.add_argument(
        "--db-path",
        default=str(get_default_hermes_root() / "kanban.db"),
        help=argparse.SUPPRESS,
    )
    parser.set_defaults(func=_cmd_restart)


__all__ = [
    "EXIT_OPERATION_FAILED",
    "EXIT_PROGRAMME_REFUSED",
    "EXIT_STRUCTURAL_ERROR",
    "EXIT_SUCCESS",
    "_cmd_restart",
    "register_cli",
]
