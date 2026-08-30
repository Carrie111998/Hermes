"""Is a lazy resume genuinely read-only, or does it just look like one?

A gateway opened merely to READ a transcript is a live session, and a live session
runs the external-turn poller -- so the inspector is eligible to consume events
addressed to that conversation, including events it was never asked about. When
the inspecting code then closes its gateway, it kills whatever turn that poller
had begun. Ordering the close before one particular enqueue does not fix this:
the hazard is any OTHER event already queued for the same session.

``session.resume(lazy=true)`` is supposed to create the live record without
building the agent, and the poller starts from the agent build. If that holds, an
inspector can be structurally incapable of consuming rather than merely careful.

This asks the question empirically, with a control, because "the event stayed
PENDING" proves nothing on its own -- it is also what a never-consumable event
looks like:

    A  create, submit, interrupt, die   -> S exists, nothing owns it
    enqueue E for S
    B  resume(S, lazy=true)             -> history works? E untouched?
    C  resume(S)  (eager)               -> E consumed, proving it WAS consumable
"""

from __future__ import annotations

import json
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

PROBE_HOME = REPO / ".probe-home-lazy"
PYTHON = REPO / "venv" / "Scripts" / "python.exe"
if not PYTHON.exists():
    PYTHON = REPO / "venv" / "bin" / "python"


def inbox(home: Path, event_id: str):
    db = home / "state.db"
    if not db.exists():
        return None
    conn = sqlite3.connect(str(db), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM session_external_turns WHERE event_id = ?", (event_id,)
        ).fetchone()
        return dict(row) if row else None
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()


def enqueue(home: Path, event_id: str, key: str, body: str) -> str:
    env = dict(os.environ)
    env["HERMES_HOME"] = str(home)
    code = (
        "from tools.session_external_turns import enqueue_external_turn;"
        f"print(enqueue_external_turn(event_id={event_id!r}, target_session_key={key!r},"
        f" body={body!r}, source='delegate-wave'))"
    )
    out = subprocess.run(
        [str(PYTHON), "-c", code], cwd=str(REPO), env=env, capture_output=True, text=True
    )
    return (out.stdout or out.stderr).strip()[-200:]


def durable_messages(home: Path, key: str) -> int:
    db = home / "state.db"
    if not db.exists():
        return 0
    conn = sqlite3.connect(str(db), timeout=10)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id = ?", (key,)
        ).fetchone()[0]
    except sqlite3.OperationalError:
        return 0
    finally:
        conn.close()


def wait_for(predicate, timeout, interval=0.25):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def main() -> int:
    shutil.rmtree(PROBE_HOME, ignore_errors=True)
    failures = []

    def check(label, ok, detail=""):
        print(f"  {'PASS' if ok else 'FAIL'}  {label}{(' -- ' + detail) if detail else ''}")
        if not ok:
            failures.append(label)

    # ── a stored session that nothing owns ───────────────────────────────
    a = Gateway("A", PROBE_HOME)
    a_pid = a.proc.pid
    try:
        sid = a.call("session.create", {"cols": 80})["result"]["session_id"]
        a.call("prompt.submit", {"session_id": sid, "text": "probe: establish the session"})
        held = registry(PROBE_HOME)
        if not held:
            raise RuntimeError("A never claimed the session")
        key = held[0]["session_id"]
        a.call("session.interrupt", {"session_id": sid})

        # GIVE THE SESSION A REAL TRANSCRIPT BEFORE ASKING WHETHER LAZY CAN READ ONE.
        # A provider-less turn never flushes one, so the seed is a real external turn: the
        # poller's hidden user row IS durable, which is exactly what an inspector must be able
        # to read. Testing lazy history against an empty session would have 'proved' that lazy
        # resume cannot read transcripts, when it had simply been given nothing to read.
        enqueue(PROBE_HOME, "L0", key, "MARK-L0 seed a durable row")
        seeded = wait_for(
            lambda: (inbox(PROBE_HOME, "L0") or {}).get("state") in ("STARTED", "FINISHED"), 60
        )
        if not seeded:
            raise RuntimeError("could not seed a durable transcript")
        wait_for(lambda: durable_messages(PROBE_HOME, key) > 0, 30)
        a.call("session.interrupt", {"session_id": sid})
    finally:
        a.proc.kill()
        a.proc.wait(timeout=30)
        a.close()
    time.sleep(1.0)
    check("the session has a durable transcript to read",
          durable_messages(PROBE_HOME, key) > 0, f"{durable_messages(PROBE_HOME, key)} rows")
    before_leases = {r.get("lease_id") for r in registry(PROBE_HOME)}
    print(f"stored session {key}")

    check("event enqueued", enqueue(PROBE_HOME, "L1", key, "MARK-L1 lazy probe") == "True")

    # ── the inspector ────────────────────────────────────────────────────
    b = Gateway("B", PROBE_HOME)
    try:
        lazy = b.call("session.resume", {"session_id": key, "lazy": True})
        ok = "result" in lazy
        check("a lazy resume succeeds", ok, json.dumps(lazy.get("error", ""))[:160])
        if ok:
            result = lazy["result"]
            count = result.get("message_count")
            check("and returns the durable transcript",
                  bool(count) and count > 0, f"message_count={count}")
            runtime = result.get("session_id")
            history = b.call("session.history", {"session_id": runtime})
            rows = history.get("result", {}).get("messages", [])
            # THE FINDING THAT DECIDES THE DESIGN.
            #
            # The transcript here is a delivered wake: one user row carrying the marker, written
            # with display_kind="hidden" so the person does not see a machine activation rendered
            # as their own speech. session.history returns the DISPLAY projection, and
            # server.py drops hidden rows from it -- so the marker is invisible to the only API
            # the producer reads.
            #
            # That is not a lazy-resume limitation. The eager path uses the same projection, so
            # NEITHER can see it, and a wake delivered exactly as designed classifies as ABSENT
            # forever.
            durable = durable_messages(PROBE_HOME, key)
            check("history shows the hidden marker the producer must reconcile against",
                  len(rows) >= durable,
                  f"{len(rows)} of {durable} durable rows visible -- hidden rows are dropped")

        # The whole question: does this inspector consume the event?
        consumed = wait_for(
            lambda: (inbox(PROBE_HOME, "L1") or {}).get("state") not in (None, "PENDING"), 8
        )
        state = (inbox(PROBE_HOME, "L1") or {}).get("state")
        check("a LAZY session does not consume the event", not consumed, f"state={state}")
        # Compared against the registry as it stood, not against a pid: the killed owner's entry
        # survives until something prunes it, so "the registry is non-empty" proves nothing.
        check("and the inspector itself took no lease",
              {r.get("lease_id") for r in registry(PROBE_HOME)} <= before_leases,
              json.dumps(registry(PROBE_HOME))[:160])
    finally:
        b.close()

    # ── the control ──────────────────────────────────────────────────────
    # Without this, "still PENDING" is also what an event nobody could ever
    # consume looks like, and the probe would prove nothing at all.
    c = Gateway("C", PROBE_HOME)
    try:
        eager = c.call("session.resume", {"session_id": key})
        check("an eager resume succeeds", "result" in eager, json.dumps(eager.get("error", ""))[:140])
        took = wait_for(
            lambda: (inbox(PROBE_HOME, "L1") or {}).get("state") in ("CLAIMED", "STARTED", "FINISHED"),
            60,
        )
        check("an EAGER session does consume it, so the event was consumable all along", took,
              str((inbox(PROBE_HOME, "L1") or {}).get("state")))
    finally:
        c.close()

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s): {', '.join(failures)}")
        print("Lazy resume is NOT a safe inspector. Do not build on it -- add a read-only")
        print("history-by-session-key path instead of resuming an executable session.")
        return 1
    print("Lazy resume reads the transcript without becoming eligible to consume.")
    print("NOTE: read the hidden-marker check above before concluding anything -- a")
    print("non-consuming inspector is useless if it cannot see what it came to read.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
