from __future__ import annotations

import sqlite3
import sys
import time

import pytest

from hermes_cli import kanban_containment as kc
from hermes_cli import kanban_db as kb


pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="cgroup-v2 containment is Linux-only"
)


def test_enabled_uses_config_with_env_only_as_internal_override(monkeypatch):
    from hermes_cli import config as config_mod

    monkeypatch.delenv("HERMES_KANBAN_CGROUP_CONTAINMENT", raising=False)
    monkeypatch.setattr(
        config_mod,
        "load_config",
        lambda: {"kanban": {"cgroup_containment": True}},
    )
    assert kc.enabled() is True

    monkeypatch.setattr(
        config_mod,
        "load_config",
        lambda: {"kanban": {"cgroup_containment": False}},
    )
    assert kc.enabled() is False

    monkeypatch.setenv("HERMES_KANBAN_CGROUP_CONTAINMENT", "1")
    assert kc.enabled() is True

    monkeypatch.setenv("HERMES_KANBAN_CGROUP_CONTAINMENT", "0")
    assert kc.enabled() is False


def test_enabled_fails_closed_when_containment_config_is_unreadable(monkeypatch):
    from hermes_cli import config as config_mod

    monkeypatch.delenv("HERMES_KANBAN_CGROUP_CONTAINMENT", raising=False)
    monkeypatch.setattr(
        config_mod,
        "load_config",
        lambda: (_ for _ in ()).throw(OSError("config unreadable")),
    )

    with pytest.raises(kc.ContainmentError, match="cannot load containment config"):
        kc.enabled()


def test_runtime_alias_mount_is_only_needed_under_shadowed_roots():
    assert kc._runtime_alias_needs_mount("/root/.local/share/uv/python/runtime")
    assert kc._runtime_alias_needs_mount("/tmp/hermes-python/runtime")
    assert not kc._runtime_alias_needs_mount(
        "/usr/local/share/uv/python/cpython-3.11-linux-x86_64-gnu"
    )
    assert not kc._runtime_alias_needs_mount("/opt/hermes-python/runtime")


def test_board_schema_materializes_durable_worker_containment_identity(tmp_path):
    conn = kb.connect(tmp_path / "kanban.db")
    try:
        columns = {
            row["name"]: row
            for row in conn.execute("PRAGMA table_info(worker_containments)")
        }
        assert set(columns) == {
            "run_id",
            "task_id",
            "claim_lock",
            "backend",
            "worker_pid",
            "cgroup_path",
            "cgroup_inode",
            "created_at",
            "retirement_started_at",
            "retirement_reason",
            "termination_certified_at",
            "unlink_intent_at",
            "cleaned_at",
        }
        assert columns["run_id"]["pk"] == 1
        assert all(
            columns[name]["notnull"] == 1
            for name in (
                "task_id",
                "claim_lock",
                "backend",
                "worker_pid",
                "cgroup_path",
                "cgroup_inode",
                "created_at",
            )
        )
    finally:
        conn.close()


def test_existing_board_without_containment_table_is_upgraded_additively(tmp_path):
    db_path = tmp_path / "kanban.db"
    conn = kb.connect(db_path)
    try:
        task_id = kb.create_task(conn, title="preserve me", assignee="default")
        conn.execute("DROP TABLE worker_containments")
        conn.commit()
    finally:
        conn.close()

    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    upgraded = kb.connect(db_path)
    try:
        assert kb.get_task(upgraded, task_id) is not None
        assert upgraded.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name = 'worker_containments'"
        ).fetchone() is not None
    finally:
        upgraded.close()


def test_register_worker_containment_cas_binds_exact_owner_atomically(tmp_path):
    conn = kb.connect(tmp_path / "kanban.db")
    try:
        task_id = kb.create_task(conn, title="durable owner", assignee="default")
        task = kb.claim_task(conn, task_id, claimer="host:owner")
        assert task is not None and task.current_run_id is not None
        assert task.claim_lock is not None

        kb._register_worker_containment(
            conn,
            task_id,
            run_id=task.current_run_id,
            claim_lock=task.claim_lock,
            worker_pid=424242,
            cgroup_path="/sys/fs/cgroup/hermes-worker-test",
            cgroup_inode=8181,
        )

        task_row = conn.execute(
            "SELECT worker_pid FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        run_row = conn.execute(
            "SELECT worker_pid FROM task_runs WHERE id = ?", (task.current_run_id,)
        ).fetchone()
        containment = conn.execute(
            "SELECT * FROM worker_containments WHERE run_id = ?",
            (task.current_run_id,),
        ).fetchone()
        assert task_row["worker_pid"] == 424242
        assert run_row["worker_pid"] == 424242
        assert containment["task_id"] == task_id
        assert containment["claim_lock"] == task.claim_lock
        assert containment["cgroup_inode"] == 8181
        assert containment["cleaned_at"] is None
    finally:
        conn.close()


def test_register_worker_containment_rejects_stale_owner_without_partial_writes(
    tmp_path,
):
    conn = kb.connect(tmp_path / "kanban.db")
    try:
        task_id = kb.create_task(conn, title="stale owner", assignee="default")
        task = kb.claim_task(conn, task_id, claimer="host:owner")
        assert task is not None and task.current_run_id is not None
        assert task.claim_lock is not None

        with pytest.raises(RuntimeError, match="ownership changed"):
            kb._register_worker_containment(
                conn,
                task_id,
                run_id=task.current_run_id,
                claim_lock="stale-claim",
                worker_pid=515151,
                cgroup_path="/sys/fs/cgroup/hermes-worker-stale",
                cgroup_inode=9191,
            )

        assert conn.execute(
            "SELECT worker_pid FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()["worker_pid"] is None
        assert conn.execute(
            "SELECT worker_pid FROM task_runs WHERE id = ?", (task.current_run_id,)
        ).fetchone()["worker_pid"] is None
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM worker_containments"
        ).fetchone()["n"] == 0
    finally:
        conn.close()


def test_retirement_is_reserved_for_exact_active_containment_before_kernel_work(
    tmp_path,
):
    conn = kb.connect(tmp_path / "kanban.db")
    try:
        task_id = kb.create_task(conn, title="retire exact", assignee="default")
        task = kb.claim_task(conn, task_id, claimer="host:owner")
        assert task is not None and task.current_run_id is not None
        assert task.claim_lock is not None
        kb._register_worker_containment(
            conn,
            task_id,
            run_id=task.current_run_id,
            claim_lock=task.claim_lock,
            worker_pid=616161,
            cgroup_path="/sys/fs/cgroup/hermes-worker-retire",
            cgroup_inode=10101,
        )
        active = conn.execute(
            "SELECT started_at FROM task_runs WHERE id = ?", (task.current_run_id,)
        ).fetchone()["started_at"]

        assert kb._reserve_worker_retirement(
            conn,
            task_id=task_id,
            run_id=task.current_run_id,
            claim_lock=task.claim_lock,
            reason="manual_reclaim",
            now=int(active) + 5,
            active_started_at=int(active),
        )

        row = conn.execute(
            "SELECT retirement_started_at, retirement_reason "
            "FROM worker_containments WHERE run_id = ?",
            (task.current_run_id,),
        ).fetchone()
        assert row["retirement_started_at"] == int(active) + 5
        assert row["retirement_reason"] == "manual_reclaim"
    finally:
        conn.close()


def test_retirement_reservation_rejects_changed_run_without_mutating_row(tmp_path):
    conn = kb.connect(tmp_path / "kanban.db")
    try:
        task_id = kb.create_task(conn, title="retire stale", assignee="default")
        task = kb.claim_task(conn, task_id, claimer="host:owner")
        assert task is not None and task.current_run_id is not None
        assert task.claim_lock is not None
        kb._register_worker_containment(
            conn,
            task_id,
            run_id=task.current_run_id,
            claim_lock=task.claim_lock,
            worker_pid=717171,
            cgroup_path="/sys/fs/cgroup/hermes-worker-stale-retire",
            cgroup_inode=11111,
        )
        active = conn.execute(
            "SELECT started_at FROM task_runs WHERE id = ?", (task.current_run_id,)
        ).fetchone()["started_at"]

        assert not kb._reserve_worker_retirement(
            conn,
            task_id=task_id,
            run_id=task.current_run_id + 1,
            claim_lock=task.claim_lock,
            reason="stale_reclaim",
            now=int(active) + 5,
            active_started_at=int(active),
        )
        row = conn.execute(
            "SELECT retirement_started_at, retirement_reason "
            "FROM worker_containments WHERE run_id = ?",
            (task.current_run_id,),
        ).fetchone()
        assert row["retirement_started_at"] is None
        assert row["retirement_reason"] is None
    finally:
        conn.close()


def test_reserved_retirement_blocks_task_identity_mutation_until_certified(tmp_path):
    conn = kb.connect(tmp_path / "kanban.db")
    try:
        task_id = kb.create_task(conn, title="guard reserved", assignee="default")
        task = kb.claim_task(conn, task_id, claimer="host:owner")
        assert task is not None and task.current_run_id is not None
        assert task.claim_lock is not None
        kb._register_worker_containment(
            conn,
            task_id,
            run_id=task.current_run_id,
            claim_lock=task.claim_lock,
            worker_pid=818181,
            cgroup_path="/sys/fs/cgroup/hermes-worker-reserved",
            cgroup_inode=12121,
        )
        active = conn.execute(
            "SELECT started_at FROM task_runs WHERE id = ?", (task.current_run_id,)
        ).fetchone()["started_at"]
        assert kb._reserve_worker_retirement(
            conn,
            task_id=task_id,
            run_id=task.current_run_id,
            claim_lock=task.claim_lock,
            reason="guarded_reclaim",
            now=int(active) + 5,
            active_started_at=int(active),
        )

        with pytest.raises(sqlite3.IntegrityError, match="retirement is reserved"):
            conn.execute(
                "UPDATE tasks SET current_run_id = NULL, claim_lock = NULL "
                "WHERE id = ?",
                (task_id,),
            )

        persisted = kb.get_task(conn, task_id)
        assert persisted is not None
        assert persisted.current_run_id == task.current_run_id
        assert persisted.claim_lock == task.claim_lock
    finally:
        conn.close()


def test_contained_worker_identity_and_event_commit_before_gate_release(tmp_path):
    db_path = tmp_path / "kanban.db"
    conn = kb.connect(db_path)
    try:
        task_id = kb.create_task(conn, title="gated registration", assignee="default")
        task = kb.claim_task(conn, task_id, claimer="host:owner")
        assert task is not None and task.current_run_id is not None
        assert task.claim_lock is not None
        observed = {}

        class FakeSpawn:
            pid = 919191
            cgroup_path = "/sys/fs/cgroup/hermes-worker-gated"
            cgroup_inode = 13131
            released = False
            aborted = False

            def __init__(self):
                self.task_id = task_id
                self.run_id = task.current_run_id
                self.claim_lock = task.claim_lock

            def release(self):
                check = kb.connect(db_path)
                try:
                    observed["containment"] = check.execute(
                        "SELECT COUNT(*) AS n FROM worker_containments WHERE run_id = ?",
                        (self.run_id,),
                    ).fetchone()["n"]
                    observed["task_pid"] = check.execute(
                        "SELECT worker_pid FROM tasks WHERE id = ?", (task_id,)
                    ).fetchone()["worker_pid"]
                    observed["run_pid"] = check.execute(
                        "SELECT worker_pid FROM task_runs WHERE id = ?", (self.run_id,)
                    ).fetchone()["worker_pid"]
                    observed["spawned"] = check.execute(
                        "SELECT COUNT(*) AS n FROM task_events "
                        "WHERE task_id = ? AND kind = 'spawned' AND run_id = ?",
                        (task_id, self.run_id),
                    ).fetchone()["n"]
                finally:
                    check.close()
                self.released = True

            def abort(self):
                self.aborted = True

        handle = FakeSpawn()
        kb._set_worker_pid(conn, task_id, handle)

        assert handle.released is True
        assert handle.aborted is False
        assert observed == {
            "containment": 1,
            "task_pid": handle.pid,
            "run_pid": handle.pid,
            "spawned": 1,
        }
    finally:
        conn.close()


def test_exact_termination_certificate_is_persisted_before_identity_release(tmp_path):
    conn = kb.connect(tmp_path / "kanban.db")
    try:
        task_id = kb.create_task(conn, title="certify empty", assignee="default")
        task = kb.claim_task(conn, task_id, claimer="host:owner")
        assert task is not None and task.current_run_id is not None
        assert task.claim_lock is not None
        kb._register_worker_containment(
            conn,
            task_id,
            run_id=task.current_run_id,
            claim_lock=task.claim_lock,
            worker_pid=929292,
            cgroup_path="/sys/fs/cgroup/hermes-worker-certified",
            cgroup_inode=14141,
        )
        active = conn.execute(
            "SELECT started_at FROM task_runs WHERE id = ?", (task.current_run_id,)
        ).fetchone()["started_at"]
        assert kb._reserve_worker_retirement(
            conn,
            task_id=task_id,
            run_id=task.current_run_id,
            claim_lock=task.claim_lock,
            reason="certified_reclaim",
            now=int(active) + 5,
            active_started_at=int(active),
        )
        identity = conn.execute(
            "SELECT * FROM worker_containments WHERE run_id = ?",
            (task.current_run_id,),
        ).fetchone()

        assert kb._persist_containment_certification(conn, identity)
        conn.execute(
            "UPDATE tasks SET status = 'ready', current_run_id = NULL, "
            "claim_lock = NULL, worker_pid = NULL WHERE id = ?",
            (task_id,),
        )
        conn.commit()

        persisted = conn.execute(
            "SELECT termination_certified_at FROM worker_containments WHERE run_id = ?",
            (task.current_run_id,),
        ).fetchone()
        assert persisted["termination_certified_at"] is not None
        current = kb.get_task(conn, task_id)
        assert current is not None and current.current_run_id is None
    finally:
        conn.close()


def test_post_commit_gate_failure_reserves_and_certifies_retirement(tmp_path):
    conn = kb.connect(tmp_path / "kanban.db")
    try:
        task_id = kb.create_task(conn, title="gate fails", assignee="default")
        task = kb.claim_task(conn, task_id, claimer="host:owner")
        assert task is not None and task.current_run_id is not None
        assert task.claim_lock is not None

        class FailingGate:
            pid = 949494
            cgroup_path = "/sys/fs/cgroup/hermes-worker-gate-failed"
            cgroup_inode = 16161
            aborted_with = None

            def __init__(self):
                self.task_id = task_id
                self.run_id = task.current_run_id
                self.claim_lock = task.claim_lock

            def release(self):
                raise kc.ContainmentError("gate release failed")

            def abort(self, *, unlink):
                self.aborted_with = unlink
                return {"containment_certified": True}

        handle = FailingGate()
        with pytest.raises(kc.ContainmentError, match="gate release failed"):
            kb._set_worker_pid(conn, task_id, handle)

        row = conn.execute(
            "SELECT retirement_started_at, retirement_reason, "
            "termination_certified_at, cleaned_at "
            "FROM worker_containments WHERE run_id = ?",
            (task.current_run_id,),
        ).fetchone()
        assert row is not None
        assert row["retirement_started_at"] is not None
        assert row["retirement_reason"] == "spawn_gate_failed"
        assert row["termination_certified_at"] is not None
        assert row["cleaned_at"] is None
        assert handle.aborted_with is False
    finally:
        conn.close()


def test_uncertain_gate_abort_converges_through_active_retirement_sweeper(
    monkeypatch,
    tmp_path,
):
    conn = kb.connect(tmp_path / "kanban.db")
    try:
        task_id = kb.create_task(conn, title="uncertain gate", assignee="default")
        task = kb.claim_task(conn, task_id, claimer="host:owner")
        assert task is not None and task.current_run_id is not None
        assert task.claim_lock is not None
        run_id = int(task.current_run_id)

        class UncertainGate:
            pid = 939393
            cgroup_path = "/sys/fs/cgroup/hermes-worker-gate-uncertain"
            cgroup_inode = 15151
            task_id = task.id
            run_id = task.current_run_id
            claim_lock = task.claim_lock

            def release(self):
                raise kc.ContainmentError("release outcome unknown")

            def abort(self, *, unlink):
                assert unlink is False
                return {"containment_certified": False, "terminated": False}

        with pytest.raises(kb.ContainmentRetirementPending) as pending:
            kb._set_worker_pid(conn, task_id, UncertainGate())
        assert pending.value.certified is False

        current = kb.get_task(conn, task_id)
        assert current is not None
        assert current.status == "running"
        assert current.current_run_id == run_id
        reserved = conn.execute(
            "SELECT retirement_reason, termination_certified_at "
            "FROM worker_containments WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        assert reserved["retirement_reason"] == "spawn_gate_failed"
        assert reserved["termination_certified_at"] is None

        monkeypatch.setattr(
            kc,
            "kill_cgroup",
            lambda path, inode: {
                "containment_certified": (
                    path == UncertainGate.cgroup_path
                    and inode == UncertainGate.cgroup_inode
                ),
                "terminated": True,
            },
        )
        monkeypatch.setattr(kc, "cgroup_absent", lambda _path: False)
        monkeypatch.setattr(kc, "cleanup_cgroup", lambda _path, _inode: True)

        assert kb.cleanup_inactive_worker_containments(conn) == 1
        current = kb.get_task(conn, task_id)
        assert current is not None
        assert current.status == "ready"
        assert current.current_run_id is None
        row = conn.execute(
            "SELECT termination_certified_at, unlink_intent_at, cleaned_at "
            "FROM worker_containments WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        assert all(value is not None for value in row)
        ended = conn.execute(
            "SELECT status, outcome, ended_at FROM task_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        assert ended["status"] == "spawn_failed"
        assert ended["outcome"] == "spawn_failed"
        assert ended["ended_at"] is not None
    finally:
        conn.close()


def test_partial_containment_schema_migrates_before_guards_are_installed(tmp_path):
    db_path = tmp_path / "kanban.db"
    conn = kb.connect(db_path)
    try:
        conn.execute("DROP TABLE worker_containments")
        conn.execute(
            """
            CREATE TABLE worker_containments (
                run_id INTEGER PRIMARY KEY,
                task_id TEXT NOT NULL,
                claim_lock TEXT NOT NULL,
                backend TEXT NOT NULL,
                worker_pid INTEGER NOT NULL,
                cgroup_path TEXT NOT NULL,
                cgroup_inode INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                cleaned_at INTEGER
            )
            """
        )
        conn.commit()
    finally:
        conn.close()

    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    upgraded = kb.connect(db_path)
    try:
        columns = {
            row["name"]
            for row in upgraded.execute("PRAGMA table_info(worker_containments)")
        }
        assert {
            "retirement_started_at",
            "retirement_reason",
            "termination_certified_at",
            "unlink_intent_at",
        } <= columns
        trigger_names = {
            row["name"]
            for row in upgraded.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            )
        }
        assert "guard_reserved_worker_identity_v2" in trigger_names
        assert "guard_uncertified_contained_retry_v3" in trigger_names
        assert "guard_uncleaned_worker_containment_delete_v2" in trigger_names
    finally:
        upgraded.close()


def test_hard_delete_refuses_to_sever_uncleaned_containment_identity(tmp_path):
    conn = kb.connect(tmp_path / "kanban.db")
    try:
        task_id = kb.create_task(conn, title="cannot sever", assignee="default")
        task = kb.claim_task(conn, task_id, claimer="host:owner")
        assert task is not None and task.current_run_id is not None
        assert task.claim_lock is not None
        kb._register_worker_containment(
            conn,
            task_id,
            run_id=task.current_run_id,
            claim_lock=task.claim_lock,
            worker_pid=959595,
            cgroup_path="/sys/fs/cgroup/hermes-worker-delete-guard",
            cgroup_inode=17171,
        )

        with pytest.raises(sqlite3.IntegrityError, match="uncleaned worker containment"):
            kb.delete_task(conn, task_id)

        assert kb.get_task(conn, task_id) is not None
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM worker_containments WHERE task_id = ?",
            (task_id,),
        ).fetchone()["n"] == 1
    finally:
        conn.close()


def test_schema_bootstrap_never_drops_a_live_retirement_guard():
    assert "DROP TRIGGER" not in kb.SCHEMA_SQL


def test_inactive_containment_cleanup_certifies_before_unlink_and_cleaned(
    monkeypatch,
    tmp_path,
):
    conn = kb.connect(tmp_path / "kanban.db")
    try:
        task_id = kb.create_task(conn, title="cleanup exact", assignee="default")
        task = kb.claim_task(conn, task_id, claimer="host:owner")
        assert task is not None and task.current_run_id is not None
        assert task.claim_lock is not None
        run_id = int(task.current_run_id)
        cgroup_path = "/sys/fs/cgroup/hermes-worker-cleanup"
        cgroup_inode = 18181
        kb._register_worker_containment(
            conn,
            task_id,
            run_id=run_id,
            claim_lock=task.claim_lock,
            worker_pid=969696,
            cgroup_path=cgroup_path,
            cgroup_inode=cgroup_inode,
        )
        assert kb.complete_task(conn, task_id, expected_run_id=run_id)

        observations = []

        def fake_kill(path, inode):
            row = conn.execute(
                "SELECT termination_certified_at, unlink_intent_at, cleaned_at "
                "FROM worker_containments WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            observations.append(("kill", path, inode, tuple(row)))
            return {"containment_certified": True, "terminated": True}

        def fake_cleanup(path, inode):
            row = conn.execute(
                "SELECT termination_certified_at, unlink_intent_at, cleaned_at "
                "FROM worker_containments WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            observations.append(("cleanup", path, inode, tuple(row)))
            return True

        monkeypatch.setattr(kc, "kill_cgroup", fake_kill, raising=False)
        monkeypatch.setattr(kc, "cleanup_cgroup", fake_cleanup, raising=False)
        monkeypatch.setattr(kc, "cgroup_absent", lambda _path: False, raising=False)

        assert kb.cleanup_inactive_worker_containments(conn) == 1
        assert observations[0] == (
            "kill",
            cgroup_path,
            cgroup_inode,
            (None, None, None),
        )
        assert observations[1][0:3] == ("cleanup", cgroup_path, cgroup_inode)
        assert observations[1][3][0] is not None
        assert observations[1][3][1] is not None
        assert observations[1][3][2] is None
        row = conn.execute(
            "SELECT termination_certified_at, unlink_intent_at, cleaned_at "
            "FROM worker_containments WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        assert all(value is not None for value in row)
    finally:
        conn.close()


def test_dispatch_sweeps_containments_before_legacy_reclaim(monkeypatch, tmp_path):
    conn = kb.connect(tmp_path / "kanban.db")
    order = []
    try:
        monkeypatch.setattr(
            kb,
            "cleanup_inactive_worker_containments",
            lambda _conn: order.append("containment") or 0,
        )
        monkeypatch.setattr(
            kb,
            "release_stale_claims",
            lambda _conn: order.append("legacy_reclaim") or 0,
        )

        kb._dispatch_once_locked(
            conn,
            max_spawn=0,
            reconcile_orphans=False,
        )
        assert order[:2] == ["containment", "legacy_reclaim"]

        order.clear()
        kb._dispatch_once_locked(
            conn,
            dry_run=True,
            max_spawn=0,
            reconcile_orphans=False,
        )
        assert "containment" not in order
    finally:
        conn.close()


def test_manual_reclaim_reserves_and_certifies_exact_cgroup_before_release(
    monkeypatch,
    tmp_path,
):
    conn = kb.connect(tmp_path / "kanban.db")
    try:
        task_id = kb.create_task(conn, title="manual exact", assignee="default")
        task = kb.claim_task(conn, task_id, claimer="host:owner")
        assert task is not None and task.current_run_id is not None
        assert task.claim_lock is not None
        run_id = int(task.current_run_id)
        cgroup_path = "/sys/fs/cgroup/hermes-worker-manual"
        cgroup_inode = 19191
        kb._register_worker_containment(
            conn,
            task_id,
            run_id=run_id,
            claim_lock=task.claim_lock,
            worker_pid=979797,
            cgroup_path=cgroup_path,
            cgroup_inode=cgroup_inode,
        )
        legacy_signals = []

        def fake_kill(path, inode):
            row = conn.execute(
                "SELECT retirement_reason, termination_certified_at "
                "FROM worker_containments WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            current = kb.get_task(conn, task_id)
            assert (path, inode) == (cgroup_path, cgroup_inode)
            assert row["retirement_reason"] == "manual_reclaim"
            assert row["termination_certified_at"] is None
            assert current is not None
            assert current.status == "running"
            assert current.current_run_id == run_id
            return {"containment_certified": True, "terminated": True}

        monkeypatch.setattr(kc, "kill_cgroup", fake_kill)
        assert kb.reclaim_task(
            conn,
            task_id,
            reason="operator retry",
            signal_fn=lambda *args: legacy_signals.append(args),
        )

        assert legacy_signals == []
        current = kb.get_task(conn, task_id)
        assert current is not None
        assert current.status == "ready"
        assert current.current_run_id is None
        row = conn.execute(
            "SELECT retirement_reason, termination_certified_at, cleaned_at "
            "FROM worker_containments WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        assert row["retirement_reason"] == "manual_reclaim"
        assert row["termination_certified_at"] is not None
        assert row["cleaned_at"] is None
    finally:
        conn.close()


def test_ttl_reclaim_reserves_and_certifies_exact_cgroup_before_release(
    monkeypatch,
    tmp_path,
):
    conn = kb.connect(tmp_path / "kanban.db")
    try:
        task_id = kb.create_task(conn, title="ttl exact", assignee="default")
        task = kb.claim_task(conn, task_id, claimer="host:owner")
        assert task is not None and task.current_run_id is not None
        assert task.claim_lock is not None
        run_id = int(task.current_run_id)
        kb._register_worker_containment(
            conn,
            task_id,
            run_id=run_id,
            claim_lock=task.claim_lock,
            worker_pid=969696,
            cgroup_path="/sys/fs/cgroup/hermes-worker-ttl",
            cgroup_inode=18181,
        )
        expired = int(time.time()) - 5
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET claim_expires = ? WHERE id = ?",
                (expired, task_id),
            )
            conn.execute(
                "UPDATE task_runs SET claim_expires = ? WHERE id = ?",
                (expired, run_id),
            )
        legacy_signals = []
        monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)

        def fake_kill(path, inode):
            row = conn.execute(
                "SELECT retirement_reason, termination_certified_at "
                "FROM worker_containments WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            current = kb.get_task(conn, task_id)
            assert (path, inode) == (
                "/sys/fs/cgroup/hermes-worker-ttl",
                18181,
            )
            assert row["retirement_reason"] == "ttl_expired"
            assert row["termination_certified_at"] is None
            assert current is not None
            assert current.status == "running"
            assert current.current_run_id == run_id
            return {"containment_certified": True, "terminated": True}

        monkeypatch.setattr(kc, "kill_cgroup", fake_kill)
        assert kb.release_stale_claims(
            conn,
            signal_fn=lambda *args: legacy_signals.append(args),
        ) == 1

        assert legacy_signals == []
        current = kb.get_task(conn, task_id)
        assert current is not None
        assert current.status == "ready"
        assert current.current_run_id is None
        row = conn.execute(
            "SELECT retirement_reason, termination_certified_at "
            "FROM worker_containments WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        assert row["retirement_reason"] == "ttl_expired"
        assert row["termination_certified_at"] is not None
    finally:
        conn.close()


def test_uncertified_contained_run_cannot_be_requeued_by_a_bypass(tmp_path):
    conn = kb.connect(tmp_path / "kanban.db")
    try:
        task_id = kb.create_task(conn, title="guard bypass", assignee="default")
        task = kb.claim_task(conn, task_id, claimer="host:owner")
        assert task is not None and task.current_run_id is not None
        assert task.claim_lock is not None
        run_id = int(task.current_run_id)
        kb._register_worker_containment(
            conn,
            task_id,
            run_id=run_id,
            claim_lock=task.claim_lock,
            worker_pid=959595,
            cgroup_path="/sys/fs/cgroup/hermes-worker-bypass",
            cgroup_inode=17171,
        )

        with pytest.raises(
            sqlite3.IntegrityError,
            match="uncertified worker containment",
        ):
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET status = 'ready', current_run_id = NULL, "
                    "claim_lock = NULL, claim_expires = NULL, worker_pid = NULL "
                    "WHERE id = ?",
                    (task_id,),
                )

        current = kb.get_task(conn, task_id)
        assert current is not None
        assert current.status == "running"
        assert current.current_run_id == run_id

        row = kb._worker_containment_for_termination(conn, task_id, run_id)
        assert row is not None
        assert kb._persist_containment_certification(conn, row)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'ready', current_run_id = NULL, "
                "claim_lock = NULL, claim_expires = NULL, worker_pid = NULL "
                "WHERE id = ?",
                (task_id,),
            )
        current = kb.get_task(conn, task_id)
        assert current is not None and current.status == "ready"
    finally:
        conn.close()


def test_max_runtime_reserves_and_certifies_exact_cgroup_before_retry(
    monkeypatch,
    tmp_path,
):
    conn = kb.connect(tmp_path / "kanban.db")
    try:
        task_id = kb.create_task(
            conn,
            title="runtime exact",
            assignee="default",
            max_runtime_seconds=1,
        )
        host = kb._claimer_id().split(":", 1)[0]
        task = kb.claim_task(conn, task_id, claimer=f"{host}:owner")
        assert task is not None and task.current_run_id is not None
        assert task.claim_lock is not None
        run_id = int(task.current_run_id)
        kb._register_worker_containment(
            conn,
            task_id,
            run_id=run_id,
            claim_lock=task.claim_lock,
            worker_pid=949494,
            cgroup_path="/sys/fs/cgroup/hermes-worker-runtime",
            cgroup_inode=16161,
        )
        old = int(time.time()) - 100
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET started_at = ? WHERE id = ?", (old, task_id))
            conn.execute(
                "UPDATE task_runs SET started_at = ? WHERE id = ?",
                (old, run_id),
            )
        legacy_signals = []

        def fake_kill(path, inode):
            row = conn.execute(
                "SELECT retirement_reason, termination_certified_at "
                "FROM worker_containments WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            assert (path, inode) == (
                "/sys/fs/cgroup/hermes-worker-runtime",
                16161,
            )
            assert row["retirement_reason"] == "max_runtime"
            assert row["termination_certified_at"] is None
            current = kb.get_task(conn, task_id)
            assert current is not None and current.status == "running"
            return {"containment_certified": True, "terminated": True}

        monkeypatch.setattr(kc, "kill_cgroup", fake_kill)
        assert kb.enforce_max_runtime(
            conn,
            signal_fn=lambda *args: legacy_signals.append(args),
        ) == [task_id]
        assert legacy_signals == []
        current = kb.get_task(conn, task_id)
        assert current is not None and current.status == "ready"
        row = conn.execute(
            "SELECT retirement_reason, termination_certified_at "
            "FROM worker_containments WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        assert row["retirement_reason"] == "max_runtime"
        assert row["termination_certified_at"] is not None
    finally:
        conn.close()


def test_stale_heartbeat_reserves_and_certifies_exact_cgroup_before_retry(
    monkeypatch,
    tmp_path,
):
    conn = kb.connect(tmp_path / "kanban.db")
    try:
        task_id = kb.create_task(conn, title="stale exact", assignee="default")
        host = kb._claimer_id().split(":", 1)[0]
        task = kb.claim_task(conn, task_id, claimer=f"{host}:owner")
        assert task is not None and task.current_run_id is not None
        assert task.claim_lock is not None
        run_id = int(task.current_run_id)
        kb._register_worker_containment(
            conn,
            task_id,
            run_id=run_id,
            claim_lock=task.claim_lock,
            worker_pid=939394,
            cgroup_path="/sys/fs/cgroup/hermes-worker-stale",
            cgroup_inode=15152,
        )
        old = int(time.time()) - 100
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET started_at = ?, last_heartbeat_at = NULL "
                "WHERE id = ?",
                (old, task_id),
            )
            conn.execute(
                "UPDATE task_runs SET started_at = ?, last_heartbeat_at = NULL "
                "WHERE id = ?",
                (old, run_id),
            )
        legacy_signals = []

        def fake_kill(path, inode):
            row = conn.execute(
                "SELECT retirement_reason, termination_certified_at "
                "FROM worker_containments WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            assert (path, inode) == (
                "/sys/fs/cgroup/hermes-worker-stale",
                15152,
            )
            assert row["retirement_reason"] == "stale_heartbeat"
            assert row["termination_certified_at"] is None
            return {"containment_certified": True, "terminated": True}

        monkeypatch.setattr(kc, "kill_cgroup", fake_kill)
        assert kb.detect_stale_running(
            conn,
            stale_timeout_seconds=1,
            signal_fn=lambda *args: legacy_signals.append(args),
        ) == [task_id]
        assert legacy_signals == []
        current = kb.get_task(conn, task_id)
        assert current is not None and current.status == "ready"
        row = conn.execute(
            "SELECT retirement_reason, termination_certified_at "
            "FROM worker_containments WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        assert row["retirement_reason"] == "stale_heartbeat"
        assert row["termination_certified_at"] is not None
    finally:
        conn.close()


def test_crash_detection_uses_exact_empty_cgroup_not_recycled_live_pid(
    monkeypatch,
    tmp_path,
):
    conn = kb.connect(tmp_path / "kanban.db")
    try:
        task_id = kb.create_task(conn, title="crash exact", assignee="default")
        host = kb._claimer_id().split(":", 1)[0]
        task = kb.claim_task(conn, task_id, claimer=f"{host}:owner")
        assert task is not None and task.current_run_id is not None
        assert task.claim_lock is not None
        run_id = int(task.current_run_id)
        kb._register_worker_containment(
            conn,
            task_id,
            run_id=run_id,
            claim_lock=task.claim_lock,
            worker_pid=929293,
            cgroup_path="/sys/fs/cgroup/hermes-worker-crash",
            cgroup_inode=14143,
        )
        old = int(time.time()) - 100
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET started_at = ? WHERE id = ?", (old, task_id))
            conn.execute(
                "UPDATE task_runs SET started_at = ? WHERE id = ?",
                (old, run_id),
            )

        monkeypatch.setattr(kb, "_pid_alive", lambda pid: True)
        monkeypatch.setattr(kc, "cgroup_populated", lambda path, inode: False)

        def fake_kill(path, inode):
            row = conn.execute(
                "SELECT retirement_reason, termination_certified_at "
                "FROM worker_containments WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            assert (path, inode) == (
                "/sys/fs/cgroup/hermes-worker-crash",
                14143,
            )
            assert row["retirement_reason"] == "crash_detected"
            assert row["termination_certified_at"] is None
            return {"containment_certified": True, "terminated": False}

        monkeypatch.setattr(kc, "kill_cgroup", fake_kill)
        assert kb.detect_crashed_workers(conn) == [task_id]
        current = kb.get_task(conn, task_id)
        assert current is not None and current.status in {"ready", "blocked"}
        row = conn.execute(
            "SELECT retirement_reason, termination_certified_at "
            "FROM worker_containments WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        assert row["retirement_reason"] == "crash_detected"
        assert row["termination_certified_at"] is not None
    finally:
        conn.close()


def test_orphan_reconciliation_uses_durable_containment_after_claim_loss(
    monkeypatch,
    tmp_path,
):
    conn = kb.connect(tmp_path / "kanban.db")
    try:
        task_id = kb.create_task(conn, title="orphan exact", assignee="default")
        task = kb.claim_task(conn, task_id, claimer="host:orphan-owner")
        assert task is not None and task.current_run_id is not None
        assert task.claim_lock is not None
        run_id = int(task.current_run_id)
        kb._register_worker_containment(
            conn,
            task_id,
            run_id=run_id,
            claim_lock=task.claim_lock,
            worker_pid=919192,
            cgroup_path="/sys/fs/cgroup/hermes-worker-orphan",
            cgroup_inode=13133,
        )
        with kb.write_txn(conn):
            conn.execute("DROP TRIGGER guard_reserved_worker_identity_v2")
            conn.execute("DROP TRIGGER guard_uncertified_contained_retry_v3")
            conn.execute(
                "UPDATE tasks SET claim_lock = NULL, claim_expires = NULL "
                "WHERE id = ?",
                (task_id,),
            )
        kb._migrate_add_optional_columns(conn)
        monkeypatch.setattr(kb, "_pid_alive", lambda pid: True)

        def fake_kill(path, inode):
            assert (path, inode) == (
                "/sys/fs/cgroup/hermes-worker-orphan",
                13133,
            )
            row = conn.execute(
                "SELECT retirement_reason FROM worker_containments WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            assert row["retirement_reason"] == "orphan_reconcile"
            return {"containment_certified": True, "terminated": True}

        monkeypatch.setattr(kc, "kill_cgroup", fake_kill)
        assert kb.reconcile_orphaned_running(conn) == [task_id]
        current = kb.get_task(conn, task_id)
        assert current is not None and current.status == "ready"
        row = conn.execute(
            "SELECT termination_certified_at FROM worker_containments "
            "WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        assert row["termination_certified_at"] is not None
    finally:
        conn.close()
