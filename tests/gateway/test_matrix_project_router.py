"""Focused Matrix project-router vertical-slice coverage."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from agent.runtime_cwd import resolve_agent_cwd, resolve_context_cwd
from agent.prompt_builder import build_context_files_prompt
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType
from gateway.project_router import PROJECTS, active_project_path, project_path
from gateway.session import SessionContext, SessionEntry, SessionSource, build_session_key
from hermes_state import SessionDB


NEWMOON_PATH = PROJECTS["newmoon"]
FIVEHOURS_PATH = "/home/rle/projects/savefivehours"


def _source() -> SessionSource:
    return SessionSource(
        platform=Platform.MATRIX,
        user_id="matrix-user",
        chat_id="matrix-room",
        user_name="tester",
        chat_type="room",
    )


def _event(text: str) -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=_source(),
        message_id="message-1",
        internal=True,
    )


def _session_entry() -> SessionEntry:
    source = _source()
    return SessionEntry(
        session_key=build_session_key(source),
        session_id="matrix-session",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.MATRIX,
        chat_type="room",
        total_tokens=0,
    )


def _runner(tmp_path):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.MATRIX: PlatformConfig(enabled=True, token="***")}
    )
    adapter = MagicMock()
    adapter.send = AsyncMock()
    adapter._pending_messages = {}
    runner.adapters = {Platform.MATRIX: adapter}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(
        emit=AsyncMock(), emit_collect=AsyncMock(return_value=[]), loaded_hooks=False
    )
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = _session_entry()
    runner.session_store.load_transcript.return_value = []
    runner.session_store.has_any_sessions.return_value = True
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._queued_events = {}
    runner._session_db = SimpleNamespace(_db=SessionDB(db_path=tmp_path / "state.db"))
    runner._session_db._db.get_session_title = MagicMock(return_value=None)
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._show_reasoning = False
    runner._is_user_authorized = lambda _source: True
    runner._should_send_voice_reply = lambda *_args, **_kwargs: False
    runner._send_voice_reply = AsyncMock()
    runner._capture_gateway_honcho_if_configured = lambda *args, **kwargs: None
    runner._emit_gateway_run_progress = AsyncMock()
    runner._update_prompt_pending = {}
    runner._busy_input_mode = "interrupt"
    runner._draining = False
    runner._session_run_generation = {}
    runner._session_sources = {}
    runner._pending_native_image_paths_by_session = {}
    runner._background_tasks = {}
    runner._background_task_counter = 0
    runner._session_model_overrides = {}
    runner._pending_model_notes = {}
    runner._service_tier = None
    runner._fast_mode_by_session = {}
    runner._goal_state_by_session = {}
    runner._goal_runs_in_progress = set()
    runner._goal_queued_by_session = set()
    runner._is_telegram_topic_root_lobby = lambda _source: False
    runner._should_send_telegram_lobby_reminder = lambda _source: False
    runner._check_slash_access = lambda _source, _command: None
    runner._begin_session_run_generation = lambda _key: 1
    runner._release_running_agent_state = lambda key: runner._running_agents.pop(key, None)
    runner._evict_cached_agent = MagicMock()
    return runner


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("key", "path"),
    [("newmoon", NEWMOON_PATH), ("fivehours", FIVEHOURS_PATH)],
)
async def test_project_selection_intercepts_persists_and_evicts_cached_agent(tmp_path, key, path):
    runner = _runner(tmp_path)
    session_key = build_session_key(_source())
    runner._handle_message_with_agent = AsyncMock()

    response = await runner._handle_message(_event(f"!project {key}"))

    assert response == f"Active project: {key} ({path})"
    assert runner._session_db._db.get_meta("matrix_project_router:" + session_key) == key
    assert active_project_path(runner._session_db._db, session_key) == project_path(key)
    runner._evict_cached_agent.assert_called_once_with(session_key)
    runner._handle_message_with_agent.assert_not_awaited()


@pytest.mark.parametrize(
    ("key", "path", "agents_text"),
    [
        ("newmoon", NEWMOON_PATH, "Authoritative context"),
        ("fivehours", FIVEHOURS_PATH, "Authoritative sources"),
    ],
)
def test_selected_matrix_session_binds_project_cwd_and_discovers_agents_md(
    tmp_path, key, path, agents_text
):
    runner = _runner(tmp_path)
    session_key = build_session_key(_source())
    runner._session_db._db.set_meta("matrix_project_router:" + session_key, key)
    context = SessionContext(
        source=_source(), connected_platforms=[], home_channels={}, session_key=session_key
    )

    tokens = runner._set_session_env(context)
    try:
        assert resolve_agent_cwd() == Path(path)
        assert resolve_context_cwd() == Path(path)
        prompt = build_context_files_prompt(cwd=str(resolve_context_cwd()), skip_soul=True)
        assert "# AGENTS.md" in prompt
        assert agents_text in prompt
    finally:
        runner._clear_session_env(tokens)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("first", "second"),
    [("newmoon", "fivehours"), ("fivehours", "newmoon")],
)
async def test_project_selection_switches_active_project_and_evicts_each_time(tmp_path, first, second):
    runner = _runner(tmp_path)
    session_key = build_session_key(_source())
    runner._handle_message_with_agent = AsyncMock()

    await runner._handle_message(_event(f"!project {first}"))
    response = await runner._handle_message(_event(f"!project {second}"))

    assert response == f"Active project: {second} ({PROJECTS[second]})"
    assert runner._session_db._db.get_meta("matrix_project_router:" + session_key) == second
    assert active_project_path(runner._session_db._db, session_key) == project_path(second)
    assert runner._evict_cached_agent.call_args_list == [call(session_key), call(session_key)]
    runner._handle_message_with_agent.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("key", "path"),
    [("newmoon", NEWMOON_PATH), ("fivehours", FIVEHOURS_PATH)],
)
async def test_project_status_reports_active_project(tmp_path, key, path):
    runner = _runner(tmp_path)
    runner._handle_message_with_agent = AsyncMock()

    await runner._handle_message(_event(f"!project {key}"))
    runner._evict_cached_agent.reset_mock()
    response = await runner._handle_message(_event("!project status"))

    assert response == f"Active project: {key}\nPath: {path}"
    runner._evict_cached_agent.assert_not_called()
    runner._handle_message_with_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_project_status_reports_none_when_no_project_is_active(tmp_path):
    runner = _runner(tmp_path)
    runner._handle_message_with_agent = AsyncMock()

    response = await runner._handle_message(_event("!project status"))

    assert response == "Active project: none"
    runner._evict_cached_agent.assert_not_called()
    runner._handle_message_with_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_project_clear_removes_state_evicts_agent_and_unbinds_follow_up(tmp_path):
    runner = _runner(tmp_path)
    session_key = build_session_key(_source())
    expected = {"final_response": "ordinary dispatch", "messages": []}
    runner._handle_message_with_agent = AsyncMock(return_value=expected)

    await runner._handle_message(_event("!project fivehours"))
    context = SessionContext(
        source=_source(), connected_platforms=[], home_channels={}, session_key=session_key
    )
    tokens = runner._set_session_env(context)
    assert resolve_context_cwd() == Path(FIVEHOURS_PATH)
    runner._evict_cached_agent.reset_mock()
    response = await runner._handle_message(_event("!project clear"))

    assert response == "Project context cleared."
    assert runner._session_db._db.get_meta("matrix_project_router:" + session_key) is None
    assert active_project_path(runner._session_db._db, session_key) is None
    runner._evict_cached_agent.assert_called_once_with(session_key)
    try:
        assert resolve_context_cwd() is None
        assert resolve_agent_cwd() not in {Path(NEWMOON_PATH), Path(FIVEHOURS_PATH)}
        prompt = build_context_files_prompt(cwd=None, skip_soul=True)
        assert "Authoritative context" not in prompt
        assert "Authoritative sources" not in prompt
    finally:
        runner._clear_session_env(tokens)

    result = await runner._handle_message(_event("ordinary Matrix message"))
    assert result == expected
    runner._handle_message_with_agent.assert_awaited_once()


@pytest.mark.asyncio
async def test_project_selection_works_after_clear(tmp_path):
    runner = _runner(tmp_path)
    session_key = build_session_key(_source())
    runner._handle_message_with_agent = AsyncMock()

    await runner._handle_message(_event("!project newmoon"))
    await runner._handle_message(_event("!project clear"))
    response = await runner._handle_message(_event("!project fivehours"))

    assert response == f"Active project: fivehours ({FIVEHOURS_PATH})"
    assert runner._session_db._db.get_meta("matrix_project_router:" + session_key) == "fivehours"
    assert runner._evict_cached_agent.call_args_list == [
        call(session_key),
        call(session_key),
        call(session_key),
    ]
    runner._handle_message_with_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_project_does_not_dispatch_and_lists_valid_keys(tmp_path):
    runner = _runner(tmp_path)
    runner._handle_message_with_agent = AsyncMock()

    response = await runner._handle_message(_event("!project unknown"))

    assert response == "Project selection failed: unknown project 'unknown'. Valid projects: fivehours, newmoon"
    runner._evict_cached_agent.assert_not_called()
    runner._handle_message_with_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_unbound_matrix_session_dispatches_normally(tmp_path):
    runner = _runner(tmp_path)
    expected = {"final_response": "ordinary dispatch", "messages": []}
    runner._handle_message_with_agent = AsyncMock(return_value=expected)

    result = await runner._handle_message(_event("ordinary Matrix message"))

    assert result == expected
    runner._handle_message_with_agent.assert_awaited_once()
