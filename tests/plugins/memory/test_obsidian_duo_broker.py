from pathlib import Path

from plugins.memory.obsidian_duo.broker import EmbeddedMemoryBroker
from plugins.memory.obsidian_duo.config import ObsidianDuoConfig
from plugins.memory.obsidian_duo.contracts import (
    Authority,
    EvidenceRecord,
    MemoryCandidate,
    MemoryEvent,
    MemoryRecord,
    RetrievalRequest,
    Verification,
)
from plugins.memory.obsidian_duo.policy import MemoryPolicy
from plugins.memory.obsidian_duo.retrieval import MemoryRetriever
from plugins.memory.obsidian_duo.store import SqliteMemoryStore
from plugins.memory.obsidian_duo.sync import CommandSyncAdapter
from plugins.memory.obsidian_duo.vault import ObsidianVault


def make_broker(tmp_path, queue_maxsize=256):
    config = ObsidianDuoConfig(str(tmp_path / "Vault"), queue_maxsize=queue_maxsize)
    store = SqliteMemoryStore(tmp_path / "memory.db")
    vault = ObsidianVault(Path(config.vault_path), config.managed_folder)
    return EmbeddedMemoryBroker(
        config=config,
        store=store,
        vault=vault,
        policy=MemoryPolicy(),
        retriever=MemoryRetriever(store),
    )


def test_broker_queue_is_bounded_and_keeps_user_correction(tmp_path):
    broker = make_broker(tmp_path, queue_maxsize=2)

    broker._events.put_nowait(MemoryEvent("turn", session_id="s1"))
    broker._events.put_nowait(MemoryEvent("turn", session_id="s1"))
    broker.observe(MemoryEvent("user_correction", content="correct", session_id="s1"))

    assert broker._events.qsize() <= 2
    assert any(event.event_type == "user_correction" for event in list(broker._events.queue))


def test_ordinary_turn_does_not_mark_external_sync_dirty(tmp_path):
    broker = make_broker(tmp_path)
    calls = []
    broker.sync_adapter = type("Sync", (), {
        "mark_dirty": lambda self, reason: calls.append(reason),
        "flush": lambda self: type("Result", (), {"success": True})(),
    })()

    broker.observe(MemoryEvent("turn", content="ordinary conversation"))
    broker.observe(MemoryEvent("explicit_remember", content="durable decision"))

    assert calls == []
    broker.shutdown(5)


def test_broker_starts_worker_lazily_and_flushes(tmp_path):
    broker = make_broker(tmp_path)
    assert broker._worker is None

    broker.observe(MemoryEvent("turn", content="hello"))

    assert broker.flush("test", 5)
    assert broker._worker is not None
    broker.shutdown(5)


def test_normal_retrieval_does_not_rebuild_external_catalogue_each_query(tmp_path, monkeypatch):
    broker = make_broker(tmp_path)
    notes = broker.vault.vault_path / "Notes"
    notes.mkdir(parents=True)
    for index in range(100):
        (notes / f"note-{index:03d}.md").write_text(f"ordinary note {index}", encoding="utf-8")
    calls = []
    original = broker.vault.catalog_external_markdown_paths

    def counted_catalogue():
        calls.append(1)
        yield from original()

    monkeypatch.setattr(broker.vault, "catalog_external_markdown_paths", counted_catalogue)
    broker.start()
    for _ in range(10):
        broker.retrieve(RetrievalRequest("phrase that is absent", max_memories=2))

    assert len(calls) == 1
    broker.shutdown(5)


def test_session_completion_consolidates_retained_events_without_turn_llm(tmp_path):
    broker = make_broker(tmp_path)
    broker.start()

    broker.observe(MemoryEvent("turn", content="We chose SQLite for durable memory", session_id="s1"))
    broker.observe(MemoryEvent("task_complete", content="Decision: use SQLite", session_id="s1"))

    assert broker.flush("session_end", 5)
    candidates = broker.store.connection().execute("SELECT payload FROM candidates").fetchall()
    assert any("SQLite" in row[0] for row in candidates)
    assert broker.store.connection().execute("SELECT COUNT(*) FROM metrics WHERE name='event.turn'").fetchone()[0] == 1
    broker.shutdown(5)


def test_session_end_consolidates_last_session_when_event_has_no_session_id(tmp_path):
    broker = make_broker(tmp_path)
    broker.start()
    broker.observe(MemoryEvent("turn", content="The confirmed decision is SQLite", session_id="s1"))
    broker.observe(MemoryEvent("session_end", session_id=""))

    assert broker.flush("session_end", 5)
    candidates = broker.store.connection().execute("SELECT payload FROM candidates").fetchall()
    assert any("SQLite" in row[0] for row in candidates)
    broker.shutdown(5)


def test_consolidation_auto_promotes_source_supported_candidate_with_evidence(tmp_path):
    broker = make_broker(tmp_path)
    broker.start()

    class Inference:
        def consolidate(self, events, evidence):
            return type("Result", (), {"parsed": {"candidates": [{
                "content": "The project uses SQLite for durable memory",
                "memory_type": "decision",
                "confidence": 0.95,
                "verification": "source_supported",
                "evidence_ids": [evidence[0].evidence_id],
                "project_id": "project-hermes",
            }]}})()

    broker.inference = Inference()
    broker.consolidate("session_end", [MemoryEvent(
        "decision_confirmed", "The project uses SQLite for durable memory", session_id="s1",
        project_id="project-hermes",
    )])

    record = broker.store.connection().execute(
        "SELECT content, authority, verification FROM memories"
    ).fetchone()
    assert record is not None
    assert record["authority"] == "source"
    assert record["verification"] == "source_supported"
    assert "SQLite" in record["content"]
    assert broker.store.connection().execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 0
    memory_id = broker.store.connection().execute("SELECT memory_id FROM memories").fetchone()[0]
    assert list(broker.vault.managed_root.rglob(f"{memory_id}.md"))
    packet = broker.retrieve(RetrievalRequest("SQLite durable memory", max_memories=2, max_tokens=40))
    assert packet.memories[0].content == record["content"]
    broker.shutdown(5)


def test_consolidation_keeps_speculative_candidate_staged(tmp_path):
    broker = make_broker(tmp_path)
    broker.start()

    class Inference:
        def consolidate(self, events, evidence):
            return type("Result", (), {"parsed": {"candidates": [{
                "content": "The user may prefer blue",
                "memory_type": "preference",
                "confidence": 0.4,
                "verification": "inferred",
                "evidence_ids": [],
            }]}})()

    broker.inference = Inference()
    broker.consolidate("session_end", [MemoryEvent("turn", "Maybe blue", session_id="s1")])

    assert broker.store.connection().execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
    assert broker.store.connection().execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 1
    broker.shutdown(5)


def test_consolidation_without_inference_stages_without_alternate_provider(tmp_path):
    broker = make_broker(tmp_path)
    broker.start()
    broker.inference = None

    broker.consolidate("session_end", [MemoryEvent("turn", "Observed SQLite", session_id="s1")])

    assert broker.store.connection().execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
    assert broker.store.connection().execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 1
    broker.shutdown(5)


def test_deferred_inference_keeps_useful_events_staged(tmp_path):
    broker = make_broker(tmp_path)
    broker.start()

    class Inference:
        def consolidate(self, events, evidence):
            return type("Result", (), {"parsed": None, "deferred": True})()

    broker.inference = Inference()
    broker.consolidate("session_end", [MemoryEvent("decision_confirmed", "Use SQLite", session_id="s1")])

    assert broker.store.connection().execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
    assert broker.store.connection().execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 1
    broker.shutdown(5)


def test_broker_recovers_written_journal_by_rescanning(tmp_path):
    broker = make_broker(tmp_path)
    broker.start()
    broker.store.record_journal("tx_1", "write_note", "written", {"path": "missing.md"})

    result = broker.recover()

    assert result.recovered == 0
    assert broker.store.connection().execute(
        "SELECT state FROM journal WHERE txn_id='tx_1'"
    ).fetchone()[0] == "recovery_failed"


def test_explicit_promotion_persists_note_index_and_memory(tmp_path):
    broker = make_broker(tmp_path)
    broker.start()
    decision = broker.propose(MemoryCandidate(
        "Use SQLite for durable memory", metadata={"event_kind": "explicit_remember"}
    ), host_confirmed=True)
    assert decision.action == "promote"
    assert broker.store.get_memory(decision.memory_id).content == "Use SQLite for durable memory"
    assert list(broker.vault.managed_root.rglob(f"{decision.memory_id}.md"))


def test_durable_promotion_marks_sync_dirty_but_staging_does_not(tmp_path):
    broker = make_broker(tmp_path)
    reasons = []
    broker.sync_adapter = type("Sync", (), {
        "mark_dirty": lambda self, reason: reasons.append(reason),
        "flush": lambda self: type("Result", (), {"success": True, "attempted": False})(),
    })()
    broker.start()

    broker.propose(MemoryCandidate("ordinary candidate"))
    assert reasons == []
    broker.propose(MemoryCandidate("durable choice", metadata={"event_kind": "explicit_remember"}), host_confirmed=True)
    assert reasons == ["promotion"]
    broker.shutdown(5)


def test_successful_durable_promotion_is_synced_on_flush(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "plugins.memory.obsidian_duo.sync.subprocess.run",
        lambda command, **kwargs: (calls.append(command) or type("Result", (), {"returncode": 0, "stderr": ""})()),
    )
    broker = make_broker(tmp_path)
    broker.sync_adapter = CommandSyncAdapter(["obsidian", "sync"], debounce_seconds=30)
    broker.start()

    broker.propose(MemoryCandidate("durable choice", metadata={"event_kind": "explicit_remember"}), host_confirmed=True)
    assert calls == []
    assert broker.flush("promotion", 5)
    assert len(calls) == 1
    broker.shutdown(5)


def test_model_proposal_cannot_claim_user_confirmation(tmp_path):
    broker = make_broker(tmp_path)
    broker.start()

    decision = broker.propose(MemoryCandidate(
        "Model asserted user preference",
        authority=Authority.USER,
        verification=Verification.USER_CONFIRMED,
        metadata={"event_kind": "explicit_remember"},
    ))

    assert decision.action == "stage"
    assert broker.store.get_memory(decision.memory_id) is None
    staged = broker.store.connection().execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
    assert staged == 1


def test_staged_candidate_retains_evidence_references(tmp_path):
    broker = make_broker(tmp_path)
    broker.start()
    broker.propose(MemoryCandidate(
        "Deferred fact",
        evidence=(EvidenceRecord("ev_stage", "turn", "support", session_id="s1"),),
        metadata={"source_session_id": "s1", "task_id": "t1"},
    ))

    payload = broker.store.connection().execute("SELECT payload FROM candidates").fetchone()[0]

    assert "ev_stage" in payload
    assert "s1" in payload
    assert "t1" in payload


def test_event_buffer_redacts_secrets_before_retention(tmp_path):
    broker = make_broker(tmp_path)
    broker._retain_event(MemoryEvent(
        "turn", "API_KEY=sk-proj-1234567890abcdefghijklmnop", session_id="s1"
    ))

    retained = broker._event_buffers["s1"][0].content

    assert "sk-proj-1234567890abcdefghijklmnop" not in retained


def test_promotion_runs_conflict_policy_before_writing(tmp_path):
    broker = make_broker(tmp_path)
    broker.start()
    broker.store.upsert_memory(
        MemoryRecord(
            "mem_old", "Use the blue theme", "preference", "global",
            importance=1.0, authority=Authority.USER,
            verification=Verification.USER_CONFIRMED,
        ),
        "test setup",
    )

    decision = broker.propose(MemoryCandidate(
        "Use the red theme",
        memory_type="preference",
        metadata={"event_kind": "decision_confirmed", "contradicts": "mem_old"},
    ), host_confirmed=True)

    assert decision.action == "conflict"
    assert decision.memory_id == "mem_old"
    assert broker.store.connection().execute("SELECT COUNT(*) FROM memories WHERE content='Use the red theme'").fetchone()[0] == 0
    assert broker.store.connection().execute(
        "SELECT COUNT(*) FROM conflicts WHERE memory_id='mem_old'"
    ).fetchone()[0] == 1


def test_promotion_persists_evidence_and_provenance(tmp_path):
    broker = make_broker(tmp_path)
    broker.start()
    candidate = MemoryCandidate(
        "Use SQLite for durable memory",
        memory_type="decision",
        evidence=(EvidenceRecord("ev_1", "user_message", "SQLite chosen", session_id="s1"),),
        metadata={
            "event_kind": "explicit_remember", "source_session_id": "s1",
            "task_id": "t1", "project_id": "p1", "mission_id": "m1", "agent_id": "a1",
        },
    )

    decision = broker.propose(candidate, host_confirmed=True)
    record = broker.store.get_memory(decision.memory_id)
    metadata = broker.vault.parse_note(next(broker.vault.managed_root.rglob(f"{decision.memory_id}.md"))).metadata

    assert record.evidence_ids == ("ev_1",)
    assert record.source_session_id == "s1"
    assert record.task_id == "t1"
    assert metadata["evidence_ids"] == ["ev_1"]
    assert metadata["project_id"] == "p1"


def test_user_correction_supersedes_existing_memory(tmp_path):
    broker = make_broker(tmp_path)
    broker.start()
    broker.store.upsert_memory(
        MemoryRecord(
            "mem_old", "Use the blue theme", "preference", "global",
            importance=1.0, authority=Authority.AGENT,
        ),
        "test setup",
    )

    decision = broker.propose(MemoryCandidate(
        "Use the red theme", memory_type="preference",
        authority=Authority.USER, verification=Verification.USER_CONFIRMED,
        metadata={"event_kind": "user_correction", "contradicts": "mem_old"},
    ), host_confirmed=True)

    assert decision.action == "promote"
    assert broker.store.get_memory("mem_old").status.value == "superseded"
    assert broker.store.get_memory(decision.memory_id).content == "Use the red theme"


def test_user_correction_marks_old_managed_note_superseded(tmp_path):
    broker = make_broker(tmp_path)
    broker.start()
    old = MemoryRecord(
        "mem_old", "Use the blue theme", "preference", "global",
        importance=0.0, authority=Authority.AGENT,
    )
    broker.store.upsert_memory(old, "test setup")
    old_note = broker.vault.write_managed_note(old)
    broker.vault.scan_managed_changes(broker.store)

    decision = broker.propose(MemoryCandidate(
        "Use the red theme", memory_type="preference",
        authority=Authority.USER, verification=Verification.USER_CONFIRMED,
        metadata={"event_kind": "user_correction", "contradicts": "mem_old"},
    ), host_confirmed=True)

    assert decision.action == "promote"
    assert broker.vault.parse_note(old_note).metadata["status"] == "superseded"


def test_archive_preserves_note_body_and_removes_active_truth(tmp_path):
    broker = make_broker(tmp_path)
    broker.start()
    old = MemoryRecord("mem_old", "Forget this exact fact", "fact", "global", authority=Authority.USER)
    broker.store.upsert_memory(old, "test setup")
    old_note = broker.vault.write_managed_note(old)
    broker.vault.scan_managed_changes(broker.store)

    decision = broker.archive_memory("mem_old")

    assert decision.action == "archive"
    assert broker.store.get_memory("mem_old").status.value == "archived"
    assert broker.vault.parse_note(old_note).body == old.content
    assert broker.vault.parse_note(old_note).metadata["status"] == "archived"
    assert broker.store.connection().execute(
        "SELECT COUNT(*) FROM memories WHERE content=''"
    ).fetchone()[0] == 0


def test_manual_edit_becomes_user_authority_without_rewriting_note(tmp_path):
    broker = make_broker(tmp_path)
    broker.start()
    note = broker.vault.write_managed_note(MemoryRecord("mem_1", "agent text", "fact", "global"))
    broker.vault.scan_managed_changes(broker.store)
    note.write_text(note.read_text().replace("agent text", "user correction"), encoding="utf-8")

    assert broker.process_manual_changes() == 1
    assert "user correction" in note.read_text()
    assert broker.store.get_memory("mem_1").content == "user correction"
    assert broker.store.get_memory("mem_1").authority.value == "user"


def test_retrieve_reconciles_eligible_manual_edit(tmp_path):
    broker = make_broker(tmp_path)
    broker.config.managed_scan_min_interval_seconds = 0
    broker.start()
    note = broker.vault.write_managed_note(MemoryRecord("mem_1", "old agent text", "fact", "global"))
    broker.vault.scan_managed_changes(broker.store)
    note.write_text(note.read_text().replace("old agent text", "manual user edit"), encoding="utf-8")

    packet = broker.retrieve(RetrievalRequest(query="manual user edit", max_memories=4, max_tokens=40))

    assert packet.memories[0].content == "manual user edit"
    assert packet.memories[0].authority is Authority.USER
    assert note.read_text().endswith("manual user edit\n")


def test_malformed_manual_edit_is_left_untouched(tmp_path):
    broker = make_broker(tmp_path)
    broker.start()
    note = broker.vault.write_managed_note(MemoryRecord("mem_1", "valid", "fact", "global"))
    broker.vault.scan_managed_changes(broker.store)
    note.write_text("---\nmalformed", encoding="utf-8")
    broken = note.read_text()

    assert broker.process_manual_changes() == 0
    assert note.read_text() == broken
    assert broker.store.connection().execute(
        "SELECT parse_status FROM note_index WHERE memory_id='mem_1'"
    ).fetchone()[0] == "needs_attention"
