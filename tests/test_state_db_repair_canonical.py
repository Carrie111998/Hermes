"""Canonical state.db repair must preserve the source until promotion is proven."""

from __future__ import annotations

import hashlib
import sqlite3
import struct
from pathlib import Path

import pytest

import hermes_state
from hermes_state import repair_state_db_schema

PAGE_SIZE = 4096


def _write_database(path: Path, *, journal_mode: str = "delete") -> None:
    conn = sqlite3.connect(str(path), isolation_level=None)
    try:
        conn.execute(f"PRAGMA page_size={PAGE_SIZE}")
        conn.execute("CREATE TABLE sessions (id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, body TEXT)")
        conn.executemany(
            "INSERT INTO sessions (name) VALUES (?)",
            [(f"session-{index}",) for index in range(3)],
        )
        conn.executemany(
            "INSERT INTO messages (body) VALUES (?)",
            [(f"message-{index}" * 40,) for index in range(20)],
        )
        actual_mode = conn.execute(
            f"PRAGMA journal_mode={journal_mode}"
        ).fetchone()[0]
        if journal_mode == "wal" and actual_mode != "wal":
            pytest.skip("SQLite/filesystem does not support WAL")
    finally:
        conn.close()


def _corrupt_schema_btree(path: Path) -> None:
    data = bytearray(path.read_bytes())
    page_count = struct.unpack(">I", data[28:32])[0]
    assert page_count >= 3
    data[100] = 0x05
    struct.pack_into(">H", data, 103, 1)
    struct.pack_into(">I", data, 108, page_count)
    struct.pack_into(">H", data, 112, PAGE_SIZE - 6)
    struct.pack_into(">I", data, PAGE_SIZE - 6, page_count)
    path.write_bytes(bytes(data))


def test_failed_repair_preserves_canonical_bytes(tmp_path):
    db = tmp_path / "state.db"
    _write_database(db)
    _corrupt_schema_btree(db)
    before = hashlib.sha256(db.read_bytes()).digest()

    report = repair_state_db_schema(db, backup=False)

    assert report["repaired"] is False
    assert report["error"]
    assert hashlib.sha256(db.read_bytes()).digest() == before


def test_successful_repair_promotes_only_the_verified_candidate(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    _write_database(db)
    monkeypatch.setattr(
        hermes_state, "_db_opens_cleanly", lambda _path: "forced-unhealthy"
    )

    def fake_strategies(candidate, report):
        with sqlite3.connect(str(candidate)) as conn:
            conn.execute("INSERT INTO sessions (name) VALUES ('verified-candidate')")
        report["repaired"] = True
        report["strategy"] = "test-verified-candidate"
        return report

    monkeypatch.setattr(hermes_state, "_run_repair_strategies", fake_strategies)

    report = repair_state_db_schema(db, backup=False)

    assert report["repaired"] is True
    assert report["strategy"] == "test-verified-candidate"
    with sqlite3.connect(str(db)) as conn:
        names = {row[0] for row in conn.execute("SELECT name FROM sessions")}
    assert "verified-candidate" in names
    assert not list(tmp_path.glob("*.repair-candidate*")), report


def test_environmental_health_probe_does_not_burn_repair_ledger(
    tmp_path, monkeypatch
):
    db = tmp_path / "state.db"
    _write_database(db)
    monkeypatch.setattr(
        hermes_state, "_db_opens_cleanly", lambda _path: "database or disk is full"
    )

    report = repair_state_db_schema(db, backup=False)

    assert report["repaired"] is False
    assert report["error"] == "database or disk is full"
    assert not hermes_state._repair_ledger_path(db).exists()


@pytest.mark.requires_wal
def test_repair_snapshot_includes_committed_wal_frames(tmp_path):
    db = tmp_path / "state.db"
    _write_database(db, journal_mode="wal")
    conn = sqlite3.connect(str(db), isolation_level=None)
    try:
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.execute("INSERT INTO messages (body) VALUES ('wal-only-commit')")
        assert db.with_name(db.name + "-wal").exists()

        candidate = tmp_path / "state.db.repair-candidate"
        hermes_state._copy_repair_snapshot(db, candidate)

        with sqlite3.connect(str(candidate)) as check:
            bodies = {row[0] for row in check.execute("SELECT body FROM messages")}
        assert "wal-only-commit" in bodies
    finally:
        conn.close()


@pytest.mark.requires_wal
def test_failed_wal_promotion_keeps_the_existing_destination(tmp_path, monkeypatch):
    source = tmp_path / "source.db"
    destination = tmp_path / "state.db"
    candidate = tmp_path / "state.db.repair-candidate"
    _write_database(source)
    _write_database(destination, journal_mode="wal")
    hermes_state._copy_repair_snapshot(source, candidate)
    live = sqlite3.connect(str(destination), isolation_level=None)
    try:
        live.execute("PRAGMA wal_autocheckpoint=0")
        live.execute("INSERT INTO sessions (name) VALUES ('existing-generation')")

        real_copy = hermes_state._copy_repair_snapshot
        calls = 0

        def interrupt_promotion(source_path, destination_path, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                connection = kwargs["destination_connection"]
                connection.execute(
                    "DELETE FROM sessions WHERE name = 'existing-generation'"
                )
                connection.commit()
                raise sqlite3.OperationalError("simulated promotion interruption")
            return real_copy(source_path, destination_path, **kwargs)

        monkeypatch.setattr(
            hermes_state, "_copy_repair_snapshot", interrupt_promotion
        )

        with pytest.raises(sqlite3.OperationalError, match="simulated promotion"):
            hermes_state._promote_repair_candidate(
                candidate,
                destination,
                live,
            )
    finally:
        live.close()

    with sqlite3.connect(str(destination)) as check:
        names = {row[0] for row in check.execute("SELECT name FROM sessions")}
    assert "existing-generation" in names


def test_failed_delete_promotion_restores_the_original_bytes(tmp_path, monkeypatch):
    source = tmp_path / "source.db"
    destination = tmp_path / "state.db"
    candidate = tmp_path / "state.db.repair-candidate"
    _write_database(source)
    _write_database(destination)
    with sqlite3.connect(str(destination), isolation_level=None) as conn:
        conn.execute("INSERT INTO sessions (name) VALUES ('existing-generation')")
    before = hashlib.sha256(destination.read_bytes()).digest()
    hermes_state._copy_repair_snapshot(source, candidate)

    real_copy = hermes_state._copy_repair_snapshot
    calls = 0

    def interrupt_promotion(source_path, destination_path, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            connection = kwargs["destination_connection"]
            connection.execute(
                "DELETE FROM sessions WHERE name = 'existing-generation'"
            )
            connection.commit()
            raise sqlite3.OperationalError("simulated promotion interruption")
        return real_copy(source_path, destination_path, **kwargs)

    monkeypatch.setattr(hermes_state, "_copy_repair_snapshot", interrupt_promotion)

    with hermes_state._exclusive_repair_db_guard(destination) as (guard, error):
        assert guard is not None, error
        with pytest.raises(sqlite3.OperationalError, match="simulated promotion"):
            hermes_state._promote_repair_candidate(candidate, destination, guard)

    assert hashlib.sha256(destination.read_bytes()).digest() == before
