"""Behavioral coverage for agent-result compression route publication."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.run as gateway_run
from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource, SessionStore
from gateway.turn_context import TurnContext


def _source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-1001",
        chat_type="group",
        user_id="12345",
    )


def _event() -> MessageEvent:
    return MessageEvent(
        text="compress during this turn",
        source=_source(),
        message_id="m1",
    )


def _runner(monkeypatch, tmp_path):
    runner = gateway_run.GatewayRunner(GatewayConfig())
    runner.adapters = {}
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._is_user_authorized = lambda _source_arg: True
    runner._set_session_env = lambda _context: None
    runner._handle_active_session_busy_message = AsyncMock(return_value=False)
    runner._session_db = None
    runner._recover_telegram_topic_thread_id = lambda _source_arg: None
    runner._cache_session_source = lambda _key, _source_arg: None
    runner._is_session_run_current = lambda _key, _gen: True
    runner._reply_anchor_for_event = lambda _event_arg: None
    runner._get_guild_id = lambda _event_arg: None
    runner._should_send_voice_reply = lambda *_args, **_kwargs: False
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()

    entry = runner.session_store.get_or_create_session(_source())
    runner.session_store.load_transcript = MagicMock(return_value=[])
    runner.session_store.append_to_transcript = MagicMock()
    runner.session_store.update_session = MagicMock()
    runner._sync_telegram_topic_binding = MagicMock()

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {"api_key": "fake"},
    )
    monkeypatch.setattr(
        "agent.model_metadata.get_model_context_length",
        lambda *_args, **_kwargs: 100_000,
    )
    return runner, entry


@pytest.mark.asyncio
async def test_agent_result_rotation_invalidates_clarify_before_lease_rebind(
    monkeypatch,
    tmp_path,
):
    """The live post-agent path CAS-advances before using the rotated route."""
    from tools import clarify_gateway as cm

    runner, entry = _runner(monkeypatch, tmp_path)
    original_session_id = entry.session_id
    target_session_id = "agent-compression-child"
    pending = cm.register(
        "agent-result-pending",
        entry.session_key,
        "Pick",
        ["A"],
        origin=cm.ClarifyOrigin("12345", "-1001"),
        session_id=original_session_id,
        active_session_transaction=lambda action: runner.session_store.run_if_session_current(
            entry.session_key,
            original_session_id,
            action,
        ),
    )
    observations = []

    def _observe_rebind(_key, _generation, new_session_id):
        observations.append(
            (
                new_session_id,
                runner.session_store.peek_session_id(entry.session_key),
                cm.resolve_bound_choice(
                    pending.clarify_id,
                    0,
                    binding=pending.binding,
                    observed_origin=pending.binding.origin,
                ),
            )
        )

    runner._rebind_turn_lease = _observe_rebind
    runner._run_agent = AsyncMock(
        return_value={
            "final_response": "done",
            "messages": [
                {"role": "user", "content": "compress during this turn"},
                {"role": "assistant", "content": "done"},
            ],
            "tools": [],
            "history_offset": 0,
            "last_prompt_tokens": 0,
            "session_id": target_session_id,
            "api_calls": 1,
            "failed": False,
        }
    )

    response = await runner._handle_message_with_agent(
        _event(),
        _source(),
        entry.session_key,
        1,
    )

    assert response == "done"
    assert observations == [(target_session_id, target_session_id, False)]
    assert runner.session_store.peek_session_id(entry.session_key) == target_session_id
    assert pending.event.is_set()
    assert pending.response == ""


def test_run_sync_stale_compression_child_cannot_overwrite_cas_winner(tmp_path):
    """The real TurnRunner path must publish compression through store CAS.

    The generation predicate is the deterministic interleaving point: the
    old implementation had already snapshotted the parent route when this
    callback published a different child, then directly overwrote that winner
    from the stale snapshot after the callback returned.
    """
    from tools import clarify_gateway as cm

    cm.clear_all()
    try:
        source = SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="-1001",
            chat_type="group",
            user_id="12345",
        )
        store = SessionStore(
            sessions_dir=tmp_path,
            config=GatewayConfig(),
            session_boundary_cleanup_fn=cm.clear_session,
        )
        store._db = None
        route = store.get_or_create_session(source)
        session_key = route.session_key
        parent_session_id = route.session_id
        winner_session_id = "compression-winner"
        stale_child_session_id = "stale-agent-child"

        pending = cm.register(
            "run-sync-stale-pending",
            session_key,
            "Pick",
            ["A"],
            origin=cm.ClarifyOrigin("12345", "-1001"),
            session_id=parent_session_id,
            active_session_transaction=lambda action: store.run_if_session_current(
                session_key,
                parent_session_id,
                action,
            ),
        )

        interleaving_calls = 0

        def _publish_winner_during_current_run_check():
            nonlocal interleaving_calls
            interleaving_calls += 1
            assert interleaving_calls == 1
            advanced = store.advance_compression_session(
                session_key,
                parent_session_id,
                winner_session_id,
            )
            assert advanced is route
            assert pending.event.is_set()
            return True

        class _StaleRotatingAgent:
            def __init__(self, **kwargs):
                self.model = kwargs["model"]
                self.session_id = kwargs["session_id"]
                self.tools = []
                self.context_compressor = SimpleNamespace(
                    last_prompt_tokens=0,
                    context_length=200_000,
                )
                self.session_prompt_tokens = 0
                self.session_completion_tokens = 0
                self._last_compaction_in_place = False

            def run_conversation(self, _message, **_kwargs):
                self.session_id = stale_child_session_id
                return {
                    "final_response": "stale result",
                    "failed": False,
                    "messages": [],
                }

        runner = MagicMock()
        runner.config = SimpleNamespace(streaming=None)
        runner._provider_routing = {}
        runner._agent_cache_lock = None
        runner._agent_cache = {}
        runner._session_db = None
        runner._prefill_messages = None
        runner._pending_model_notes = {}
        runner._pending_skills_reload_notes = {}
        runner.session_store = store
        runner._running = True
        runner._draining = False
        runner._get_system_prompt_for_channel.return_value = None
        runner._resolve_session_agent_runtime.return_value = ("test-model", {})
        runner._resolve_session_reasoning_config.return_value = None
        runner._resolve_session_service_tier.return_value = None
        runner._resolve_turn_agent_config.return_value = {
            "model": "test-model",
            "runtime": {},
        }
        runner._agent_config_signature.return_value = ("test-signature",)
        runner._extract_cache_busting_config.return_value = {}
        runner._refresh_fallback_model.return_value = None
        runner._consume_pending_native_image_paths.return_value = []
        runner._consume_pending_turn_sidecar_notes.return_value = []
        runner._is_telegram_topic_lane.return_value = False
        runner._is_discord_auto_thread_lane.return_value = False
        runner._is_relay_discord_channel_lane.return_value = False

        ctx = TurnContext(
            source=source,
            message="compress during this turn",
            history=[],
            session_id=parent_session_id,
            session_key=session_key,
            user_config={},
            AIAgent=_StaleRotatingAgent,
            resolve_display_setting=lambda *_args: False,
            _run_still_current=_publish_winner_during_current_run_check,
            _hooks_ref=SimpleNamespace(loaded_hooks=False),
        )

        result = gateway_run.TurnRunner(runner, ctx).run_sync()

        assert result["session_id"] == stale_child_session_id
        assert interleaving_calls == 1
        assert store.peek_session_id(session_key) == winner_session_id
        assert cm.resolve_bound_choice(
            pending.clarify_id,
            0,
            binding=pending.binding,
            observed_origin=pending.binding.origin,
        ) is False
        runner._sync_telegram_topic_binding.assert_not_called()
    finally:
        cm.clear_all()
