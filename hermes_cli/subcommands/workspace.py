"""Read-only ``hermes workspace`` parser and command handler for V1."""
from __future__ import annotations

import json
from typing import Any, Callable

from hermes_cli.workspace_lifecycle import (
    build_closeout_manifest,
    collect_inventory,
    import_dry_run,
)


def build_workspace_parser(subparsers: Any, *, cmd_workspace: Callable[[Any], int]) -> None:
    parser = subparsers.add_parser(
        "workspace",
        help="Inventory and classify worktrees without removal (V1)",
        description="Fail-closed workspace lifecycle controls. V1 commands are read-only.",
    )
    commands = parser.add_subparsers(dest="workspace_command", required=True)
    for name, help_text in (
        ("inventory", "Read Git worktree registrations without writes"),
        ("classify", "Classify Git worktree registrations fail-closed"),
        ("import", "Preview legacy import without writing the registry"),
        ("manifest", "Build a hashed owner-review packet without writes"),
    ):
        child = commands.add_parser(name, help=help_text)
        child.add_argument("--repo", required=True, help="Repository to inspect")
        child.add_argument("--json", action="store_true", required=True, help="Emit stable JSON")
        if name == "import":
            child.add_argument("--dry-run", action="store_true", required=True,
                               help="Required: V1 does not import live records")
    parser.set_defaults(func=cmd_workspace)


def cmd_workspace(args: Any) -> int:
    """Run a non-mutating workspace command and emit its stable JSON report."""
    action = args.workspace_command
    if action == "import":
        report = import_dry_run(args.repo)
    elif action == "manifest":
        report = build_closeout_manifest(args.repo)
    else:
        report = collect_inventory(args.repo)
        if action == "classify":
            report["operation"] = "classify"
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0
