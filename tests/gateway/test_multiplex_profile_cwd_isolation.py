from pathlib import Path
from types import SimpleNamespace

import yaml

from agent.prompt_builder import build_context_files_prompt
from agent.runtime_cwd import resolve_agent_cwd, resolve_context_cwd
from gateway.config import Platform
from gateway.run import GatewayRunner


def _runner(profile_home: Path) -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    runner.config = SimpleNamespace(multiplex_profiles=True)
    runner.adapters = {}
    runner._resolve_profile_home_for_source = lambda _source: profile_home
    return runner


def _context(profile: str = "polymarket"):
    source = SimpleNamespace(
        platform=Platform.TELEGRAM,
        chat_id="chat",
        chat_type=None,
        chat_name="",
        thread_id=None,
        user_id="user",
        user_id_alt=None,
        user_name="",
        scope_id=None,
        message_id="message",
        profile=profile,
    )
    return SimpleNamespace(source=source, session_key="agent:polymarket:telegram:chat")


def test_multiplex_turn_uses_routed_profile_cwd_and_project_rules(tmp_path, monkeypatch):
    foreign = tmp_path / "general"
    foreign.mkdir()
    (foreign / "AGENTS.md").write_text("FOREIGN_GENERAL_CONTEXT", encoding="utf-8")
    monkeypatch.setenv("TERMINAL_CWD", str(foreign))

    project = tmp_path / "polimarket"
    project.mkdir()
    (project / ".git").mkdir()
    (project / ".hermes.md").write_text("POLYMARKET_PROJECT_CONTEXT", encoding="utf-8")

    profile_home = tmp_path / "profiles" / "polymarket"
    profile_home.mkdir(parents=True)
    (profile_home / "config.yaml").write_text(
        yaml.safe_dump({"terminal": {"backend": "local", "cwd": str(project)}}),
        encoding="utf-8",
    )

    runner = _runner(profile_home)
    tokens = runner._set_session_env(_context())
    try:
        assert resolve_context_cwd() == project
        assert resolve_agent_cwd() == project
        prompt = build_context_files_prompt(cwd=str(resolve_context_cwd()), skip_soul=True)
        assert "POLYMARKET_PROJECT_CONTEXT" in prompt
        assert "FOREIGN_GENERAL_CONTEXT" not in prompt
    finally:
        runner._clear_session_env(tokens)


def test_multiplex_missing_profile_cwd_never_falls_back_to_gateway_owner(tmp_path, monkeypatch):
    foreign = tmp_path / "general"
    foreign.mkdir()
    monkeypatch.setenv("TERMINAL_CWD", str(foreign))

    profile_home = tmp_path / "profiles" / "polymarket"
    profile_home.mkdir(parents=True)
    (profile_home / "config.yaml").write_text("terminal: {}\n", encoding="utf-8")

    runner = _runner(profile_home)
    tokens = runner._set_session_env(_context())
    try:
        assert resolve_agent_cwd() == profile_home
        assert resolve_context_cwd() == profile_home
        assert resolve_agent_cwd() != foreign
    finally:
        runner._clear_session_env(tokens)
