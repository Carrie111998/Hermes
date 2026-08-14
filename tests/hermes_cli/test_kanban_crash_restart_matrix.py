from __future__ import annotations

import multiprocessing as mp
import os
import signal
import sqlite3
import time

from hermes_cli.kanban_delivery_outbox import (
    due_children,
    init_schema,
    lease_child,
    mark_sending,
    mark_sent,
    materialize_parent,
    recover_expired,
)


def _source(event: str) -> dict:
    return {
        "board_uuid": "board",
        "event_id": event,
        "subscription_id": "sub",
        "destination": "telegram:1",
        "payload_version": "v1",
    }


def _push() -> dict:
    return {
        "version": "route-capability-v1",
        "adapter_type": "telegram",
        "adapter_version": "1",
        "route_kind": "push",
        "supports_async_delivery": True,
        "creator_wake_applicable": False,
        "artifact_transport": "telegram",
        "artifact_policy_version": "artifact-policy-v1",
    }


def _authority_holder(state_root: str, ready) -> None:
    os.environ["HERMES_STATE_ROOT"] = state_root
    from hermes_cli.dispatcher_authority import acquire_machine_dispatcher

    acquired = acquire_machine_dispatcher("crash-holder")
    ready.send(acquired.state.value)
    if acquired.lease is None:
        return
    while True:
        time.sleep(10)


def _crash_after_sending(db_path: str, child_id: str, ready) -> None:
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    token = lease_child(conn, child_id, "crash-worker", now=10, lease_seconds=1)
    mark_sending(conn, child_id, token, now=10)
    conn.close()
    ready.send("sending")
    os.kill(os.getpid(), signal.SIGKILL)


def _crash_mid_artifacts(db_path: str, first: str, second: str, ready) -> None:
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    token1 = lease_child(conn, first, "crash-worker", now=10, lease_seconds=1)
    mark_sending(conn, first, token1, now=10)
    mark_sent(conn, first, token1, receipt="safe:first", now=10)
    token2 = lease_child(conn, second, "crash-worker", now=10, lease_seconds=1)
    mark_sending(conn, second, token2, now=10)
    conn.close()
    ready.send("middle")
    os.kill(os.getpid(), signal.SIGKILL)


def test_g_authority_sigkill_releases_lock_and_restart_gets_fresh_lease(tmp_path, monkeypatch):
    from hermes_cli.dispatcher_authority import AcquireState, acquire_machine_dispatcher

    state_root = str(tmp_path / "state")
    monkeypatch.setenv("HERMES_STATE_ROOT", state_root)
    parent, child = mp.Pipe(duplex=False)
    proc = mp.Process(target=_authority_holder, args=(state_root, child))
    proc.start()
    assert parent.recv() == "acquired"
    denied = acquire_machine_dispatcher("contender")
    assert denied.state is AcquireState.CONTENDED
    assert proc.pid is not None
    os.kill(proc.pid, signal.SIGKILL)
    proc.join(timeout=5)
    assert proc.exitcode == -signal.SIGKILL
    restarted = acquire_machine_dispatcher("restart")
    assert restarted.state is AcquireState.ACQUIRED
    assert restarted.lease is not None
    restarted.lease.release()


def test_n_crash_after_sending_recovers_same_child_identity(tmp_path):
    db = tmp_path / "outbox.db"
    conn = sqlite3.connect(db, isolation_level=None)
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    parent_id = materialize_parent(conn, source=_source("n1"), capability=_push(), text="done")
    child_id = conn.execute(
        "SELECT child_id FROM kanban_delivery_children WHERE parent_id=? AND kind='primary_text'",
        (parent_id,),
    ).fetchone()[0]
    conn.close()

    parent, child = mp.Pipe(duplex=False)
    proc = mp.Process(target=_crash_after_sending, args=(str(db), child_id, child))
    proc.start()
    assert parent.recv() == "sending"
    proc.join(timeout=5)
    assert proc.exitcode == -signal.SIGKILL

    conn = sqlite3.connect(db, isolation_level=None)
    conn.row_factory = sqlite3.Row
    assert recover_expired(conn, now=12) == [child_id]
    assert [row["child_id"] for row in due_children(conn, parent_id=parent_id, now=12)] == [child_id]
    assert conn.execute(
        "SELECT child_id FROM kanban_delivery_children WHERE parent_id=?", (parent_id,)
    ).fetchone()[0] == child_id
    conn.close()


def test_n_partial_artifact_batch_preserves_sent_sibling_and_retries_only_incomplete(tmp_path):
    db = tmp_path / "artifacts.db"
    a = tmp_path / "a.bin"
    b = tmp_path / "b.bin"
    a.write_bytes(b"a")
    b.write_bytes(b"b")
    import hashlib

    artifacts = [
        {"manifest_id": "a", "path": str(a), "sha256": hashlib.sha256(b"a").hexdigest()},
        {"manifest_id": "b", "path": str(b), "sha256": hashlib.sha256(b"b").hexdigest()},
    ]
    conn = sqlite3.connect(db, isolation_level=None)
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    parent_id = materialize_parent(
        conn, source=_source("n2"), capability=_push(), text="done", artifacts=artifacts
    )
    rows = conn.execute(
        "SELECT child_id FROM kanban_delivery_children WHERE parent_id=? AND kind='artifact_upload' ORDER BY ordinal",
        (parent_id,),
    ).fetchall()
    first, second = rows[0][0], rows[1][0]
    text_child = conn.execute(
        "SELECT child_id FROM kanban_delivery_children WHERE parent_id=? AND kind='primary_text'",
        (parent_id,),
    ).fetchone()[0]
    text_token = lease_child(conn, text_child, "setup", now=1, lease_seconds=5)
    mark_sending(conn, text_child, text_token, now=1)
    mark_sent(conn, text_child, text_token, receipt="safe:text", now=1)
    conn.close()

    parent, child = mp.Pipe(duplex=False)
    proc = mp.Process(target=_crash_mid_artifacts, args=(str(db), first, second, child))
    proc.start()
    assert parent.recv() == "middle"
    proc.join(timeout=5)
    assert proc.exitcode == -signal.SIGKILL

    conn = sqlite3.connect(db, isolation_level=None)
    conn.row_factory = sqlite3.Row
    assert recover_expired(conn, now=12) == [second]
    states = dict(
        conn.execute(
            "SELECT child_id,state FROM kanban_delivery_children WHERE child_id IN (?,?)",
            (first, second),
        ).fetchall()
    )
    assert states == {first: "sent", second: "failed"}
    assert [row["child_id"] for row in due_children(conn, parent_id=parent_id, now=12)] == [second]
    conn.close()
