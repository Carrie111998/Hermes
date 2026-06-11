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


# ── deployed-config-derived profile (config-drift killer) ─────────────────


def test_eval_profile_derives_base_from_deployed_config(tmp_path):
    """tgg-eval-gpt54-mini = deployed config + NAMED deltas only: main model
    under evaluation -> gpt-5.4-mini via OpenAI direct; vision KEEPS the
    deployed fanout (gemini / deployed vision model)."""
    import yaml

    from scripts.tgg_christopher_hermes_replay import TGG_CONFIG

    profile = _resolve_replay_profile(
        _args(_sqlite_db(tmp_path / "tgg.db"), profile="tgg-eval-gpt54-mini")
    )

    # Named deltas: the model under evaluation.
    assert profile.model == "gpt-5.4-mini"
    assert profile.main_provider == "openai-direct-primary"
    assert profile.transport == "codex_responses"

    # Inherited from the DEPLOYED config: the vision fanout. provider "main"
    # in the deployed auxiliary section resolves to the deployed main
    # provider (gemini), and the model comes from auxiliary.vision.model.
    deployed = yaml.safe_load(TGG_CONFIG.read_text(encoding="utf-8"))
    deployed_main_provider = deployed["model"]["provider"]
    deployed_vision = deployed["auxiliary"]["vision"]
    expected_vision_provider = (
        deployed_main_provider
        if deployed_vision.get("provider") == "main"
        else deployed_vision.get("provider")
    )
    assert profile.vision_enabled is True
    assert profile.vision_provider == expected_vision_provider == "gemini"
    assert profile.vision_model == deployed_vision["model"] == "gemini-3.1-flash-lite"

    # Harness-level safety values unchanged from the legacy contract.
    assert profile.business_mode == "copied-db-local-operator"
    assert profile.allow_prod_url is False
    assert profile.debounce_seconds == 300


def test_legacy_profiles_still_resolve(tmp_path):
    for name in (
        "tgg-local-gpt54-mini-gemini-vision",
        "tgg-local-gpt54-mini-native-vision",
        "tgg-local-gemini-live",
    ):
        profile = _resolve_replay_profile(
            _args(_sqlite_db(tmp_path / f"{name}.db"), profile=name)
        )
        assert profile.name == name


def test_bare_reaction_records_are_skipped_at_feed():
    from scripts.tgg_christopher_hermes_replay import (
        ReplayRecord,
        _is_bare_reaction_record,
    )

    def _rec(text, kind="text", has_media=False):
        return ReplayRecord(
            source_ref="r1", chat_jid="c@g.us", chat_name="c", sender_id="s",
            ts=1, sgt="2026-06-10 10:00:00", text=text, message_kind=kind,
            has_media=has_media, media_refs=[], quoted_text="",
            reply_to_source_ref="", raw_json={},
        )

    assert _is_bare_reaction_record(_rec("[reaction: 👍]")) is True
    assert _is_bare_reaction_record(_rec("[reaction: 👍]", kind="reaction")) is True
    assert _is_bare_reaction_record(_rec("", kind="reaction")) is True
    # Real content never skipped
    assert _is_bare_reaction_record(_rec("epoxy applied, done")) is False
    assert _is_bare_reaction_record(_rec("", kind="image", has_media=True)) is False
