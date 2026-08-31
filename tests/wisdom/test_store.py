import sqlite3
from pathlib import Path

from hermes_wisdom.store import WisdomStore


def test_profile_store_permissions_identity_and_rename(tmp_path: Path):
    store = WisdomStore(tmp_path / "wisdom")
    assert store.existing_installation_identity() is None
    skill = tmp_path / "skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text("hello", encoding="utf-8")
    first = store.register_skill(skill, content_hash="sha256:a", source_kind="local")
    moved = tmp_path / "renamed"
    skill.rename(moved)
    second = store.register_skill(moved, content_hash="sha256:b", source_kind="local")
    assert first == second
    assert store.installation_identity() == store.installation_identity()
    assert store.existing_installation_identity() == store.installation_identity()
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


def test_local_events_hide_a_contribution_that_already_reached_publication(
    tmp_path: Path,
):
    store = WisdomStore(tmp_path / "wisdom")
    skill_path = tmp_path / "skill"
    skill_path.mkdir()
    (skill_path / "SKILL.md").write_text("hello", encoding="utf-8")
    skill_id = store.register_skill(
        skill_path, content_hash="sha256:source", source_kind="local"
    )
    store.emit_local_event(
        kind="wisdom.candidate",
        skill_id=skill_id,
        content_hash="sha256:source",
        payload={"skill_name": "skill"},
        session_id="session-1",
        task_id="task-1",
        qualification="manual_selection",
    )
    store.record_draft({
        "id": "draft-1",
        "skill_id": skill_id,
        "source_hash": "sha256:source",
        "overlay_path": str(tmp_path / "overlay"),
        "state": "published",
        "description": "Owner copy",
        "content_hash": "sha256:content",
        "description_hash": "sha256:description",
        "manifest_hash": "sha256:manifest",
    })

    assert store.local_events(kind="wisdom.candidate", session_id="session-1") == []


def test_verified_org_change_deactivates_stale_managed_installs(tmp_path: Path):
    store = WisdomStore(tmp_path / "wisdom")
    store.installation_identity()
    store.verify_installation_identity("org-1")
    store.record_install({
        "skill_id": "skill-1",
        "org_id": "org-1",
        "slug": "managed",
        "version": 1,
        "content_hash": "sha256:content",
        "baseline": {"SKILL.md": "sha256:file"},
        "target_path": str(tmp_path / "skills" / "_wisdom" / "org-1" / "managed"),
        "update_mode": "MANUAL",
    })

    store.verify_installation_identity("org-2")

    assert store.active_org_id() == "org-2"
    assert store.installation("skill-1")["state"] == "inactive"


def test_identity_rotation_is_atomic_with_org_activation(tmp_path: Path):
    store = WisdomStore(tmp_path / "wisdom")
    old_identity = store.installation_identity()
    store.verify_installation_identity("org-1")

    store.activate_installation_identity("hwi_" + "n" * 32, "org-2")

    assert store.existing_installation_identity() != old_identity
    assert store.existing_installation_identity() == "hwi_" + "n" * 32
    assert store.active_org_id() == "org-2"


def test_schema_v6_tracks_profile_local_usage_and_telegram_delivery(tmp_path: Path):
    store = WisdomStore(tmp_path / "wisdom")
    with store.transaction() as db:
        snapshot_columns = {
            row[1] for row in db.execute("PRAGMA table_info(snapshot)").fetchall()
        }
        stability_columns = {
            row[1] for row in db.execute("PRAGMA table_info(stability_job)").fetchall()
        }
        usage_columns = {
            row[1] for row in db.execute("PRAGMA table_info(usage_day)").fetchall()
        }
        event_columns = {
            row[1] for row in db.execute("PRAGMA table_info(local_event)").fetchall()
        }
        version = db.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()[0]
    assert "skill_text" in snapshot_columns
    assert {"session_id", "task_id"} <= stability_columns
    assert {"day_local", "timezone_name"} <= usage_columns
    assert "day_utc" not in usage_columns
    assert "telegram_delivered_at" in event_columns
    assert version == "6"


def test_schema_v6_preserves_v4_usage_in_an_explicit_utc_bucket(tmp_path: Path):
    root = tmp_path / "wisdom"
    root.mkdir()
    with sqlite3.connect(root / "wisdom.db") as db:
        db.executescript(
            """
            CREATE TABLE local_skill (
              id TEXT PRIMARY KEY,
              canonical_path TEXT NOT NULL,
              fs_identity TEXT,
              current_hash TEXT,
              source_kind TEXT NOT NULL,
              deleted_at TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE usage_day (
              skill_id TEXT NOT NULL,
              day_utc TEXT NOT NULL,
              use_count INTEGER NOT NULL,
              PRIMARY KEY(skill_id, day_utc),
              FOREIGN KEY(skill_id) REFERENCES local_skill(id) ON DELETE CASCADE
            );
            INSERT INTO local_skill VALUES(
              'skill-1','/tmp/skill',NULL,NULL,'local',NULL,'now','now'
            );
            INSERT INTO usage_day VALUES('skill-1','2026-08-03',2);
            """
        )

    store = WisdomStore(root)

    with store.transaction() as db:
        row = db.execute(
            "SELECT day_local,timezone_name,use_count FROM usage_day"
        ).fetchone()
    assert tuple(row) == ("2026-08-03", "UTC", 2)


def test_telegram_delivery_is_session_scoped_without_consuming_candidate(tmp_path: Path):
    store = WisdomStore(tmp_path / "wisdom")
    skill_path = tmp_path / "skill"
    skill_path.mkdir()
    (skill_path / "SKILL.md").write_text("hello", encoding="utf-8")
    skill_id = store.register_skill(
        skill_path, content_hash="sha256:source", source_kind="local"
    )
    event_id = store.emit_local_event(
        kind="wisdom.candidate",
        skill_id=skill_id,
        content_hash="sha256:source",
        payload={"skill_name": "skill"},
        session_id="telegram-session",
        task_id="task-1",
        qualification="high_usage",
    )
    assert event_id is not None

    assert store.pending_telegram_events(
        kind="wisdom.candidate", session_id="other-session"
    ) == []
    pending = store.pending_telegram_events(
        kind="wisdom.candidate", session_id="telegram-session"
    )
    assert [item["id"] for item in pending] == [event_id]

    store.mark_telegram_delivered([event_id])

    assert store.pending_telegram_events(
        kind="wisdom.candidate", session_id="telegram-session"
    ) == []
    assert [
        item["id"]
        for item in store.local_events(
            kind="wisdom.candidate", session_id="telegram-session"
        )
    ] == [event_id]
