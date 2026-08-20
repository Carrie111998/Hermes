"""Tests for the CLI incognito mode contract."""

import hashlib
from unittest.mock import MagicMock

from hermes_cli._parser import build_top_level_parser
from run_agent import AIAgent


class _FakeOpenAI:
    def __init__(self, **kwargs):
        self.api_key = kwargs.get("api_key", "test")
        self.base_url = kwargs.get("base_url", "http://test")

    def close(self):
        pass


def _make_agent(monkeypatch, **kwargs):
    monkeypatch.setattr("run_agent.get_tool_definitions", lambda **_: [])
    monkeypatch.setattr("run_agent.check_toolset_requirements", lambda: {})
    monkeypatch.setattr("run_agent.OpenAI", _FakeOpenAI)
    return AIAgent(
        api_key="test-key",
        base_url="http://test",
        provider="openrouter",
        api_mode="chat_completions",
        max_iterations=1,
        quiet_mode=True,
        skip_context_files=True,
        **kwargs,
    )


def test_incognito_flag_is_available_before_and_after_chat_subcommand():
    parser, _subparsers, _chat_parser = build_top_level_parser()

    assert parser.parse_args(["--incognito"]).incognito is True
    assert parser.parse_args(["chat", "--incognito"]).incognito is True


def test_incognito_help_describes_memory_and_session_isolation():
    parser, _subparsers, chat_parser = build_top_level_parser()
    expected = (
        "Run in incognito mode: memory is not read or written, and the "
        "session is not persisted."
    )

    help_text = " ".join(
        f"{parser.format_help()} {chat_parser.format_help()}".split()
    )
    assert " ".join(expected.split()) in help_text


def test_incognito_disables_memory_store_and_session_persistence(monkeypatch, tmp_path):
    hermes_home = tmp_path / "hm"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    agent = _make_agent(
        monkeypatch,
        incognito=True,
        skip_memory=True,
        enabled_toolsets=["memory"],
    )

    assert agent.incognito is True
    assert agent._memory_store is None
    assert agent._memory_manager is None
    assert agent._persist_disabled is True
    assert "Incognito mode is active" in agent._build_system_prompt("test")

    from hermes_state import SessionDB

    session_db = SessionDB(db_path=hermes_home / "state.db")
    agent._session_db = session_db
    agent._session_db_created = False
    agent._save_session_log = MagicMock()
    agent._persist_session([{"role": "user", "content": "temporary"}], [])
    agent._ensure_db_session()
    agent._flush_messages_to_session_db([], [])

    agent._save_session_log.assert_not_called()
    assert session_db.list_sessions_rich(limit=10) == []
    assert not (hermes_home / "sessions" / f"{agent.session_id}.jsonl").exists()
    session_db.close()


def test_incognito_cli_close_removes_any_empty_session_row(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hm"))

    from hermes_state import SessionDB
    from cli import HermesCLI

    db = SessionDB(db_path=tmp_path / "hm" / "state.db")
    db.create_session("incognito-row", source="cli")
    cli = HermesCLI.__new__(HermesCLI)
    cli.incognito = True
    cli.session_id = "incognito-row"
    cli._session_db = db

    cli._persist_active_session_before_close()

    assert db.list_sessions_rich(limit=10) == []
    db.close()


def test_incognito_cli_session_does_not_open_or_modify_state_db(monkeypatch, tmp_path):
    """An incognito CLI lifecycle must not touch SQLite at all.

    The mtime assertion is intentional: SQLite can modify sidecar or database
    metadata without changing the logical contents, and strict incognito mode
    must not silently allow that kind of store access.
    """
    hermes_home = tmp_path / "hm"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    from hermes_state import SessionDB

    seed_db = SessionDB(db_path=hermes_home / "state.db")
    seed_db.create_session("existing", source="cli")
    seed_db.close()

    tracked_paths = [
        hermes_home / "state.db",
        hermes_home / "state.db-wal",
        hermes_home / "state.db-shm",
    ]

    def snapshot(path):
        if not path.exists():
            return None
        return (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)

    before = {path: snapshot(path) for path in tracked_paths}

    session_db_factory = MagicMock(name="SessionDB")
    monkeypatch.setattr("hermes_state.SessionDB", session_db_factory)

    from cli import HermesCLI
    from hermes_cli import mcp_startup

    cli = HermesCLI(incognito=True, compact=True)
    cli._install_tool_callbacks = lambda: None
    cli._ensure_tirith_security = lambda: None
    cli._ensure_runtime_credentials = lambda: True
    monkeypatch.setattr(mcp_startup, "ensure_mcp_discovery_before_agent_build", lambda **_: None)
    agent = MagicMock()
    agent.run_conversation.return_value = {
        "final_response": "temporary response",
        "messages": [],
        "api_calls": 1,
        "completed": True,
    }
    agent_factory = MagicMock(return_value=agent)
    monkeypatch.setattr("cli.AIAgent", agent_factory)

    assert cli.chat("temporary prompt") == "temporary response"
    cli._persist_active_session_before_close()

    session_db_factory.assert_not_called()
    assert agent_factory.call_args.kwargs["session_db"] is None
    after = {path: snapshot(path) for path in tracked_paths}
    assert after == before
