"""Mixed-surface trigram policy regression tests for one shared state.db."""

from __future__ import annotations

import sqlite3

import pytest

from hermes_cli import config as hermes_config
from hermes_state import SessionDB


def _table_exists(db_path, name: str) -> bool:
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = ?",
            (name,),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def _trigger_exists(db_path, name: str) -> bool:
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'trigger' AND name = ?",
            (name,),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def test_mixed_surface_trigram_policy_converges_without_hot_teardown(
    tmp_path,
    monkeypatch,
):
    """Legacy env drift may disable trigram once, but ordinary opens do not drop it."""
    db_path = tmp_path / "state.db"

    monkeypatch.setattr(
        hermes_config,
        "load_config_readonly",
        lambda: {"sessions": {"trigram_fts": True}},
    )
    gateway_db = SessionDB(db_path=db_path)
    try:
        if not gateway_db._trigram_available:
            pytest.skip("SQLite build lacks the trigram tokenizer")
        gateway_db.create_session("gateway-session", "telegram")
        gateway_db.append_message(
            "gateway-session",
            role="user",
            content="大别山 policy probe",
        )
        assert gateway_db.get_meta("trigram_fts_policy") == "enabled"
        assert _table_exists(db_path, "messages_fts_trigram")
    finally:
        gateway_db.close()

    # Simulate a pre-policy database so the next opener is allowed to translate
    # one legacy process-local setting into shared DB policy.
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("DELETE FROM state_meta WHERE key = 'trigram_fts_policy'")
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(
        hermes_config,
        "load_config_readonly",
        lambda: {"sessions": {"trigram_fts": False}},
    )
    monkeypatch.setenv("HERMES_DISABLE_FTS_TRIGRAM", "1")
    desktop_db = SessionDB(db_path=db_path)
    try:
        assert desktop_db.get_meta("trigram_fts_policy") == "disabled"
        assert _table_exists(db_path, "messages_fts_trigram")
        assert not _trigger_exists(db_path, "messages_fts_trigram_insert")
    finally:
        desktop_db.close()

    # A later gateway process with the opposite env/config must honor persisted
    # DB policy and must not recreate trigram triggers or tear down the table.
    monkeypatch.setattr(
        hermes_config,
        "load_config_readonly",
        lambda: {"sessions": {"trigram_fts": True}},
    )
    monkeypatch.delenv("HERMES_DISABLE_FTS_TRIGRAM", raising=False)
    reopened = SessionDB(db_path=db_path)
    try:
        assert reopened.get_meta("trigram_fts_policy") == "disabled"
        assert reopened._trigram_available is False
        assert _table_exists(db_path, "messages_fts_trigram")
        assert not _trigger_exists(db_path, "messages_fts_trigram_insert")
    finally:
        reopened.close()
