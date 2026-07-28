from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from gateway.worker_bridge_watchers import (
    GatewayWorkerBridgeWatchersMixin,
    _resolve_alert_settings,
    baseline_cursor,
    claim_task_for_dispatch,
    collect_new_transitions,
    format_alert_text,
    load_cursor,
    select_dispatchable_tasks,
)


def test_recovered_mixin_api_surface_exists():
    for method in (
        "_worker_bridge_notifier_watcher",
        "_worker_bridge_tick",
        "_worker_bridge_auto_dispatch",
        "_worker_bridge_idle_nudge",
        "_resolve_worker_alert_target",
    ):
        assert hasattr(GatewayWorkerBridgeWatchersMixin, method)


def test_gateway_alert_config_reader_resolves_required_keys():
    settings = _resolve_alert_settings(
        {
            "worker_bridge": {
                "gateway_alerts": {
                    "enabled": True,
                    "interval_seconds": 23,
                    "statuses": ["failed"],
                    "platform": "discord",
                    "chat_id": "alerts",
                }
            }
        }
    )

    assert settings is not None
    assert settings["interval"] == 23
    assert settings["statuses"] == frozenset({"failed"})
    assert settings["platform"] == "discord"
    assert settings["chat_id"] == "alerts"


def test_gateway_alert_config_reader_is_disabled_when_absent():
    assert _resolve_alert_settings({}) is None


def test_alert_text_injects_terminal_status_pending_work_and_triage_contract():
    text = format_alert_text(
        [{"task_id": "task-1", "worker": "codex", "status": "failed"}],
        [{"task_id": "task-2", "status": "created", "objective": "repair"}],
    )

    assert "task-1 (codex) → failed" in text
    assert "task-2 [created] repair" in text
    assert "Triage failures" in text
    assert "hermes worker tasks start <task_id>" in text


def _create_dispatch_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE tasks (
            task_id TEXT PRIMARY KEY,
            worker TEXT,
            status TEXT,
            priority INTEGER,
            spec TEXT,
            runtime TEXT,
            result TEXT,
            created_at REAL,
            updated_at REAL
        );
        """
    )
    for task_id, status in (("task-created", "created"), ("task-queued", "queued")):
        conn.execute(
            "INSERT INTO tasks VALUES (?, 'codex', ?, 50, ?, '{}', '{}', 1, 1)",
            (
                task_id,
                status,
                json.dumps({"objective": task_id, "metadata": {}}),
            ),
        )
    conn.commit()
    conn.close()


def test_dispatch_selection_owns_created_and_queued(tmp_path):
    """This watcher is the sole dispatcher of both statuses.

    It used to own `queued` only, and this test asserted that. The split was a
    recovery artefact, not a design: the thin dispatcher that owned `created`
    applies none of the guards here, so `created` tasks were dispatched without
    the manual-hold, dependency, permission, input or retry checks that
    `queued` tasks got. Ownership was handed over deliberately; the thin
    dispatcher now stands down, so nothing races for these rows.
    """
    db_path = tmp_path / "bridge.db"
    _create_dispatch_db(db_path)

    eligible, skipped = select_dispatchable_tasks(db_path, 10)

    assert {task["task_id"] for task in eligible} == {"task-created", "task-queued"}
    assert not [task for task in skipped if task["task_id"] == "task-created"]

    # Claiming a `created` row reserves it into `queued` in the same
    # transaction, and the snapshot records the status it had so a release can
    # put it back rather than stranding it in `queued`.
    snapshot = claim_task_for_dispatch(db_path, "task-created")
    assert snapshot == {"status": "created", "runtime": {}}
    conn = sqlite3.connect(db_path)
    try:
        status = conn.execute(
            "SELECT status FROM tasks WHERE task_id='task-created'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert status == "queued", "claim must reserve created->queued atomically"

    # Already claimed by this live pid — a second claim must not hand it out.
    assert claim_task_for_dispatch(db_path, "task-created") is None

    snapshot = claim_task_for_dispatch(db_path, "task-queued")
    assert snapshot == {"status": "queued", "runtime": {}}


def test_cursor_startup_baseline_never_replays_backlog(tmp_path):
    db_path = tmp_path / "bridge.db"
    state_file = tmp_path / "gateway_alerts_cursor.json"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE events (event_id INTEGER PRIMARY KEY, kind TEXT, "
        "task_id TEXT, payload TEXT, created_at REAL)"
    )
    conn.executemany(
        "INSERT INTO events VALUES (?, 'task.status', 'task-1', "
        "'{\"status\":\"failed\"}', 1)",
        [(1,), (2,), (1003,)],
    )
    conn.commit()
    conn.close()
    state_file.write_text('{"last_event_id": 2}', encoding="utf-8")

    assert baseline_cursor(db_path, state_file) == 1003
    assert load_cursor(state_file) == 1003


def test_cursor_startup_baselines_missing_cursor_to_current_head(tmp_path):
    db_path = tmp_path / "bridge.db"
    state_file = tmp_path / "gateway_alerts_cursor.json"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE events (event_id INTEGER PRIMARY KEY, kind TEXT, "
        "task_id TEXT, payload TEXT, created_at REAL)"
    )
    conn.execute(
        "INSERT INTO events VALUES "
        "(42, 'task.status', 'task-1', '{\"status\":\"failed\"}', 1)"
    )
    conn.commit()
    conn.close()

    assert not state_file.exists()
    assert baseline_cursor(db_path, state_file) == 42
    assert load_cursor(state_file) == 42


def test_cursor_startup_preserves_small_pending_window(tmp_path):
    db_path = tmp_path / "bridge.db"
    state_file = tmp_path / "gateway_alerts_cursor.json"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE events (event_id INTEGER PRIMARY KEY, kind TEXT, "
        "task_id TEXT, payload TEXT, created_at REAL)"
    )
    conn.executemany(
        "INSERT INTO events VALUES (?, 'task.status', 'task-1', "
        "'{\"status\":\"failed\"}', 1)",
        [(100,), (117,)],
    )
    conn.commit()
    conn.close()
    state_file.write_text('{"last_event_id": 100}', encoding="utf-8")

    assert baseline_cursor(db_path, state_file) == 100
    assert load_cursor(state_file) == 100


def test_recent_cursor_preserves_and_collects_terminal_transitions(tmp_path):
    state_file = tmp_path / "gateway_alerts_cursor.json"
    db_path = tmp_path / "bridge.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE tasks (
            task_id TEXT PRIMARY KEY,
            worker TEXT,
            status TEXT,
            spec TEXT,
            runtime TEXT,
            result TEXT
        );
        CREATE TABLE events (
            event_id INTEGER PRIMARY KEY,
            kind TEXT,
            task_id TEXT,
            payload TEXT,
            created_at REAL
        );
        """
    )
    conn.execute(
        "INSERT INTO tasks VALUES "
        "('task-1', 'codex', 'failed', ?, '{}', ?)",
        (
            json.dumps({"objective": "repair"}),
            json.dumps({"error": "boom"}),
        ),
    )
    conn.executemany(
        "INSERT INTO events VALUES (?, 'task.status', 'task-1', ?, 1)",
        [
            (100, json.dumps({"status": "running"})),
            (117, json.dumps({"status": "failed"})),
        ],
    )
    conn.commit()
    conn.close()
    state_file.write_text('{"last_event_id": 100}', encoding="utf-8")

    assert baseline_cursor(db_path, state_file) == 100
    transitions, head = collect_new_transitions(
        db_path, state_file, frozenset({"failed", "succeeded", "timed_out"})
    )

    assert head == 117
    assert transitions == [
        {
            "event_id": 117,
            "task_id": "task-1",
            "status": "failed",
            "at": 1.0,
            "worker": "codex",
            "task_status": "failed",
            "objective": "repair",
            "error": "boom",
        }
    ]
