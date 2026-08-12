"""Tests for the gateway max_concurrent_sessions active-session cap."""

import asyncio
import time
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner, _AGENT_PENDING_SENTINEL
from gateway.session import SessionSource, build_session_key
from gateway.status import read_runtime_status
from gateway.turn_lease import SessionTurnLeaseRegistry
from hermes_cli.active_sessions import active_session_registry_snapshot


@pytest.fixture(autouse=True)
def _isolated_active_session_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))


class _FakeAdapter:
    def __init__(self):
        self._pending_messages = {}
        self._active_sessions = {}

    async def send(self, chat_id, text, **kwargs):
        return None

    async def interrupt_session_activity(self, session_key, chat_id):
        event = self._active_sessions.get(session_key)
        if event is not None:
            event.set()


def _make_source(chat_id: str = "chat-1") -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id=chat_id,
        chat_type="dm",
        user_id=f"user-{chat_id}",
    )


def _make_event(text: str = "hello", chat_id: str = "chat-1") -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=_make_source(chat_id),
    )


def _make_runner(max_concurrent_sessions: int | None = None) -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")},
        max_concurrent_sessions=max_concurrent_sessions,
    )
    runner.adapters = {Platform.TELEGRAM: _FakeAdapter()}
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._active_session_leases = {}
    runner._session_run_generation = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._voice_mode = {}
    runner._background_tasks = set()
    runner._draining = False
    runner._restart_requested = False
    runner._restart_task_started = False
    runner._restart_detached = False
    runner._restart_via_service = False
    runner._restart_drain_timeout = 0.0
    runner._stop_task = None
    runner._exit_code = None
    runner._busy_ack_ts = {}
    runner._busy_input_mode = "interrupt"
    runner._busy_text_mode = "interrupt"
    runner._queued_events = {}
    runner._update_runtime_status = MagicMock()
    runner._is_user_authorized = lambda _source: True
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()
    runner.session_store = MagicMock()
    runner.delivery_router = MagicMock()
    return runner


def _occupy_session(runner: GatewayRunner, chat_id: str = "busy"):
    source = _make_source(chat_id)
    session_key = build_session_key(source)
    runner._running_agents[session_key] = MagicMock()
    runner._running_agents_ts[session_key] = time.time()
    return session_key


def _silence_global_gateway_hooks(monkeypatch):
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *args, **kwargs: [])
    monkeypatch.setattr("tools.slash_confirm.get_pending", lambda *args, **kwargs: None)
    monkeypatch.setattr("tools.slash_confirm.clear_if_stale", lambda *args, **kwargs: None)
    monkeypatch.setattr("tools.approval.has_blocking_approval", lambda *args, **kwargs: False)


def test_new_session_gets_clean_error_at_active_session_limit(monkeypatch):
    _silence_global_gateway_hooks(monkeypatch)
    runner = _make_runner(max_concurrent_sessions=1)
    _occupy_session(runner, "busy")
    event = _make_event(chat_id="new")
    new_key = build_session_key(event.source)

    async def fail_if_agent_runs(self_inner, ev, src, qk, generation):
        raise AssertionError("_handle_message_with_agent should not run at capacity")

    with patch.object(GatewayRunner, "_handle_message_with_agent", fail_if_agent_runs):
        result = asyncio.run(runner._handle_message(event))

    assert result == (
        "Hermes is at the active session limit (1/1). "
        "Try again when another session finishes."
    )
    assert new_key not in runner._running_agents
    runner.session_store.get_or_create_session.assert_not_called()


def test_status_command_bypasses_active_session_limit(monkeypatch):
    _silence_global_gateway_hooks(monkeypatch)
    runner = _make_runner(max_concurrent_sessions=1)
    _occupy_session(runner, "busy")
    runner._handle_status_command = AsyncMock(return_value="status ok")

    result = asyncio.run(runner._handle_message(_make_event("/status", chat_id="new")))

    assert result == "status ok"
    runner._handle_status_command.assert_awaited_once()


@pytest.mark.asyncio
async def test_refresh_claim_failure_releases_claimed_active_session(monkeypatch):
    """Setup failures after the public turn claim must not defer restart forever."""
    _silence_global_gateway_hooks(monkeypatch)
    runner = _make_runner(max_concurrent_sessions=1)
    runner._turn_leases = SessionTurnLeaseRegistry()
    runner._pending_refresh_notes = {}
    runner._restart_after_turn_timeout = 60.0
    runner._restore_moa_one_shot = MagicMock()
    runner._restore_pending_one_turn_model_override = MagicMock()
    runner._clear_durable_active_turn = AsyncMock()
    event = _make_event("next turn")
    session_key = build_session_key(event.source)
    pending_note = {
        "token": "refresh-token",
        "note": "[fresh context]",
        "after": event.timestamp,
        "generation": 1,
        "reserved_by": None,
    }
    runner._pending_refresh_notes[session_key] = [pending_note]

    class RefreshClaimFailure(RuntimeError):
        pass

    def fail_refresh_claim(*_args, **_kwargs):
        raise RefreshClaimFailure("sentinel refresh claim failure")

    runner._claim_refresh_context_note = fail_refresh_claim

    with pytest.raises(RefreshClaimFailure, match="sentinel refresh claim failure"):
        await runner._handle_message(event)

    state = runner._session_state(session_key)
    assert session_key not in runner._running_agents
    assert session_key not in runner._running_agents_ts
    assert state.turn.lease is None
    assert state.turn.lease_token is None
    assert active_session_registry_snapshot() == []
    assert read_runtime_status()["active_agents"] == 0
    assert runner._pending_refresh_notes[session_key] == [pending_note]
    assert pending_note["reserved_by"] is None
    assert await runner._await_active_work_before_restart() is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failing_cleanup",
    [
        "refresh_metadata",
        "finish_refresh",
        "restore_moa",
        "restore_model",
        "clear_durable_turn",
    ],
)
async def test_setup_failure_survives_each_early_cleanup_failure(
    monkeypatch, failing_cleanup
):
    """Every cleanup stage is isolated and the setup error stays primary."""
    _silence_global_gateway_hooks(monkeypatch)
    runner = _make_runner(max_concurrent_sessions=1)
    runner._turn_leases = MagicMock()
    runner._pending_refresh_notes = {}
    runner._restart_after_turn_timeout = 60.0
    event = _make_event("next turn")
    session_key = build_session_key(event.source)
    original_metadata = event.metadata

    class SetupFailure(RuntimeError):
        pass

    class CleanupFailure(RuntimeError):
        pass

    class CleanupMetadata(dict):
        fail_reads = False

        def get(self, key, default=None):
            if self.fail_reads:
                raise CleanupFailure("refresh metadata cleanup failed")
            return super().get(key, default)

    cleanup_metadata = CleanupMetadata(original_metadata or {"probe": True})
    event.metadata = cleanup_metadata

    def finish_refresh(*_args, **_kwargs):
        if failing_cleanup == "finish_refresh":
            raise CleanupFailure("finish refresh cleanup failed")

    def restore_moa(*_args, **_kwargs):
        if failing_cleanup == "restore_moa":
            raise CleanupFailure("moa restore cleanup failed")

    def restore_model(*_args, **_kwargs):
        if failing_cleanup == "restore_model":
            raise CleanupFailure("model restore cleanup failed")

    async def clear_durable(*_args, **_kwargs):
        if failing_cleanup == "clear_durable_turn":
            raise CleanupFailure("durable turn cleanup failed")

    async def fail_setup(*_args, **_kwargs):
        state = runner._session_state(session_key)
        state.turn.lease_token = object()
        state.turn.lease_generation = 1
        if failing_cleanup == "refresh_metadata":
            cleanup_metadata.fail_reads = True
        raise SetupFailure("sentinel setup failure")

    runner._claim_refresh_context_note = MagicMock(return_value=None)
    runner._finish_refresh_context_note = finish_refresh
    runner._restore_moa_one_shot = restore_moa
    runner._restore_pending_one_turn_model_override = restore_model
    runner._clear_durable_active_turn = clear_durable
    runner._handle_message_with_agent = fail_setup

    with pytest.raises(SetupFailure, match="sentinel setup failure"):
        await runner._handle_message(event)

    state = runner._session_state(session_key)
    assert state.turn.agent is None
    assert state.turn.started_ts == 0.0
    assert state.turn.lease is None
    assert state.turn.lease_token is None
    assert state.turn.lease_generation is None
    runner._turn_leases.release.assert_called_once()
    assert active_session_registry_snapshot() == []
    assert read_runtime_status()["active_agents"] == 0
    assert await runner._await_active_work_before_restart() is True


@pytest.mark.asyncio
async def test_explicit_release_retries_running_state_registry_write_failure(monkeypatch):
    """The outer release retries a lease write failed by state cleanup."""
    _silence_global_gateway_hooks(monkeypatch)
    runner = _make_runner(max_concurrent_sessions=1)
    runner._turn_leases = SessionTurnLeaseRegistry()
    runner._pending_refresh_notes = {}
    runner._restart_after_turn_timeout = 60.0
    runner._restore_moa_one_shot = MagicMock()
    runner._restore_pending_one_turn_model_override = MagicMock()
    runner._clear_durable_active_turn = AsyncMock()
    event = _make_event("next turn")
    session_key = build_session_key(event.source)

    class SetupFailure(RuntimeError):
        pass

    async def fail_setup(*_args, **_kwargs):
        raise SetupFailure("sentinel setup failure")

    runner._claim_refresh_context_note = MagicMock(return_value=None)
    runner._handle_message_with_agent = fail_setup

    from hermes_cli import active_sessions

    real_write_entries = active_sessions._write_entries
    failed_release_write = False

    def fail_first_release_write(state_path, entries):
        nonlocal failed_release_write
        if not entries and not failed_release_write:
            failed_release_write = True
            raise OSError("transient registry write failure")
        return real_write_entries(state_path, entries)

    monkeypatch.setattr(active_sessions, "_write_entries", fail_first_release_write)

    with pytest.raises(SetupFailure, match="sentinel setup failure"):
        await runner._handle_message(event)

    assert failed_release_write is True
    assert active_session_registry_snapshot() == []
    state = runner._session_state(session_key)
    assert state.turn.agent is None
    assert state.turn.lease is None
    assert read_runtime_status()["active_agents"] == 0
    assert await runner._await_active_work_before_restart() is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "setup_stage",
    ["session_state", "persist_active", "begin_generation", "refresh_metadata"],
)
async def test_setup_window_failure_releases_public_turn_claim(monkeypatch, setup_stage):
    _silence_global_gateway_hooks(monkeypatch)
    runner = _make_runner(max_concurrent_sessions=1)
    runner._turn_leases = SessionTurnLeaseRegistry()
    runner._pending_refresh_notes = {}
    runner._restart_after_turn_timeout = 60.0
    runner._restore_moa_one_shot = MagicMock()
    runner._restore_pending_one_turn_model_override = MagicMock()
    runner._clear_durable_active_turn = AsyncMock()
    event = _make_event("next turn")
    session_key = build_session_key(event.source)

    class SetupFailure(RuntimeError):
        pass

    if setup_stage == "session_state":
        real_session_state = runner._session_state
        failed = False

        def fail_first_session_state(key):
            nonlocal failed
            if not failed:
                failed = True
                raise SetupFailure("session state setup failed")
            return real_session_state(key)

        runner._session_state = fail_first_session_state
    elif setup_stage == "persist_active":
        real_persist = runner._persist_active_agents
        failed = False

        def fail_first_persist():
            nonlocal failed
            if not failed:
                failed = True
                raise SetupFailure("active persistence setup failed")
            return real_persist()

        runner._persist_active_agents = fail_first_persist
    elif setup_stage == "begin_generation":
        runner._begin_session_run_generation = MagicMock(
            side_effect=SetupFailure("generation setup failed")
        )
    else:
        pending_note = {
            "token": "refresh-token",
            "note": "[fresh context]",
            "after": event.timestamp - timedelta(seconds=1),
            "generation": 1,
            "reserved_by": None,
        }
        runner._pending_refresh_notes[session_key] = [pending_note]

        class FailingRefreshMetadata(dict):
            def __setitem__(self, key, value):
                if key == "refresh_context_note":
                    raise SetupFailure("refresh metadata setup failed")
                return super().__setitem__(key, value)

        event.metadata = FailingRefreshMetadata(event.metadata)

    with pytest.raises(SetupFailure, match="setup failed"):
        await runner._handle_message(event)

    state = runner._session_state(session_key)
    assert state.turn.agent is None
    assert state.turn.started_ts == 0.0
    assert state.turn.lease is None
    assert state.turn.lease_token is None
    assert active_session_registry_snapshot() == []
    assert read_runtime_status()["active_agents"] == 0
    if setup_stage == "refresh_metadata":
        assert runner._pending_refresh_notes[session_key] == [pending_note]
        assert pending_note["reserved_by"] is None
    assert await runner._await_active_work_before_restart() is True
