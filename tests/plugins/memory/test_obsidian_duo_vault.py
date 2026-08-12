from pathlib import Path

import pytest

from plugins.memory.obsidian_duo.contracts import MemoryRecord, MemoryStatus
from plugins.memory.obsidian_duo.store import SqliteMemoryStore
from plugins.memory.obsidian_duo.vault import ObsidianVault


def test_note_frontmatter_identity_survives_rename(tmp_path):
    vault = ObsidianVault(tmp_path / "Vault", "Hermes Memory")
    record = MemoryRecord("mem_stable", "The HUD supports dragging.", "project", "global")

    path = vault.write_managed_note(record)
    parsed = vault.parse_note(path)
    renamed = path.with_name("renamed-note.md")
    path.rename(renamed)

    assert parsed.memory_id == "mem_stable"
    assert vault.parse_note(renamed).memory_id == "mem_stable"
    assert vault.parse_note(renamed).body == record.content


def test_provenance_and_evidence_round_trip_through_markdown(tmp_path):
    vault = ObsidianVault(tmp_path / "Vault", "Hermes Memory")
    record = MemoryRecord(
        "mem_provenance", "A durable decision", "decision", "global",
        evidence_ids=("ev_1",), relationships=("supersedes:mem_old",),
        source_session_id="session_1", task_id="task_1", project_id="project_1",
        child_session_id="child_1", mission_id="mission_1", agent_id="agent_1",
    )

    path = vault.write_managed_note(record)
    metadata = vault.parse_note(path).metadata

    assert metadata["evidence_ids"] == ["ev_1"]
    assert metadata["source_session_id"] == "session_1"
    assert metadata["task_id"] == "task_1"
    assert metadata["project_id"] == "project_1"
    assert metadata["child_session_id"] == "child_1"
    assert metadata["mission_id"] == "mission_1"
    assert metadata["agent_id"] == "agent_1"


def test_atomic_write_preserves_original_and_cleans_temp(tmp_path, monkeypatch):
    vault = ObsidianVault(tmp_path / "Vault", "Hermes Memory")
    record = MemoryRecord("mem_atomic", "original", "fact", "global")
    path = vault.write_managed_note(record)

    monkeypatch.setattr("plugins.memory.obsidian_duo.vault.os.replace", lambda *_: (_ for _ in ()).throw(OSError("boom")))
    with pytest.raises(OSError, match="boom"):
        vault.write_managed_note(MemoryRecord("mem_atomic", "changed", "fact", "global"))

    assert vault.parse_note(path).body == "original"
    assert not list(path.parent.glob(f".{path.name}.*.tmp"))


def test_scan_only_reparses_changed_notes(tmp_path):
    vault = ObsidianVault(tmp_path / "Vault", "Hermes Memory")
    store = SqliteMemoryStore(tmp_path / "memory.db")
    store.initialize()
    for index in range(10):
        vault.write_managed_note(MemoryRecord(f"mem_{index}", f"note {index}", "fact", "global"))

    first = vault.scan_managed_changes(store)
    (vault.managed_root / "Entities" / "mem_3.md").write_text(
        "---\nmemory_id: mem_3\nmemory_type: fact\nscope: global\n---\nchanged\n",
        encoding="utf-8",
    )
    second = vault.scan_managed_changes(store)

    assert len(first.reparsed_paths) == 10
    assert [path.name for path in second.reparsed_paths] == ["mem_3.md"]


def test_full_rebuild_indexes_external_markdown_without_writing_it(tmp_path):
    vault = ObsidianVault(tmp_path / "Vault", "Hermes Memory")
    store = SqliteMemoryStore(tmp_path / "memory.db")
    store.initialize()
    external = vault.vault_path / "Notes" / "guide.md"
    external.parent.mkdir(parents=True)
    original = "Pointer capture keeps the HUD drag stable."
    external.write_text(original, encoding="utf-8")

    result = vault.rebuild_from_vault(store, full=True)

    assert result.scanned == 1
    assert store.search_fts('"Pointer" AND "capture"', 4)
    assert external.read_text(encoding="utf-8") == original


def test_full_rebuild_ignores_internal_directories_and_redacts_external_secrets(tmp_path):
    vault = ObsidianVault(tmp_path / "Vault", "Hermes Memory")
    store = SqliteMemoryStore(tmp_path / "memory.db")
    store.initialize()
    ignored = vault.vault_path / ".obsidian" / "cache.md"
    ignored.parent.mkdir(parents=True)
    ignored.write_text("Pointer capture", encoding="utf-8")
    external = vault.vault_path / "Notes" / "credentials.md"
    external.parent.mkdir(parents=True)
    external.write_text("API_KEY=sk-proj-1234567890abcdefghijklmnop", encoding="utf-8")

    vault.rebuild_from_vault(store, full=True)

    assert not store.search_fts('"Pointer"', 4)
    hits = store.search_fts('"API_KEY"', 4)
    assert hits
    assert "sk-proj-1234567890abcdefghijklmnop" not in hits[0].body


def test_external_catalogue_is_not_rebuilt_for_each_index_batch(tmp_path, monkeypatch):
    vault = ObsidianVault(tmp_path / "Vault", "Hermes Memory")
    store = SqliteMemoryStore(tmp_path / "memory.db")
    store.initialize()
    notes = vault.vault_path / "Notes"
    notes.mkdir(parents=True)
    for index in range(100):
        (notes / f"note-{index:03d}.md").write_text(f"content {index}", encoding="utf-8")

    calls = []
    original = vault.catalog_external_markdown_paths

    def counted_catalogue():
        calls.append(1)
        yield from original()

    monkeypatch.setattr(vault, "catalog_external_markdown_paths", counted_catalogue)
    vault.refresh_external_catalog(store)
    for _ in range(10):
        vault.index_external_paths(store, limit=10)

    assert len(calls) == 1
    assert store.connection().execute("SELECT COUNT(*) FROM external_index").fetchone()[0] == 100


def test_external_index_cursor_progresses_to_different_notes(tmp_path):
    vault = ObsidianVault(tmp_path / "Vault", "Hermes Memory")
    store = SqliteMemoryStore(tmp_path / "memory.db")
    store.initialize()
    notes = vault.vault_path / "Notes"
    notes.mkdir(parents=True)
    for index in range(20):
        (notes / f"note-{index:03d}.md").write_text(f"content {index}", encoding="utf-8")

    vault.refresh_external_catalog(store)
    vault.index_external_paths(store, limit=10)
    first = {
        row["path"] for row in store.connection().execute("SELECT path FROM external_index").fetchall()
    }
    vault.index_external_paths(store, limit=10)
    second = {
        row["path"] for row in store.connection().execute("SELECT path FROM external_index").fetchall()
    } - first

    assert len(first) == 10
    assert len(second) == 10
    assert first.isdisjoint(second)


def test_external_catalogue_refresh_adds_and_removes_derived_state(tmp_path):
    vault = ObsidianVault(tmp_path / "Vault", "Hermes Memory")
    store = SqliteMemoryStore(tmp_path / "memory.db")
    store.initialize()
    notes = vault.vault_path / "Notes"
    notes.mkdir(parents=True)
    original = notes / "original.md"
    original.write_text("original content", encoding="utf-8")

    vault.refresh_external_catalog(store)
    vault.index_external_paths(store, limit=10)
    assert store.connection().execute("SELECT COUNT(*) FROM external_index").fetchone()[0] == 1
    original.unlink()
    replacement = notes / "replacement.md"
    replacement.write_text("replacement content", encoding="utf-8")

    vault.refresh_external_catalog(store)

    assert store.connection().execute("SELECT COUNT(*) FROM external_catalog").fetchone()[0] == 1
    assert store.connection().execute("SELECT COUNT(*) FROM external_index").fetchone()[0] == 0
    assert store.connection().execute("SELECT COUNT(*) FROM memories WHERE memory_id LIKE 'external_%'").fetchone()[0] == 0
