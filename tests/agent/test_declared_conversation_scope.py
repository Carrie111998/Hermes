"""Tests for host-declared conversation affinity scope resolution (issue #96811).

Hosts that mint one physical session_id per RESPONSE (e.g. Hermes Studio's
group chat, /v1/responses stateless requests) re-key every conversation-affinity
hint Hermes sends (prompt_cache_key, OpenRouter/Nous sticky session_id,
x-grok-conv-id) on every reply unless a logical conversation scope is declared.

These tests assert that:
1. When a host declares a gateway session key (and optional epoch), consecutive
   per-response sessions map to the SAME stable `gwk_<sha256[:24]>` affinity scope.
2. When `/new` (reset_session) happens or the conversation epoch advances, the
   affinity scope ROTATES to a fresh value (preserving #79017/#86733 isolation).
3. Explicit fork children (/branch, delegates, reset children, tool-tagged children, background review)
   and DB errors degrade to their own isolated physical scope.
4. Wire-level kwargs (prompt_cache_key, sticky session_id, x-grok-conv-id) stay
   stable across turns within an epoch and rotate across epochs.
"""

import hashlib
from typing import Any, Optional
import pytest

from agent.portal_tags import (
    get_affinity_scope,
    reset_affinity_scope,
    reset_conversation_context,
    set_affinity_scope,
    set_conversation_context,
)
from agent.prompt_cache_scope import (
    declared_conversation_scope,
    declared_conversation_scope_safe,
    resolve_prompt_cache_scope,
)
from gateway.session import SessionEntry, SessionStore
from hermes_state import SessionDB
from providers import get_provider_profile


RUN_1 = "gc_run_room42_default_Worker_5f2c1ab9d4e34f7a8b0c6d1e2f3a4b5c"
RUN_2 = "gc_run_room42_default_Worker_9a7e3b1c05d24e6fb83a1c7d9e0f2a4b"
CHAT_KEY = "agent:main:telegram:dm:12345"


class DummyAgent:
    def __init__(
        self,
        session_id: str,
        session_db: Any = None,
        gateway_session_key: Optional[str] = None,
        gateway_conversation_epoch: int = 1,
    ):
        self.session_id = session_id
        self._session_db = session_db
        self._gateway_session_key = gateway_session_key
        self._gateway_conversation_epoch = gateway_conversation_epoch
        self._persist_disabled = False


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "test_state.db"
    return SessionDB(db_path)


class TestDeclaredConversationScopeResolution:
    def test_per_response_sessions_share_declared_scope(self, db):
        """Consecutive per-response nonces resolve to identical gwk_ scope."""
        db.create_session(RUN_1, source="api_server")
        db.create_session(RUN_2, source="api_server")

        agent_1 = DummyAgent(RUN_1, db, CHAT_KEY, gateway_conversation_epoch=1)
        agent_2 = DummyAgent(RUN_2, db, CHAT_KEY, gateway_conversation_epoch=1)

        scope_1 = resolve_prompt_cache_scope(agent_1)
        scope_2 = resolve_prompt_cache_scope(agent_2)

        assert scope_1.startswith("gwk_")
        assert scope_1 == scope_2

    def test_scope_rotates_on_epoch_advance_or_new(self, db):
        """When /new rotates the conversation epoch, the affinity scope rotates."""
        db.create_session(RUN_1, source="api_server")
        db.create_session(RUN_2, source="api_server")

        agent_epoch1 = DummyAgent(RUN_1, db, CHAT_KEY, gateway_conversation_epoch=1)
        agent_epoch2 = DummyAgent(RUN_2, db, CHAT_KEY, gateway_conversation_epoch=2)

        scope_epoch1 = resolve_prompt_cache_scope(agent_epoch1)
        scope_epoch2 = resolve_prompt_cache_scope(agent_epoch2)

        assert scope_epoch1.startswith("gwk_")
        assert scope_epoch2.startswith("gwk_")
        assert scope_epoch1 != scope_epoch2

    def test_branch_child_ignores_declaration(self, db):
        """Explicit /branch children keep their isolated physical scope."""
        db.create_session("parent-sess", source="telegram")
        db.create_session(
            "branch-child",
            source="telegram",
            parent_session_id="parent-sess",
            model_config={"_branched_from": "parent-sess"},
        )

        agent = DummyAgent("branch-child", db, CHAT_KEY)
        assert declared_conversation_scope(agent) is None
        assert resolve_prompt_cache_scope(agent) == "branch-child"

    def test_delegate_child_ignores_declaration(self, db):
        """Delegate subagents keep their isolated physical scope."""
        db.create_session("parent-sess", source="telegram")
        db.create_session(
            "delegate-child",
            source="telegram",
            parent_session_id="parent-sess",
            model_config={"_delegate_from": "parent-sess"},
        )

        agent = DummyAgent("delegate-child", db, CHAT_KEY)
        assert declared_conversation_scope(agent) is None
        assert resolve_prompt_cache_scope(agent) == "delegate-child"

    def test_tool_child_ignores_declaration(self, db):
        """Tool-spawned subagents keep their isolated physical scope."""
        db.create_session("parent-sess", source="telegram")
        db.create_session(
            "tool-child",
            source="tool",
            parent_session_id="parent-sess",
        )

        agent = DummyAgent("tool-child", db, CHAT_KEY)
        assert declared_conversation_scope(agent) is None
        assert resolve_prompt_cache_scope(agent) == "tool-child"

    def test_real_session_store_reset_and_per_response_flow(self, tmp_path):
        """Full flow: SessionStore.reset_session advances epoch; opening session and subsequent per-response turns share new epoch scope."""
        from gateway.config import GatewayConfig
        from gateway.session import Platform, SessionSource

        db_path = tmp_path / "gateway_state.db"
        db = SessionDB(db_path)
        store = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
        store._db = db
        source = SessionSource(platform=Platform.TELEGRAM, chat_id="12345", chat_type="dm")

        # 1. First conversation epoch 1
        entry_epoch1 = store.get_or_create_session(source)
        agent_epoch1_turn1 = DummyAgent(
            entry_epoch1.session_id, db, entry_epoch1.session_key,
            gateway_conversation_epoch=entry_epoch1.conversation_epoch
        )
        agent_epoch1_turn2 = DummyAgent(
            "gc_run_epoch1_turn2", db, entry_epoch1.session_key,
            gateway_conversation_epoch=entry_epoch1.conversation_epoch
        )

        scope_epoch1_t1 = resolve_prompt_cache_scope(agent_epoch1_turn1)
        scope_epoch1_t2 = resolve_prompt_cache_scope(agent_epoch1_turn2)

        assert scope_epoch1_t1.startswith("gwk_")
        assert scope_epoch1_t1 == scope_epoch1_t2

        # 2. User types /new -> reset_session advances conversation_epoch to 2
        entry_epoch2 = store.reset_session(entry_epoch1.session_key)
        assert entry_epoch2.conversation_epoch == 2
        assert entry_epoch2.session_id != entry_epoch1.session_id

        agent_epoch2_opening = DummyAgent(
            entry_epoch2.session_id, db, entry_epoch2.session_key,
            gateway_conversation_epoch=entry_epoch2.conversation_epoch
        )
        agent_epoch2_turn2 = DummyAgent(
            "gc_run_epoch2_turn2", db, entry_epoch2.session_key,
            gateway_conversation_epoch=entry_epoch2.conversation_epoch
        )
        agent_epoch2_turn3 = DummyAgent(
            "gc_run_epoch2_turn3", db, entry_epoch2.session_key,
            gateway_conversation_epoch=entry_epoch2.conversation_epoch
        )

        scope_epoch2_opening = resolve_prompt_cache_scope(agent_epoch2_opening)
        scope_epoch2_t2 = resolve_prompt_cache_scope(agent_epoch2_turn2)
        scope_epoch2_t3 = resolve_prompt_cache_scope(agent_epoch2_turn3)

        assert scope_epoch2_opening.startswith("gwk_")
        # All turns in epoch 2 share the exact same affinity scope
        assert scope_epoch2_opening == scope_epoch2_t2 == scope_epoch2_t3
        # But epoch 2 scope is completely rotated and isolated from epoch 1 scope
        assert scope_epoch1_t1 != scope_epoch2_opening

    def test_background_review_fork_ignores_declaration(self, db):
        """Background-review forks (_persist_disabled) keep their isolated physical scope."""
        db.create_session("live-sess", source="telegram")
        agent = DummyAgent("review-fork", db, CHAT_KEY)
        agent._persist_disabled = True

        assert declared_conversation_scope(agent) is None
        assert resolve_prompt_cache_scope(agent) == "review-fork"

    def test_blank_declarations_are_ignored(self, db):
        """Empty or whitespace declarations fall back to physical scope."""
        db.create_session(RUN_1, source="api_server")
        for blank in (None, "", "   "):
            agent = DummyAgent(RUN_1, db, blank)
            assert declared_conversation_scope(agent) is None
            assert resolve_prompt_cache_scope(agent) == RUN_1

    def test_safe_variant_never_raises(self):
        """declared_conversation_scope_safe never raises on hostile properties."""
        class ExplodingAgent:
            @property
            def _gateway_session_key(self):
                raise RuntimeError("hostile property")

        assert declared_conversation_scope_safe(ExplodingAgent()) is None
        agent = DummyAgent("sess", None, CHAT_KEY)
        assert declared_conversation_scope_safe(agent).startswith("gwk_")


class TestPromptCacheKeyStability:
    """Wire-layer validation for OpenAI, Codex, OpenRouter, Nous, Grok."""

    INSTRUCTIONS = "You are Reviewer in room7."
    TOOLS = [{"type": "function", "name": "terminal"}]

    def test_responses_transport_key_matches_across_responses(self, db):
        from agent.transports.codex import ResponsesApiTransport

        db.create_session(RUN_1, source="api_server")
        db.create_session(RUN_2, source="api_server")
        transport = ResponsesApiTransport()
        base = dict(
            model="gpt-5.5",
            messages=[
                {"role": "system", "content": self.INSTRUCTIONS},
                {"role": "user", "content": "hi"},
            ],
            tools=[],
        )

        agent_1 = DummyAgent(RUN_1, db, CHAT_KEY, gateway_conversation_epoch=1)
        agent_2 = DummyAgent(RUN_2, db, CHAT_KEY, gateway_conversation_epoch=1)

        key_1 = transport.build_kwargs(
            **base,
            session_id=RUN_1,
            cache_scope_id=resolve_prompt_cache_scope(agent_1),
        )["prompt_cache_key"]
        key_2 = transport.build_kwargs(
            **base,
            session_id=RUN_2,
            cache_scope_id=resolve_prompt_cache_scope(agent_2),
        )["prompt_cache_key"]

        assert key_1 == key_2

    def test_chat_completions_key_matches_across_responses(self, db):
        from agent.transports.chat_completions import _add_prompt_cache_key

        db.create_session(RUN_1, source="api_server")
        db.create_session(RUN_2, source="api_server")
        messages = [{"role": "system", "content": self.INSTRUCTIONS}]

        agent_1 = DummyAgent(RUN_1, db, CHAT_KEY, gateway_conversation_epoch=1)
        agent_2 = DummyAgent(RUN_2, db, CHAT_KEY, gateway_conversation_epoch=1)

        kwargs_1: dict = {}
        _add_prompt_cache_key(
            kwargs_1,
            messages=messages,
            tools=None,
            supports_prompt_cache_key=True,
            session_id=RUN_1,
            cache_scope_id=resolve_prompt_cache_scope(agent_1),
        )
        kwargs_2: dict = {}
        _add_prompt_cache_key(
            kwargs_2,
            messages=messages,
            tools=None,
            supports_prompt_cache_key=True,
            session_id=RUN_2,
            cache_scope_id=resolve_prompt_cache_scope(agent_2),
        )

        assert kwargs_1["prompt_cache_key"] == kwargs_2["prompt_cache_key"]


class TestProviderStickyKeys:
    """OpenRouter and Nous sticky keys, plus x-grok-conv-id header."""

    @pytest.fixture(autouse=True)
    def _clean_context(self):
        affinity = set_affinity_scope(None)
        conversation = set_conversation_context(None)
        try:
            yield
        finally:
            reset_conversation_context(conversation)
            reset_affinity_scope(affinity)

    def test_declared_affinity_scope_sets_openrouter_sticky_key(self):
        profile = get_provider_profile("openrouter")
        scope = "gwk_1234567890abcdef123456"
        token = set_affinity_scope(scope)
        try:
            body_1 = profile.build_extra_body(session_id=RUN_1)
            body_2 = profile.build_extra_body(session_id=RUN_2)
        finally:
            reset_affinity_scope(token)

        assert body_1["session_id"] == scope
        assert body_2["session_id"] == scope

    def test_declared_affinity_scope_sets_nous_sticky_key(self):
        profile = get_provider_profile("nous")
        scope = "gwk_1234567890abcdef123456"
        token = set_affinity_scope(scope)
        try:
            body_1 = profile.build_extra_body(session_id=RUN_1)
            body_2 = profile.build_extra_body(session_id=RUN_2)
        finally:
            reset_affinity_scope(token)

        assert body_1["session_id"] == scope
        assert body_2["session_id"] == scope

    def test_declared_affinity_scope_sets_x_grok_conv_id_header(self):
        profile = get_provider_profile("openrouter")
        scope = "gwk_1234567890abcdef123456"
        token = set_affinity_scope(scope)
        try:
            _, top_1 = profile.build_api_kwargs_extras(
                model="x-ai/grok-4", session_id=RUN_1
            )
            _, top_2 = profile.build_api_kwargs_extras(
                model="x-ai/grok-4", session_id=RUN_2
            )
        finally:
            reset_affinity_scope(token)

        assert top_1["extra_headers"]["x-grok-conv-id"] == scope
        assert top_2["extra_headers"]["x-grok-conv-id"] == scope

    def test_nested_child_turn_shadows_parent_affinity_scope(self):
        """A nested child/fork turn explicitly shadows the parent ContextVar to None."""
        parent_scope = "gwk_parent1234567890123456"
        token_parent = set_affinity_scope(parent_scope)
        try:
            assert get_affinity_scope() == parent_scope

            # Child/fork agent turn runs (declared_scope is None)
            child_declared_scope = None
            token_child = set_affinity_scope(child_declared_scope)
            try:
                # Inside child turn, affinity scope is shadowed to None
                assert get_affinity_scope() is None
                profile = get_provider_profile("openrouter")
                body = profile.build_extra_body(session_id="child-sess-42")
                # Fallback to physical session_id works because get_affinity_scope() is None
                assert body.get("session_id") == "child-sess-42"
            finally:
                reset_affinity_scope(token_child)

            # When child finishes, parent context is fully restored
            assert get_affinity_scope() == parent_scope
        finally:
            reset_affinity_scope(token_parent)

        assert get_affinity_scope() is None

    def test_agent_run_turn_shadows_parent_affinity_scope_live(self, tmp_path):
        """Live AIAgent.run_turn shadows parent affinity scope to None during child turns."""
        from unittest.mock import patch
        from run_agent import AIAgent

        db_path = tmp_path / "live_turn_test.db"
        db = SessionDB(db_path)
        db.create_session("parent-sess", source="telegram")
        db.create_session(
            "child-fork",
            source="telegram",
            parent_session_id="parent-sess",
            model_config={"_branched_from": "parent-sess"},
        )

        with patch("run_agent.get_tool_definitions", return_value=[]), \
             patch("run_agent.check_toolset_requirements", return_value={}), \
             patch("run_agent.OpenAI"):

            parent_agent = AIAgent(
                session_id="parent-sess",
                api_key="test-key-1234567890",
                base_url="https://openrouter.ai/api/v1",
                provider="openrouter",
                gateway_session_key=CHAT_KEY,
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
            )
            parent_agent._session_db = db

            child_agent = AIAgent(
                session_id="child-fork",
                api_key="test-key-1234567890",
                base_url="https://openrouter.ai/api/v1",
                provider="openrouter",
                gateway_session_key=CHAT_KEY,
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
            )
            child_agent._session_db = db

            child_observed_scope = "UNSET"
            parent_inside_turn_scope = "UNSET"

            def fake_child_run_conv(*args, **kwargs):
                nonlocal child_observed_scope
                child_observed_scope = get_affinity_scope()
                return "child-done"

            def fake_parent_run_conv(*args, **kwargs):
                nonlocal parent_inside_turn_scope
                parent_inside_turn_scope = get_affinity_scope()
                # Nested child turn runs inside parent turn
                with patch("agent.conversation_loop.run_conversation", side_effect=fake_child_run_conv):
                    child_agent.run_conversation("child prompt")
                # Parent scope should still be intact after child returns
                assert get_affinity_scope() == parent_inside_turn_scope
                return "parent-done"

            assert get_affinity_scope() is None
            with patch("agent.conversation_loop.run_conversation", side_effect=fake_parent_run_conv):
                parent_agent.run_conversation("parent prompt")

            assert parent_inside_turn_scope.startswith("gwk_")
            assert child_observed_scope is None
            assert get_affinity_scope() is None


class TestGatewaySessionStoreResetEpoch:
    """SessionStore advances conversation_epoch on reset_session (/new)."""

    def test_session_store_reset_advances_epoch(self, tmp_path):
        from gateway.config import GatewayConfig
        from gateway.session import Platform, SessionSource

        store = SessionStore(sessions_dir=tmp_path, config=GatewayConfig())
        source = SessionSource(platform=Platform.TELEGRAM, chat_id="12345", chat_type="dm")
        entry_1 = store.get_or_create_session(source)
        assert entry_1.conversation_epoch == 1

        entry_2 = store.reset_session(entry_1.session_key)
        assert entry_2 is not None
        assert entry_2.session_id != entry_1.session_id
        assert entry_2.conversation_epoch == 2

    def test_turn_context_and_agent_epoch_propagation(self):
        from gateway.turn_context import TurnContext

        ctx = TurnContext(
            session_id="sess-1",
            session_key="agent:main:telegram:dm:12345",
            conversation_epoch=3,
        )
        assert ctx.conversation_epoch == 3

    def test_auto_reset_advances_epoch_and_gwk_scope(self, tmp_path):
        from unittest.mock import patch
        from gateway.config import GatewayConfig
        from gateway.session import Platform, SessionSource
        from agent.prompt_cache_scope import declared_conversation_scope

        store = SessionStore(sessions_dir=tmp_path, config=GatewayConfig())
        source = SessionSource(platform=Platform.TELEGRAM, chat_id="chat-auto-1", chat_type="dm")
        entry_1 = store.get_or_create_session(source)
        assert entry_1.conversation_epoch == 1
        assert store.get_conversation_epoch(entry_1.session_key) == 1

        agent_1 = DummyAgent(session_id=entry_1.session_id, gateway_session_key=entry_1.session_key, gateway_conversation_epoch=1)
        scope_1 = declared_conversation_scope(agent_1)

        # Trigger auto-reset by forcing _should_reset to return "idle"
        with patch.object(store, "_should_reset", return_value="idle"):
            entry_2 = store.get_or_create_session(source)

        assert entry_2.session_id != entry_1.session_id
        assert entry_2.was_auto_reset is True
        assert entry_2.auto_reset_reason == "idle"
        assert entry_2.conversation_epoch == 2
        assert store.get_conversation_epoch(entry_2.session_key) == 2

        agent_2 = DummyAgent(session_id=entry_2.session_id, gateway_session_key=entry_2.session_key, gateway_conversation_epoch=2)
        scope_2 = declared_conversation_scope(agent_2)

        assert scope_1.startswith("gwk_")
        assert scope_2.startswith("gwk_")
        assert scope_1 != scope_2

    def test_mixed_new_and_auto_reset_never_rolls_back_epoch_aba(self, tmp_path):
        from unittest.mock import patch
        from gateway.config import GatewayConfig
        from gateway.session import Platform, SessionSource

        store = SessionStore(sessions_dir=tmp_path, config=GatewayConfig())
        source = SessionSource(platform=Platform.TELEGRAM, chat_id="chat-aba-1", chat_type="dm")
        
        # Epoch 1: initial session
        entry_1 = store.get_or_create_session(source)
        assert entry_1.conversation_epoch == 1

        # Epoch 2: explicit /new
        entry_2 = store.reset_session(entry_1.session_key)
        assert entry_2.conversation_epoch == 2

        # Epoch 3: policy auto-reset (must be 3, NEVER rolling back to 1)
        with patch.object(store, "_should_reset", return_value="daily"):
            entry_3 = store.get_or_create_session(source)

        assert entry_3.was_auto_reset is True
        assert entry_3.conversation_epoch == 3
        assert store.get_conversation_epoch(entry_3.session_key) == 3

    def test_persistence_and_reload_preserves_epoch_before_reset(self, tmp_path):
        from gateway.config import GatewayConfig
        from gateway.session import Platform, SessionSource

        config = GatewayConfig()
        store_1 = SessionStore(sessions_dir=tmp_path, config=config)
        source = SessionSource(platform=Platform.TELEGRAM, chat_id="chat-persist-1", chat_type="dm")
        
        entry_1 = store_1.get_or_create_session(source)
        entry_2 = store_1.reset_session(entry_1.session_key)
        assert entry_2.conversation_epoch == 2

        # Instantiate fresh SessionStore from the same directory to simulate service restart
        store_2 = SessionStore(sessions_dir=tmp_path, config=config)
        assert store_2.get_conversation_epoch(entry_2.session_key) == 2

        # Subsequent reset on reloaded store advances to epoch 3
        entry_3 = store_2.reset_session(entry_2.session_key)
        assert entry_3.conversation_epoch == 3

