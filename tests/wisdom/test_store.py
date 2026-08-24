from pathlib import Path

from hermes_wisdom.store import WisdomStore


def test_profile_store_permissions_identity_and_rename(tmp_path: Path):
    store = WisdomStore(tmp_path / "wisdom")
    skill = tmp_path / "skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text("hello", encoding="utf-8")
    first = store.register_skill(skill, content_hash="sha256:a", source_kind="local")
    moved = tmp_path / "renamed"
    skill.rename(moved)
    second = store.register_skill(moved, content_hash="sha256:b", source_kind="local")
    assert first == second
    assert store.installation_identity() == store.installation_identity()
    assert store.root.stat().st_mode & 0o777 == 0o700
    assert store.path.stat().st_mode & 0o777 == 0o600


def test_delete_recreate_does_not_inherit_identity(tmp_path: Path):
    store = WisdomStore(tmp_path / "wisdom")
    path = tmp_path / "skill"
    path.mkdir()
    (path / "SKILL.md").write_text("one", encoding="utf-8")
    first = store.register_skill(path, content_hash="sha256:a", source_kind="local")
    (path / "SKILL.md").unlink()
    path.rmdir()
    path.mkdir()
    (path / "SKILL.md").write_text("two", encoding="utf-8")
    store.mark_missing_skills(set())
    second = store.register_skill(path, content_hash="sha256:b", source_kind="local")
    assert first != second


def test_rename_falls_back_to_unambiguous_content_hash_without_filesystem_identity(
    monkeypatch, tmp_path: Path
):
    monkeypatch.setattr("hermes_wisdom.store.filesystem_identity", lambda _path: None)
    store = WisdomStore(tmp_path / "wisdom")
    original = tmp_path / "original"
    original.mkdir()
    (original / "SKILL.md").write_text("same", encoding="utf-8")
    first = store.register_skill(
        original, content_hash="sha256:same", source_kind="local"
    )
    renamed = tmp_path / "renamed"
    original.rename(renamed)
    store.mark_missing_skills({str(renamed.resolve())})
    second = store.register_skill(
        renamed, content_hash="sha256:same", source_kind="local"
    )
    assert second == first


def test_ambiguous_content_hash_move_creates_new_identity(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("hermes_wisdom.store.filesystem_identity", lambda _path: None)
    store = WisdomStore(tmp_path / "wisdom")
    for name in ("one", "two"):
        path = tmp_path / name
        path.mkdir()
        (path / "SKILL.md").write_text("same", encoding="utf-8")
        store.register_skill(path, content_hash="sha256:same", source_kind="local")
    moved = tmp_path / "moved"
    moved.mkdir()
    (moved / "SKILL.md").write_text("same", encoding="utf-8")
    new_id = store.register_skill(
        moved, content_hash="sha256:same", source_kind="local"
    )
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM local_skill").fetchone()[0] == 3
        assert db.execute(
            "SELECT canonical_path FROM local_skill WHERE id=?", (new_id,)
        ).fetchone()[0] == str(moved.resolve())


def test_operation_journal_survives_restart(tmp_path: Path):
    root = tmp_path / "wisdom"
    store = WisdomStore(root)
    operation = store.journal("install", "skill-1", "downloaded", {"version": 1})
    store.advance(operation, "files_committed")
    resumed = WisdomStore(root).pending_operations()
    assert resumed[0]["id"] == operation
    assert resumed[0]["phase"] == "files_committed"
