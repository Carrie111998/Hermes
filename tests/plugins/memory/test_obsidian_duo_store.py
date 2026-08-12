from plugins.memory.obsidian_duo.contracts import (
    Authority,
    EvidenceRecord,
    MemoryCandidate,
    MemoryRecord,
    MemoryStatus,
    Verification,
)
from plugins.memory.obsidian_duo.store import SqliteMemoryStore, new_id


def test_store_enables_wal_foreign_keys_and_fts(tmp_path):
    store = SqliteMemoryStore(tmp_path / "memory.db")
    store.initialize()

    with store.connection() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        conn.execute(
            "INSERT INTO memory_fts(memory_id,title,body,tags,entities) VALUES(?,?,?,?,?)",
            ("mem_1", "HUD drag", "pointer dragging", "hermes", "HUD"),
        )
        rows = conn.execute(
            "SELECT memory_id FROM memory_fts WHERE memory_fts MATCH ?",
            ('"pointer"',),
        ).fetchall()

    assert rows[0][0] == "mem_1"


def test_store_versions_memories_and_rebuilds_fts(tmp_path):
    store = SqliteMemoryStore(tmp_path / "memory.db")
    store.initialize()
    record = MemoryRecord(
        memory_id="mem_1",
        content="The HUD supports pointer dragging.",
        memory_type="fact",
        scope="project:hermes",
        authority=Authority.USER,
        verification=Verification.USER_CONFIRMED,
        status=MemoryStatus.ACTIVE,
    )

    store.upsert_memory(record, "user confirmed")
    assert store.get_memory("mem_1") == record
    store.upsert_memory(record.__class__(**{**record.__dict__, "content": "The HUD supports drag."}), "correction")
    assert store.connection().execute(
        "SELECT COUNT(*) FROM memory_versions WHERE memory_id = ?", ("mem_1",)
    ).fetchone()[0] == 1

    with store.connection() as conn:
        conn.execute("DELETE FROM memory_fts")
    store.rebuild_fts()

    assert store.search_fts("drag", 5)[0].memory_id == "mem_1"


def test_store_records_evidence_candidates_and_journal(tmp_path):
    store = SqliteMemoryStore(tmp_path / "memory.db")
    store.initialize()
    evidence = EvidenceRecord("ev_1", "session", "Observed in session", session_id="s1")
    candidate = MemoryCandidate("A candidate", evidence=(evidence,))
    memory = MemoryRecord("mem_1", "Observed", "fact", "global")

    store.upsert_memory(memory, "initial")
    store.insert_evidence(evidence)
    store.link_evidence("mem_1", "ev_1")
    candidate_id = store.stage_candidate(candidate)
    store.record_journal("tx_1", "promote", "prepared", {"candidate_id": candidate_id})

    with store.connection() as conn:
        assert conn.execute("SELECT kind FROM evidence WHERE evidence_id='ev_1'").fetchone()[0] == "session"
        assert conn.execute("SELECT state FROM journal WHERE txn_id='tx_1'").fetchone()[0] == "prepared"
