"""Repair-storm containment for a malformed state.db (OOF-106).

Production incident this guards against: an unrepairable, page-corrupt
state.db was re-attempted by every short-lived process that touched it
(cron ticker, dispatcher, CLI, dashboard workers). Each attempt claimed a
"fresh" per-process one-shot, copied another identical full-size backup
beside the database, failed, and exited — accumulating 505 malformed
backups (241MB, only two distinct content generations) while
``/api/status`` kept reporting storage "ok" for 10+ days.

Four properties are pinned here:

1. The one-shot repair claim is persistent across processes (marker file
   keyed by a stat fingerprint of the attempted bytes).
2. Malformed backups are content-deduplicated and the family is capped.
3. A failed repair points operators at the offline recovery pipeline and
   never re-arms itself while the file is unchanged.
4. The gateway readiness probe reports the unrepaired-corruption state as
   degraded instead of a false green.
"""
import json
import os
import sqlite3
import uuid
from pathlib import Path

import pytest

import hermes_state
from hermes_state import SessionDB, repair_state_db_schema


def _build_healthy_db(db_path: Path) -> str:
    db = SessionDB(db_path=db_path)
    sid = db.create_session(session_id=str(uuid.uuid4()), source="cli")
    for i in range(5):
        db.append_message(sid, role="user", content=f"hello world {i}")
        db.append_message(sid, role="assistant", content=f"reply {i}")
    db.close()
    return sid


def _corrupt_duplicate_fts(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA writable_schema=ON")
    conn.execute(
        "INSERT INTO sqlite_master (type, name, tbl_name, rootpage, sql) "
        "SELECT type, name, tbl_name, rootpage, sql FROM sqlite_master "
        "WHERE name='messages_fts'"
    )
    conn.commit()
    conn.close()


def _unrepairable_bytes() -> bytes:
    return b"SQLite format 3\x00" + b"\x00\xde\xad\xbe\xef" * 200


def _fresh_process(monkeypatch) -> None:
    """Simulate a brand-new process: empty in-memory repair-claim set."""
    monkeypatch.setattr(hermes_state, "_repair_attempted_paths", set())


def _bump_mtime(path: Path) -> None:
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))


# ── 1) persistent cross-process repair claim ─────────────────────────────


def test_repair_claim_survives_process_restart(tmp_path, monkeypatch):
    """A second process must NOT re-attempt repair on unchanged bytes."""
    db_path = tmp_path / "state.db"
    db_path.write_bytes(_unrepairable_bytes())

    _fresh_process(monkeypatch)
    assert hermes_state._claim_repair_attempt(db_path) is True
    marker = db_path.with_name("state.db.repair-attempted.json")
    assert marker.exists()

    # "Restart": in-memory set is empty, only the marker persists.
    _fresh_process(monkeypatch)
    assert hermes_state._claim_repair_attempt(db_path) is False


def test_repair_claim_rearms_when_file_contents_change(tmp_path, monkeypatch):
    """A new corruption event (different bytes) gets its own attempt."""
    db_path = tmp_path / "state.db"
    db_path.write_bytes(_unrepairable_bytes())

    _fresh_process(monkeypatch)
    assert hermes_state._claim_repair_attempt(db_path) is True

    db_path.write_bytes(_unrepairable_bytes() + b"different")
    _bump_mtime(db_path)
    _fresh_process(monkeypatch)
    assert hermes_state._claim_repair_attempt(db_path) is True


def test_repair_claim_fails_open_without_marker_io(tmp_path, monkeypatch):
    """Marker bookkeeping failures must never block the repair attempt."""
    db_path = tmp_path / "state.db"
    db_path.write_bytes(_unrepairable_bytes())

    _fresh_process(monkeypatch)
    monkeypatch.setattr(
        hermes_state, "_db_stat_fingerprint", lambda _p: None
    )
    assert hermes_state._claim_repair_attempt(db_path) is True
    # Degrades to per-process semantics.
    assert hermes_state._claim_repair_attempt(db_path) is False


def test_corrupt_marker_file_is_ignored(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    db_path.write_bytes(_unrepairable_bytes())
    db_path.with_name("state.db.repair-attempted.json").write_text("{not json")

    _fresh_process(monkeypatch)
    assert hermes_state._claim_repair_attempt(db_path) is True


def test_successful_repair_clears_marker(tmp_path, monkeypatch):
    """After a real repair the marker is gone: future corruption re-arms."""
    db_path = tmp_path / "state.db"
    _build_healthy_db(db_path)
    _corrupt_duplicate_fts(db_path)

    _fresh_process(monkeypatch)
    assert hermes_state._claim_repair_attempt(db_path) is True
    report = repair_state_db_schema(db_path)
    assert report["repaired"] is True
    assert not db_path.with_name("state.db.repair-attempted.json").exists()


def test_failed_repair_restamps_marker_for_mutated_bytes(tmp_path, monkeypatch):
    """Failed surgery may mutate the file; the marker must track the new
    bytes so the next process doesn't treat them as a fresh corruption."""
    db_path = tmp_path / "state.db"
    db_path.write_bytes(_unrepairable_bytes())

    _fresh_process(monkeypatch)
    assert hermes_state._claim_repair_attempt(db_path) is True
    report = repair_state_db_schema(db_path)
    assert report["repaired"] is False

    marker = json.loads(
        db_path.with_name("state.db.repair-attempted.json").read_text()
    )
    st = db_path.stat()
    assert marker["fingerprint"] == {
        "size": st.st_size,
        "mtime_ns": st.st_mtime_ns,
    }
    _fresh_process(monkeypatch)
    assert hermes_state._claim_repair_attempt(db_path) is False


def test_open_after_failed_repair_does_not_retry_in_new_process(
    tmp_path, monkeypatch
):
    """End-to-end: SessionDB open in a 'new process' must raise without
    invoking repair again while the file is unchanged."""
    db_path = tmp_path / "state.db"
    _build_healthy_db(db_path)
    _corrupt_duplicate_fts(db_path)

    calls = {"n": 0}

    def failing_repair(path, **kw):
        calls["n"] += 1
        return {"repaired": False, "strategy": None, "backup_path": None, "error": "x"}

    monkeypatch.setattr(hermes_state, "repair_state_db_schema", failing_repair)

    _fresh_process(monkeypatch)
    with pytest.raises(sqlite3.DatabaseError):
        SessionDB(db_path=db_path)
    assert calls["n"] == 1

    _fresh_process(monkeypatch)  # simulate the next cron/CLI process
    with pytest.raises(sqlite3.DatabaseError):
        SessionDB(db_path=db_path)
    assert calls["n"] == 1  # marker suppressed the second attempt


# ── 2) backup dedup + retention cap ──────────────────────────────────────


def test_backup_dedup_same_bytes_single_copy(tmp_path):
    db_path = tmp_path / "state.db"
    db_path.write_bytes(_unrepairable_bytes())

    first, first_err = hermes_state._backup_db_file(db_path)
    assert first is not None and first.exists() and first_err is None
    second, second_err = hermes_state._backup_db_file(db_path)
    assert second == first  # existing backup returned, no new copy
    assert second_err is None

    backups = hermes_state._malformed_backups(db_path)
    assert len(backups) == 1


def test_backup_new_generation_gets_new_copy(tmp_path):
    db_path = tmp_path / "state.db"
    db_path.write_bytes(_unrepairable_bytes())
    first, _ = hermes_state._backup_db_file(db_path)

    db_path.write_bytes(_unrepairable_bytes() + b"gen2")
    second, _ = hermes_state._backup_db_file(db_path)

    assert first != second
    assert len(hermes_state._malformed_backups(db_path)) == 2


def test_backup_retention_cap_prunes_oldest(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    monkeypatch.setattr(hermes_state, "_MALFORMED_BACKUP_KEEP", 3)

    names = []
    for gen in range(6):
        db_path.write_bytes(_unrepairable_bytes() + bytes([gen]))
        backup, backup_err = hermes_state._backup_db_file(db_path)
        assert backup is not None and backup_err is None
        names.append(backup.name)

    remaining = [p.name for p in hermes_state._malformed_backups(db_path)]
    assert len(remaining) == 3
    assert remaining == names[-3:]  # oldest created pruned first


def test_prune_removes_sidecars(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    monkeypatch.setattr(hermes_state, "_MALFORMED_BACKUP_KEEP", 1)

    db_path.write_bytes(_unrepairable_bytes() + b"a")
    old, _ = hermes_state._backup_db_file(db_path)
    old.with_name(old.name + "-wal").write_bytes(b"wal")
    old.with_name(old.name + "-shm").write_bytes(b"shm")

    db_path.write_bytes(_unrepairable_bytes() + b"b")
    hermes_state._backup_db_file(db_path)

    assert not old.exists()
    assert not old.with_name(old.name + "-wal").exists()
    assert not old.with_name(old.name + "-shm").exists()


def test_legacy_backup_names_participate_in_retention(tmp_path, monkeypatch):
    """Backups from before the digest suffix still count toward the cap."""
    db_path = tmp_path / "state.db"
    monkeypatch.setattr(hermes_state, "_MALFORMED_BACKUP_KEEP", 2)
    legacy = db_path.with_name("state.db.malformed-backup-20260701_000000")
    legacy.write_bytes(b"legacy")

    db_path.write_bytes(_unrepairable_bytes() + b"x")
    hermes_state._backup_db_file(db_path)
    db_path.write_bytes(_unrepairable_bytes() + b"y")
    hermes_state._backup_db_file(db_path)

    assert not legacy.exists()  # oldest, pruned
    assert len(hermes_state._malformed_backups(db_path)) == 2


# ── 4) readiness probe: no false green on unrepaired corruption ─────────


def test_readiness_degrades_on_unrepaired_corruption_marker(tmp_path, monkeypatch):
    from gateway.readiness import _probe_state_db

    home = tmp_path / ".hermes"
    home.mkdir()
    db_path = home / "state.db"
    db_path.write_bytes(_unrepairable_bytes())

    _fresh_process(monkeypatch)
    hermes_state._claim_repair_attempt(db_path)  # writes the marker

    result = _probe_state_db(home)
    assert result["status"] == "degraded"
    assert result["detail"] == "unrepaired corruption"


def test_readiness_ok_after_marker_cleared_by_repair(tmp_path, monkeypatch):
    from gateway.readiness import _probe_state_db

    home = tmp_path / ".hermes"
    home.mkdir()
    db_path = home / "state.db"
    _build_healthy_db(db_path)
    _corrupt_duplicate_fts(db_path)

    _fresh_process(monkeypatch)
    hermes_state._claim_repair_attempt(db_path)
    assert repair_state_db_schema(db_path)["repaired"] is True

    assert _probe_state_db(home)["status"] == "ok"


def test_readiness_stale_marker_does_not_flag_recovered_file(tmp_path, monkeypatch):
    """A marker left behind for OLD bytes (file since replaced/restored)
    must not mark the healthy database degraded."""
    from gateway.readiness import _probe_state_db

    home = tmp_path / ".hermes"
    home.mkdir()
    db_path = home / "state.db"
    db_path.write_bytes(_unrepairable_bytes())

    _fresh_process(monkeypatch)
    hermes_state._claim_repair_attempt(db_path)

    db_path.unlink()
    _build_healthy_db(db_path)  # snapshot restore / fresh init

    assert _probe_state_db(home)["status"] == "ok"


def test_readiness_probe_walks_sessions_btree(tmp_path):
    """The probe must descend into the sessions table so page-level damage
    beyond page 1 cannot report a false 'ok' (the OOF-106 gap)."""
    from gateway.readiness import _probe_state_db

    home = tmp_path / ".hermes"
    home.mkdir()
    db_path = home / "state.db"
    _build_healthy_db(db_path)
    assert _probe_state_db(home)["status"] == "ok"

    # Zero out the sessions table's root page: sqlite_master (page 1) still
    # reads fine, but any descent into sessions raises.
    conn = sqlite3.connect(str(db_path))
    rootpage = conn.execute(
        "SELECT rootpage FROM sqlite_master WHERE type='table' AND name='sessions'"
    ).fetchone()[0]
    page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    conn.close()
    with open(db_path, "r+b") as fh:
        fh.seek((rootpage - 1) * page_size)
        fh.write(b"\xff" * page_size)

    assert _probe_state_db(home)["status"] == "degraded"


def test_readiness_ok_on_pre_schema_db(tmp_path):
    """A database without a sessions table yet (first boot) is not corrupt."""
    from gateway.readiness import _probe_state_db

    home = tmp_path / ".hermes"
    home.mkdir()
    with sqlite3.connect(home / "state.db") as conn:
        conn.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY)")

    assert _probe_state_db(home)["status"] == "ok"
