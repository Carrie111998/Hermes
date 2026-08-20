"""Tests for the CLI incognito mode contract."""

import hashlib
import sys
from unittest.mock import MagicMock

import pytest

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
    agent._memory_manager = None
    agent.commit_memory_session = MagicMock()
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


@pytest.mark.parametrize(
    "argv",
    [
        ["hermes", "--incognito", "-z", "temporary prompt"],
        ["hermes", "chat", "--incognito", "-q", "temporary prompt"],
    ],
    ids=["top-level-oneshot", "chat-subcommand"],
)
def test_real_cli_entry_propagates_incognito_to_execution_path(
    monkeypatch, tmp_path, argv
):
    """The supported command forms must preserve incognito at the real entrypoint."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hm"))
    monkeypatch.setattr(sys, "argv", argv)

    from hermes_cli import main as main_mod

    monkeypatch.setattr(main_mod, "_set_process_title", lambda: None)
    monkeypatch.setattr(main_mod, "_advertise_agent_env", lambda: None)
    monkeypatch.setattr(main_mod, "_cleanup_quarantined_exes", lambda: None)
    monkeypatch.setattr(main_mod, "_sweep_stale_bytecode_if_checkout_changed", lambda: None)
    monkeypatch.setattr(main_mod, "_recover_from_interrupted_install", lambda: None)
    monkeypatch.setattr(main_mod, "_try_termux_fast_tui_launch", lambda: False)
    monkeypatch.setattr(main_mod, "_try_termux_fast_cli_launch", lambda: False)
    monkeypatch.setattr(main_mod, "_try_fast_chat_launch", lambda: False)
    monkeypatch.setattr(main_mod, "_prepare_agent_startup", lambda _args: None)
    monkeypatch.setattr(main_mod, "_confirm_startup_expensive_model_override", lambda _args: None)

    captured = {}

    if "chat" in argv:
        monkeypatch.setattr(main_mod, "_has_any_provider_configured", lambda: True)
        monkeypatch.setattr(main_mod, "_sync_bundled_skills_for_startup", lambda: None)
        monkeypatch.setattr(main_mod, "_termux_should_prefetch_update_check", lambda: False)

        def fake_cli_main(**kwargs):
            captured.update(kwargs)

        monkeypatch.setattr("cli.main", fake_cli_main)
        main_mod.main()
        assert captured["query"] == "temporary prompt"
    else:
        def fake_run_and_exit(prompt, **kwargs):
            captured["prompt"] = prompt
            captured.update(kwargs)
            raise SystemExit(0)

        monkeypatch.setattr(main_mod, "_run_and_exit_oneshot", fake_run_and_exit)
        with pytest.raises(SystemExit) as exc_info:
            main_mod.main()
        assert exc_info.value.code == 0
        assert captured["prompt"] == "temporary prompt"

    assert captured["incognito"] is True


@pytest.mark.parametrize(
    "argv",
    [
        ["hermes", "--incognito", "--resume", "session-id"],
        ["hermes", "--incognito", "--continue"],
        ["hermes", "--incognito", "--resume", "latest"],
    ],
    ids=["resume-id", "continue", "resume-latest"],
)
def test_real_cli_entry_rejects_incognito_resume_without_opening_db(
    monkeypatch, tmp_path, capsys, argv
):
    """Resume rejection happens before any session-store access."""
    hermes_home = tmp_path / "hm"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(sys, "argv", argv)

    from hermes_state import SessionDB

    seed_db = SessionDB(db_path=hermes_home / "state.db")
    seed_db.create_session("session-id", source="cli")
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

    from hermes_cli import main as main_mod

    monkeypatch.setattr(main_mod, "_set_process_title", lambda: None)
    monkeypatch.setattr(main_mod, "_advertise_agent_env", lambda: None)
    monkeypatch.setattr(main_mod, "_cleanup_quarantined_exes", lambda: None)
    monkeypatch.setattr(main_mod, "_sweep_stale_bytecode_if_checkout_changed", lambda: None)
    monkeypatch.setattr(main_mod, "_recover_from_interrupted_install", lambda: None)
    monkeypatch.setattr(main_mod, "_try_termux_fast_tui_launch", lambda: False)
    monkeypatch.setattr(main_mod, "_try_termux_fast_cli_launch", lambda: False)
    monkeypatch.setattr(main_mod, "_try_fast_chat_launch", lambda: False)
    monkeypatch.setattr(main_mod, "_prepare_agent_startup", lambda _args: None)

    with pytest.raises(SystemExit) as exc_info:
        main_mod.main()

    assert exc_info.value.code == 1
    assert "incognito" in capsys.readouterr().err.lower()
    session_db_factory.assert_not_called()
    assert {path: snapshot(path) for path in tracked_paths} == before


def test_real_cli_chat_keeps_existing_storage_unchanged(monkeypatch, tmp_path):
    """A real `chat -q` CLI path does not persist an incognito turn."""
    hermes_home = tmp_path / "hm"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    memory_dir = hermes_home / "memory"
    memory_dir.mkdir(parents=True)
    memory_file = memory_dir / "notes.json"
    memory_file.write_text('{"baseline": true}\n', encoding="utf-8")

    from hermes_state import SessionDB

    seed_db = SessionDB(db_path=hermes_home / "state.db")
    seed_db.create_session("existing", source="cli")
    seed_db.set_session_title("existing", "baseline")
    seed_db.append_message("existing", role="user", content="baseline")
    seed_db.close()

    tracked_paths = [
        hermes_home / "state.db",
        hermes_home / "state.db-wal",
        hermes_home / "state.db-shm",
        memory_file,
    ]

    def snapshot(path):
        if not path.exists():
            return None
        return (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)

    before = {path: snapshot(path) for path in tracked_paths}

    from hermes_cli import main as main_mod

    parser, _subparsers, chat_parser = build_top_level_parser()
    chat_parser.set_defaults(func=main_mod.cmd_chat)
    args = parser.parse_args(
        ["chat", "--incognito", "-q", "temporary prompt", "--model", "test-model"]
    )

    monkeypatch.setattr(main_mod, "_resolve_use_tui", lambda _args: False)
    monkeypatch.setattr(main_mod, "_has_any_provider_configured", lambda: True)
    monkeypatch.setattr(main_mod, "_sync_bundled_skills_for_startup", lambda: None)
    monkeypatch.setattr(main_mod, "_termux_should_prefetch_update_check", lambda: False)
    monkeypatch.setattr(main_mod, "_prepare_agent_startup", lambda _args: None)
    monkeypatch.setattr(main_mod, "_confirm_startup_expensive_model_override", lambda _args: None)
    monkeypatch.setattr("cli.HermesCLI._ensure_runtime_credentials", lambda self: True)
    monkeypatch.setattr("cli.HermesCLI._claim_active_session", lambda self, *a, **k: True)
    monkeypatch.setattr("cli.HermesCLI._install_tool_callbacks", lambda self: None)
    monkeypatch.setattr("cli.HermesCLI._ensure_tirith_security", lambda self: None)
    monkeypatch.setattr(
        "hermes_cli.mcp_startup.ensure_mcp_discovery_before_agent_build",
        lambda **_kwargs: None,
    )

    agent = MagicMock()
    agent._memory_manager = None
    agent.commit_memory_session = MagicMock()
    agent.run_conversation.return_value = {
        "final_response": "temporary response",
        "messages": [],
        "api_calls": 1,
        "completed": True,
    }
    agent_factory = MagicMock(return_value=agent)
    monkeypatch.setattr("cli.AIAgent", agent_factory)

    main_mod.cmd_chat(args)

    assert agent_factory.call_args.kwargs["incognito"] is True
    assert agent_factory.call_args.kwargs["session_db"] is None
    assert {path: snapshot(path) for path in tracked_paths} == before
    sessions_dir = hermes_home / "sessions"
    assert not sessions_dir.exists() or not list(sessions_dir.glob("*"))
    agent.commit_memory_session.assert_not_called()
    assert getattr(agent, "_memory_manager", None) is None or not agent._memory_manager.mock_calls
