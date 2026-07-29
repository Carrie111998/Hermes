"""Regression tests: /personality stages a pivot note for the next turn.

``/personality <name>`` writes ``config.yaml`` and sets
``_ephemeral_system_prompt``, but with a populated transcript the model
imitates the style of its own prior assistant turns and ignores the new
system prompt for one more turn.  The handler therefore also stages a note
under ``_pending_personality_notes[session_key]``, which
``gateway/run.py`` prepends to the next user message (same mechanism as
``_pending_model_notes`` for ``/model``).

These lock in:

  * the note is staged, keyed by session, for both set and clear
  * ``_ephemeral_system_prompt`` still tracks the selected personality
  * a session boundary (/new) drops the staged note for that session only,
    via ``_CONVERSATION_SCOPED_STATE``
"""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionEntry, SessionSource, build_session_key


PERSONALITIES = {
    "chadwick": "You are a loud gym bro. Call the user Champ.",
    "catgirl": "You are a cheerful cat girl. Use nya~ liberally.",
}


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="dm",
    )


def _make_event(text: str) -> MessageEvent:
    return MessageEvent(text=text, source=_make_source(), message_id="m1")


def _make_runner():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")}
    )
    adapter = MagicMock()
    adapter.send = AsyncMock()
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
    runner._session_model_overrides = {}
    runner._session_reasoning_overrides = {}
    runner._pending_model_notes = {}
    runner._pending_personality_notes = {}
    runner._ephemeral_system_prompt = ""
    runner._background_tasks = set()

    session_key = build_session_key(_make_source())
    session_entry = SessionEntry(
        session_key=session_key,
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = session_entry
    runner.session_store.reset_session.return_value = session_entry
    runner.session_store._entries = {session_key: session_entry}
    runner.session_store._generate_session_key.return_value = session_key
    runner._running_agents = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._session_db = None
    runner._agent_cache_lock = None  # disables _evict_cached_agent lock path
    runner._is_user_authorized = lambda _source: True
    runner._format_session_info = lambda: ""
    return runner


@pytest.fixture
def personality_config(monkeypatch, tmp_path):
    """Serve a config with personalities and swallow the config.yaml write.

    ``_handle_personality_command`` imports ``_hermes_home`` and
    ``_load_gateway_config`` from ``gateway.run`` at call time and writes via
    ``atomic_config_write``.  Without patching all three the test would
    overwrite the developer's real ``~/.hermes/config.yaml``.
    """
    config = {"agent": {"personalities": dict(PERSONALITIES)}}
    written = {}

    monkeypatch.setattr("gateway.run._load_gateway_config", lambda: config)
    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    monkeypatch.setattr(
        "gateway.slash_commands.atomic_config_write",
        lambda path, cfg: written.update({"path": path, "cfg": cfg}),
    )
    return SimpleNamespace(config=config, written=written)


@pytest.mark.asyncio
async def test_personality_set_stages_pivot_note(personality_config):
    """/personality <name> stages a note for that session's next message."""
    runner = _make_runner()
    session_key = build_session_key(_make_source())

    await runner._handle_personality_command(_make_event("/personality chadwick"))

    assert session_key in runner._pending_personality_notes
    note = runner._pending_personality_notes[session_key]
    assert "chadwick" in note
    assert PERSONALITIES["chadwick"] in note
    assert runner._ephemeral_system_prompt == PERSONALITIES["chadwick"]


@pytest.mark.asyncio
async def test_second_switch_replaces_the_staged_note(personality_config):
    """Swapping again before the note is consumed must not stack notes.

    This is the reported failure: the second and later swaps in one process
    were the ones that came back in the previous persona's voice.
    """
    runner = _make_runner()
    session_key = build_session_key(_make_source())

    await runner._handle_personality_command(_make_event("/personality chadwick"))
    await runner._handle_personality_command(_make_event("/personality catgirl"))

    note = runner._pending_personality_notes[session_key]
    assert PERSONALITIES["catgirl"] in note
    assert PERSONALITIES["chadwick"] not in note
    assert runner._ephemeral_system_prompt == PERSONALITIES["catgirl"]


@pytest.mark.asyncio
async def test_personality_none_stages_a_clear_note(personality_config):
    """/personality none must also pivot — not silently keep the old voice."""
    runner = _make_runner()
    session_key = build_session_key(_make_source())

    await runner._handle_personality_command(_make_event("/personality chadwick"))
    await runner._handle_personality_command(_make_event("/personality none"))

    assert runner._ephemeral_system_prompt == ""
    note = runner._pending_personality_notes[session_key]
    assert PERSONALITIES["chadwick"] not in note
    assert "default" in note.lower()


@pytest.mark.asyncio
async def test_unknown_personality_stages_nothing(personality_config):
    """An unrecognised name must not leave a note behind."""
    runner = _make_runner()
    session_key = build_session_key(_make_source())

    await runner._handle_personality_command(_make_event("/personality nope"))

    assert session_key not in runner._pending_personality_notes


@pytest.mark.asyncio
async def test_new_command_clears_pending_personality_note():
    """A staged-but-unconsumed note must not survive a session boundary."""
    runner = _make_runner()
    session_key = build_session_key(_make_source())
    runner._pending_personality_notes[session_key] = "[Note: now chadwick.]"

    await runner._handle_reset_command(_make_event("/new"))

    assert session_key not in runner._pending_personality_notes


@pytest.mark.asyncio
async def test_new_command_only_clears_own_session_note():
    """/new must leave other sessions' staged notes alone."""
    runner = _make_runner()
    session_key = build_session_key(_make_source())
    other_key = "other_session_key"
    runner._pending_personality_notes[session_key] = "[Note: now chadwick.]"
    runner._pending_personality_notes[other_key] = "[Note: now catgirl.]"

    await runner._handle_reset_command(_make_event("/new"))

    assert session_key not in runner._pending_personality_notes
    assert other_key in runner._pending_personality_notes
