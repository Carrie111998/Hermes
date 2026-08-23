"""Regression: multiplex profile_routes must load transcripts from the routed profile store.

Under multiplex + profile_routes, the primary platform adapter enters
``_handle_message`` with the default HERMES_HOME. Session transcripts for a
routed profile live in ``profiles/<name>/state.db``. If ``load_transcript``
runs before the routed-profile scope is installed, it reads the default-home
stub (often history=0/1) while the agent later writes under the work profile
— permanent per-message amnesia on routed free-response channels.

This is independent of slash-command guild_id routing. Normal messages hit
the same path.

Fix: ``_handle_message_with_agent`` installs the routed profile scope for the
whole agent-turn body (session resolve, load, hygiene, run), matching what
``_run_agent`` already did for the write side only.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.run import GatewayRunner, _profile_runtime_scope
from gateway.session import SessionSource
from hermes_constants import get_hermes_home, reset_hermes_home_override, set_hermes_home_override
from hermes_state import SessionDB


@pytest.fixture
def multiplex_homes(tmp_path, monkeypatch):
    import hermes_state

    root = tmp_path / "hermes"
    work = root / "profiles" / "work"
    root.mkdir(parents=True)
    work.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", hermes_state._IMPORT_DEFAULT_DB_PATH)
    return root, work


def test_default_load_misses_work_transcript_that_agent_writes(multiplex_homes):
    """Reproduce the dual-cabinet split without Discord."""
    root, work = multiplex_homes
    sid = "20260823_repro_session"

    # Agent write path: under work home (what _run_agent already did)
    tok = set_hermes_home_override(str(work))
    try:
        db_w = SessionDB()
        db_w.create_session(sid, "discord")
        db_w.append_message(sid, "user", "ping 192.168.1.149")
        db_w.append_message(sid, "assistant", "host is reachable")
        db_w.close()
    finally:
        reset_hermes_home_override(tok)

    # Broken load path: still under default home (pre-fix load_transcript)
    tok = set_hermes_home_override(str(root))
    try:
        db_d = SessionDB()
        # Stub row that routing might create in the default store
        db_d.create_session(sid, "discord")
        loaded_broken = db_d.get_messages_as_conversation(sid, repair_alternation=True)
        db_d.close()
    finally:
        reset_hermes_home_override(tok)

    # Fixed load path: under work home
    tok = set_hermes_home_override(str(work))
    try:
        db_w = SessionDB()
        loaded_fixed = db_w.get_messages_as_conversation(sid, repair_alternation=True)
        db_w.close()
    finally:
        reset_hermes_home_override(tok)

    assert len(loaded_broken) <= 1, "default store must not see the real transcript"
    assert len(loaded_fixed) >= 2
    assert any("192.168.1.149" in str(m.get("content", "")) for m in loaded_fixed)


def test_handle_message_with_agent_scopes_before_unscoped_body(multiplex_homes):
    """Wrapper must enter routed profile home before the unscoped body runs."""
    root, work = multiplex_homes
    seen_homes: list[Path] = []

    async def fake_unscoped(self, event, source, quick_key, run_generation):
        seen_homes.append(get_hermes_home())
        return "ok"

    cfg = GatewayConfig()
    cfg.multiplex_profiles = True
    runner = object.__new__(GatewayRunner)
    runner.config = cfg

    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="1539145500689702964",
        chat_type="group",
        user_id="354817597868605441",
        user_name="Jonathan",
        guild_id="1528411182514438296",
        profile="work",
    )

    with (
        patch.object(GatewayRunner, "_handle_message_with_agent_unscoped", fake_unscoped),
        patch.object(
            GatewayRunner,
            "_resolve_profile_home_for_source",
            return_value=work,
        ),
    ):
        import asyncio

        result = asyncio.get_event_loop().run_until_complete(
            GatewayRunner._handle_message_with_agent(
                runner, event=MagicMock(), source=source, _quick_key="k", run_generation=1,
            )
        )

    assert result == "ok"
    assert seen_homes == [work.resolve()]


def test_wrapper_is_independent_of_slash_guild_id_fix():
    """Guardrail: this fix must not live only in slash-command build_source."""
    src = inspect.getsource(GatewayRunner._handle_message_with_agent)
    assert "_profile_runtime_scope" in src
    assert "_handle_message_with_agent_unscoped" in src
    # Body does the load; wrapper only scopes then delegates.
    body = inspect.getsource(GatewayRunner._handle_message_with_agent_unscoped)
    assert "load_transcript" in body
