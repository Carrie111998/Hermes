"""Pre-dispatch state.db health probe + quarantine gate for the kanban dispatcher.

Regression tests for the 2026-08-03 incident class (session-DB corruption
draining the worker fleet): the dispatcher must never spawn a worker whose
assignee's state.db is unhealthy — the worker would open the store fine,
fail its FIRST canonical transcript write (``session_persistence_failed``),
and drain via the failure circuit breaker.

The contract (pinned by ``tests/state/test_state_db_corruption_worker_drain.py``):

* ``hermes_cli.kanban_db.pre_dispatch_state_db_probe(profile_name) ->
  Optional[str]`` — None when the profile's state.db is healthy enough to
  spawn a worker, else a reason naming the corruption (delegates to
  ``hermes_state._db_opens_cleanly``).
* ``dispatch_once`` consults it per assignee before spawning and quarantines
  the task (blocks it with the high-signal diagnostic) instead of spawning a
  doomed worker.
* A crashed worker whose assignee's store is unhealthy surfaces
  ``profile <name> store unhealthy: <error>; worker blocked`` in the run
  history rather than a low-signal ``pid N not alive``.

The store is never replaced or deleted — recovery relies on timestamped
backups (``repair_state_db_schema`` / manual restore).
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
import uuid
from pathlib import Path

import pytest


# ── Fixture: isolated HERMES_HOME + kanban board + profiles ──────────────────


@pytest.fixture()
def kanban_with_profiles(monkeypatch):
    """Fresh HERMES_HOME with kanban DB + alpha/beta profiles."""
    test_home = tempfile.mkdtemp(prefix="kanban_store_quarantine_test_")
    for prof in ("alpha", "beta"):
        os.makedirs(os.path.join(test_home, "profiles", prof), exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", test_home)
    # Make crash detection deterministic: no grace window.
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    for mod in list(sys.modules.keys()):
        if (
            mod.startswith("hermes_cli")
            or mod.startswith("hermes_state")
            or mod == "hermes_constants"
        ):
            del sys.modules[mod]
    from hermes_cli import kanban_db

    kanban_db._recent_worker_exits.clear()
    yield kanban_db, Path(test_home)


def _fake_spawn(*args, **kwargs):
    return 12345


def _profile_dir(root: Path, name: str) -> Path:
    return root / "profiles" / name if name != "default" else root


# ── Real-corruption fixtures (same technique as the incident tests) ──────────


def _build_healthy_db(db_path: Path) -> None:
    """Create a small healthy state.db with one session and 10 messages."""
    from hermes_state import SessionDB

    db = SessionDB(db_path=db_path)
    sid = db.create_session(session_id=str(uuid.uuid4()), source="cli")
    for i in range(5):
        db.append_message(sid, role="user", content=f"hello world {i}")
        db.append_message(sid, role="assistant", content=f"reply about pizza {i}")
    db.close()


def _corrupt_messages_btree(db_path: Path) -> None:
    """Physically corrupt the ``messages`` table b-tree root page.

    Sets the page's cell-count field to 0xFFFF so SQLite can never resolve
    rowid bounds — the "database disk image is malformed" class from the
    incident. Real on-disk corruption, not a mocked cursor exception.
    """
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("VACUUM")
        row = conn.execute(
            "SELECT rootpage FROM sqlite_master "
            "WHERE type = 'table' AND name = 'messages'"
        ).fetchone()
        assert row is not None
        root_page = int(row[0])
    finally:
        conn.close()

    data = bytearray(db_path.read_bytes())
    page_size = int.from_bytes(data[16:18], "big")
    if page_size == 1:
        page_size = 65_536
    page_start = (root_page - 1) * page_size
    header_offset = page_start + (100 if root_page == 1 else 0)
    assert data[header_offset] in {0x02, 0x05, 0x0A, 0x0D}, (
        f"unexpected messages b-tree page type {data[header_offset]:#x}"
    )
    data[header_offset + 3 : header_offset + 5] = b"\xff\xff"
    db_path.write_bytes(data)


def _make_store_unhealthy(root: Path, profile: str) -> Path:
    """Build a healthy state.db for *profile* then corrupt its messages
    b-tree — the non-self-healing incident class."""
    db_path = _profile_dir(root, profile) / "state.db"
    _build_healthy_db(db_path)
    _corrupt_messages_btree(db_path)
    return db_path


# ── The probe itself ─────────────────────────────────────────────────────────


def test_probe_returns_none_for_healthy_profile(kanban_with_profiles):
    kb, root = kanban_with_profiles
    db_path = _profile_dir(root, "alpha") / "state.db"
    _build_healthy_db(db_path)

    assert kb.pre_dispatch_state_db_probe("alpha") is None


def test_probe_returns_none_when_state_db_missing(kanban_with_profiles):
    """A fresh profile with no state.db yet is healthy — the worker's
    SessionDB() open creates it on first use."""
    kb, _ = kanban_with_profiles
    assert kb.pre_dispatch_state_db_probe("alpha") is None


def test_probe_returns_none_for_default_profile(kanban_with_profiles):
    kb, root = kanban_with_profiles
    _build_healthy_db(root / "state.db")
    assert kb.pre_dispatch_state_db_probe("default") is None


def test_probe_flags_corrupt_store_with_path_and_error(kanban_with_profiles):
    kb, root = kanban_with_profiles
    db_path = _make_store_unhealthy(root, "alpha")

    reason = kb.pre_dispatch_state_db_probe("alpha")
    assert reason is not None
    # Exact evidence: DB path + SQLite error naming the corruption.
    assert str(db_path) in reason
    assert "malformed" in reason.lower()


def test_probe_never_mutates_the_store(kanban_with_profiles):
    """Probing leaves the DB byte-identical (rolled-back write probe, no
    repair attempt) — the corruption stays in place for timestamped-backup
    recovery."""
    kb, root = kanban_with_profiles
    db_path = _make_store_unhealthy(root, "alpha")
    before = db_path.read_bytes()

    assert kb.pre_dispatch_state_db_probe("alpha") is not None

    assert db_path.read_bytes() == before


# ── Quarantine gate in dispatch ──────────────────────────────────────────────


def test_dispatch_blocks_ready_task_on_unhealthy_store(kanban_with_profiles):
    kb, root = kanban_with_profiles
    _make_store_unhealthy(root, "alpha")
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        task_id = kb.create_task(conn, title="doomed", assignee="alpha")

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn)

    # Spawned nowhere, quarantined with the high-signal diagnostic.
    assert res.spawned == []
    assert len(res.quarantined) == 1
    q_task, q_assignee, q_reason = res.quarantined[0]
    assert q_task == task_id
    assert q_assignee == "alpha"
    assert "store unhealthy" in q_reason
    assert "malformed" in q_reason.lower()
    assert "worker blocked" in q_reason
    assert res.store_unhealthy_profiles["alpha"] is not None

    with kb.connect_closing() as conn:
        row = conn.execute(
            "SELECT status, block_kind FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        assert row["status"] == "blocked"
        assert row["block_kind"] == "capability"
        # The diagnostic lands in the blocked event + the synthesized run.
        ev = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'blocked' "
            "ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        assert ev is not None
        assert "store unhealthy" in (ev["payload"] or "")
        run = conn.execute(
            "SELECT summary FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        assert run is not None
        assert "store unhealthy" in (run["summary"] or "")


def test_healthy_profile_dispatches_normally_alongside_quarantine(
    kanban_with_profiles,
):
    """Acceptance: healthy profiles dispatch normally; only the unhealthy
    profile's tasks are quarantined."""
    kb, root = kanban_with_profiles
    _make_store_unhealthy(root, "alpha")
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        bad_id = kb.create_task(conn, title="bad", assignee="alpha")
        good_id = kb.create_task(conn, title="good", assignee="beta")

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn)

    spawned_ids = [s[0] for s in res.spawned]
    assert spawned_ids == [good_id]
    assert bad_id not in spawned_ids
    assert len(res.quarantined) == 1 and res.quarantined[0][0] == bad_id

    with kb.connect_closing() as conn:
        states = dict(
            conn.execute("SELECT id, status FROM tasks WHERE id IN (?, ?)", (bad_id, good_id))
        )
    assert states[bad_id] == "blocked"
    assert states[good_id] == "running"


def test_dispatch_spawns_when_state_db_missing(kanban_with_profiles):
    """A profile that simply has no state.db yet is healthy — no quarantine."""
    kb, _ = kanban_with_profiles
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        task_id = kb.create_task(conn, title="fresh", assignee="alpha")

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn)

    assert res.quarantined == []
    assert [s[0] for s in res.spawned] == [task_id]


def test_quarantine_memoizes_probe_per_tick(kanban_with_profiles, monkeypatch):
    """A fan-out tick with many ready tasks for one unhealthy profile probes
    once and blocks every task."""
    kb, root = kanban_with_profiles
    _make_store_unhealthy(root, "alpha")
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        ids = [
            kb.create_task(conn, title=f"fan{i}", assignee="alpha")
            for i in range(3)
        ]

    calls = []
    real_probe = kb.pre_dispatch_state_db_probe

    def counting_probe(profile_name):
        calls.append(profile_name)
        return real_probe(profile_name)

    monkeypatch.setattr(kb, "pre_dispatch_state_db_probe", counting_probe)

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn)

    assert res.spawned == []
    assert len(res.quarantined) == 3
    assert len(calls) == 1  # probed once, memoized for the other two
    with kb.connect_closing() as conn:
        states = dict(
            conn.execute(
                "SELECT id, status FROM tasks WHERE id IN (%s)"
                % ",".join("?" * len(ids)),
                ids,
            )
        )
    assert all(states[i] == "blocked" for i in ids)


def test_dry_run_records_quarantine_without_blocking(kanban_with_profiles):
    kb, root = kanban_with_profiles
    _make_store_unhealthy(root, "alpha")
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        task_id = kb.create_task(conn, title="dry", assignee="alpha")

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=True)

    assert len(res.quarantined) == 1
    assert res.quarantined[0][0] == task_id
    # Not blocked in dry-run — stays ready for a real tick.
    with kb.connect_closing() as conn:
        row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
    assert row["status"] == "ready"


def test_review_task_quarantined_without_claim(kanban_with_profiles):
    """Review cards for an unhealthy profile are skipped (no claim, no
    spawn) and stay in the review lane; the diagnostic is recorded."""
    kb, root = kanban_with_profiles
    _make_store_unhealthy(root, "alpha")
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        task_id = kb.create_task(conn, title="review", assignee="alpha")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'review' WHERE id = ?", (task_id,)
            )

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn)

    assert res.spawned == []
    assert len(res.quarantined) == 1
    assert res.quarantined[0][0] == task_id
    with kb.connect_closing() as conn:
        row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
        assert row["status"] == "review"  # untouched, waiting for store fix
        ev = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'quarantined'",
            (task_id,),
        ).fetchone()
        assert ev is not None
        assert "store unhealthy" in ev["payload"]


# ── Crash diagnostic: high-signal instead of 'pid not alive' ─────────────────


def _plant_crashed_worker(kb, conn, task_id: str, profile: str, pid: int = 999_999_999) -> None:
    """Claim *task_id* exactly like the dispatcher would, then give it a
    dead worker pid so ``detect_crashed_workers`` sees a crashed worker."""
    claimed = kb.claim_task(conn, task_id, ttl_seconds=60)
    assert claimed is not None, "claim_task should succeed on a ready task"
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET worker_pid = ?, started_at = ? WHERE id = ?",
            (pid, int(time.time()) - 600, task_id),
        )


def test_crash_surfaces_store_diagnostic_not_pid_not_alive(kanban_with_profiles):
    """A crashed worker (unknown exit) whose assignee's store is unhealthy
    records ``profile <name> store unhealthy: <error>; worker blocked`` in
    the run history instead of ``pid N not alive`` — and the requeued task
    is then quarantined by the next dispatch tick (no transcript-loss
    retry loop)."""
    kb, root = kanban_with_profiles
    _make_store_unhealthy(root, "alpha")
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        task_id = kb.create_task(conn, title="crashed", assignee="alpha")
        _plant_crashed_worker(kb, conn, task_id, "alpha")

    with kb.connect_closing() as conn:
        crashed = kb.detect_crashed_workers(conn)

    assert crashed == [task_id]
    with kb.connect_closing() as conn:
        run = conn.execute(
            "SELECT error FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        assert run is not None
        err = run["error"] or ""
        assert "store unhealthy" in err
        assert "malformed" in err.lower()
        assert "worker blocked" in err
        assert "not alive" not in err
        # Requeued to ready — the next tick must quarantine it, not respawn.
        row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
        assert row["status"] == "ready"

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn)
    assert res.spawned == []
    assert len(res.quarantined) == 1
    with kb.connect_closing() as conn:
        row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
        assert row["status"] == "blocked"


def test_crash_keeps_pid_message_when_store_healthy(kanban_with_profiles):
    """Healthy store → the crash diagnostic stays the low-signal pid text
    (nothing wrong with the store to report)."""
    kb, _ = kanban_with_profiles
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        task_id = kb.create_task(conn, title="crashed-ok", assignee="beta")
        _plant_crashed_worker(kb, conn, task_id, "beta")

    with kb.connect_closing() as conn:
        crashed = kb.detect_crashed_workers(conn)

    assert crashed == [task_id]
    with kb.connect_closing() as conn:
        run = conn.execute(
            "SELECT error FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        err = run["error"] or ""
        assert "not alive" in err
        assert "store unhealthy" not in err


# ── Queue-drain alert contract events (task-2 seam) ─────────────────────────


def _fetch_event_payloads(conn, task_id: str, kind: str) -> list:
    return [
        r["payload"] for r in conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id",
            (task_id, kind),
        ).fetchall()
    ]


def test_gate_emits_profile_quarantined_event(kanban_with_profiles):
    """The quarantine gate must emit the ``profile_quarantined`` event the
    queue-drain alert's default provider scans for (contract pinned in
    tests/hermes_cli/test_kanban_queue_drain_alert.py: the pre-dispatch
    health probe emits ``profile_quarantined`` events and the alert picks
    them up with zero provider registration)."""
    kb, root = kanban_with_profiles
    _make_store_unhealthy(root, "alpha")
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        task_id = kb.create_task(conn, title="doomed", assignee="alpha")

    with kb.connect_closing() as conn:
        kb.dispatch_once(conn, spawn_fn=_fake_spawn)

    with kb.connect_closing() as conn:
        payloads = _fetch_event_payloads(conn, task_id, "profile_quarantined")
    assert len(payloads) == 1
    payload = json.loads(payloads[0])
    assert payload["profile"] == "alpha"
    assert payload["reason"] == "store_unhealthy"
    assert "malformed" in payload["error"].lower()
    assert "state.db" in payload["db_path"]


def test_gate_emits_single_quarantine_event_per_tick_fanout(kanban_with_profiles):
    """A fan-out tick with many ready tasks for one unhealthy profile emits
    exactly one ``profile_quarantined`` event (deduped per profile per tick),
    while every task is still blocked."""
    kb, root = kanban_with_profiles
    _make_store_unhealthy(root, "alpha")
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        ids = [
            kb.create_task(conn, title=f"fan{i}", assignee="alpha")
            for i in range(3)
        ]

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn)

    assert len(res.quarantined) == 3
    total = 0
    with kb.connect_closing() as conn:
        for tid in ids:
            total += len(_fetch_event_payloads(conn, tid, "profile_quarantined"))
    assert total == 1  # deduped across the fan-out


def test_gate_emits_healthy_clear_when_store_heals(kanban_with_profiles):
    """After a profile's store heals, the next dispatch tick must emit
    ``profile_store_healthy`` so the queue-drain alert's default provider
    stops treating the profile as gated. This is the recovery path."""
    kb, root = kanban_with_profiles
    db_path = _make_store_unhealthy(root, "alpha")
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        task_id = kb.create_task(conn, title="recover", assignee="alpha")

    # Tick 1: quarantined + profile_quarantined event.
    with kb.connect_closing() as conn:
        kb.dispatch_once(conn, spawn_fn=_fake_spawn)
    with kb.connect_closing() as conn:
        assert _fetch_event_payloads(conn, task_id, "profile_quarantined")

    # Heal the store: rebuild a healthy DB in place (probe must pass).
    db_path.unlink()
    _build_healthy_db(db_path)
    assert kb.pre_dispatch_state_db_probe("alpha") is None

    # Tick 2: task was blocked, so unblock it first, then dispatch.
    with kb.connect_closing() as conn:
        kb.unblock_task(conn, task_id)
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn)

    assert [s[0] for s in res.spawned] == [task_id]
    with kb.connect_closing() as conn:
        payloads = _fetch_event_payloads(conn, task_id, "profile_store_healthy")
    assert len(payloads) == 1
    payload = json.loads(payloads[0])
    assert payload["profile"] == "alpha"
    assert payload["reason"] == "store_recovered"


def test_no_healthy_event_spam_for_never_quarantined(kanban_with_profiles):
    """A healthy profile that was never quarantined must not emit any
    profile_store_healthy noise — the clear event only fires on an actual
    quarantine → healthy transition."""
    kb, _ = kanban_with_profiles
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        task_id = kb.create_task(conn, title="clean", assignee="beta")

    with kb.connect_closing() as conn:
        kb.dispatch_once(conn, spawn_fn=_fake_spawn)

    with kb.connect_closing() as conn:
        assert _fetch_event_payloads(conn, task_id, "profile_store_healthy") == []
        assert _fetch_event_payloads(conn, task_id, "profile_quarantined") == []
