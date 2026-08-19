"""Tests for the CLI incognito mode contract."""

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
