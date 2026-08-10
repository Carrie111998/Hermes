from pathlib import Path

from plugins.memory.obsidian_duo.broker import EmbeddedMemoryBroker
from plugins.memory.obsidian_duo.config import ObsidianDuoConfig
from plugins.memory.obsidian_duo.contracts import MemoryCandidate, MemoryEvent, MemoryRecord
from plugins.memory.obsidian_duo.policy import MemoryPolicy
from plugins.memory.obsidian_duo.retrieval import MemoryRetriever
from plugins.memory.obsidian_duo.store import SqliteMemoryStore
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


def test_broker_starts_worker_lazily_and_flushes(tmp_path):
    broker = make_broker(tmp_path)
    assert broker._worker is None

    broker.observe(MemoryEvent("turn", content="hello"))

    assert broker.flush("test", 5)
    assert broker._worker is not None
    broker.shutdown(5)


def test_broker_recovers_written_journal_by_rescanning(tmp_path):
    broker = make_broker(tmp_path)
    broker.start()
    broker.store.record_journal("tx_1", "write_note", "written", {"path": "missing.md"})

    result = broker.recover()

    assert result.recovered >= 1
    assert broker.store.connection().execute(
        "SELECT state FROM journal WHERE txn_id='tx_1'"
    ).fetchone()[0] == "recovery_failed"


def test_explicit_promotion_persists_note_index_and_memory(tmp_path):
    broker = make_broker(tmp_path)
    broker.start()
    decision = broker.propose(MemoryCandidate(
        "Use SQLite for durable memory", metadata={"event_kind": "explicit_remember"}
    ))
    assert decision.action == "promote"
    assert broker.store.get_memory(decision.memory_id).content == "Use SQLite for durable memory"
    assert list(broker.vault.managed_root.rglob(f"{decision.memory_id}.md"))


def test_manual_edit_becomes_user_authority_without_rewriting_note(tmp_path):
    broker = make_broker(tmp_path)
    broker.start()
    note = broker.vault.write_managed_note(MemoryRecord("mem_1", "agent text", "Facts", "global"))
    broker.vault.scan_managed_changes(broker.store)
    note.write_text(note.read_text().replace("agent text", "user correction"), encoding="utf-8")

    assert broker.process_manual_changes() == 1
    assert "user correction" in note.read_text()
    assert broker.store.get_memory("mem_1").content == "user correction"
    assert broker.store.get_memory("mem_1").authority.value == "user"


def test_malformed_manual_edit_is_left_untouched(tmp_path):
    broker = make_broker(tmp_path)
    broker.start()
    note = broker.vault.write_managed_note(MemoryRecord("mem_1", "valid", "Facts", "global"))
    broker.vault.scan_managed_changes(broker.store)
    note.write_text("---\nmalformed", encoding="utf-8")
    broken = note.read_text()

    assert broker.process_manual_changes() == 0
    assert note.read_text() == broken
    assert broker.store.connection().execute(
        "SELECT parse_status FROM note_index WHERE memory_id='mem_1'"
    ).fetchone()[0] == "needs_attention"
