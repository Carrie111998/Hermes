"""Multiplex profile-scope regression for the per-turn credit-notice policy.

Under ``gateway.multiplex_profiles`` a single gateway process serves every
profile, and the agent cache (keyed by session) is shared across them.  A
cached ``AIAgent`` computes ``display.credits_notices`` once and caches it in
``_credits_notices_enabled_cache``; the gateway then re-binds
``notice_callback`` unconditionally on every turn.  If a session first runs
under a profile with ``display.credits_notices=true`` and is later routed to a
profile where it is ``false``, the reused agent keeps the stale ``True`` and
keeps emitting credit notices into the routed chat (and the reverse: a
disabled-first agent would never emit for an enabled profile).  Each routed
turn must re-stamp the ACTIVE profile's policy onto the reused agent before
any provider response.

These tests drive the real ``TurnRunner.run_sync`` callback-binding seam under
alternating ``_profile_runtime_scope`` entries against real profile-scoped
config, reusing one shared cached agent across turns.
"""

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

from gateway.config import Platform
from gateway.session import SessionSource
from gateway.turn_context import TurnContext
from run_agent import AIAgent


# Real profile-scoped configs (fail-open default is true, so an explicit
# false must be honored and an explicit true kept).
_ROOT_CONFIG = {"display": {"credits_notices": True}}
_SECONDARY_CONFIG = {"display": {"credits_notices": False}}


class _StubAgent(AIAgent):
    """Minimal AIAgent stand-in: real ``_emit_credits_notices`` /
    ``_credits_notices_enabled`` (so the policy cache is genuinely consulted),
    plus the handful of attributes ``run_sync`` reads after a turn."""

    def __init__(self, **kwargs):
        self.model = kwargs.get("model", "")
        self.base_url = kwargs.get("base_url", "")
        self.session_id = kwargs.get("session_id", None)
        self.tools = []
        self.context_compressor = SimpleNamespace(
            last_prompt_tokens=0,
            context_length=200_000,
        )
        self.session_prompt_tokens = 0
        self.session_completion_tokens = 0
        self._credits_state = None
        self._credits_latch = None
        self._credits_notices_enabled_cache = None
        self.notice_callback = None
        self.notice_clear_callback = None

    def run_conversation(
        self,
        user_message: Any,
        system_message: str = None,
        conversation_history: Optional[List[Dict[str, Any]]] = None,
        task_id: str = None,
        stream_callback: Optional[callable] = None,
        persist_user_message: Optional[Any] = None,
        persist_user_timestamp: Optional[float] = None,
        persist_user_display_kind: Optional[str] = None,
        persist_user_display_metadata: Optional[Dict[str, Any]] = None,
        moa_config: Optional[dict] = None,
        **_kwargs,
    ):
        # Simulate the post-provider-response path: the credits policy runs
        # after each API response and is where the cached setting is read.
        self._emit_credits_notices()
        return {"final_response": "ok", "failed": False, "interrupted": False,
                "completed": True, "messages": []}


def _make_gateway_runner():
    """A runner whose per-turn agent cache is REAL and shared across turns."""
    runner = MagicMock()
    runner.config = SimpleNamespace(streaming=None)
    runner._provider_routing = {}
    runner._agent_cache_lock = __import__("threading").Lock()
    runner._agent_cache = {}
    runner._session_db = None
    runner._prefill_messages = None
    runner._pending_model_notes = {}
    runner._pending_skills_reload_notes = {}
    runner.session_store._entries = {}
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
    return runner


def _run_turn(runner, profile_home, session_key, user_config, source):
    """Drive one real ``TurnRunner.run_sync`` turn under a profile scope.

    Returns the (possibly reused) agent instance the turn ran on.
    """
    from gateway.run import TurnRunner, _profile_runtime_scope

    ctx = TurnContext(
        source=source,
        message="continue",
        history=[],
        session_id="test-session",
        session_key=session_key,
        run_generation=1,
        user_config=user_config,
        AIAgent=_StubAgent,
        resolve_display_setting=lambda *_args: False,
        _run_still_current=lambda: True,
        _hooks_ref=SimpleNamespace(loaded_hooks=False),
        _loop_for_step=None,
    )
    with _profile_runtime_scope(Path(profile_home)):
        TurnRunner(runner, ctx).run_sync()
    return ctx.agent_holder[0]


def _write_profile_config(profile_home, enabled: bool):
    profile_home.mkdir(parents=True, exist_ok=True)
    (profile_home / "config.yaml").write_text(
        "display:\n  credits_notices: %s\n" % ("true" if enabled else "false"),
        encoding="utf-8",
    )


class TestMultiplexCreditNoticesProfileScope:
    def test_reused_agent_re_stamps_policy_per_routed_turn(self, tmp_path, monkeypatch):
        """The active profile's display.credits_notices must be stamped onto a
        shared cached agent before every provider response.

        root=true -> secondary=false -> root=true, alternating scopes, with one
        cached agent reused across all three turns. Without the per-turn stamp
        the agent keeps the root value (True) and the callback stays bound, so
        the secondary turn would emit credit notices the profile disabled.
        """
        root_home = tmp_path / "root"
        secondary_home = tmp_path / "profiles" / "secondary"
        _write_profile_config(root_home, enabled=True)
        _write_profile_config(secondary_home, enabled=False)
        monkeypatch.setenv("HERMES_HOME", str(root_home))

        runner = _make_gateway_runner()
        session_key = "agent:test:local:chat"
        source = SessionSource(
            platform=Platform.LOCAL,
            chat_id="chat",
            user_id="user",
        )

        # Turn 1 — root profile: enabled. The agent is created here.
        agent = _run_turn(
            runner, root_home, session_key, _ROOT_CONFIG, source
        )
        assert agent is not None
        assert agent._credits_notices_enabled_cache is True
        assert agent.notice_callback is not None

        # Turn 2 — routed to the secondary profile: disabled. The SAME cached
        # agent instance must be re-stamped to the active profile's policy.
        agent2 = _run_turn(
            runner, secondary_home, session_key, _SECONDARY_CONFIG, source
        )
        assert agent2 is agent, "the cached agent must be reused across turns"
        assert agent._credits_notices_enabled_cache is False
        assert agent.notice_callback is None

        # Turn 3 — back to root: enabled again, callback restored.
        agent3 = _run_turn(
            runner, root_home, session_key, _ROOT_CONFIG, source
        )
        assert agent3 is agent
        assert agent._credits_notices_enabled_cache is True
        assert agent.notice_callback is not None

        # No contamination: the policy exactly follows the alternating scope.
        assert agent._credits_notices_enabled_cache is True

    def test_fail_open_when_config_has_no_policy(self, tmp_path, monkeypatch):
        """An absent/error-shaped policy stays enabled (fail-open True).

        ``ctx.user_config`` is the loader's fail-open ``{}`` on any config
        error, and ``display.credits_notices`` defaults to true.  The per-turn
        stamp must keep emitting (callback bound) rather than silently
        suppressing notices when the active profile's config is unreadable.
        """
        root_home = tmp_path / "root"
        _write_profile_config(root_home, enabled=True)
        monkeypatch.setenv("HERMES_HOME", str(root_home))

        runner = _make_gateway_runner()
        session_key = "agent:test:local:chat"
        source = SessionSource(
            platform=Platform.LOCAL,
            chat_id="chat",
            user_id="user",
        )

        agent = _run_turn(runner, root_home, session_key, {}, source)
        assert agent is not None
        assert agent._credits_notices_enabled_cache is True
        assert agent.notice_callback is not None
