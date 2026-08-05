"""Regression test for Issue #79624: Gateway crashes during preflight compaction on restart.

Verifies:
1. Durable failure cooldowns survive startup initialization via bind_session_state.
2. Gateway _record_hygiene_cooldown helper properly persists failure cooldowns.
3. _handle_hygiene_worker_exception on GatewayRunner handles worker timeouts and runtime exceptions non-fatally
   (records failure cooldown and returns True), while re-raising system cancellation signals (KeyboardInterrupt, CancelledError, SystemExit).
4. Real end-to-end GatewayRunner session hygiene test driving the actual `except asyncio.TimeoutError:` branch with _cancelled=True,
   asserting graceful non-fatal continuation without UnboundLocalError (_compressed).
"""

import asyncio
from datetime import datetime
import importlib
import sys
import time
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.context_compressor import ContextCompressor


def test_durable_cooldown_survives_bind_session_state_startup():
    """Verify that binding session state restores active durable failure cooldowns."""
    compressor = ContextCompressor("gpt-4")
    mock_db = MagicMock()
    mock_db.get_compression_failure_cooldown.return_value = {
        "cooldown_until": 9999999999.0,
        "remaining_seconds": 600.0,
        "error": "Request timed out.",
    }

    compressor.bind_session_state(mock_db, "session_oversized_123")
    cooldown = compressor.get_active_compression_failure_cooldown(refresh=True)

    assert cooldown is not None
    assert cooldown["remaining_seconds"] > 0
    assert cooldown["error"] == "Request timed out."


def test_record_hygiene_cooldown_helper():
    """Verify gateway _record_hygiene_cooldown persists cooldown to DB."""
    from gateway.run import _record_hygiene_cooldown

    mock_gateway = MagicMock()
    mock_db = MagicMock()
    mock_session_store = MagicMock()
    mock_session_store._db = mock_db
    mock_gateway._session_db = mock_session_store

    _record_hygiene_cooldown(mock_gateway, "sess_79624", 600)
    mock_db.record_compression_failure_cooldown.assert_called_once()


def test_handle_hygiene_worker_exception_non_fatal_runtime_error():
    """Verify _handle_hygiene_worker_exception catches worker RuntimeError/Timeout non-fatally and persists cooldown."""
    from gateway.run import _handle_hygiene_worker_exception

    mock_gateway = MagicMock()
    mock_db = MagicMock()
    mock_session_store = MagicMock()
    mock_session_store._db = mock_db
    mock_gateway._session_db = mock_session_store
    mock_fence = MagicMock()

    worker_exc = RuntimeError("Request timed out during hygiene compaction")

    res = _handle_hygiene_worker_exception(
        gateway=mock_gateway,
        exc=worker_exc,
        session_id="sess_79624_timeout",
        cooldown_seconds=600.0,
        commit_fence=mock_fence,
    )

    assert res is True
    mock_fence.revoke_commit_admission.assert_called_once()
    mock_db.record_compression_failure_cooldown.assert_called_once()


def test_handle_hygiene_worker_exception_re_raises_system_cancellation():
    """Verify _handle_hygiene_worker_exception re-raises system cancellation signals (KeyboardInterrupt / CancelledError / SystemExit)."""
    from gateway.run import _handle_hygiene_worker_exception

    mock_gateway = MagicMock()

    with pytest.raises(asyncio.CancelledError):
        _handle_hygiene_worker_exception(
            gateway=mock_gateway,
            exc=asyncio.CancelledError(),
            session_id="sess_79624_cancelled",
            cooldown_seconds=600.0,
        )

    with pytest.raises(KeyboardInterrupt):
        _handle_hygiene_worker_exception(
            gateway=mock_gateway,
            exc=KeyboardInterrupt(),
            session_id="sess_79624_ki",
            cooldown_seconds=600.0,
        )


@pytest.mark.asyncio
async def test_session_hygiene_timeout_cancelled_branch_returns_gracefully(monkeypatch, tmp_path):
    """Real end-to-end regression test for Issue #79624: Drives the actual GatewayRunner session hygiene routine

    when worker times out with _cancelled=True. Asserts that the routine logs a warning, records failure cooldown,
    and returns non-fatally without raising UnboundLocalError (_compressed).
    """
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.platforms.base import MessageEvent
    from gateway.session import SessionEntry, SessionSource

    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    fake_db = MagicMock()
    fake_db.get_compression_failure_cooldown.return_value = None

    class TimeoutCompressAgent:
        def __init__(self, **kwargs):
            # Rotated session ID simulates continuation minting scenario
            self.session_id = "sess-rotated-child-79624"
            self._session_db = kwargs.get("session_db")
            self._last_compaction_in_place = False
            self.context_compressor = SimpleNamespace(
                bind_session_state=MagicMock(),
                _last_compress_aborted=False,
                _last_aux_model_failure_model=None,
            )
            self.shutdown_memory_provider = MagicMock()
            self.close = MagicMock()

        def _compress_context(self, messages, *_args, commit_fence=None, **_kwargs):
            time.sleep(0.3)
            return (messages, None)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = TimeoutCompressAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "compression:\n"
        "  enabled: true\n"
        "  hygiene_timeout_seconds: 0.01\n"
        "  hygiene_failure_cooldown_seconds: 120\n"
    )

    gateway_run = importlib.import_module("gateway.run")
    GatewayRunner = gateway_run.GatewayRunner

    class DummyAdapter:
        async def send(self, *args, **kwargs):
            pass

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="fake-token")}
    )
    runner.adapters = {Platform.TELEGRAM: DummyAdapter()}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)

    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = SessionEntry(
        session_key="agent:main:telegram:dm:79624",
        session_id="sess-79624-parent",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )

    def _make_msgs(count):
        msgs = []
        for i in range(count):
            role = "user" if i % 2 == 0 else "assistant"
            msgs.append({"role": role, "content": f"msg {i}" * 50})
        return msgs

    runner.session_store.load_transcript.return_value = _make_msgs(10)
    runner.session_store.has_any_sessions.return_value = True
    runner.session_store.rewrite_transcript = MagicMock()
    runner.session_store.append_to_transcript = MagicMock()
    runner._running_agents = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._session_db = SimpleNamespace(_db=fake_db)
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._run_agent = AsyncMock(
        return_value={
            "final_response": "ok_after_timeout",
            "messages": [],
            "tools": [],
            "history_offset": 0,
            "last_prompt_tokens": 0,
        }
    )

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"})
    monkeypatch.setattr(
        "agent.model_metadata.get_model_context_length",
        lambda *_args, **_kwargs: 100,
    )

    event = MessageEvent(
        text="hello issue 79624",
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="79624",
            chat_type="dm",
            user_id="79624",
        ),
        message_id="1",
    )

    res = await runner._handle_message(event)

    assert res == "ok_after_timeout"
    assert runner._run_agent.await_count == 1
    fake_db.record_compression_failure_cooldown.assert_called_once()
