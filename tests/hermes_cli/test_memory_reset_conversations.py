from __future__ import annotations

import argparse
from argparse import Namespace

from hermes_cli.memory_reset import cmd_memory_reset
from hermes_cli.subcommands.memory import build_memory_parser
from hermes_state import SessionDB


def _seed_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    memories = home / "memories"
    memories.mkdir(parents=True)
    (memories / "MEMORY.md").write_text("remember this", encoding="utf-8")
    (memories / "USER.md").write_text("user profile", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))

    db = SessionDB(home / "state.db")
    db.create_session("session-1", "cli")
    db.append_message("session-1", "user", "hello")
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
    db.close()
    return home


def _assert_conversations_cleared_and_state_preserved(home):
    db = SessionDB(home / "state.db")
    try:
        assert db.session_count(include_archived=True) == 0
        assert db.message_count() == 0
        assert db.get_meta("memory-reset-preservation") == "keep"
        assert db.load_gateway_routing_entries(scope="test-scope") == {
            "route-1": '{"session_id":"session-1"}'
        }
    finally:
        db.close()


def test_memory_reset_parser_routes_conversation_targets_to_safe_handler():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")

    def legacy_handler(_args):
        raise AssertionError("legacy cmd_memory handler should not run for reset")

    build_memory_parser(subparsers, cmd_memory=legacy_handler)

    conversations = parser.parse_args(
        ["memory", "reset", "--target", "conversations", "--yes"]
    )
    everything = parser.parse_args(
        ["memory", "reset", "--target", "everything", "--yes"]
    )

    assert conversations.target == "conversations"
    assert everything.target == "everything"
    assert conversations.func is not legacy_handler
    assert everything.func is conversations.func


def test_conversations_target_preserves_memory_files_and_unrelated_state(
    tmp_path, monkeypatch
):
    home = _seed_home(tmp_path, monkeypatch)

    result = cmd_memory_reset(Namespace(target="conversations", yes=True))

    assert result == 0
    assert (home / "memories" / "MEMORY.md").is_file()
    assert (home / "memories" / "USER.md").is_file()
    _assert_conversations_cleared_and_state_preserved(home)


def test_everything_target_clears_files_and_conversations(tmp_path, monkeypatch):
    home = _seed_home(tmp_path, monkeypatch)

    result = cmd_memory_reset(Namespace(target="everything", yes=True))

    assert result == 0
    assert not (home / "memories" / "MEMORY.md").exists()
    assert not (home / "memories" / "USER.md").exists()
    _assert_conversations_cleared_and_state_preserved(home)


def test_confirmation_denied_leaves_everything_untouched(
    tmp_path, monkeypatch
):
    home = _seed_home(tmp_path, monkeypatch)
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
