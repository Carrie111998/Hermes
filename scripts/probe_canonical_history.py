"""Two transcript projections, and the difference between them made executable.

    session.history            what a PERSON should see -- hidden scaffolding removed
    session.canonical_history  what DURABLY HAPPENED -- hidden rows included

The bug this closes: the routed wake transport delivers its marker with
display_kind="hidden", so a machine activation is not rendered as somebody's
speech, and then reconciles by searching canonical history for that marker.
session.history drops exactly those rows, so a delivery that worked perfectly
looked like one that never happened.

The two assertions that matter most are deliberately symmetrical:

    session.history            hidden marker count == 0
    session.canonical_history  hidden marker count == 1

Everything else here is about the reader being genuinely inert: an inspector
that could consume an event, take a lease, or leave a live session behind would
be a different kind of hazard than the one it was built to remove.
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

PROBE_HOME = REPO / ".probe-home-canon"
PYTHON = REPO / "venv" / "Scripts" / "python.exe"
if not PYTHON.exists():
    PYTHON = REPO / "venv" / "bin" / "python"

HIDDEN_MARKER = "WAKE-MARKER-canon-7c1f"
VISIBLE_TEXT = "VISIBLE-ROW something the person typed"


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


def wait_for(predicate, timeout, interval=0.25):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def count_marker(messages, needle):
    return sum(1 for m in messages if needle in str(m.get("text") or ""))


def main() -> int:
    shutil.rmtree(PROBE_HOME, ignore_errors=True)
    failures = []

    def check(label, ok, detail=""):
        print(f"  {'PASS' if ok else 'FAIL'}  {label}{(' -- ' + detail) if detail else ''}")
        if not ok:
            failures.append(label)

    # ── build a transcript with one of each kind of row ───────────────────
    a = Gateway("A", PROBE_HOME)
    try:
        sid = a.call("session.create", {"cols": 80})["result"]["session_id"]
        a.call("prompt.submit", {"session_id": sid, "text": "probe: establish the session"})
        held = registry(PROBE_HOME)
        if not held:
            raise RuntimeError("A never claimed the session")
        key = held[0]["session_id"]
        a.call("session.interrupt", {"session_id": sid})

        # A hidden row, written the way a real wake is: through the inbox, consumed
        # by the session's own poller.
        enqueue(PROBE_HOME, "CH1", key, HIDDEN_MARKER + " done - fixed the run filter")
        if not wait_for(
            lambda: (inbox(PROBE_HOME, "CH1") or {}).get("state") in ("STARTED", "FINISHED"), 60
        ):
            raise RuntimeError("the hidden wake was never consumed")
        time.sleep(2.0)
        a.call("session.interrupt", {"session_id": sid})
    finally:
        a.proc.kill()
        a.proc.wait(timeout=30)
        a.close()
    time.sleep(1.0)

    # A visible row, so "canonical shows more" is not vacuously true.
    conn = sqlite3.connect(str(PROBE_HOME / "state.db"), timeout=10)
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?,?,?,?)",
        (key, "user", VISIBLE_TEXT, "2026-08-26T10:00:00Z"),
    )
    conn.commit()
    durable = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE session_id = ?", (key,)
    ).fetchone()[0]
    conn.close()
    print(f"stored session {key}, {durable} durable rows")
    check("the transcript has both kinds of row", durable >= 2, f"{durable} rows")

    baseline_leases = {r.get("lease_id") for r in registry(PROBE_HOME)}

    # ── the two projections, read through the real RPCs ───────────────────
    reader = Gateway("R", PROBE_HOME)
    try:
        canon = reader.call("session.canonical_history", {"session_id": key})
        ok = "result" in canon
        check("canonical_history answers", ok, json.dumps(canon.get("error", ""))[:200])
        if not ok:
            raise RuntimeError("cannot continue without canonical history")
        rows = canon["result"]["messages"]
        check("session.canonical_history: hidden marker count == 1",
              count_marker(rows, HIDDEN_MARKER) == 1,
              f"{count_marker(rows, HIDDEN_MARKER)} in {len(rows)} rows")
        check("and it carries the visible row too", count_marker(rows, VISIBLE_TEXT) == 1)
        hidden_rows = [m for m in rows if m.get("display_kind") == "hidden"]
        check("hidden rows are labelled as such, not silently mixed in",
              len(hidden_rows) >= 1, f"{len(hidden_rows)} labelled")
        check("it reports which session it resolved to",
              canon["result"].get("resolved_session_id") is not None,
              str(canon["result"].get("resolved_session_id")))

        # The display projection, for contrast. Needs a resumed runtime, which is
        # itself the thing canonical_history exists to avoid.
        resumed = reader.call("session.resume", {"session_id": key})
        runtime = resumed.get("result", {}).get("session_id")
        display = reader.call("session.history", {"session_id": runtime})
        drows = display.get("result", {}).get("messages", [])
        check("session.history: hidden marker count == 0",
              count_marker(drows, HIDDEN_MARKER) == 0,
              f"{count_marker(drows, HIDDEN_MARKER)} in {len(drows)} rows")
        check("but the person still sees their own row", count_marker(drows, VISIBLE_TEXT) == 1)
        check("so canonical is strictly the larger projection", len(rows) > len(drows),
              f"canonical {len(rows)} vs display {len(drows)}")
    finally:
        reader.close()

    # ── the reader must be inert ──────────────────────────────────────────
    # Asked in a FRESH process that only ever calls canonical_history, so nothing
    # else it did can account for the absence of side effects.
    time.sleep(1.0)
    enqueue(PROBE_HOME, "CH2", key, "MARK-CH2 must not be consumed by a reader")
    inert = Gateway("I", PROBE_HOME)
    try:
        for _ in range(3):
            answer = inert.call("session.canonical_history", {"session_id": key})
            check("repeated reads keep answering", "result" in answer,
                  json.dumps(answer.get("error", ""))[:120])
        time.sleep(6.0)
        state = (inbox(PROBE_HOME, "CH2") or {}).get("state")
        check("a canonical read consumes nothing", state == "PENDING", f"state={state}")
        check("and takes no active-session lease",
              {r.get("lease_id") for r in registry(PROBE_HOME)} <= baseline_leases,
              json.dumps(registry(PROBE_HOME))[:200])
        sessions = inert.call("session.active_list", {})
        live = sessions.get("result", {}).get("sessions", sessions.get("result", {}))
        check("and leaves no live session behind in that process",
              not live or len(live) == 0, json.dumps(live)[:160])
    finally:
        inert.close()

    # ── it still works when somebody else owns the conversation ──────────
    owner = Gateway("O", PROBE_HOME)
    try:
        owner.call("session.resume", {"session_id": key})
        held_answer = None
        second = Gateway("S", PROBE_HOME)
        try:
            held_answer = second.call("session.canonical_history", {"session_id": key})
        finally:
            second.close()
        check("canonical history is readable while another process owns the session",
              "result" in (held_answer or {}), json.dumps((held_answer or {}).get("error", ""))[:160])
        check("and still shows the hidden marker",
              count_marker(held_answer["result"]["messages"], HIDDEN_MARKER) == 1)
    finally:
        owner.close()

    # ── an unknown session is an answer, not a crash ─────────────────────
    last = Gateway("U", PROBE_HOME)
    try:
        missing = last.call("session.canonical_history", {"session_id": "no_such_session"})
        check("an unknown session is refused cleanly",
              missing.get("error", {}).get("code") == 4007,
              json.dumps(missing)[:160])
        blank = last.call("session.canonical_history", {})
        check("and a missing id is refused cleanly",
              blank.get("error", {}).get("code") == 4006, json.dumps(blank)[:160])
        caps = last.call("gateway.capabilities", {}).get("result", {})
        check("the capability is advertised",
              caps.get("session_canonical_history_v1") is True, json.dumps(caps))
    finally:
        last.close()

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s): {', '.join(failures)}")
        return 1
    print("All canonical-history checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
