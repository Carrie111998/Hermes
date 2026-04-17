import json
from pathlib import Path

import pytest

from scripts.migrate_hermes_notification_paths import migrate


@pytest.fixture
def fake_hermes(tmp_path):
    root = tmp_path
    profile = root / "profiles" / "main"
    (profile / "telegram").mkdir(parents=True)
    (profile / "notifications").mkdir(parents=True)
    (profile / "events").mkdir(parents=True)
    (profile / "mailbox").mkdir(parents=True)

    (profile / "telegram" / "topics.json").write_text(
        json.dumps({"group_chat_id": "-100", "topics": {}}), encoding="utf-8"
    )
    (profile / "telegram" / "verbosity.json").write_text(
        json.dumps({"system": {"mode": "digest_only"}}), encoding="utf-8"
    )
    (profile / "notifications" / "quiet_hours.json").write_text(
        json.dumps({"enabled": True}), encoding="utf-8"
    )
    (profile / "events" / "event_bus.db").write_bytes(b"FAKE_SQLITE")
    (profile / "events" / "audit.jsonl").write_text('{}\n', encoding="utf-8")

    (root / "telegram").mkdir()
    (root / "telegram" / "topics.json").write_text(
        json.dumps({"group_chat_id": "-OLD"}), encoding="utf-8"
    )
    return root


def test_migrate_moves_profile_files_to_root(fake_hermes):
    migrate(root=fake_hermes)
    assert (fake_hermes / "telegram" / "topics.json").exists()
    data = json.loads((fake_hermes / "telegram" / "topics.json").read_text())
    assert data["group_chat_id"] == "-100"
    # old global preserved
    assert (fake_hermes / "telegram" / "topics.json.pre-2026-04-16").exists()


def test_migrate_preserves_all_notification_artifacts(fake_hermes):
    migrate(root=fake_hermes)
    assert (fake_hermes / "telegram" / "verbosity.json").exists()
    assert (fake_hermes / "notifications" / "quiet_hours.json").exists()
    assert (fake_hermes / "events" / "event_bus.db").exists()
    assert (fake_hermes / "events" / "audit.jsonl").exists()


def test_migrate_idempotent(fake_hermes):
    migrate(root=fake_hermes)
    migrate(root=fake_hermes)
    backups = list((fake_hermes / "telegram").glob("topics.json.pre-*"))
    assert len(backups) == 1


def test_migrate_dry_run_does_not_move(fake_hermes):
    migrate(root=fake_hermes, dry_run=True)
    assert (fake_hermes / "profiles" / "main" / "telegram" / "topics.json").exists()
    data = json.loads((fake_hermes / "telegram" / "topics.json").read_text())
    assert data["group_chat_id"] == "-OLD"
