"""Stress test for parent-completion invariant at the claim gate.

Simulates the create-then-link race described in RCA t_a6acd07d:

  Thread A: repeatedly inserts a child row with status='ready' (racy
            writer) and a split-second-later inserts the parent link,
            emulating the pre-fix _kanban_create path.
  Thread B: repeatedly runs claim_task against every ready task.

Pass criteria, both required:

  1. No task is ever 'claimed' while a parent link that ALREADY EXISTED
     at claim time points at a parent that had not completed. The
     claim_task gate in hermes_cli/kanban_db.py must demote such tasks
     back to 'todo' and emit 'claim_rejected' instead of spawning. The
     qualifier matters: this test deliberately links children *after*
     creating them, so a claim that precedes its own link is correct
     behaviour, not a violation, and only the event log can tell the two
     apart.
  2. The gate must have rejected at least once. A run where it never
     fired cannot have detected anything, and one such run reported
     success for exactly that reason (see the seed-parent note below).

Run as a script (`python tests/stress/test_concurrency_parent_gate.py`).
tests/stress/conftest.py sets collect_ignore_glob = ["*.py"], so pytest
never collects this file; running it directly is the only path.
"""
from __future__ import annotations

import os
import random
import sys
import threading
import time
from pathlib import Path

from _temphome import keep_for_debugging, temp_home

WT = str(Path(__file__).resolve().parents[2])
sys.path.insert(0, WT)

NUM_CREATE_ROUNDS = 200
WORKERS_RUN_DURATION_S = 8


def run() -> int:
    with temp_home("hermes_parent_gate_stress_") as home:
        return _run_parent_gate(home)


def _run_parent_gate(home: str) -> int:
    os.environ["HERMES_HOME"] = home
    os.environ["HOME"] = home

    from hermes_cli import kanban_db as kb

    kb.init_db()

    # Seed N parents in 'ready' state. They must stay ready for the whole
    # run (never 'done'), so every child linked to one of them must remain
    # unclaimable -- that is the only thing that gives the claim gate
    # anything to reject.
    #
    # This used to be a comment describing behaviour the code did not have.
    # worker_loop selects *any* status='ready' row, and these are created
    # ready, so the workers claimed and completed the parents themselves:
    # in the 2026-08-17 run all 10 were done within 2s of a 7s run, 104 of
    # 200 children were created after every parent had already completed,
    # and the whole run logged ZERO claim_rejected events. The gate under
    # test never fired, and the script still printed
    # "PARENT-GATE INVARIANT HELD UNDER RACE". worker_loop now excludes
    # these ids explicitly, and the pass criteria below require the gate to
    # have actually rejected something.
    parent_ids: list[str] = []
    conn = kb.connect()
    try:
        for i in range(10):
            parent_ids.append(
                kb.create_task(conn, title=f"parent-{i}", assignee="a")
            )
    finally:
        conn.close()

    created_children: list[str] = []
    created_lock = threading.Lock()
    stop = threading.Event()

    def racy_creator() -> None:
        """Inserts child rows with status='ready' and links them after.

        This is the pre-fix _kanban_create behavior — the very race
        the gate in claim_task must catch.
        """
        conn = kb.connect()
        try:
            for _ in range(NUM_CREATE_ROUNDS):
                if stop.is_set():
                    return
                parents = random.sample(parent_ids, k=2)
                # Step 1: insert child WITHOUT parents (ends up ready).
                child = kb.create_task(
                    conn, title="child", assignee="a", parents=[],
                )
                # Tiny delay so worker threads get a chance to see the
                # ready row before the links are inserted.
                time.sleep(random.uniform(0.0001, 0.002))
                # Step 2: add the parent links after the fact.
                for p in parents:
                    try:
                        kb.link_tasks(conn, parent_id=p, child_id=child)
                    except Exception:
                        pass
                with created_lock:
                    created_children.append(child)
        finally:
            conn.close()

    # The seed parents are 'ready' like everything else, so they have to be
    # excluded by id or the workers complete them and the gate goes idle.
    _parent_slots = ",".join("?" * len(parent_ids))

    def worker_loop() -> None:
        conn = kb.connect()
        try:
            end = time.monotonic() + WORKERS_RUN_DURATION_S
            while time.monotonic() < end and not stop.is_set():
                row = conn.execute(
                    "SELECT id FROM tasks WHERE status='ready' "
                    "AND claim_lock IS NULL "
                    f"AND id NOT IN ({_parent_slots}) "
                    "ORDER BY RANDOM() LIMIT 1",
                    parent_ids,
                ).fetchone()
                if row is None:
                    time.sleep(0.002)
                    continue
                tid = row["id"]
                # There is deliberately NO in-loop parent check here; the
                # post-run audit below is the invariant. Two variants were
                # tried and measured, and both are worse than nothing:
                #
                # * Reading the parents AFTER a successful claim (the
                #   original) is a false positive. racy_creator inserts the
                #   links microseconds later, so a link that appeared after
                #   a legitimate claim read as a gate failure: 82 of 82 such
                #   reports were post-claim links, 0 were pre-existing. It
                #   stayed hidden only while the workers were completing the
                #   seed parents, which made `p.status != 'done'` match
                #   nothing.
                # * Reading them BEFORE the claim is sound but unreachable.
                #   A child is created unlinked and linked 0.1-2ms later,
                #   and four spinning workers essentially always claim it
                #   inside that window; a child that is both linked and
                #   still 'ready' barely exists, because the first worker to
                #   touch one gets it rejected and demoted to 'todo'. A
                #   mutant that bypassed the gate whenever that read
                #   returned rows never once fired it.
                #
                # The audit orders by task_events.id and so can distinguish
                # the two cases after the fact, which is the only place that
                # distinction is available.
                try:
                    claimed = kb.claim_task(conn, tid, claimer="w")
                except Exception:
                    continue
                if claimed is None:
                    continue
                # Release so the run doesn't leak and the next round sees ready.
                kb.complete_task(conn, tid, result="stress-ok")
        finally:
            conn.close()

    creator = threading.Thread(target=racy_creator, daemon=True)
    workers = [threading.Thread(target=worker_loop, daemon=True)
               for _ in range(4)]
    creator.start()
    for w in workers:
        w.start()
    creator.join()
    # Give the workers a chance to fully drain ready rows before we stop.
    time.sleep(0.5)
    stop.set()
    for w in workers:
        w.join(timeout=WORKERS_RUN_DURATION_S + 2)

    # Post-run audit: the DB event log must show no 'claimed' event on any
    # task whose parents were not 'done' at the time of the claim.
    conn = kb.connect()
    try:
        # Ordered by task_events.id, NOT by the clock.
        #
        # The previous version joined the FINAL task_links set against
        # historical 'claimed' events. task_links is (parent_id, child_id)
        # and carries no timestamp, so it cannot tell that a link was added
        # *after* a claim -- and racy_creator above exists precisely to
        # create that window. 66 of 210 claims in the 2026-08-17 run
        # preceded their own link, all of them legitimate: at claim time
        # the child genuinely had no parents, so the gate had nothing to
        # reject.
        #
        # It reported those as violations only when a one-second boundary
        # happened to fall between the claim and the parent's completion,
        # because created_at/completed_at are unix SECONDS and the test
        # compared `p.completed_at > c.t`. Of those 66 benign cases the
        # deltas ran -6s..+1s and exactly one was positive -- so the same
        # race passed four runs in five and failed the fifth.
        #
        # task_events.id is a global AUTOINCREMENT, so it orders events
        # that share a second. A claim is a violation iff a link existed
        # before it (lk.id < c.id) and the parent had not completed before
        # it (no 'completed' event with done.id < c.id).
        bad = conn.execute(
            """
            SELECT c.task_id,
                   json_extract(lk.payload, '$.parent') AS parent_id,
                   c.id  AS claim_event_id,
                   lk.id AS link_event_id
            FROM task_events c
            JOIN task_events lk
              ON lk.kind = 'linked'
             AND lk.task_id = c.task_id
             AND lk.id < c.id
            LEFT JOIN task_events done
              ON done.kind = 'completed'
             AND done.task_id = json_extract(lk.payload, '$.parent')
             AND done.id < c.id
            WHERE c.kind = 'claimed'
              AND done.id IS NULL
            """
        ).fetchall()
        rejections = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE kind='claim_rejected'"
        ).fetchone()[0]
    finally:
        conn.close()

    print(f"children created:  {len(created_children)}")
    print(f"event-log bad:     {len(bad)}")
    print(f"claim_rejected:    {rejections}")

    if bad:
        for row in list(bad)[:10]:
            print("  EVENT-LOG BAD:", dict(row))
        # Failure here is a return code, not an exception, so temp_home
        # cannot see it -- say so explicitly or the evidence is deleted.
        keep_for_debugging(home)
        return 1

    # An unarmed instrument cannot report a violation, so "no violations"
    # from a run where the gate never rejected anything is not evidence of
    # anything. The 2026-08-17 run printed the success banner below on
    # exactly that basis: zero claim_rejected events in the whole database.
    # Requiring a rejection is what makes the green above mean something.
    if rejections == 0:
        print("  ✗ the claim gate never rejected anything -- this run proves")
        print("    nothing. Expected children to be linked to a still-ready")
        print("    parent and demoted to 'todo'. Check that worker_loop is")
        print("    still excluding the seed parents.")
        keep_for_debugging(home)
        return 1

    print("PARENT-GATE INVARIANT HELD UNDER RACE")
    return 0


if __name__ == "__main__":
    sys.exit(run())
