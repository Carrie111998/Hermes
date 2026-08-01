#!/usr/bin/env python3
"""Synthetic usage generator: simulates a period of dashboard interaction so the
evolution curator has something to chew on. Run while the server is up (or
point at the same DB when it's down).

Usage:
  python demos/amorphous/simulate.py [--db demos/amorphous/amorphous.db] [--user demo]

Simulated persona: an SRE who lives in the incident table + triage workflow,
never touches the Metabase signups table or quick links, and keeps asking the
chat the same deploy-status question (so the curator should mint a shortcut).
"""
from __future__ import annotations

import argparse
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from store import Store  # noqa: E402

HOT = ["tbl-incidents", "wf-btn-triage", "ts-requests", "m-errors"]
COLD = ["tbl-signups", "ql-runbooks", "m-uptime"]
REPEATED_PROMPT = "what is the current deploy status of api-gateway"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(Path(__file__).parent / "amorphous.db"))
    ap.add_argument("--user", default="demo")
    ap.add_argument("--events", type=int, default=120)
    args = ap.parse_args()

    store = Store(args.db)
    rng = random.Random(7)
    n = 0

    for _ in range(args.events):
        roll = rng.random()
        if roll < 0.45:
            cid = rng.choice(HOT)
            store.record_event(args.user, "click", cid)
            store.record_event(args.user, "focus_dwell", cid,
                               {"seconds": round(rng.uniform(5, 45), 1)})
            n += 2
        elif roll < 0.6:
            store.record_event(args.user, "workflow_run", rng.choice(HOT[:2]),
                               {"workflow_id": "wf-triage"})
            n += 1
        elif roll < 0.72:
            store.record_event(args.user, "chat", None, {"text": REPEATED_PROMPT})
            n += 1
        elif roll < 0.8:
            store.record_event(args.user, "chat", None, {"text": rng.choice([
                "summarize open incidents",
                "who is on call today",
                "did the 14:20 deploy finish",
            ])})
            n += 1
        else:
            store.record_event(args.user, "view", rng.choice(HOT + ["agent-activity"]))
            n += 1

    # the user manually hid a cold component at some point
    store.record_event(args.user, "hide", "ql-runbooks")
    n += 1

    print(f"Simulated {n} interaction events for user '{args.user}' in {args.db}")
    print("Now press '⚗ Evolve now' in the dashboard (or POST /api/curator/run) "
          "to see the curator propose changes:")
    print("  - promote incidents table / triage workflow")
    print(f"  - mint a one-click workflow for: \"{REPEATED_PROMPT}\"")
    print("  - shrink/hide untouched panels (signups, uptime, runbooks)")


if __name__ == "__main__":
    main()
