from __future__ import annotations

import os

os.environ.setdefault("LOCALAPPDATA", os.environ.get("TEMP", r"C:\Windows\Temp"))

from hermes_state import SCHEMA_VERSION, SessionDB


ATTACHMENT = {
    "hermes_session_id": "h-1",
    "provider": "claude-cli",
    "provider_session_id": "c-1",
    "model_requested": "opus",
    "model_reported": "claude-opus-5",
    "tool_catalog_fingerprint": "sha256:tools",
    "system_prompt_fingerprint": "sha256:system",
    "message_count": 2,
    "history_fingerprint": "sha256:history",
    "last_success_at": 123.0,
}


def test_provider_attachment_round_trips_without_credentials(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("h-1", "cli")

    db.upsert_provider_attachment(**ATTACHMENT)

    assert db.get_provider_attachment("h-1", "claude-cli") == ATTACHMENT
    serialized = str(db.get_provider_attachment("h-1", "claude-cli")).lower()
    assert "oauth" not in serialized
    assert "api_key" not in serialized


def test_upsert_keeps_one_attachment_per_session_and_provider(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("h-1", "cli")
    db.upsert_provider_attachment(**ATTACHMENT)

    replacement = {
        **ATTACHMENT,
        "provider_session_id": "c-2",
        "last_success_at": 456.0,
    }
    db.upsert_provider_attachment(**replacement)

    assert db.get_provider_attachment("h-1", "claude-cli") == replacement
    count = db._conn.execute(
        "SELECT COUNT(*) FROM provider_session_attachments "
        "WHERE hermes_session_id='h-1' AND provider='claude-cli'"
    ).fetchone()[0]
    assert count == 1


def test_attachment_is_profile_scoped_by_database(tmp_path):
    first = SessionDB(tmp_path / "profile-a" / "state.db")
    second = SessionDB(tmp_path / "profile-b" / "state.db")
    first.create_session("h-1", "cli")
    second.create_session("h-1", "cli")

    first.upsert_provider_attachment(**ATTACHMENT)

    assert first.get_provider_attachment("h-1", "claude-cli") == ATTACHMENT
    assert second.get_provider_attachment("h-1", "claude-cli") is None


def test_deleting_session_cascades_attachment(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("h-1", "cli")
    db.upsert_provider_attachment(**ATTACHMENT)

    assert db.delete_session("h-1") is True

    assert db.get_provider_attachment("h-1", "claude-cli") is None


def test_delete_attachment_is_idempotent(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("h-1", "cli")
    db.upsert_provider_attachment(**ATTACHMENT)

    assert db.delete_provider_attachment("h-1", "claude-cli") is True
    assert db.delete_provider_attachment("h-1", "claude-cli") is False


def test_schema_upgrade_preserves_existing_session_rows(tmp_path):
    path = tmp_path / "state.db"
    db = SessionDB(path)
    db.create_session("before-upgrade", "cli")
    db._conn.execute("UPDATE schema_version SET version=22")
    db._conn.commit()
    db.close()

    upgraded = SessionDB(path)

    assert upgraded.get_session("before-upgrade")["id"] == "before-upgrade"
    assert upgraded._conn.execute(
        "SELECT version FROM schema_version"
    ).fetchone()[0] == SCHEMA_VERSION
    assert upgraded._conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='provider_session_attachments'"
    ).fetchone()[0] == "provider_session_attachments"
