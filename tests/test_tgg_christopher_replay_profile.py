from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import pytest

from scripts.tgg_christopher_hermes_replay import (
    _resolve_replay_profile,
    _validate_replay_args,
)


def _args(db_path: Path, **overrides):
    values = {
        "profile": "tgg-local-gpt54-mini-gemini-vision",
        "model": None,
        "vision_provider": None,
        "vision_model": None,
        "vision_concurrency": None,
        "debounce_seconds": None,
        "rotate_session_every_turns": None,
        "business_base_url": None,
        "no_local_operator_backend": False,
        "db": str(db_path),
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def _sqlite_db(path: Path) -> Path:
    with sqlite3.connect(path) as conn:
        conn.execute("create table smoke (id integer primary key)")
        conn.commit()
    return path


def test_replay_profile_defaults_to_safe_tgg_local_contract(tmp_path):
    profile = _resolve_replay_profile(_args(_sqlite_db(tmp_path / "tgg.db")))

    assert profile.model == "gpt-5.4-mini"
    assert profile.vision_provider == "gemini"
    assert profile.vision_model == "gemini-3.1-flash-lite"
    assert profile.vision_concurrency == 8
    assert profile.business_mode == "copied-db-local-operator"
    assert profile.debounce_seconds == 300


def test_replay_preflight_rejects_prod_business_url(tmp_path):
    db_path = _sqlite_db(tmp_path / "tgg.db")

    with pytest.raises(SystemExit, match="localhost"):
        _validate_replay_args(
            _args(db_path, business_base_url="https://systems.papercut-labs.com")
        )


def test_replay_preflight_rejects_sqlite_sidecars(tmp_path):
    db_path = _sqlite_db(tmp_path / "tgg.db")
    Path(str(db_path) + "-wal").touch()

    with pytest.raises(SystemExit, match="sidecars"):
        _validate_replay_args(_args(db_path))
