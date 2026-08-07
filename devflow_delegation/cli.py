"""DDP control-plane CLI — for scripts/producers that cannot import the
package in-process, and for operator/script-slot ticks.

    <request-json> | python -m devflow_delegation.cli delegate [--dry-run]
    python -m devflow_delegation.cli status
    python -m devflow_delegation.cli reconcile
    python -m devflow_delegation.cli transition --request-id RID --to STATE --actor NAME

Exit codes: 0 completed (any policy outcome), 2 bad input, 1 unexpected error.
"""
from __future__ import annotations

import argparse
import json
import sys

from devflow_delegation.emitter import DelegationEmitter
from devflow_delegation.lifecycle import IllegalTransitionError, transition


def _cmd_delegate(args) -> int:
    try:
        kwargs = json.loads(sys.stdin.read())
    except ValueError as exc:
        print(f"ERROR: stdin is not valid JSON: {exc}", file=sys.stderr)
        return 2
    if not isinstance(kwargs, dict):
        print("ERROR: stdin JSON must be an object of delegate() kwargs", file=sys.stderr)
        return 2
    if args.dry_run:
        kwargs["mode"] = "dry_run"
    try:
        result = DelegationEmitter().delegate(**kwargs)
    except TypeError as exc:
        print(f"ERROR: bad delegate kwargs: {exc}", file=sys.stderr)
        return 2
    print(f"status={result.status} request_id={result.request_id} "
          f"fingerprint={result.fingerprint} reason={result.reason}")
    return 0


def _cmd_status(args) -> int:
    em = DelegationEmitter()
    counts = em.ledger.summary_counts()
    print(f"total={counts['total']} by_state={json.dumps(counts['by_state'])} "
          f"by_source={json.dumps(counts['by_source'])}")
    requested = em.ledger.list_requests(state="REQUESTED", limit=1)
    if requested:
        print(f"oldest_requested={requested[0]['created_at']} request_id={requested[0]['request_id']}")
    return 0


def _cmd_reconcile(args) -> int:
    counts = DelegationEmitter().reconcile()
    print(f"adopted={counts['adopted']} rewritten={counts['rewritten']}")
    return 0


def _cmd_transition(args) -> int:
    em = DelegationEmitter()
    try:
        new_state = transition(
            em.ledger, em.bus, args.request_id, args.to,
            actor=args.actor, evidence_ref=args.evidence_ref)
    except IllegalTransitionError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(f"request_id={args.request_id} state={new_state}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="devflow_delegation")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_delegate = sub.add_parser("delegate", help="queue a work request (JSON kwargs on stdin)")
    p_delegate.add_argument("--dry-run", action="store_true",
                            help="classify only; no ledger/mailbox/event side effects")
    p_delegate.set_defaults(func=_cmd_delegate)

    p_status = sub.add_parser("status", help="queue depth by state/source")
    p_status.set_defaults(func=_cmd_status)

    p_reconcile = sub.add_parser("reconcile", help="idempotent ledger<->mailbox reconciliation")
    p_reconcile.set_defaults(func=_cmd_reconcile)

    p_transition = sub.add_parser("transition", help="apply a legal lifecycle transition")
    p_transition.add_argument("--request-id", required=True)
    p_transition.add_argument("--to", required=True)
    p_transition.add_argument("--actor", required=True)
    p_transition.add_argument("--evidence-ref", default=None)
    p_transition.set_defaults(func=_cmd_transition)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except IllegalTransitionError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # pragma: no cover - last-resort guard
        print(f"ERROR: unexpected: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
