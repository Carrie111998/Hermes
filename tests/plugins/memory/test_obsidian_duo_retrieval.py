from plugins.memory.obsidian_duo.contracts import (
    EvidenceRecord,
    MemoryRecord,
    MemoryStatus,
    RetrievalRequest,
    Verification,
)
from plugins.memory.obsidian_duo.retrieval import MemoryRetriever, RecallClass
from plugins.memory.obsidian_duo.store import SqliteMemoryStore


def test_retrieval_is_scope_aware_and_excludes_superseded(tmp_path):
    store = SqliteMemoryStore(tmp_path / "memory.db")
    store.initialize()
    store.upsert_memory(
        MemoryRecord("hud_decision", "HUD drag uses pointer capture", "decision", "project:hermes", verification=Verification.USER_CONFIRMED),
        "seed",
    )
    store.upsert_memory(
        MemoryRecord("hud_old", "HUD drag used polling", "decision", "project:hermes", status=MemoryStatus.SUPERSEDED),
        "seed",
    )
    store.upsert_memory(
        MemoryRecord("other", "Pointer capture in another project", "lesson", "project:other"),
        "seed",
    )

    packet = MemoryRetriever(store).retrieve(
        RetrievalRequest("HUD drag pointer", scope="project:hermes", max_memories=5)
    )

    ids = [memory.memory_id for memory in packet.memories]
    assert ids[0] == "hud_decision"
    assert "hud_old" not in ids
    assert "other" not in ids


def test_retrieval_surfaces_conflicts_and_bounded_no_result(tmp_path):
    store = SqliteMemoryStore(tmp_path / "memory.db")
    store.initialize()
    store.upsert_memory(MemoryRecord("mem_1", "Use blue theme", "preference", "global"), "seed")
    store.record_conflict("mem_1", "mem_2", "red versus blue")
    retriever = MemoryRetriever(store)

    packet = retriever.retrieve(RetrievalRequest("blue theme", max_memories=1, max_tokens=3))
    empty = retriever.retrieve(RetrievalRequest("unrelated spacecraft", max_memories=1))

    assert packet.conflicts == ("mem_1",)
    assert len(packet.memories) == 1
    assert empty.no_verified_memory is True


def test_unverified_recall_does_not_claim_verified_memory(tmp_path):
    store = SqliteMemoryStore(tmp_path / "memory.db")
    store.initialize()
    store.upsert_memory(
        MemoryRecord("mem_unverified", "Use blue theme", "preference", "global"),
        "seed",
    )

    packet = MemoryRetriever(store).retrieve(RetrievalRequest("blue theme", max_memories=1))

    assert packet.memories
    assert packet.no_verified_memory is True


def test_retrieval_includes_referenced_evidence(tmp_path):
    store = SqliteMemoryStore(tmp_path / "memory.db")
    store.initialize()
    store.insert_evidence(EvidenceRecord("ev_1", "user_message", "confirmed choice", session_id="s1"))
    store.upsert_memory(
        MemoryRecord("mem_1", "Use blue theme", "preference", "global", evidence_ids=("ev_1",), verification=Verification.SOURCE_SUPPORTED),
        "seed",
    )

    packet = MemoryRetriever(store).retrieve(RetrievalRequest("blue theme"))

    assert [item.evidence_id for item in packet.evidence] == ["ev_1"]


def test_retrieval_does_not_admit_record_over_token_budget(tmp_path):
    store = SqliteMemoryStore(tmp_path / "memory.db")
    store.initialize()
    store.upsert_memory(MemoryRecord("mem_big", "one two three four", "fact", "global"), "seed")
    packet = MemoryRetriever(store).retrieve(RetrievalRequest("one", max_tokens=2))
    assert packet.memories == ()
    assert packet.no_verified_memory


def test_query_classification_is_deterministic(tmp_path):
    store = SqliteMemoryStore(tmp_path / "memory.db")
    retriever = MemoryRetriever(store)

    assert retriever.classify_query("") is RecallClass.NONE
    assert retriever.classify_query('"exact decision"') is RecallClass.EXACT
    assert retriever.classify_query("project:hermes status") is RecallClass.STRUCTURED


def test_curated_fixture_has_broad_evaluation_coverage():
    fixture = Path(__file__).parent / "fixtures" / "obsidian_duo_retrieval.json"
    cases = json.loads(fixture.read_text(encoding="utf-8"))

    assert len(cases) >= 30
    assert any(case["forbidden"] for case in cases)
    assert any(not case["expected"] for case in cases)
import json
from pathlib import Path
