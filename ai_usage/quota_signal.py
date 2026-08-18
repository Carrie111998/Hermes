from __future__ import annotations

from typing import Optional

# Phase 1's existing severity ladder -- reusing these two outcome strings
# means Phase 3 needs zero routing changes: "diverted" already routes WARN
# to the Alerts topic, "chain_exhausted" already routes ACT to Action
# Required plus WhatsApp (see events' classify()).
WARN_PCT = 90.0

# The DeepSeek key is a finite prepaid top-up, historically funded around
# $10 at a time. $2.00 gives roughly one to a few days of runway at typical
# burn before the balance actually hits $0 and requests start failing --
# enough lead time to switch models, not so early that a $9.98 balance
# nags on every poll.
BALANCE_WARN_USD = 2.0


def _outcome_for_pct(used_pct: float) -> Optional[str]:
    if used_pct >= 100.0:
        return "chain_exhausted"
    if used_pct >= WARN_PCT:
        return "diverted"
    return None


def _outcome_for_balance(balance_usd: float) -> Optional[str]:
    if balance_usd <= 0.0:
        return "chain_exhausted"
    if balance_usd <= BALANCE_WARN_USD:
        return "diverted"
    return None


def evaluate(snapshot: dict) -> list[dict]:
    """Pure. One finding per provider/window that warrants a signal.

    Finding: {provider, window_id, window_label, kind,
              used_pct | balance, outcome, resets_at}
    kind in {"window", "balance"}; outcome in {"diverted", "chain_exhausted"}

    No event bus, no file I/O, no imports from events.*, no clock reads.
    `resets_at` is carried through verbatim for DISPLAY ONLY -- it is never
    read to decide whether a window has "recovered". A live capture showed
    anthropic's weekly resets_at a day in the past while used_pct was still
    100.0; branching on resets_at would have wrongly suppressed that finding.
    Recovery (usage dropping back down) is Phase 1 episode-machinery
    business, wired up in Task 2 -- not this function's job.
    """
    findings: list[dict] = []

    for provider in snapshot.get("providers", []):
        key = provider.get("key")
        mode = provider.get("mode")

        if mode == "balance":
            balance = provider.get("balance_usd")
            if balance is None:
                continue
            outcome = _outcome_for_balance(float(balance))
            if outcome is None:
                continue
            findings.append(
                {
                    "provider": key,
                    "window_id": None,
                    "window_label": None,
                    "kind": "balance",
                    "balance": balance,
                    "outcome": outcome,
                    "resets_at": None,
                }
            )
            continue

        for window in provider.get("windows", []):
            used_pct = window.get("used_pct")
            # Absent window -> never iterated at all (unknown).
            # Present but None -> unknown too; never treat as 0% "recovered".
            if used_pct is None:
                continue
            outcome = _outcome_for_pct(float(used_pct))
            if outcome is None:
                continue
            findings.append(
                {
                    "provider": key,
                    "window_id": window.get("id"),
                    "window_label": window.get("label"),
                    "kind": "window",
                    "used_pct": used_pct,
                    "outcome": outcome,
                    "resets_at": window.get("resets_at"),
                }
            )

    return findings
