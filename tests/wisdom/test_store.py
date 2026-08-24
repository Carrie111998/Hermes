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


def test_operation_journal_survives_restart(tmp_path: Path):
    root = tmp_path / "wisdom"
    store = WisdomStore(root)
    operation = store.journal("install", "skill-1", "downloaded", {"version": 1})
    store.advance(operation, "files_committed")
    resumed = WisdomStore(root).pending_operations()
    assert resumed[0]["id"] == operation
    assert resumed[0]["phase"] == "files_committed"
