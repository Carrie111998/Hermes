"""Behavior tests for the append-only Kanban lifecycle ledger."""

from __future__ import annotations

import concurrent.futures
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def ledger_db(tmp_path, monkeypatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    path = home / "kanban.db"
    kb.init_db(path)
    return path


def test_append_lifecycle_event_records_versioned_server_timestamp(ledger_db):
    with kb.connect_closing(ledger_db) as conn:
        event = kb.append_lifecycle_event(
            conn,
            ticket_id="JOB-42",
            event_type="pr_merged",
            pr_number=17,
            merge_sha="A" * 40,
            metadata={"source": "github", "labels": ["measurement"]},
        )

        row = conn.execute("SELECT * FROM kanban_lifecycle_events").fetchone()

    assert event.id == row["id"]
    assert event.schema_version == kb.LIFECYCLE_LEDGER_SCHEMA_VERSION == 1
    assert event.merge_sha == "a" * 40
    assert event.metadata == {"labels": ["measurement"], "source": "github"}
    assert event.recorded_at.endswith("Z")
    assert datetime.fromisoformat(event.recorded_at.replace("Z", "+00:00")).utcoffset().total_seconds() == 0


def test_duplicate_retry_returns_original_without_insert(ledger_db):
    with kb.connect_closing(ledger_db) as conn:
        original = kb.append_lifecycle_event(
            conn,
            ticket_id="JOB-42",
            event_type="pr_opened",
            pr_number=17,
            metadata={"attempt": 1},
        )
        retry = kb.append_lifecycle_event(
            conn,
            ticket_id="JOB-42",
            event_type="pr_opened",
            pr_number=17,
            metadata={"attempt": 2},
        )
        count = conn.execute("SELECT COUNT(*) FROM kanban_lifecycle_events").fetchone()[0]

    assert retry == original
    assert retry.metadata == {"attempt": 1}
    assert count == 1


def test_caller_timestamps_do_not_control_ledger_order(ledger_db, monkeypatch):
    server_times = iter(("2026-01-01T00:00:00.000001Z", "2026-01-01T00:00:00.000002Z"))
    monkeypatch.setattr(kb, "_lifecycle_utc_now", lambda: next(server_times))

    with kb.connect_closing(ledger_db) as conn:
        first = kb.append_lifecycle_event(
            conn,
            ticket_id="JOB-42",
            event_type="ticket_started",
            metadata={"caller_timestamp": "2099-01-01T00:00:00Z"},
        )
        second = kb.append_lifecycle_event(
            conn,
            ticket_id="JOB-42",
            event_type="pr_opened",
            metadata={"caller_timestamp": "1999-01-01T00:00:00Z"},
        )

    assert first.id < second.id
    assert first.recorded_at < second.recorded_at


@pytest.mark.parametrize(
    "kwargs",
    [
        {"ticket_id": "", "event_type": "ticket_created"},
        {"ticket_id": "JOB-42", "event_type": "invented"},
        {"ticket_id": "JOB-42", "event_type": "pr_opened", "pr_number": 0},
        {"ticket_id": "JOB-42", "event_type": "pr_merged", "merge_sha": "xyz"},
        {"ticket_id": "JOB-42", "event_type": "ticket_created", "metadata": ["not", "an", "object"]},
        {"ticket_id": "JOB-42", "event_type": "ticket_created", "metadata": {"blob": "x" * 20_000}},
    ],
)
def test_append_lifecycle_event_rejects_malformed_input(ledger_db, kwargs):
    with kb.connect_closing(ledger_db) as conn:
        with pytest.raises(ValueError):
            kb.append_lifecycle_event(conn, **kwargs)
        assert conn.execute("SELECT COUNT(*) FROM kanban_lifecycle_events").fetchone()[0] == 0


@pytest.mark.parametrize("pr_number", [1, 9223372036854775807])
def test_pr_number_accepts_sqlite_integer_boundaries(ledger_db, pr_number):
    with kb.connect_closing(ledger_db) as conn:
        event = kb.append_lifecycle_event(
            conn,
            ticket_id="JOB-42",
            event_type="pr_opened",
            pr_number=pr_number,
        )

    assert event.pr_number == pr_number


def test_pr_number_above_sqlite_integer_range_raises_value_error(ledger_db):
    with kb.connect_closing(ledger_db) as conn:
        with pytest.raises(ValueError, match="9223372036854775807"):
            kb.append_lifecycle_event(
                conn,
                ticket_id="JOB-42",
                event_type="pr_opened",
                pr_number=9223372036854775808,
            )
        assert conn.execute("SELECT COUNT(*) FROM kanban_lifecycle_events").fetchone()[0] == 0


def test_concurrent_duplicate_submissions_insert_exactly_once(ledger_db):
    workers = 8
    barrier = threading.Barrier(workers)

    def submit(_index: int):
        with kb.connect_closing(ledger_db) as conn:
            barrier.wait(timeout=5)
            return kb.append_lifecycle_event(
                conn,
                ticket_id="JOB-42",
                event_type="pr_merged",
                pr_number=17,
                merge_sha="b" * 40,
                metadata={"worker": _index},
            )

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        results = list(executor.map(submit, range(workers)))

    with kb.connect_closing(ledger_db) as conn:
        rows = conn.execute("SELECT * FROM kanban_lifecycle_events").fetchall()

    assert len(rows) == 1
    assert {result.id for result in results} == {rows[0]["id"]}
    assert {result.recorded_at for result in results} == {rows[0]["recorded_at"]}


def test_lifecycle_ledger_rows_cannot_be_updated_or_deleted(ledger_db):
    with kb.connect_closing(ledger_db) as conn:
        event = kb.append_lifecycle_event(
            conn,
            ticket_id="JOB-42",
            event_type="ticket_created",
        )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "UPDATE kanban_lifecycle_events SET event_type = 'ticket_closed' WHERE id = ?",
                (event.id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM kanban_lifecycle_events WHERE id = ?", (event.id,))


def test_lifecycle_ledger_rows_cannot_be_replaced_with_recursive_triggers_off(ledger_db):
    with kb.connect_closing(ledger_db) as conn:
        event = kb.append_lifecycle_event(
            conn,
            ticket_id="JOB-42",
            event_type="pr_opened",
            pr_number=17,
            metadata={"attempt": 1},
        )
        original = dict(conn.execute("SELECT * FROM kanban_lifecycle_events").fetchone())
        conn.execute("PRAGMA recursive_triggers = OFF")

        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "INSERT OR REPLACE INTO kanban_lifecycle_events "
                "(id, schema_version, ticket_id, event_type, pr_number, metadata, recorded_at) "
                "VALUES (?, 1, 'JOB-42', 'pr_closed', 17, '{}', '2099-01-01T00:00:00Z')",
                (event.id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "INSERT OR REPLACE INTO kanban_lifecycle_events "
                "(schema_version, ticket_id, event_type, pr_number, metadata, recorded_at) "
                "VALUES (1, 'JOB-42', 'pr_opened', 17, '{}', '2099-01-01T00:00:00Z')"
            )

        rows = conn.execute("SELECT * FROM kanban_lifecycle_events").fetchall()

    assert len(rows) == 1
    assert dict(rows[0]) == original
