"""Tests for Ebbinghaus AGIASI experiential-memory extensions.

All store I/O uses tmp_path / temporary HERMES_HOME. Never open the live
operator database under ~/.hermes.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from plugins.memory.ebbinghaus.models import (
    AccessState,
    BeliefStatus,
    InsightStatus,
    RetrievalOutcome,
)
from plugins.memory.ebbinghaus.policies import EbbinghausPolicies, PolicyConfigError
from plugins.memory.ebbinghaus.store import EbbinghausMemoryStore


def create_legacy_ebbinghaus_database(db_path: Path, *, rows: int = 1) -> None:
    """Create a pre-experience Ebbinghaus DB without opening the current store."""
    db_path = Path(db_path)
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(
            """
            CREATE TABLE memories (
                memory_id          INTEGER PRIMARY KEY AUTOINCREMENT,
                content            TEXT NOT NULL UNIQUE,
                encoded            TEXT NOT NULL,
                cues               TEXT DEFAULT '',
                tags               TEXT DEFAULT '',
                salience           REAL DEFAULT 0.6,
                valence            REAL DEFAULT 0.0,
                strength           REAL DEFAULT 1.0,
                rehearsal_count    INTEGER DEFAULT 0,
                retrieval_count    INTEGER DEFAULT 0,
                source             TEXT DEFAULT '',
                session_id         TEXT DEFAULT '',
                created_at         REAL NOT NULL,
                updated_at         REAL NOT NULL,
                last_rehearsed_at  REAL,
                last_retrieved_at  REAL,
                state              TEXT NOT NULL DEFAULT 'active',
                last_anchor_at     REAL,
                sleep_rehearsal_count INTEGER NOT NULL DEFAULT 0,
                last_sleep_at      REAL,
                archived_at        REAL,
                archive_reason     TEXT DEFAULT '',
                memory_type        TEXT NOT NULL DEFAULT 'episodic',
                dream_candidate    INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        now = time.time()
        for index in range(rows):
            conn.execute(
                """
                INSERT INTO memories(
                    content, encoded, cues, tags, salience, valence, strength,
                    created_at, updated_at, last_anchor_at, state
                ) VALUES (?, ?, '', '', 0.6, 0.0, 1.0, ?, ?, ?, 'active')
                """,
                (
                    f"legacy memory {index + 1}",
                    '{"version":1,"kind":"cue_encoding","summary":"legacy","cues":[]}',
                    now - float(index),
                    now - float(index),
                    now - float(index),
                ),
            )
        conn.commit()
    finally:
        conn.close()


def test_experience_policy_defaults_are_backward_compatible():
    policies = EbbinghausPolicies()
    assert policies.experience.enabled is False
    assert policies.experience.functional_forgetting is True
    assert policies.revision.enabled is True
    assert policies.insight.require_validation is True


def test_experience_policy_rejects_invalid_threshold_order():
    with pytest.raises(PolicyConfigError):
        EbbinghausPolicies.from_config(
            {
                "experience": {
                    "latent_retention_threshold": 0.05,
                    "archive_retention_threshold": 0.10,
                }
            }
        )


def test_experience_enums_have_stable_wire_values():
    assert AccessState.LATENT.value == "latent"
    assert BeliefStatus.SUPERSEDED.value == "superseded"
    assert RetrievalOutcome.RESCUED.value == "rescued"
    assert InsightStatus.REJECTED.value == "rejected"


def test_experience_migration_is_additive_and_idempotent(tmp_path):
    db = tmp_path / "legacy.db"
    create_legacy_ebbinghaus_database(db, rows=3)

    store = EbbinghausMemoryStore(db)
    first_count = store.stats()["count"]
    columns = {
        row["name"]
        for row in store._conn.execute("PRAGMA table_info(memories)").fetchall()
    }
    assert "access_state" in columns
    assert "belief_id" in columns
    assert first_count == 3
    migration_count = store._conn.execute(
        "SELECT COUNT(*) FROM ebbinghaus_schema_migrations WHERE version = 1"
    ).fetchone()[0]
    assert migration_count == 1
    store.close()

    reopened = EbbinghausMemoryStore(db)
    assert reopened.stats()["count"] == 3
    migration_count = reopened._conn.execute(
        "SELECT COUNT(*) FROM ebbinghaus_schema_migrations WHERE version = 1"
    ).fetchone()[0]
    assert migration_count == 1
    reopened.close()


def test_experience_migration_creates_one_integrity_checked_backup(tmp_path):
    db = tmp_path / "memory.db"
    create_legacy_ebbinghaus_database(db, rows=2)

    store = EbbinghausMemoryStore(db)
    rows = store._conn.execute(
        "SELECT backup_path FROM ebbinghaus_schema_migrations WHERE version = 1"
    ).fetchall()
    assert len(rows) == 1
    backup = Path(rows[0]["backup_path"])
    assert backup.exists()
    with sqlite3.connect(backup) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 2
    store.close()


def test_experience_migration_backfills_stable_belief_ids(tmp_path):
    db = tmp_path / "legacy.db"
    create_legacy_ebbinghaus_database(db, rows=2)
    store = EbbinghausMemoryStore(db)
    rows = store._conn.execute(
        "SELECT memory_id, belief_id, belief_version, belief_status, access_state "
        "FROM memories ORDER BY memory_id"
    ).fetchall()
    assert rows[0]["belief_id"] == f"memory-{rows[0]['memory_id']}"
    assert rows[0]["belief_version"] == 1
    assert rows[0]["belief_status"] == "current"
    assert rows[0]["access_state"] == "accessible"
    store.close()
