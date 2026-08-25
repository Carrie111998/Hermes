"""Cross-process falsification of the external-activation route.

Two properties, neither of which can be shown inside one interpreter:

  ONE EVENT PRODUCES AT MOST ONE TURN, IN THE PROCESS THAT OWNS THE SESSION.
  Both candidate consumers must be real processes, with their own pollers,
  their own lease claims and their own view of the database, because the design
  exists to make a race between two such processes safe rather than unlikely.

  A BUSY OWNER MAKES THE EVENT WAIT.
  The wake becomes the next turn. It never steers or interrupts the turn a
  person is already watching.

    A  create + submit + interrupt   -> A owns S and is idle
    B  resume S                      -> B is live on S but owns nothing
    enqueue E                        -> both pollers can see it
    ...                              -> exactly one turn, and it is A's

    A  submit (left running)         -> A is busy
    enqueue E3                       -> stays PENDING while A is busy
    A  interrupt                     -> now, and only now, it is consumed

A provider is deliberately NOT configured. A submitted turn with no model to
answer it stays running until it errors out, which gives a reliable "busy"
state, and ``session.interrupt`` gives a reliable transition back to idle. What
is under test is who dispatches a turn and when, not what a model replies.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
from probe_active_session_exclusivity import Gateway, registry  # noqa: E402

PROBE_HOME = REPO / ".probe-home-ext"
PYTHON = REPO / "venv" / "Scripts" / "python.exe"
if not PYTHON.exists():
    PYTHON = REPO / "venv" / "bin" / "python"


def _rows(home: Path, sql: str, args=()):
    db = home / "state.db"
    if not db.exists():
        return []
    conn = sqlite3.connect(str(db), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, args)]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def inbox(home: Path, event_id: str):
    found = _rows(home, "SELECT * FROM session_external_turns WHERE event_id = ?", (event_id,))
    return found[0] if found else None


def turns(home: Path, key: str, needle: str):
    """User rows carrying our body -- proof a turn was actually dispatched."""
    return _rows(
        home,
        "SELECT id, role, content, display_kind FROM messages "
        "WHERE session_id = ? AND content LIKE ?",
        (key, f"%{needle}%"),
    )


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
    return (out.stdout or out.stderr)[-200:].strip()


def wait_for(predicate, timeout: float, interval: float = 0.5):
    """Poll rather than sleep a fixed span: an idle gateway can take a while to
    give up on a model that will never answer, and a fixed sleep turns that into
    a flaky test rather than a slow one."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def main() -> int:
    import shutil

    shutil.rmtree(PROBE_HOME, ignore_errors=True)
    failures = []

    def check(label, ok, detail=""):
        print(f"  {'PASS' if ok else 'FAIL'}  {label}{(' -- ' + detail) if detail else ''}")
        if not ok:
            failures.append(label)

    a = Gateway("A", PROBE_HOME)
    b = None
    try:
        sid_a = a.call("session.create", {"cols": 80})["result"]["session_id"]
        a.call("prompt.submit", {"session_id": sid_a, "text": "probe: A takes the session"})
        held = registry(PROBE_HOME)
        if not held:
            raise RuntimeError("A never claimed the session")
        key, a_pid = held[0]["session_id"], held[0]["pid"]
        a.call("session.interrupt", {"session_id": sid_a})
        print(f"A owns stored session {key} (pid {a_pid})")

        b = Gateway("B", PROBE_HOME)
        resumed = b.call("session.resume", {"session_id": key})
        check("B is live on S but owns nothing", "result" in resumed,
              json.dumps(resumed.get("error", ""))[:140])
        now_held = registry(PROBE_HOME)
        check("the lease still names A", len(now_held) == 1 and now_held[0]["pid"] == a_pid)

        # ── one event, two eligible-looking processes ────────────────────
        body = "WAKE-MARKER-7f3a done - fixed the run filter"
        check("event enqueued", enqueue(PROBE_HOME, "E1", key, body) == "True")
        consumed = wait_for(lambda: (inbox(PROBE_HOME, "E1") or {}).get("state") == "CONSUMED", 60)
        row = inbox(PROBE_HOME, "E1") or {}
        check("the event was consumed", consumed, str(row.get("state")))
        check("the OWNER consumed it, not the bystander", row.get("owner_pid") == a_pid,
              f"consumer={row.get('owner_pid')} owner={a_pid}")

        got = turns(PROBE_HOME, key, "WAKE-MARKER-7f3a")
        check("exactly one turn resulted from one event", len(got) == 1, f"{len(got)} rows")
        if got:
            check("and it is hidden, not rendered as the person's speech",
                  str(got[0].get("display_kind") or "") == "hidden",
                  f"display_kind={got[0].get('display_kind')!r}")

        # ── a busy owner must make the event WAIT, not be interrupted ────
        # Consuming E1 left A running a turn no model will ever answer, so
        # return it to idle first. Submitting on top of an already-busy session
        # queues a prompt instead, and the phase would then be measuring the
        # queue rather than the inbox.
        a.call("session.interrupt", {"session_id": sid_a})
        a.call("prompt.submit", {"session_id": sid_a, "text": "probe: A is busy now"})
        busy_body = "WAKE-MARKER-c55e must wait for the current turn"
        check("event enqueued while the owner is busy",
              enqueue(PROBE_HOME, "E3", key, busy_body) == "True")
        # Sampled across several poll ticks. The property is that the event is
        # not DELIVERED while the owner is mid-turn; a row momentarily CLAIMED
        # by the owner between its idle check and its release is an internal
        # step of that wait, not a violation of it. What must never happen is
        # the bystander claiming a session it does not own.
        seen = []
        for _ in range(12):
            time.sleep(0.5)
            snap = inbox(PROBE_HOME, "E3") or {}
            seen.append((snap.get("state"), snap.get("owner_pid")))
        states = {st for st, _ in seen}
        check("it is never delivered while the owner is busy", "CONSUMED" not in states,
              " ".join(sorted(states)))
        stealers = {pid for st, pid in seen if st == "CLAIMED" and pid not in (None, a_pid)}
        check("and no process but the owner ever claims it", not stealers, str(stealers))
        check("and no turn was forced into the running one",
              len(turns(PROBE_HOME, key, "WAKE-MARKER-c55e")) == 0)

        a.call("session.interrupt", {"session_id": sid_a})
        drained = wait_for(lambda: (inbox(PROBE_HOME, "E3") or {}).get("state") == "CONSUMED", 60)
        check("and is delivered once the owner goes idle", drained,
              str((inbox(PROBE_HOME, "E3") or {}).get("state")))
        check("as exactly one turn", len(turns(PROBE_HOME, key, "WAKE-MARKER-c55e")) == 1)

        # ── the owner dies; the bystander becomes eligible ───────────────
        a.call("session.interrupt", {"session_id": sid_a})
        a.proc.kill()
        a.proc.wait(timeout=30)
        body2 = "WAKE-MARKER-b209 second event after the owner died"
        check("second event enqueued", enqueue(PROBE_HOME, "E2", key, body2) == "True")
        took = wait_for(lambda: (inbox(PROBE_HOME, "E2") or {}).get("state") == "CONSUMED", 90)
        row2 = inbox(PROBE_HOME, "E2") or {}
        check("the successor consumed it once A's lease was stale", took, str(row2.get("state")))
        check("and the successor is B, not the dead owner", row2.get("owner_pid") not in (None, a_pid),
              f"consumer={row2.get('owner_pid')} dead owner={a_pid}")
        check("exactly one turn for the second event",
              len(turns(PROBE_HOME, key, "WAKE-MARKER-b209")) == 1)
    finally:
        if b is not None:
            b.close()
        a.close()

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s): {', '.join(failures)}")
        return 1
    print("All external-route checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
