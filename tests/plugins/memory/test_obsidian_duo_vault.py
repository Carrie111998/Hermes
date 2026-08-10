from pathlib import Path

import pytest

from plugins.memory.obsidian_duo.contracts import MemoryRecord, MemoryStatus
from plugins.memory.obsidian_duo.store import SqliteMemoryStore
from plugins.memory.obsidian_duo.vault import ObsidianVault


def test_note_frontmatter_identity_survives_rename(tmp_path):
    vault = ObsidianVault(tmp_path / "Vault", "Hermes Memory")
    record = MemoryRecord("mem_stable", "The HUD supports dragging.", "Projects", "global")

    path = vault.write_managed_note(record)
    parsed = vault.parse_note(path)
    renamed = path.with_name("renamed-note.md")
    path.rename(renamed)

    assert parsed.memory_id == "mem_stable"
    assert vault.parse_note(renamed).memory_id == "mem_stable"
    assert vault.parse_note(renamed).body == record.content


def test_atomic_write_preserves_original_and_cleans_temp(tmp_path, monkeypatch):
    vault = ObsidianVault(tmp_path / "Vault", "Hermes Memory")
    record = MemoryRecord("mem_atomic", "original", "Facts", "global")
    path = vault.write_managed_note(record)

    monkeypatch.setattr("plugins.memory.obsidian_duo.vault.os.replace", lambda *_: (_ for _ in ()).throw(OSError("boom")))
    with pytest.raises(OSError, match="boom"):
        vault.write_managed_note(MemoryRecord("mem_atomic", "changed", "Facts", "global"))

    assert vault.parse_note(path).body == "original"
    assert not list(path.parent.glob(f".{path.name}.*.tmp"))


def test_scan_only_reparses_changed_notes(tmp_path):
    vault = ObsidianVault(tmp_path / "Vault", "Hermes Memory")
    store = SqliteMemoryStore(tmp_path / "memory.db")
    store.initialize()
    for index in range(10):
        vault.write_managed_note(MemoryRecord(f"mem_{index}", f"note {index}", "Facts", "global"))

    first = vault.scan_managed_changes(store)
    (vault.managed_root / "Facts" / "mem_3.md").write_text(
        "---\nmemory_id: mem_3\nmemory_type: Facts\nscope: global\n---\nchanged\n",
        encoding="utf-8",
    )
    second = vault.scan_managed_changes(store)

    assert len(first.reparsed_paths) == 10
    assert [path.name for path in second.reparsed_paths] == ["mem_3.md"]
