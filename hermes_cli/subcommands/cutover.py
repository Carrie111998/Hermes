"""CLI surface for read-only cutover rehearsal and verification."""

from __future__ import annotations

import argparse

from hermes_constants import get_default_hermes_root
from hermes_cli.cutover.rehearse import rehearse_cutover
from hermes_cli.cutover.verify import verify_cutover


def run_rehearse_command(args: argparse.Namespace) -> int:
    report = rehearse_cutover(
        db_path=args.db_path,
        lane_manifest_path=args.lane_manifest_path,
        service_manifest_path=args.service_manifest_path,
        repo_root=args.repo_root,
    )
    print(report.to_json())
    print()
    print(report.to_markdown())
    return report.exit_code


def run_verify_command(args: argparse.Namespace) -> int:
    report = verify_cutover(
        restart_not_before=args.restart_not_before,
        db_path=args.db_path,
        lane_manifest_path=args.lane_manifest_path,
        service_manifest_path=args.service_manifest_path,
        doctrine_seed_path=args.doctrine_seed_path,
        repo_root=args.repo_root,
    )
    print(report.to_json())
    print()
    print(report.to_markdown())
    return report.exit_code


def cli_command(args: argparse.Namespace) -> int:
    handler = getattr(args, "cutover_handler", run_rehearse_command)
    exit_code = handler(args)
    if exit_code:
        raise SystemExit(exit_code)
    return 0


def register_cli(subparsers: argparse._SubParsersAction) -> None:
    root = get_default_hermes_root()
    parser = subparsers.add_parser(
        "cutover",
        help="Inspect the operator-controlled cutover sequence",
    )
    children = parser.add_subparsers(
        dest="cutover_command",
        required=True,
    )
    rehearse = children.add_parser(
        "rehearse",
        help="Run a signed read-only cutover rehearsal",
    )
    rehearse.add_argument(
        "--db-path",
        default=str(root / "kanban.db"),
        help=argparse.SUPPRESS,
    )
    rehearse.add_argument(
        "--lane-manifest-path",
        default=str(root / "lane_manifest.yaml"),
        help=argparse.SUPPRESS,
    )
    rehearse.add_argument(
        "--service-manifest-path",
        default=str(root / "service_manifest.yaml"),
        help=argparse.SUPPRESS,
    )
    rehearse.add_argument(
        "--repo-root",
        default=None,
        help=argparse.SUPPRESS,
    )
    rehearse.set_defaults(
        func=cli_command,
        cutover_handler=run_rehearse_command,
    )

    verify = children.add_parser(
        "verify",
        help="Verify a freshly restarted cutover without live writes",
    )
    verify.add_argument(
        "--restart-not-before",
        default=None,
        metavar="ISO_TIMESTAMP",
        help=(
            "Require every protected process to have started after this "
            "timestamp (default: 30 minutes before verification)"
        ),
    )
    verify.add_argument(
        "--db-path",
        default=str(root / "kanban.db"),
        help=argparse.SUPPRESS,
    )
    verify.add_argument(
        "--lane-manifest-path",
        default=str(root / "lane_manifest.yaml"),
        help=argparse.SUPPRESS,
    )
    verify.add_argument(
        "--service-manifest-path",
        default=str(root / "service_manifest.yaml"),
        help=argparse.SUPPRESS,
    )
    verify.add_argument(
        "--doctrine-seed-path",
        default=None,
        help=argparse.SUPPRESS,
    )
    verify.add_argument(
        "--repo-root",
        default=None,
        help=argparse.SUPPRESS,
    )
    verify.set_defaults(
        func=cli_command,
        cutover_handler=run_verify_command,
    )


__all__ = [
    "cli_command",
    "register_cli",
    "run_rehearse_command",
    "run_verify_command",
]
