from __future__ import annotations

import argparse
import sqlite3
from argparse import Namespace

import hermes_cli.memory_reset as memory_reset_module
from hermes_cli.memory_reset import cmd_memory_reset
from hermes_cli.subcommands.memory import build_memory_parser
from hermes_state import SessionDB


_SEARCH_NEEDLE = "memoryresetneedle"


def _fts_schema_objects(home) -> set[tuple[str, str]]:
    with sqlite3.connect(home / "state.db") as conn:
        return {
            (row[0], row[1])
            for row in conn.execute(
                "SELECT type, name FROM sqlite_master "
                "WHERE name LIKE 'messages_fts%' ORDER BY type, name"
            ).fetchall()
        }


def _seed_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    memories = home / "memories"
    sessions = home / "sessions"
    memories.mkdir(parents=True)
    sessions.mkdir()
    (memories / "MEMORY.md").write_text("remember this", encoding="utf-8")
    (memories / "USER.md").write_text("user profile", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))

    db = SessionDB(home / "state.db")
    db.create_session("session-1", "cli")
    db.append_message("session-1", "user", f"hello {_SEARCH_NEEDLE}")
    db.create_session("session-2", "telegram")
    db.append_message("session-2", "assistant", "world")
    db.set_session_archived("session-2", True)
    db.create_session(
        "session-child",
        "tool",
        parent_session_id="session-1",
    )
    db.append_message("session-child", "assistant", "delegated work")
    db.set_meta("memory-reset-preservation", "keep")
    db.save_gateway_routing_entry(
        "route-1",
        '{"session_id":"session-1"}',
        scope="test-scope",
    )
    assert db.search_messages(_SEARCH_NEEDLE)
    db.close()

    # Existing SessionDB deletion contracts remove these session-owned files.
    (sessions / "session-1.jsonl").write_text("transcript", encoding="utf-8")
    (sessions / "session-child.json").write_text("transcript", encoding="utf-8")
    (sessions / "request_dump_session-2_001.json").write_text(
        "request", encoding="utf-8"
    )
    (sessions / "unrelated.jsonl").write_text("keep", encoding="utf-8")

    # A sentinel platform table proves reset does not repeat the old
    # sqlite_master loop that emptied every user table.
    with sqlite3.connect(home / "state.db") as conn:
        conn.execute(
            "CREATE TABLE platform_state_sentinel "
            "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO platform_state_sentinel(key, value) VALUES (?, ?)",
            ("topic-1", "keep"),
        )

    return home, _fts_schema_objects(home)


def _assert_conversations_cleared_and_state_preserved(home, fts_objects):
    db = SessionDB(home / "state.db")
    try:
        assert db.session_count(include_archived=True) == 0
        assert db.message_count() == 0
        assert not db.search_messages(_SEARCH_NEEDLE)
        assert db.get_meta("memory-reset-preservation") == "keep"
        assert db.load_gateway_routing_entries(scope="test-scope") == {
            "route-1": '{"session_id":"session-1"}'
        }
    finally:
        db.close()

    with sqlite3.connect(home / "state.db") as conn:
        assert conn.execute(
            "SELECT value FROM platform_state_sentinel WHERE key = ?",
            ("topic-1",),
        ).fetchone() == ("keep",)

    assert _fts_schema_objects(home) == fts_objects
    assert not (home / "sessions" / "session-1.jsonl").exists()
    assert not (home / "sessions" / "session-child.json").exists()
    assert not (home / "sessions" / "request_dump_session-2_001.json").exists()
    assert (home / "sessions" / "unrelated.jsonl").read_text(
        encoding="utf-8"
    ) == "keep"


def test_memory_reset_parser_invokes_exactly_one_handler(monkeypatch):
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    legacy_calls: list[str] = []
    safe_calls: list[str] = []

    def legacy_handler(args):
        legacy_calls.append(args.target)
        return 0

    def safe_handler(args):
        safe_calls.append(args.target)
        return 0

    monkeypatch.setattr(memory_reset_module, "cmd_memory_reset", safe_handler)
    build_memory_parser(subparsers, cmd_memory=legacy_handler)

    legacy_args = parser.parse_args(["memory", "reset", "--target", "all"])
    conversation_args = parser.parse_args(
        ["memory", "reset", "--target", "conversations", "--yes"]
    )

    assert legacy_args.func(legacy_args) == 0
    assert legacy_calls == ["all"]
    assert safe_calls == []

    assert conversation_args.func(conversation_args) == 0
    assert legacy_calls == ["all"]
    assert safe_calls == ["conversations"]


def test_conversations_target_preserves_memory_files_and_unrelated_state(
    tmp_path, monkeypatch
):
    home, fts_objects = _seed_home(tmp_path, monkeypatch)

    result = cmd_memory_reset(Namespace(target="conversations", yes=True))

    assert result == 0
    assert (home / "memories" / "MEMORY.md").is_file()
    assert (home / "memories" / "USER.md").is_file()
    _assert_conversations_cleared_and_state_preserved(home, fts_objects)


def test_everything_target_clears_files_and_conversations(tmp_path, monkeypatch):
    home, fts_objects = _seed_home(tmp_path, monkeypatch)

    result = cmd_memory_reset(Namespace(target="everything", yes=True))

    assert result == 0
    assert not (home / "memories" / "MEMORY.md").exists()
    assert not (home / "memories" / "USER.md").exists()
    _assert_conversations_cleared_and_state_preserved(home, fts_objects)


def test_running_gateway_blocks_reset_without_touching_state(tmp_path, monkeypatch):
    home, _fts_objects = _seed_home(tmp_path, monkeypatch)
    monkeypatch.setattr(
        memory_reset_module,
        "_get_running_gateway_pid",
        lambda _hermes_home: 4242,
    )

    result = cmd_memory_reset(Namespace(target="everything", yes=True))

    assert result == 1
    assert (home / "memories" / "MEMORY.md").is_file()
    assert (home / "memories" / "USER.md").is_file()
    db = SessionDB(home / "state.db")
    try:
        assert db.session_count(include_archived=True) == 3
        assert db.message_count() == 3
        assert db.search_messages(_SEARCH_NEEDLE)
    finally:
        db.close()


def test_confirmation_denied_leaves_everything_untouched(tmp_path, monkeypatch):
    home, _fts_objects = _seed_home(tmp_path, monkeypatch)
    monkeypatch.setattr("builtins.input", lambda _prompt: "no")

    result = cmd_memory_reset(Namespace(target="everything", yes=False))

    assert result == 0
    assert (home / "memories" / "MEMORY.md").is_file()
    assert (home / "memories" / "USER.md").is_file()

    db = SessionDB(home / "state.db")
    try:
        assert db.session_count(include_archived=True) == 3
        assert db.message_count() == 3
        assert db.get_meta("memory-reset-preservation") == "keep"
        assert db.search_messages(_SEARCH_NEEDLE)
    finally:
        db.close()


def test_conversations_target_does_not_create_an_empty_database(
    tmp_path, monkeypatch
):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    result = cmd_memory_reset(Namespace(target="conversations", yes=True))

    assert result == 0
    assert not (home / "state.db").exists()


def test_direct_handler_rejects_legacy_target_without_side_effects(
    tmp_path, monkeypatch
):
    home, _fts_objects = _seed_home(tmp_path, monkeypatch)

    result = cmd_memory_reset(Namespace(target="all", yes=True))

    assert result == 2
    assert (home / "memories" / "MEMORY.md").is_file()
    db = SessionDB(home / "state.db")
    try:
        assert db.session_count(include_archived=True) == 3
        assert db.message_count() == 3
    finally:
        db.close()
