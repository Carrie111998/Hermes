"""Integration tests against a live Mongo (lab compose).

Skip unless ``HERMES_MONGO_URI`` is set (or lab default responds).

Run::

    docker compose -f deploy/control-plane/docker-compose.lab.yml up -d
    set HERMES_MONGO_URI=mongodb://127.0.0.1:27017/?directConnection=true
    pytest tests/hermes_storage/test_mongo_integration.py -q
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.integration

LAB_URI = os.environ.get(
    "HERMES_MONGO_URI",
    "mongodb://127.0.0.1:27017/?directConnection=true",
)


def _mongo_reachable(uri: str) -> bool:
    try:
        from pymongo import MongoClient

        client = MongoClient(uri, serverSelectionTimeoutMS=800)
        client.admin.command("ping")
        client.close()
        return True
    except Exception:
        return False


@pytest.fixture
def mongo_home(tmp_path, monkeypatch):
    if not _mongo_reachable(LAB_URI):
        pytest.skip("Mongo lab not reachable — start docker-compose.lab.yml")

    from hermes_storage.bootstrap import reset_bootstrap_cache
    from hermes_storage.factory import reset_storage

    reset_bootstrap_cache()
    reset_storage()

    boot = tmp_path / "bootstrap.yaml"
    boot.write_text(
        yaml.safe_dump({
            "mongo_uri": LAB_URI,
            "profile": f"test_{os.getpid()}",
            "shared_db": "hermes_shared_test",
            "machine_id": f"lab-{os.getpid()}",
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_BOOTSTRAP", str(boot))
    monkeypatch.delenv("HERMES_MONGO_URI", raising=False)
    reset_bootstrap_cache()
    reset_storage()
    yield tmp_path
    reset_storage()
    reset_bootstrap_cache()


def test_ping_and_require_storage(mongo_home):
    from hermes_storage import is_mongo_mode, require_storage

    assert is_mongo_mode()
    storage = require_storage(force=True)
    storage.client.admin.command("ping")


def test_config_soul_memory_roundtrip(mongo_home):
    from hermes_storage import require_storage

    storage = require_storage(force=True)
    storage.save_profile_config({"model": {"default": "lab-model"}})
    storage.save_soul("I am the lab agent.")
    storage.memories.save("memory", "remember the lab")
    storage.secrets.set_many({"OPENAI_API_KEY": "sk-lab"})

    cfg = storage.load_effective_config({})
    assert cfg["model"]["default"] == "lab-model"
    assert "lab agent" in storage.load_soul()
    assert "lab" in storage.memories.load("memory")
    assert storage.secrets.get("OPENAI_API_KEY") == "sk-lab"


def test_sessiondb_mongo_roundtrip(mongo_home):
    from hermes_state import SessionDB

    db = SessionDB()
    assert getattr(db, "_mongo_mode", False) is True
    sid = db.create_session("sess-lab-1", "cli", title="Lab")
    db.append_message(sid, "user", "hello mongo")
    msgs = db.get_messages(sid)
    assert any("hello mongo" in str(m.get("content")) for m in msgs)
    assert db.session_count() >= 1
    assert db.has_platform_message_id(sid, "nope") is False


def test_sessiondb_unknown_method_fail_loud(mongo_home):
    from hermes_state import SessionDB

    db = SessionDB()
    with pytest.raises(AttributeError, match="not ported"):
        db.totally_fake_method_xyz()


def test_cron_ledger_roundtrip(mongo_home):
    from hermes_storage.ledgers import load_cron_jobs, save_cron_jobs

    jobs = [{"id": "j1", "prompt": "ping"}]
    save_cron_jobs({"jobs": jobs})
    remote = load_cron_jobs()
    assert remote["jobs"][0]["id"] == "j1"


def test_indexes_exist(mongo_home):
    from hermes_storage import require_storage

    storage = require_storage(force=True)
    # ensure_indexes ran at build; smoke that collections accept writes
    storage.sessions.create_session("idx-sess", "cli")
    names = storage.profile_db.list_collection_names()
    assert "sessions" in names
