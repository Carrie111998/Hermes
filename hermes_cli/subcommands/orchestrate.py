"""Operator-facing commands for governed builder dispatches."""

from __future__ import annotations

import json
import os
from pathlib import Path


DEFAULT_CONFIG = "~/.hermes/builder-adapter/runtime.json"


def build_orchestrate_parser(subparsers, *, cmd_orchestrate):
    parser = subparsers.add_parser(
        "orchestrate", help="Run and monitor governed implementation jobs"
    )
    parser.add_argument(
        "--config",
        default=os.environ.get("HERMES_BUILDER_ADAPTER_CONFIG", DEFAULT_CONFIG),
        help="Owner-only builder adapter runtime configuration",
    )
    parser.add_argument("--key-id", help="Authentication key when more than one is active")
    actions = parser.add_subparsers(dest="orchestrate_action", required=True)

    actions.add_parser("health", help="Check whether the local adapter is reachable")
    actions.add_parser("cycles", help="List jobs registered by the owner")

    start = actions.add_parser("start", help="Start one registered implementation job")
    start.add_argument("cycle_id")
    start.add_argument("--dispatch-id", help="Reuse a previously chosen UUID for idempotent recovery")

    for name, help_text in (
        ("status", "Show the current job state"),
        ("evidence", "Show completion evidence for a finished job"),
    ):
        action = actions.add_parser(name, help=help_text)
        action.add_argument("dispatch_id")
        action.add_argument("--cycle", required=True, dest="cycle_id")

    cancel = actions.add_parser("cancel", help="Cancel a running job and terminate its worker")
    cancel.add_argument("dispatch_id")
    cancel.add_argument("--cycle", required=True, dest="cycle_id")
    cancel.add_argument(
        "--reason",
        default="HUMAN_CANCELLED",
        choices=["HUMAN_CANCELLED", "CONTRACT_SUPERSEDED", "TIMEOUT", "GOVERNANCE_REJECTED"],
    )
    parser.set_defaults(func=cmd_orchestrate)
    return parser


def _client(args, *, authenticated: bool = True):
    from plugins.builder_adapter.client import BuilderAdapterClient, load_operator_key
    from plugins.builder_adapter.runtime import RuntimeSettings

    settings = RuntimeSettings.from_file(Path(args.config).expanduser())
    key = load_operator_key(settings, args.key_id) if authenticated else None
    return settings, BuilderAdapterClient(settings.socket_path, key)


def _print_result(result: dict, *, raw: bool = False) -> None:
    if raw:
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    print(f"Status: {result.get('status', 'UNKNOWN')}")
    if result.get("dispatch_id"):
        print(f"Dispatch: {result['dispatch_id']}")
    if result.get("cycle_id"):
        print(f"Cycle: {result['cycle_id']}")
    if result.get("kanban_task_id"):
        print(f"Builder task: {result['kanban_task_id']}")
    if result.get("attempt_count") is not None:
        print(f"Attempts: {result['attempt_count']}")
    for error in result.get("errors", []):
        print(f"Error: {error.get('code')}: {error.get('message')}")


def run_operator_command(args) -> int:
    from plugins.builder_adapter.errors import AdapterError

    try:
        if args.orchestrate_action == "health":
            _, client = _client(args, authenticated=False)
            result = client.health()
            state = "reachable" if "capability_id" in result else "invalid response"
            print(f"Adapter: {state}")
            print(f"Capability: {result.get('capability_id', 'unknown')}")
            return 0

        if args.orchestrate_action == "cycles":
            settings, _ = _client(args, authenticated=False)
            if not settings.cycle_registry:
                print("No registered jobs.")
                return 0
            for cycle_id, cycle in sorted(settings.cycle_registry.items()):
                print(
                    f"{cycle_id}  repo={cycle.get('repository_id')}  "
                    f"revision={cycle.get('revision')}  branch={cycle.get('branch')}"
                )
            return 0

        settings, client = _client(args)
        if args.orchestrate_action == "start":
            cycle = settings.cycle_registry.get(args.cycle_id)
            if not isinstance(cycle, dict):
                raise AdapterError("CONTRACT_MISMATCH", "cycle is not registered")
            result = client.start(args.cycle_id, cycle, dispatch_id=args.dispatch_id)
            _print_result(result)
            print(
                "Next: hermes orchestrate status "
                f"{result['dispatch_id']} --cycle {args.cycle_id}"
            )
            return 0

        if args.orchestrate_action in {"status", "evidence"}:
            result = client.status(args.dispatch_id, args.cycle_id)
            if args.orchestrate_action == "evidence":
                evidence = result.get("completion_evidence")
                if evidence is None:
                    print(f"No completion evidence yet (status: {result.get('status', 'UNKNOWN')}).")
                    return 2
                print(json.dumps(evidence, indent=2, sort_keys=True))
            else:
                _print_result(result)
            return 0

        result = client.cancel(args.dispatch_id, args.cycle_id, args.reason)
        _print_result(result)
        return 0
    except AdapterError as error:
        print(f"Orchestrator error [{error.code}]: {error.safe_message}")
        return 2


def cmd_orchestrate(args):
    raise SystemExit(run_operator_command(args))
