#!/usr/bin/env python3
"""Run the synthetic P2 workflow dispatcher acceptance on a dev runtime."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import uuid

from hermes_cli import kanban_db as kb
from hermes_cli import wf_engine


def _spec(template_id: str) -> dict:
    return {
        "id": template_id,
        "correlation_keys": ["ref"],
        "create_on": [{"type": "start"}],
        "steps": [
            {
                "key": "one",
                "turn": {
                    "brief": "Call wf_context, then advance to step two.",
                    "max_runtime_seconds": 180,
                },
                "advance_to": "two",
            },
            {
                "key": "two",
                "turn": {
                    "brief": "Call wf_context, then advance to step three.",
                    "max_runtime_seconds": 180,
                },
                "advance_to": "three",
            },
            {
                "key": "three",
                "turn": {
                    "brief": "Call wf_context, then advance to done.",
                    "max_runtime_seconds": 180,
                },
                "advance_to": "done",
            },
            {"key": "done"},
        ],
    }


def _new_instance(conn, template_id: str, *, assignee: str, label: str) -> str:
    event_id = wf_engine.ingest_event(
        conn,
        source="p2-synthetic",
        external_id=f"{label}-{uuid.uuid4().hex}",
        payload={"synthetic": True, "label": label},
        corr={"ref": f"{label}-{uuid.uuid4().hex}"},
        event_type="start",
    )
    task_id = wf_engine.create_instance(
        conn,
        template_id=template_id,
        entity_key=f"{label}-{uuid.uuid4().hex}",
        corr={"ref": f"{label}-{uuid.uuid4().hex}"},
        vars={"synthetic": True},
        source_event_id=event_id,
    )
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET assignee = ?, tenant = ? WHERE id = ?",
            (assignee, "p2-synthetic", task_id),
        )
    return task_id


def _task_snapshot(conn, task_id: str) -> dict:
    row = conn.execute(
        """
        SELECT t.status, t.current_step_key, i.state
          FROM tasks t
          JOIN wf_instance i ON i.task_id = t.id
         WHERE t.id = ?
        """,
        (task_id,),
    ).fetchone()
    runs = conn.execute(
        """
        SELECT step_key, status, outcome, worker_pid
          FROM task_runs
         WHERE task_id = ?
         ORDER BY id
        """,
        (task_id,),
    ).fetchall()
    return {
        "task_id": task_id,
        "status": row["status"],
        "step": row["current_step_key"],
        "instance_state": row["state"],
        "runs": [dict(run) for run in runs],
        "event_kinds": [event.kind for event in kb.list_events(conn, task_id)],
    }


def _wait_for_three_stage_completion(
    conn,
    task_id: str,
    *,
    board: str,
    timeout_seconds: int,
) -> dict:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        kb.dispatch_once(conn, board=board, max_spawn=1)
        snapshot = _task_snapshot(conn, task_id)
        if snapshot["status"] in {"done", "blocked"}:
            return snapshot
        time.sleep(1)
    raise TimeoutError(json.dumps(_task_snapshot(conn, task_id), sort_keys=True))


def _exercise_exit_path(
    conn,
    template_id: str,
    *,
    assignee: str,
    board: str,
    label: str,
    returncode: int,
) -> dict:
    task_id = _new_instance(
        conn,
        template_id,
        assignee=assignee,
        label=label,
    )
    if returncode:
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET max_retries = 1 WHERE id = ?", (task_id,))
    claimed = kb.claim_task(conn, task_id)
    if claimed is None:
        raise RuntimeError(f"failed to claim synthetic {label} task")
    proc = subprocess.Popen(
        [sys.executable, "-c", f"raise SystemExit({returncode})"],
        start_new_session=True,
    )
    kb._set_worker_pid(conn, task_id, proc.pid)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        result = kb.dispatch_once(conn, board=board, max_spawn=0)
        if task_id in result.auto_blocked:
            break
        time.sleep(0.1)
    snapshot = _task_snapshot(conn, task_id)
    if (snapshot["status"], snapshot["instance_state"]) != ("blocked", "exception"):
        raise RuntimeError(json.dumps(snapshot, sort_keys=True))
    expected_event = "protocol_violation" if returncode == 0 else "crashed"
    if expected_event not in snapshot["event_kinds"]:
        raise RuntimeError(json.dumps(snapshot, sort_keys=True))
    return snapshot


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--board", default="workflow")
    parser.add_argument("--assignee", default="default")
    parser.add_argument("--timeout-seconds", type=int, default=360)
    args = parser.parse_args()

    suffix = uuid.uuid4().hex[:10]
    with kb.connect(board=args.board) as conn:
        template_id, _ = wf_engine.register_template(
            conn,
            _spec(f"p2-synthetic-{suffix}"),
        )
        happy_task = _new_instance(
            conn,
            template_id,
            assignee=args.assignee,
            label="three-stage",
        )
        happy = _wait_for_three_stage_completion(
            conn,
            happy_task,
            board=args.board,
            timeout_seconds=args.timeout_seconds,
        )
        if happy["status"] != "done":
            raise RuntimeError(json.dumps(happy, sort_keys=True))
        completed_steps = [
            run["step_key"]
            for run in happy["runs"]
            if run["outcome"] == "completed"
        ]
        if completed_steps != ["one", "two", "three"]:
            raise RuntimeError(json.dumps(happy, sort_keys=True))

        protocol = _exercise_exit_path(
            conn,
            template_id,
            assignee=args.assignee,
            board=args.board,
            label="protocol-violation",
            returncode=0,
        )
        crash = _exercise_exit_path(
            conn,
            template_id,
            assignee=args.assignee,
            board=args.board,
            label="crash",
            returncode=7,
        )

    print(
        json.dumps(
            {
                "board": args.board,
                "template_id": template_id,
                "happy": happy,
                "protocol_violation": protocol,
                "crash": crash,
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
