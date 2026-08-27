"""Cell 2 — consume-once under cross-process concurrent delivery.

Contract clause (arXiv:2608.03836): a parked handoff is claimed by exactly
one consumer, no matter how many independent processes race for it. This is
the cell the paper found failing at saturation 1.0 in 36/40 cells across
deployed frameworks; Hermes' ``claim_handoff`` ships the paper's own repair
shape (single UPDATE with a state predicate, rowcount-checked), which the
tracking-issue probe confirmed. This cell pins it permanently.

8 independent OS processes are released from a file barrier simultaneously;
each calls ``SessionDB.claim_handoff()`` on the same pending session.
"""

from __future__ import annotations

from tests.conformance.persistence._harness import (
    on_disk_journal_mode,
    reap,
    spawn_child,
    wait_for,
)

CLAIMANT = r"""
import sys, time
from pathlib import Path
from hermes_state import SessionDB

db_path = Path({db_path!r})
barrier = Path({barrier!r})
ready = Path({ready!r})

ready.touch()
deadline = time.monotonic() + 55
while not barrier.exists():
    if time.monotonic() > deadline:
        sys.exit(3)
    time.sleep(0.005)

db = SessionDB(db_path=db_path)
won = db.claim_handoff("cell2")
# Disjoint codes: 0=won, 10=lost. An unhandled exception exits 1, which must
# NEVER be confusable with a clean "lost the claim" — a run where one
# claimant wins and seven CRASH is not a consume-once proof.
sys.exit(0 if won else 10)
"""

N_CLAIMANTS = 8


