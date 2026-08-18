"""Safe historical adoption: migrate already-approved legacy fix-requests
into the v3 delegation ledger directly at TRIAGED, using the
source_idempotency_key join established in the DDP operator hand-off.

DESIGN (from the hand-off report + plan Phase 1):
  * Scan mailbox/main/{inbox,processed} for DEVFLOW_APPROVAL_REQUEST envelopes
    with status "AWAITING HUMAN APPROVAL" → collect source_idempotency_key.
  * Match those keys against DEVFLOW_FIX_REQUEST idempotency_keys in
    mailbox/devflow/processed/*.json.
  * Convert each matched fix-request to a v3 WorkRequest via
    contract.parse_v2_fix_request + contract.parse_request.
  * Insert into the ledger via ledger.insert_request() (REQUESTED, idempotent).
  * Immediately transition to TRIAGED via lifecycle.transition(ledger, bus=None)
    — NEVER leave a backfilled row at REQUESTED across a reconcile (flood
    landmine: emitter.reconcile() re-writes REQUESTED rows lacking inbox
    envelopes BACK into the mailbox inbox).
  * Both calls are pure SQL — no EventBus emission, no mailbox write — fully
    reversible (DELETE FROM requests WHERE source_agent =
    'ddp.historical-adoption').

SAFETY:
  * Dry-run mode (--dry-run) reports what WOULD happen without writing.
  * Idempotent: re-running with the same keys is a no-op (insert_request raises
    IntegrityError on duplicate idempotency_key, caught and counted as skipped
    when the existing row is already TRIAGED).
  * Actor tag is "ddp.historical-adoption" to distinguish from live intake.
  * A summary is printed to stdout and the return dict carries counts.
"""
from __future__ import annotations

import glob
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Tuple

HERMES_ROOT = Path.home() / ".hermes"

# Bootstrap for `python devflow_delegation/adopt_history.py`, where sys.path[0]
# is the package directory and the sibling `from devflow_delegation...` imports
# below cannot resolve. Two things were wrong with the previous unconditional
# `sys.path.insert(0, Path.home() / ".hermes" / "agent-src")`:
#
#   * it named the DEPLOYED checkout rather than this file's own, so importing
#     this module from a worktree put ~/.hermes/agent-src AHEAD of the checkout
#     under test, and every later first-time import of a Hermes package
#     resolved from deployed code instead;
#   * it ran even when the package was already importable -- i.e. on every
#     ordinary `import devflow_delegation.adopt_history` and every
#     `python -m devflow_delegation.adopt_history`, where it is redundant by
#     construction.
#
# ``__package__`` is the package name for both import forms and "" or None only
# for the run-this-file-by-path form, which is exactly when the insert is
# needed. Caught by tests/test_live_root_isolation.py, which measured 23 copies
# of the deployed checkout surviving a tests/devflow_delegation run.
if not __package__:  # pragma: no cover - only the run-by-path entry point
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from devflow_delegation.contract import parse_request, parse_v2_fix_request  # noqa: E402
from devflow_delegation.lifecycle import transition  # noqa: E402
from devflow_delegation.emitter import DelegationEmitter  # noqa: E402
from devflow_delegation.ledger import DelegationLedger  # noqa: E402


def _load_json(path: str) -> Dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _load_mailbox(pattern: str) -> List[Dict[str, Any]]:
    out = []
    for f in sorted(glob.glob(pattern)):
        try:
            out.append(_load_json(f))
        except Exception:
            continue
    return out


def gather_approved_keys() -> Dict[str, List[str]]:
    """Return {source_idempotency_key: [source_file_names]} for every
    DEVFLOW_APPROVAL_REQUEST that is "AWAITING HUMAN APPROVAL"."""
    approved: Dict[str, List[str]] = defaultdict(list)
    for env in _load_mailbox(str(HERMES_ROOT / "mailbox/main/inbox/*.json")):
        if env.get("type") != "DEVFLOW_APPROVAL_REQUEST":
            continue
        if "AWAIT" not in (env.get("status") or "").upper():
            continue
        key = (env.get("payload") or {}).get("source_idempotency_key")
        if key:
            approved[key].append(env.get("_file", "?"))
    for env in _load_mailbox(str(HERMES_ROOT / "mailbox/main/processed/*.json")):
        if env.get("type") != "DEVFLOW_APPROVAL_REQUEST":
            continue
        if "AWAIT" not in (env.get("status") or "").upper():
            continue
        key = (env.get("payload") or {}).get("source_idempotency_key")
        if key:
            approved[key].append(env.get("_file", "?"))
    return dict(approved)


def gather_fix_requests() -> Dict[str, Dict[str, Any]]:
    """Return {idempotency_key: fix_request_envelope} for every
    DEVFLOW_FIX_REQUEST in mailbox/devflow/processed/."""
    fixes: Dict[str, Dict[str, Any]] = {}
    for env in _load_mailbox(str(HERMES_ROOT / "mailbox/devflow/processed/*.json")):
        if env.get("type") != "DEVFLOW_FIX_REQUEST":
            continue
        # The v2 mailbox protocol stores idempotency_key at the envelope
        # top level (not inside payload). parse_v2_fix_request preserves it
        # when producing the v3 work-request payload.
        key = env.get("idempotency_key")
        if key:
            fixes[key] = env
    return fixes


def dry_run(approved_keys: Dict[str, List[str]],
            fixes: Dict[str, Dict[str, Any]]) -> Tuple[int, int, List[str]]:
    """Report what WOULD happen. Returns (matched, unmatched, sample_matched_keys)."""
    matched = 0
    unmatched = 0
    sample: List[str] = []
    for key in sorted(approved_keys):
        if key in fixes:
            matched += 1
            if len(sample) < 10:
                sample.append(key)
        else:
            unmatched += 1
    return matched, unmatched, sample


def adopt(approved_keys: Dict[str, List[str]],
          fixes: Dict[str, Dict[str, Any]],
          ledger: DelegationLedger,
          *,
          actor: str = "ddp.historical-adoption") -> Dict[str, int]:
    """Adopt matched fix-requests into the ledger at TRIAGED. Pure SQL — no
    EventBus, no mailbox write.

    Returns {adopted, triaged, skipped_already_triaged, errors}.
    """
    adopted = 0
    triaged_count = 0
    skipped = 0
    errors = 0

    for key in sorted(approved_keys):
        fix = fixes.get(key)
        if fix is None:
            continue
        try:
            v3 = parse_v2_fix_request(fix)
        except Exception:
            errors += 1
            continue

        # Check the indexed idempotency key before writing. `parse_request`
        # creates the WorkRequest; `insert_request` is ledger-only (unlike the
        # emitter) and begins it at REQUESTED.
        existing = ledger.find_by_idempotency_key(key)
        if existing:
            if existing.get("state") == "TRIAGED":
                skipped += 1
                continue
            errors += 1
            continue

        try:
            request = parse_request(v3)
            # Preserve the original v2 envelope timestamp so historical
            # observability surfaces when the work was first identified, not
            # when it was migrated. The reconcile path (adopt_envelope) does
            # this too for crash-recovery envelopes.
            legacy_ts = fix.get("timestamp")
            if legacy_ts:
                request.created_at = legacy_ts
            ledger.insert_request(request)
            request_id = request.request_id
            adopted += 1
            # Immediately transition to TRIAGED BEFORE any reconcile can see
            # this otherwise-envelope-less REQUESTED record. Passing bus=None
            # makes the historical import pure SQL: no EventBus telemetry.
            transition(ledger, None, request_id, "TRIAGED", actor=actor)
            triaged_count += 1
        except Exception:
            errors += 1
            continue

    return {
        "adopted": adopted,
        "triaged": triaged_count,
        "skipped_already_triaged": skipped,
        "errors": errors,
    }


def main(argv: List[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Adopt historical approved fix-requests into the v3 DDP ledger at TRIAGED"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would happen without writing")
    parser.add_argument("--actor", default="ddp.historical-adoption",
                        help="Actor tag (default: ddp.historical-adoption)")
    args = parser.parse_args(argv)

    print("=== DDP historical adoption ===")
    approved_keys = gather_approved_keys()
    print(f"approved DEVFLOW_APPROVAL_REQUEST keys: {len(approved_keys)}")

    fixes = gather_fix_requests()
    print(f"DEVFLOW_FIX_REQUEST in processed/: {len(fixes)}")

    matched, unmatched, sample = dry_run(approved_keys, fixes)
    print(f"matched (approval key → fix-request): {matched}")
    print(f"unmatched (approval keys with no fix-request): {unmatched}")
    if sample:
        print(f"sample matched keys: {sample[:5]}...")

    if args.dry_run:
        print("\n[dry-run] No writes performed. Re-run without --dry-run to adopt.")
        return 0

    if matched == 0:
        print("\nNothing to adopt.")
        return 0

    ledger = DelegationEmitter().ledger
    try:
        result = adopt(approved_keys, fixes, ledger, actor=args.actor)
    finally:
        ledger.close()

    print(f"\nadopted (new ledger rows): {result['adopted']}")
    print(f"triaged (REQUESTED→TRIAGED): {result['triaged']}")
    print(f"skipped (already TRIAGED): {result['skipped_already_triaged']}")
    print(f"errors: {result['errors']}")
    print("\nVerify: python -m devflow_delegation.cli status")
    print("Reverse: DELETE FROM requests WHERE source_agent = 'ddp.historical-adoption';")
    return 0 if result["errors"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
