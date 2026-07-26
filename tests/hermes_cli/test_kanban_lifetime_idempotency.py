from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban import run_slash


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _meta_packet_key(mission_id: str, packet_id: str) -> str:
    digest = hashlib.sha256(f"{mission_id}\0{packet_id}".encode()).hexdigest()
    return f"lifetime-v1:meta-packet-v1:{digest}"


def _event_rows(conn: sqlite3.Connection, task_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id = ? ORDER BY id",
        (task_id,),
    ).fetchall()


@pytest.mark.parametrize(
    "bad_key",
    [
        "lifetime-v1:",
        "lifetime-v1:namespace",
        "lifetime-v1:namespace:",
        "lifetime-v1:bad namespace:token",
        "lifetime-v1:namespace:token/with/slash",
    ],
)
def test_lifetime_key_rejects_malformed_reserved_representation(kanban_home, bad_key):
    with kb.connect() as conn, pytest.raises(ValueError, match="lifetime-v1"):
        kb.create_task(conn, title="invalid lifetime key", idempotency_key=bad_key)


@pytest.mark.parametrize("terminal_status", ["done", "blocked", "archived"])
def test_lifetime_key_converges_after_terminal_and_archive(
    kanban_home, terminal_status
):
    key = _meta_packet_key("mission-a", f"packet-{terminal_status}")
    with kb.connect() as conn:
        original = kb.create_task(conn, title="first owner", idempotency_key=key)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = ? WHERE id = ?",
                (terminal_status, original),
            )

        duplicate = kb.create_task(
            conn,
            title="renamed retry must converge",
            idempotency_key=key,
        )

        assert duplicate == original
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE idempotency_key = ?", (key,)
            ).fetchone()[0]
            == 1
        )
        events = _event_rows(conn, original)
        suppression = [row for row in events if row["kind"] == "duplicate_suppressed"]
        assert len(suppression) == 1
        payload = json.loads(suppression[0]["payload"])
        assert payload["idempotency_key"] == key
        assert payload["requested_title"] == "renamed retry must converge"


@pytest.mark.parametrize(
    "invalid_route",
    [
        {"skills": ["web"]},
        {"provider_override": "provider-without-model"},
        {"workspace_kind": "scratch", "branch_name": "wt/invalid"},
    ],
    ids=["skill-toolset", "provider-without-model", "branch-without-worktree"],
)
def test_lifetime_duplicate_suppresses_before_optional_route_validation(
    kanban_home, invalid_route
):
    key = _meta_packet_key("mission-order", "packet-order")
    with kb.connect() as conn:
        original = kb.create_task(conn, title="canonical", idempotency_key=key)
        duplicate = kb.create_task(
            conn,
            title="renamed invalid route",
            idempotency_key=key,
            **invalid_route,
        )
        assert duplicate == original
        assert any(
            row["kind"] == "duplicate_suppressed" for row in _event_rows(conn, original)
        )


def test_different_lifetime_key_creates_independent_packet(kanban_home):
    with kb.connect() as conn:
        first = kb.create_task(
            conn,
            title="packet one",
            idempotency_key=_meta_packet_key("mission-a", "packet-1"),
        )
        second = kb.create_task(
            conn,
            title="packet two",
            idempotency_key=_meta_packet_key("mission-a", "packet-2"),
        )
        assert first != second


def test_lifetime_key_is_atomic_across_concurrent_creates(kanban_home):
    key = _meta_packet_key("mission-race", "packet-race")
    start = threading.Barrier(2)
    results: list[str] = []
    errors: list[BaseException] = []

    def create(index: int) -> None:
        conn = kb.connect()
        try:
            start.wait(timeout=10)
            results.append(
                kb.create_task(
                    conn,
                    title=f"race attempt {index}",
                    idempotency_key=key,
                )
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=create, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    assert len(results) == 2
    assert results[0] == results[1]
    with kb.connect() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE idempotency_key = ?", (key,)
            ).fetchone()[0]
            == 1
        )
        assert any(
            row["kind"] == "duplicate_suppressed"
            for row in _event_rows(conn, results[0])
        )


def test_integrity_error_recovers_canonical_lifetime_task(kanban_home, monkeypatch):
    key = _meta_packet_key("mission-recovery", "packet-recovery")
    with kb.connect() as conn:
        original = kb.create_task(conn, title="canonical", idempotency_key=key)

        real_find = getattr(kb, "_find_idempotent_task_id")
        calls = 0

        def miss_once(connection, lookup_key, *, lifetime):
            nonlocal calls
            calls += 1
            if calls == 1:
                return None
            return real_find(connection, lookup_key, lifetime=lifetime)

        monkeypatch.setattr(kb, "_find_idempotent_task_id", miss_once)
        recovered = kb.create_task(
            conn,
            title="forced unique-index conflict",
            idempotency_key=key,
        )

        assert recovered == original
        assert any(
            row["kind"] == "duplicate_suppressed" for row in _event_rows(conn, original)
        )


def test_unique_partial_index_blocks_direct_lifetime_duplicate(kanban_home):
    key = _meta_packet_key("mission-index", "packet-index")
    with kb.connect() as conn:
        first = kb.create_task(conn, title="first", idempotency_key=key)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'archived' WHERE id = ?", (first,))
        other = kb.create_task(
            conn, title="ordinary task", idempotency_key="legacy-key"
        )

        with pytest.raises(sqlite3.IntegrityError):
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET idempotency_key = ? WHERE id = ?",
                    (key, other),
                )


def test_migration_fails_closed_on_preexisting_lifetime_duplicates(tmp_path):
    db_path = tmp_path / "legacy-duplicate.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(kb.SCHEMA_SQL)
        key = _meta_packet_key("legacy-mission", "legacy-packet")
        conn.executemany(
            "INSERT INTO tasks (id, title, status, created_at, idempotency_key) "
            "VALUES (?, ?, 'archived', ?, ?)",
            [("t_first", "first", 1, key), ("t_second", "second", 2, key)],
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(RuntimeError, match="duplicate lifetime idempotency"):
        kb.connect(db_path=db_path)


def test_real_cli_returns_canonical_id_and_suppression_event(kanban_home):
    key = _meta_packet_key("mission-cli", "packet-cli")
    first = json.loads(run_slash(f"create CLI-first --idempotency-key {key} --json"))
    task_id = first["id"]
    run_slash(f"archive {task_id}")

    duplicate = json.loads(
        run_slash(f"create CLI-renamed --idempotency-key {key} --json")
    )
    assert duplicate["id"] == task_id

    shown = json.loads(run_slash(f"show {task_id} --json"))
    assert any(event["kind"] == "duplicate_suppressed" for event in shown["events"])


def test_legacy_key_still_allows_reuse_after_archive(kanban_home):
    with kb.connect() as conn:
        first = kb.create_task(conn, title="legacy first", idempotency_key="legacy")
        kb.archive_task(conn, first)
        second = kb.create_task(conn, title="legacy second", idempotency_key="legacy")
        assert second != first
