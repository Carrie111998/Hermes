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
from plugins.memory.ebbinghaus.policies import (
    EbbinghausPolicies,
    ExperiencePolicy,
    PolicyConfigError,
)
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


def test_recall_miss_is_recorded_without_query_excerpt_by_default(tmp_path):
    policies = EbbinghausPolicies(
        experience=ExperiencePolicy(enabled=True, record_query_excerpt=False)
    )
    store = EbbinghausMemoryStore(tmp_path / "memory.db", policies=policies)

    outcome = store.recall_with_experience("unknown private query", reinforce=False)

    assert outcome.outcome is RetrievalOutcome.MISS
    row = store._conn.execute(
        "SELECT query_hash, query_excerpt, outcome FROM retrieval_attempts"
    ).fetchone()
    assert len(row["query_hash"]) == 64
    assert row["query_excerpt"] == ""
    assert row["outcome"] == "miss"
    store.close()


def test_legacy_recall_still_returns_a_list(tmp_path):
    store = EbbinghausMemoryStore(tmp_path / "memory.db")
    remembered = store.remember("The current board is ASRock A320.")
    results = store.recall("ASRock A320", reinforce=False)
    assert isinstance(results, list)
    assert results[0]["memory_id"] == remembered["memory_id"]
    store.close()


def test_normal_recall_excludes_historical_belief_statuses(tmp_path):
    store = EbbinghausMemoryStore(tmp_path / "memory.db")
    memory = store.remember("Old claim.")
    store._conn.execute(
        "UPDATE memories SET belief_status = 'superseded' WHERE memory_id = ?",
        (memory["memory_id"],),
    )
    store._conn.commit()
    assert store.recall("Old claim", reinforce=False) == []
    historical = store.recall_with_experience(
        "Old claim",
        reinforce=False,
        include_history=True,
        allow_rescue=False,
        track=False,
    )
    assert historical.results[0]["historical"] is True
    store.close()


def test_functional_forgetting_marks_memories_latent_before_archive(tmp_path):
    clock = {"now": 1_700_000_000.0}

    def now_fn() -> float:
        return clock["now"]

    policies = EbbinghausPolicies.from_config(
        {
            "experience": {
                "enabled": True,
                "functional_forgetting": True,
                "latent_retention_threshold": 0.50,
                "archive_retention_threshold": 0.05,
                "latent_archive_after_days": 30,
            },
            "sleep": {
                "forget_threshold": 0.50,
                "rehearse_threshold": 0.90,
                "salience_keep_threshold": 0.95,
                "prune_mode": "archive",
                "limit": 50,
            },
        }
    )
    store = EbbinghausMemoryStore(
        tmp_path / "memory.db", policies=policies, time_fn=now_fn
    )
    remembered = store.remember(
        "Temporary scratch note about ASRock motherboard firmware.",
        salience=0.2,
    )
    mid = remembered["memory_id"]
    # Retention should sit between archive_threshold and latent_threshold.
    store._conn.execute(
        "UPDATE memories SET strength = 0.35, last_anchor_at = ? WHERE memory_id = ?",
        (clock["now"] - 86400.0 * 10.0, mid),
    )
    store._conn.commit()

    report = store.sleep_cycle(prune_mode="archive")
    row = store.get(mid)

    assert mid in report["forgotten"]
    assert mid in report["latent"]
    assert mid not in report["archived"]
    assert row["state"] == "active"
    assert row["access_state"] == "latent"
    event = store._conn.execute(
        "SELECT event_type FROM memory_events WHERE memory_id = ? "
        "AND event_type = 'memory_became_latent'",
        (mid,),
    ).fetchone()
    assert event is not None
    store.close()


def test_protected_memory_is_not_auto_latentized(tmp_path):
    policies = EbbinghausPolicies.from_config(
        {
            "experience": {
                "enabled": True,
                "functional_forgetting": True,
                "latent_retention_threshold": 0.99,
            },
            "sleep": {
                "forget_threshold": 0.99,
                "rehearse_threshold": 1.0,
                "salience_keep_threshold": 0.95,
                "limit": 20,
            },
            "capacity": {"protected_tags": ["protected"]},
        }
    )
    store = EbbinghausMemoryStore(tmp_path / "memory.db", policies=policies)
    remembered = store.remember(
        "Never auto-forget this user preference.",
        tags=["protected"],
        salience=0.2,
    )
    mid = remembered["memory_id"]
    store._conn.execute(
        "UPDATE memories SET strength = 0.01, last_anchor_at = ? WHERE memory_id = ?",
        (store._now() - 86400.0 * 60.0, mid),
    )
    store._conn.commit()

    report = store.sleep_cycle()
    row = store.get(mid)
    assert mid not in report.get("latent", [])
    assert row["access_state"] == "accessible"
    assert row["state"] == "active"
    store.close()


def test_rescue_reactivates_latent_memory_after_prior_miss(tmp_path):
    policies = EbbinghausPolicies.from_config(
        {
            "experience": {
                "enabled": True,
                "rescue_enabled": True,
                "rescue_min_score": 0.12,
                "record_query_excerpt": False,
            }
        }
    )
    store = EbbinghausMemoryStore(tmp_path / "memory.db", policies=policies)
    remembered = store.remember(
        "The current motherboard is ASRock A320M-HDV.",
        tags=["hardware", "board"],
    )
    mid = remembered["memory_id"]
    store._conn.execute(
        """
        UPDATE memories
        SET access_state = 'latent', latent_at = ?
        WHERE memory_id = ?
        """,
        (store._now(), mid),
    )
    store._conn.commit()

    miss = store.recall_with_experience(
        "ASRock motherboard model",
        reinforce=False,
        allow_rescue=False,
    )
    assert miss.outcome is RetrievalOutcome.MISS

    rescued = store.recall_with_experience(
        "ASRock motherboard model",
        reinforce=True,
        allow_rescue=True,
    )
    assert rescued.outcome is RetrievalOutcome.RESCUED
    assert rescued.rescued_memory_id == mid
    assert rescued.results[0]["memory_id"] == mid
    assert "previously inaccessible" in rescued.state_note.lower()
    row = store.get(mid)
    assert row["access_state"] == "reactivated"
    assert row["state"] == "active"
    assert int(row.get("reactivation_count") or 0) >= 1
    store.close()


def test_rescue_never_revives_superseded_or_retracted(tmp_path):
    policies = EbbinghausPolicies.from_config(
        {"experience": {"enabled": True, "rescue_enabled": True, "rescue_min_score": 0.01}}
    )
    store = EbbinghausMemoryStore(tmp_path / "memory.db", policies=policies)
    memory = store.remember("Superseded board claim ASRock B450.")
    mid = memory["memory_id"]
    store._conn.execute(
        """
        UPDATE memories
        SET access_state = 'latent', belief_status = 'superseded', latent_at = ?
        WHERE memory_id = ?
        """,
        (store._now(), mid),
    )
    store._conn.commit()

    outcome = store.recall_with_experience(
        "ASRock B450 board",
        reinforce=False,
        allow_rescue=True,
    )
    assert outcome.outcome is RetrievalOutcome.MISS
    assert store.get(mid)["access_state"] == "latent"
    store.close()


def test_experience_disabled_sleep_keeps_legacy_archive_path(tmp_path):
    policies = EbbinghausPolicies.from_config(
        {
            "experience": {"enabled": False, "functional_forgetting": True},
            "sleep": {
                "forget_threshold": 0.99,
                "rehearse_threshold": 1.0,
                "salience_keep_threshold": 0.95,
                "prune_mode": "archive",
                "limit": 20,
            },
        }
    )
    store = EbbinghausMemoryStore(tmp_path / "memory.db", policies=policies)
    remembered = store.remember("Legacy forget path scratch note.", salience=0.2)
    mid = remembered["memory_id"]
    store._conn.execute(
        "UPDATE memories SET strength = 0.01, last_anchor_at = ? WHERE memory_id = ?",
        (store._now() - 86400.0 * 40.0, mid),
    )
    store._conn.commit()
    report = store.sleep_cycle(prune_mode="archive")
    assert mid in report["forgotten"]
    assert mid in report["archived"]
    assert report.get("latent", []) == []
    assert store.get(mid)["state"] == "archived"
    store.close()


def test_revise_memory_supersedes_old_belief_and_queues_rehearsal(tmp_path):
    store = EbbinghausMemoryStore(tmp_path / "memory.db")
    old = store.remember("The current board is ASRock A320.")
    revised = store.revise_memory(
        old["memory_id"],
        "The current board is ASRock B450.",
        reason="user correction",
        evidence=[{"kind": "user", "note": "said B450"}],
        test_query="current board ASRock",
    )
    assert revised["status"] == "revised"
    assert revised["old_memory_id"] == old["memory_id"]
    assert revised["new_memory_id"] != old["memory_id"]
    assert revised["queued_rehearsal_id"]
    old_row = store.get(old["memory_id"])
    assert old_row["belief_status"] == "superseded"
    assert old_row["state"] == "archived"
    history = store.belief_history(memory_id=revised["new_memory_id"])
    assert [h["belief_version"] for h in history] == [1, 2]
    assert store.recall("ASRock B450", reinforce=False)[0]["memory_id"] == revised[
        "new_memory_id"
    ]
    a320 = store.recall("ASRock A320", reinforce=False)
    assert all(item["memory_id"] != old["memory_id"] for item in a320)
    store.close()


def test_remember_does_not_revive_superseded_content(tmp_path):
    store = EbbinghausMemoryStore(tmp_path / "memory.db")
    old = store.remember("Legacy board ASRock A320.")
    revised = store.revise_memory(
        old["memory_id"],
        "Legacy board ASRock B450.",
        reason="corrected",
    )
    dup = store.remember("Legacy board ASRock A320.")
    assert dup["status"] == "historical_duplicate"
    assert dup["historical_memory_id"] == old["memory_id"]
    assert dup["current_memory_id"] == revised["new_memory_id"]
    store.close()


def test_retract_memory_archives_without_delete(tmp_path):
    store = EbbinghausMemoryStore(tmp_path / "memory.db")
    memory = store.remember("Retract this belief about ASRock.")
    result = store.retract_memory(memory["memory_id"], reason="no longer true")
    assert result["status"] == "retracted"
    row = store.get(memory["memory_id"])
    assert row["belief_status"] == "retracted"
    assert row["state"] == "archived"
    assert row["access_state"] == "latent"
    assert store.recall("ASRock", reinforce=False) == []
    store.close()
