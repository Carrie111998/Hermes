"""Integration coverage for routing isolation, migration, events, and provenance."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.routing_contract import RoutingContractError


ROSTER = {
    "roles": {
        "executor": {
            "model": "exec-model",
            "provider": "exec-provider",
            "invocation": "auto",
            "may_edit": True,
            "review_capable": False,
        },
        "auditor": {
            "model": "audit-model",
            "provider": "audit-provider",
            "invocation": "auto",
            "may_edit": False,
            "review_capable": True,
        },
        "reviewer": {
            "model": "review-model",
            "provider": "review-provider",
            "invocation": "auto",
            "may_edit": False,
            "review_capable": True,
        },
    }
}


def _insert_snapshot_run(
    conn: sqlite3.Connection, task_id: str, source: str, model: str
) -> int:
    """Insert one minimal frozen routing snapshot and return its local run id."""
    now = int(time.time())
    conn.execute(
        "INSERT INTO tasks (id,title,status,created_at) VALUES (?,?,?,?)",
        (task_id, task_id, "done", now),
    )
    run_id = conn.execute(
        "INSERT INTO task_runs "
        "(task_id,status,started_at,ended_at,routing_model,routing_source) "
        "VALUES (?,?,?,?,?,?)",
        (task_id, "completed", now - 1, now, model, source),
    ).lastrowid
    assert run_id is not None
    conn.commit()
    return int(run_id)


def _legacy_pre_routing_db(path: Path, *, run_count: int = 0) -> list[int]:
    """Build a legacy DB with optional runs, then strip routing additions."""
    kb.init_db(path)
    with sqlite3.connect(path) as conn:
        run_ids = [
            _insert_legacy_run(conn, f"legacy-{index}", "done")
            for index in range(run_count)
        ]
        conn.execute("DROP TABLE kanban_metadata")
        conn.execute("ALTER TABLE tasks DROP COLUMN routing_role")
        conn.execute("ALTER TABLE task_runs DROP COLUMN routing_source")
        conn.commit()
    kb._INITIALIZED_PATHS.discard(str(path.resolve()))
    return run_ids


def _roster() -> tuple[dict, str]:
    """Return the deterministic role roster used by review integration tests."""
    return ROSTER, "integration-roster-digest"


def _disable_routing_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent host configuration from influencing routing resolution."""
    monkeypatch.setattr(kb, "read_board_metadata", lambda *args, **kwargs: {})
    monkeypatch.setattr(kb, "_load_profile_model_config", lambda profile: (None, None))


def _insert_legacy_run(
    conn: sqlite3.Connection,
    task_id: str,
    task_status: str,
    *,
    ended: bool = True,
) -> int:
    """Insert a legacy run whose eligibility is controlled by task terminality."""
    now = int(time.time())
    conn.execute(
        "INSERT INTO tasks (id,title,status,created_at) VALUES (?,?,?,?)",
        (task_id, task_id, task_status, now),
    )
    run_id = conn.execute(
        "INSERT INTO task_runs (task_id,profile,status,started_at,ended_at) "
        "VALUES (?,?,?,?,?)",
        (task_id, "coder", "completed", now - 1, now if ended else None),
    )
    assert run_id.lastrowid is not None
    return int(run_id.lastrowid)


def test_cross_board_run_ids_are_connection_local_and_mutations_are_isolated(tmp_path):
    """Equal board-local run ids resolve only through their own DB connection."""
    left_db = tmp_path / "left.db"
    right_db = tmp_path / "right.db"
    kb.init_db(left_db)
    kb.init_db(right_db)

    with kb.connect_closing(left_db) as left, kb.connect_closing(right_db) as right:
        left_run = _insert_snapshot_run(left, "left-task", "task_role", "left-model")
        right_run = _insert_snapshot_run(right, "right-task", "board_default", "right-model")
        assert left_run == right_run == 1

        left_snapshot = kb.get_routing_snapshot(left, left_run, board="left-board")
        right_snapshot = kb.get_routing_snapshot(right, right_run, board="right-board")
        assert (left_snapshot.task_id, left_snapshot.board, left_snapshot.routing_model) == (
            "left-task", "left-board", "left-model"
        )
        assert (right_snapshot.task_id, right_snapshot.board, right_snapshot.routing_model) == (
            "right-task", "right-board", "right-model"
        )

        left.execute(
            "UPDATE task_runs SET routing_model='left-mutated' WHERE id=?", (left_run,)
        )
        left.commit()
        assert kb.get_routing_snapshot(left, left_run, board="left-board").routing_model == "left-mutated"
        assert kb.get_routing_snapshot(right, right_run, board="right-board").routing_model == "right-model"


def test_concurrent_connect_closing_migrates_one_legacy_db_atomically(tmp_path):
    """Concurrent first opens publish a complete schema and exact legacy cutoff."""
    db_path = tmp_path / "legacy.db"
    legacy_run_ids = _legacy_pre_routing_db(db_path, run_count=3)
    expected_cutoff = max(legacy_run_ids)
    errors: list[BaseException] = []
    barrier = threading.Barrier(8)

    def open_once() -> None:
        try:
            barrier.wait(timeout=5)
            with kb.connect_closing(db_path) as conn:
                conn.execute("SELECT 1").fetchone()
        except BaseException as exc:  # collected in the main test thread
            errors.append(exc)

    threads = [threading.Thread(target=open_once) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    with kb.connect_closing(db_path) as conn:
        metadata = conn.execute(
            "SELECT key,value,COUNT(*) AS n FROM kanban_metadata "
            "GROUP BY key,value ORDER BY key"
        ).fetchall()
        assert [(row["key"], row["value"], row["n"]) for row in metadata] == [
            ("migration_cutoff_id", str(expected_cutoff), 1),
            ("routing_schema_version", "1", 1),
        ]
        assert "routing_role" in {
            row["name"] for row in conn.execute("PRAGMA table_info(tasks)")
        }
        run_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(task_runs)")
        }
        assert {
            "routing_role", "routing_model", "routing_provider", "routing_contract",
            "routing_reason", "roster_digest", "routing_policy", "ac_revision",
            "routing_source",
        } <= run_columns
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        post_migration_id = _insert_legacy_run(conn, "post-migration", "done")
        conn.commit()

    assert post_migration_id > expected_cutoff


def test_routing_rejection_events_remain_compatible_with_list_events(tmp_path, monkeypatch):
    """New rejection kinds and old kinds remain JSON-readable via ``list_events``."""
    # Explicit arguments keep this integration test independent of module-level fixtures.
    db_path = tmp_path / "events.db"
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as connection:
        task_id = kb.create_task(connection, title="event compatibility", assignee="coder")
        connection.execute("UPDATE tasks SET status='ready' WHERE id=?", (task_id,))
        monkeypatch.setattr(
            kb,
            "_resolve_routing_snapshot",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                RoutingContractError("invalid integration route")
            ),
        )
        with pytest.raises(RoutingContractError, match="invalid integration route"):
            kb.claim_task(connection, task_id)

        run_id = connection.execute(
            "INSERT INTO task_runs (task_id,status,started_at) VALUES (?,?,?)",
            (task_id, "failed", int(time.time())),
        ).lastrowid
        assert run_id is not None
        with kb.write_txn(connection):
            kb._append_spawn_rejected_event(
                connection, task_id, int(run_id), "spawn route rejected", 2, 3
            )
            kb._append_event(connection, task_id, "commented", {"body": "legacy consumer"})

        events = kb.list_events(connection, task_id)
        by_kind = {event.kind: event for event in events}
        assert {"created", "claim_rejected", "spawn_rejected", "commented"} <= set(by_kind)
        assert by_kind["claim_rejected"].payload["reason"] == "invalid integration route"
        assert by_kind["spawn_rejected"].payload == {
            "run_id": int(run_id),
            "reason": "spawn route rejected",
            "consecutive_failures": 2,
            "max_retries": 3,
        }
        assert by_kind["commented"].payload == {"body": "legacy consumer"}
        for kind in ("claim_rejected", "spawn_rejected", "commented"):
            assert json.loads(json.dumps(by_kind[kind].payload)) == by_kind[kind].payload


def test_review_claims_persist_capable_and_coerced_provenance(tmp_path, monkeypatch):
    """Review claims freeze both preserved-role and coerced-review provenance."""
    monkeypatch.setattr(kb, "_load_roster", _roster)
    _disable_routing_defaults(monkeypatch)
    db_path = tmp_path / "reviews.db"
    kb.init_db(db_path)

    with kb.connect_closing(db_path) as conn:
        capable = kb.create_task(conn, title="capable review", assignee="coder")
        coerced = kb.create_task(conn, title="coerced review", assignee="coder")
        kb.set_routing_override(conn, capable, role="auditor")
        kb.set_routing_override(conn, coerced, role="executor")
        conn.execute("UPDATE tasks SET status='review' WHERE id IN (?,?)", (capable, coerced))
        conn.commit()

        assert kb.claim_review_task(conn, capable, claimer="capable-agent") is not None
        assert kb.claim_review_task(conn, coerced, claimer="coerced-agent") is not None
        rows = {
            row["task_id"]: row
            for row in conn.execute(
                "SELECT task_id,routing_role,routing_source,routing_reason FROM task_runs"
            )
        }

    assert tuple(rows[capable]) == (
        capable,
        "auditor",
        "review_capable",
        "review phase: role 'auditor' already review-capable",
    )
    assert tuple(rows[coerced]) == (
        coerced,
        "reviewer",
        "review_coerced",
        "review phase: role 'executor' not review-capable, coerced to reviewer",
    )


def test_backfill_exact_terminality_end_cutoff_and_rerun_idempotence(tmp_path):
    """Backfill only ended, pre-cutoff runs whose exact task status is terminal."""
    db_path = tmp_path / "backfill.db"
    home = tmp_path / ".hermes"
    kb.init_db(db_path)
    active_statuses = ("triage", "todo", "scheduled", "ready", "running", "blocked", "review")

    with kb.connect_closing(db_path) as conn:
        eligible = {
            status: _insert_legacy_run(conn, f"terminal-{status}", status)
            for status in ("done", "archived")
        }
        active = {
            status: _insert_legacy_run(conn, f"active-{status}", status)
            for status in active_statuses
        }
        no_end = _insert_legacy_run(conn, "done-without-end", "done", ended=False)
        cutoff = no_end
        conn.execute(
            "UPDATE kanban_metadata SET value=? WHERE key='migration_cutoff_id'",
            (str(cutoff),),
        )
        post_cutoff = _insert_legacy_run(conn, "done-after-cutoff", "done")
        conn.commit()

        first = kb.backfill_routing_metadata(conn, hermes_home=home)
        sources_after_first = {
            row["id"]: row["routing_source"]
            for row in conn.execute("SELECT id,routing_source FROM task_runs")
        }
        second = kb.backfill_routing_metadata(conn, hermes_home=home)
        sources_after_second = {
            row["id"]: row["routing_source"]
            for row in conn.execute("SELECT id,routing_source FROM task_runs")
        }

    assert first.processed == first.unknown == 2
    assert first.inferred == first.errors == 0
    assert all(sources_after_first[run_id] == "legacy_unknown" for run_id in eligible.values())
    assert all(sources_after_first[run_id] is None for run_id in active.values())
    assert sources_after_first[no_end] is None
    assert post_cutoff > cutoff
    assert sources_after_first[post_cutoff] is None
    assert second.processed == second.inferred == second.unknown == second.errors == 0
    assert sources_after_second == sources_after_first
