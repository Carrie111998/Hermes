"""What a killed owner leaves behind, and whether a producer can still recover.

The route probe shows the happy path. This one asks the question that decides
whether a producer can trust the lifecycle at all:

    CAN A ROW SAY "STARTED" WHILE THE TRANSCRIPT HOLDS NO MARKER?

It can, and the reason is structural rather than unlucky. ``_run_prompt_submit``
returns as soon as the turn THREAD is running, and that thread persists the user
row afterwards, so between the poller writing STARTED and the marker becoming
durable there is a real window. A process killed inside it leaves a row claiming
a turn began and a conversation containing no evidence of one.

That state cannot be resolved by the inbox. Deciding whether the event landed
means reading canonical history, which is the producer's authority. So the
recovery is explicit and producer-driven -- ``reopen_external_turn`` -- and this
probe proves both halves: that the state is reachable, and that reopening it
leads to exactly one delivered turn rather than two or none.

Killing at a chosen instant is inherently probabilistic, so the early kill is
repeated across a spread of delays and every (state, marker) pair observed is
reported. A window that never appears in a run is reported as not observed,
never as impossible.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
from probe_active_session_exclusivity import Gateway, registry  # noqa: E402
from probe_external_turn_route import enqueue, inbox, turns, wait_for  # noqa: E402

PROBE_HOME = REPO / ".probe-home-crash"
PYTHON = REPO / "venv" / "Scripts" / "python.exe"
if not PYTHON.exists():
    PYTHON = REPO / "venv" / "bin" / "python"


def call(home: Path, snippet: str) -> str:
    env = dict(os.environ)
    env["HERMES_HOME"] = str(home)
    out = subprocess.run(
        [str(PYTHON), "-c", "from tools.session_external_turns import *\n" + snippet],
        cwd=str(REPO), env=env, capture_output=True, text=True,
    )
    return (out.stdout or out.stderr).strip()[-300:]


def own_session(home: Path):
    """A gateway that owns a stored session and is idle."""
    g = Gateway("owner", home)
    sid = g.call("session.create", {"cols": 80})["result"]["session_id"]
    g.call("prompt.submit", {"session_id": sid, "text": "probe: take the session"})
    held = registry(home)
    if not held:
        raise RuntimeError("owner never claimed the session")
    g.call("session.interrupt", {"session_id": sid})
    return g, sid, held[0]["session_id"]


def main() -> int:
    shutil.rmtree(PROBE_HOME, ignore_errors=True)
    failures = []
    observed = []

    def check(label, ok, detail=""):
        print(f"  {'PASS' if ok else 'FAIL'}  {label}{(' -- ' + detail) if detail else ''}")
        if not ok:
            failures.append(label)

    # ── kill after CLAIM but before dispatch ─────────────────────────────
    # Simulated rather than raced: the window is a few microseconds wide inside
    # the poller, and what matters is that a claim orphaned before dispatch is
    # recoverable, which is a property of the row, not of the timing.
    g, sid, key = own_session(PROBE_HOME)
    try:
        enqueue(PROBE_HOME, "C1", key, "MARK-C1 killed before dispatch")
        out = call(PROBE_HOME, "print(claim_external_turn('C1'))")
        check("a claim can be taken", out not in ("None", ""), out)
        # That claiming process has now exited, so its claim is dead.
        row = call(PROBE_HOME, "r=get_external_turn('C1');print(r['state'], r['owner_alive'])")
        check("a claim orphaned before dispatch is dead and recoverable",
              row.startswith("CLAIMED") and row.endswith("False"), row)
        delivered = wait_for(
            lambda: (inbox(PROBE_HOME, "C1") or {}).get("state") in ("STARTED", "FINISHED"), 60
        )
        check("and the live owner picks it up", delivered,
              str((inbox(PROBE_HOME, "C1") or {}).get("state")))
        check("exactly once", len(turns(PROBE_HOME, key, "MARK-C1")) == 1)
    finally:
        g.close()

    # ── kill at a spread of delays after dispatch ────────────────────────
    # The interesting window: STARTED written by the poller, marker written by
    # the turn thread. Killing across a spread maps what is actually reachable.
    for i, delay in enumerate((0.0, 0.02, 0.05, 0.15, 0.4)):
        home = PROBE_HOME.parent / f".probe-home-crash-{i}"
        shutil.rmtree(home, ignore_errors=True)
        g, sid, key = own_session(home)
        eid = f"K{i}"
        try:
            enqueue(home, eid, key, f"MARK-{eid} killed {delay}s after dispatch")
            # Wait until the row leaves PENDING, then kill after `delay`.
            wait_for(lambda: (inbox(home, eid) or {}).get("state") != "PENDING", 60)
            time.sleep(delay)
            g.proc.kill()
            g.proc.wait(timeout=30)
        finally:
            g.close()
        row = inbox(home, eid) or {}
        marker = len(turns(home, key, f"MARK-{eid}"))
        observed.append((delay, row.get("state"), marker))
        print(f"    kill +{delay:<5} -> state={row.get('state'):<8} marker_rows={marker}")
        shutil.rmtree(home, ignore_errors=True)

    started_without_marker = [d for d, st, m in observed if st == "STARTED" and m == 0]
    print()
    if started_without_marker:
        print(f"  OBSERVED: STARTED with no marker at delays {started_without_marker}")
        print("            -> the producer MUST have a recovery path for this state")
    else:
        print("  NOT OBSERVED in this run: STARTED with no marker.")
        print("            -> structurally reachable (the turn thread persists the")
        print("               marker after dispatch returns); do not treat as impossible")

    # ── recovery of a dead STARTED with NO marker ────────────────────────
    # The killed owner must leave the transcript empty for this event, because
    # that is the only case a producer may reopen. An event whose marker DID
    # land is reconciled against history instead -- reopening it would announce
    # one thing twice.
    home = PROBE_HOME.parent / ".probe-home-crash-rec"
    shutil.rmtree(home, ignore_errors=True)
    g, sid, key = own_session(home)
    try:
        enqueue(home, "R1", key, "MARK-R1 recovery case")
        # Polled tightly. At the default half-second interval the observation of
        # STARTED lands well after the turn thread has already persisted the
        # marker, and the case this phase exists to build -- STARTED with an
        # empty transcript -- would never be constructed.
        wait_for(lambda: (inbox(home, "R1") or {}).get("state") == "STARTED", 60, interval=0.01)
        g.proc.kill()
        g.proc.wait(timeout=30)
    finally:
        g.close()
    check("the killed turn left no marker, which is what makes reopen legitimate",
          len(turns(home, key, "MARK-R1")) == 0, f"{len(turns(home, key, 'MARK-R1'))} rows")

    row = inbox(home, "R1") or {}
    check("a killed owner leaves the row STARTED", row.get("state") == "STARTED",
          str(row.get("state")))
    alive = call(home, "print(get_external_turn('R1')['owner_alive'])")
    check("and reports its owner as dead, so the producer knows to reconcile",
          alive == "False", alive)

    # The successor must own THIS stored session, so it resumes rather than
    # creating a new one -- a fresh session has a different key and its poller
    # would never see this event.
    g2 = Gateway("successor", home)
    resumed = g2.call("session.resume", {"session_id": key})
    check("a successor can resume the stored session", "result" in resumed, str(resumed)[:120])
    try:
        busy = call(home, "print(reopen_external_turn('R1', 'test'))")
        # R1's owner is dead, so this legitimately reopens.
        check("the producer can reopen a dead STARTED event", busy == "True", busy)
        delivered = wait_for(
            lambda: (inbox(home, "R1") or {}).get("state") in ("STARTED", "FINISHED"), 60
        )
        check("a live owner then delivers it", delivered,
              str((inbox(home, "R1") or {}).get("state")))
        wait_for(lambda: len(turns(home, key, "MARK-R1")) >= 1, 30)
        check("exactly one turn, not two", len(turns(home, key, "MARK-R1")) == 1,
              str(len(turns(home, key, "MARK-R1"))))
        # And reopening cannot touch a turn that is currently running.
        again = call(home, "print(reopen_external_turn('R1', 'test'))")
        check("reopening refuses while the owner is alive", again == "False", again)
    finally:
        g2.close()
    shutil.rmtree(home, ignore_errors=True)

    # ── closing the session racing the dispatch that hosts its event ─────
    # The consumer registers _external_turn_in_flight only AFTER dispatch
    # returns and STARTED is written. If a close lands inside that gap the
    # poller exits without ever closing the lifecycle, and the row would be left
    # CLAIMED or STARTED under a gateway process that is STILL ALIVE -- which no
    # other process may recover, because a live holder is exactly what
    # _claimer_alive protects. That is the one shape this rail cannot tolerate,
    # so it is pinned rather than assumed.
    #
    # The gateway keeps a SECOND session open, so closing the first does not
    # simply end the process and make every row trivially recoverable.
    print()
    print("  closing the hosting session at a spread of delays:")
    for i, delay in enumerate((0.0, 0.05, 0.2, 0.6, 1.2)):
        home = PROBE_HOME.parent / f".probe-home-close-{i}"
        shutil.rmtree(home, ignore_errors=True)
        g = Gateway("closer", home)
        eid = f"X{i}"
        try:
            sid1 = g.call("session.create", {"cols": 80})["result"]["session_id"]
            g.call("prompt.submit", {"session_id": sid1, "text": "probe: own it"})
            held = registry(home)
            if not held:
                raise RuntimeError("never claimed")
            key1 = held[0]["session_id"]
            g.call("session.interrupt", {"session_id": sid1})
            # A second live session so the process outlives the first close.
            g.call("session.create", {"cols": 80})

            enqueue(home, eid, key1, f"MARK-{eid} close race")
            wait_for(lambda: (inbox(home, eid) or {}).get("state") != "PENDING", 60,
                     interval=0.01)
            time.sleep(delay)
            g.call("session.close", {"session_id": sid1})
            time.sleep(2.0)

            row = inbox(home, eid) or {}
            state = row.get("state")
            gateway_alive = g.proc.poll() is None
            holder_alive = bool(row.get("owner_pid")) and gateway_alive
            stranded = state in ("CLAIMED", "STARTED") and holder_alive
            print(f"    close +{delay:<4} -> state={str(state):<8} "
                  f"gateway_alive={gateway_alive} stranded={stranded}")
            check(f"an event is not stranded by a close at +{delay}s", not stranded,
                  f"state={state} under live pid {row.get('owner_pid')}")
        finally:
            g.close()
            shutil.rmtree(home, ignore_errors=True)

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s): {', '.join(failures)}")
        return 1
    print("All crash-boundary checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
