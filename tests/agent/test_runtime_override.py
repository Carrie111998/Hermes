"""Unit tests for pre_llm_call runtime_override (issue #23739)."""

from __future__ import annotations

import pytest

from agent.runtime_override import (
    RUNTIME_OVERRIDE_KEYS,
    apply_runtime_override,
    validate_runtime_override,
)


# ---------------------------------------------------------------------------
# validate_runtime_override
# ---------------------------------------------------------------------------

class TestValidate:
    def test_full_valid_dict(self):
        ro = validate_runtime_override({
            "model": "gpt-5.6",
            "provider": "openai",
            "base_url": "https://api.openai.com/v1",
            "api_key": "sk-test",
            "api_mode": "chat_completions",
        })
        assert ro == {
            "model": "gpt-5.6",
            "provider": "openai",
            "base_url": "https://api.openai.com/v1",
            "api_key": "sk-test",
            "api_mode": "chat_completions",
        }

    def test_system_prompt_rejected(self):
        # system_prompt is intentionally NOT supported (cache-prefix sacred):
        # it must be dropped, not applied.
        ro = validate_runtime_override({
            "model": "gpt-5.6",
            "system_prompt": "You are a test.",
        })
        assert ro == {"model": "gpt-5.6"}

    def test_unknown_api_mode_rejected(self):
        ro = validate_runtime_override({"api_mode": "invalid_wire"})
        assert ro == {}

    def test_known_api_mode_accepted(self):
        for mode in ("chat_completions", "anthropic_messages", "codex_responses", "bedrock_converse"):
            assert validate_runtime_override({"api_mode": mode}) == {"api_mode": mode}

    def test_empty_dict(self):
        assert validate_runtime_override({}) == {}

    def test_not_a_dict(self):
        # Non-dict runtime_override (e.g. 42) -> warning + {} (never crash).
        assert validate_runtime_override(42) == {}

    def test_unsupported_key_ignored(self):
        ro = validate_runtime_override({"model": "m", "temperature": 0.7})
        assert ro == {"model": "m"}

    def test_invalid_value_type_ignored(self):
        ro = validate_runtime_override({"model": 12345})
        assert ro == {}

    def test_empty_string_ignored(self):
        ro = validate_runtime_override({"model": "", "provider": "  "})
        assert ro == {}

    def test_whitelist_matches_spec(self):
        assert RUNTIME_OVERRIDE_KEYS == frozenset({
            "model", "provider", "base_url", "api_key", "api_mode",
        })


# ---------------------------------------------------------------------------
# apply_runtime_override (context manager snapshot/restore)
# ---------------------------------------------------------------------------

class _FakeAgent:
    """Minimal stand-in for AIAgent with the attributes the override touches."""

    def __init__(self):
        self.model = "orig-model"
        self.provider = "orig-provider"
        self.api_mode = "chat_completions"
        self.api_key = "orig-key"
        self._base_url = "https://orig.example.com/v1"
        self._base_url_lower = self._base_url.lower()
        self._base_url_hostname = "orig.example.com"
        self._client_kwargs = {"api_key": "orig-key", "base_url": "https://orig.example.com/v1"}
        self._anthropic_api_key = "orig-anthropic-key"
        self._anthropic_base_url = "https://orig.anthropic.example.com"
        self._is_anthropic_oauth = False
        self.requested_provider = "orig-provider"
        self.request_overrides = {"service_tier": "standard"}
        self.runtime_capabilities = {"native_compaction": False}
        self._transport_cache = {"chat_completions": "warmed-transport"}
        self._fallback_activated = False

    @property
    def base_url(self):
        return self._base_url

    @base_url.setter
    def base_url(self, value):
        self._base_url = value
        self._base_url_lower = value.lower()
        self._base_url_hostname = value.split("//", 1)[1].split("/", 1)[0]


class TestApply:
    def test_apply_and_restore(self):
        agent = _FakeAgent()
        with apply_runtime_override(agent, {
            "model": "new-model",
            "provider": "new-provider",
            "base_url": "https://new.example.com/v1",
            "api_key": "new-key",
            "api_mode": "anthropic_messages",
        }):
            assert agent.model == "new-model"
            assert agent.provider == "new-provider"
            assert agent.base_url == "https://new.example.com/v1"
            assert agent._base_url_hostname == "new.example.com"
            assert agent.api_key == "new-key"
            assert agent.api_mode == "anthropic_messages"
            assert agent._client_kwargs["api_key"] == "new-key"
            assert agent._client_kwargs["base_url"] == "https://new.example.com/v1"
            assert agent._anthropic_api_key == "new-key"
            assert agent._anthropic_base_url == "https://new.example.com/v1"
        # Restored on exit.
        assert agent.model == "orig-model"
        assert agent.provider == "orig-provider"
        assert agent.base_url == "https://orig.example.com/v1"
        assert agent.api_key == "orig-key"
        assert agent.api_mode == "chat_completions"
        assert agent._client_kwargs == {"api_key": "orig-key", "base_url": "https://orig.example.com/v1"}
        assert agent._anthropic_api_key == "orig-anthropic-key"

    def test_restore_on_exception(self):
        agent = _FakeAgent()
        with pytest.raises(RuntimeError):
            with apply_runtime_override(agent, {"model": "new-model"}):
                assert agent.model == "new-model"
                raise RuntimeError("boom")
        assert agent.model == "orig-model"

    def test_bare_agent_not_polluted(self):
        # Agent created via __new__ has NO attributes; entering the scope must
        # not manufacture attributes on the agent that survive the exit.
        agent = object.__new__(_FakeAgent)
        with apply_runtime_override(agent, {"model": "m", "api_key": "k"}):
            assert agent.model == "m"
        assert not hasattr(agent, "model")
        assert not hasattr(agent, "_client_kwargs")

    def test_partial_override_only_changes_given_keys(self):
        agent = _FakeAgent()
        with apply_runtime_override(agent, {"model": "only-model"}):
            assert agent.model == "only-model"
            assert agent.provider == "orig-provider"  # untouched
            assert agent.api_mode == "chat_completions"  # untouched

    def test_api_mode_change_invalidates_and_restores_transport_cache(self):
        # P1-3: an api_mode-changing override must clear the eagerly-warmed
        # transport cache (mirroring switch_model / agent_init) and restore the
        # pre-override content on exit so no override-mode transport leaks.
        agent = _FakeAgent()
        assert agent._transport_cache == {"chat_completions": "warmed-transport"}
        with apply_runtime_override(agent, {
            "api_mode": "anthropic_messages",
            "api_key": "new-key",
        }):
            assert agent._transport_cache == {}  # invalidated on api_mode change
            assert agent._anthropic_api_key == "new-key"
            assert agent._is_anthropic_oauth is False
        assert agent._transport_cache == {"chat_completions": "warmed-transport"}
        assert agent._anthropic_api_key == "orig-anthropic-key"
        assert agent._is_anthropic_oauth is False  # pre-override value restored

    def test_route_change_refreshes_and_restores_derived_state(self):
        # P1-3: provider/base_url/model changes refresh the route-derived
        # identity (requested_provider, request_overrides, runtime_capabilities)
        # like the canonical switch, and restore atomically on exit.
        agent = _FakeAgent()
        with apply_runtime_override(agent, {
            "model": "new-model",
            "provider": "openai",
            "base_url": "https://api.openai.com/v1",
        }):
            assert agent.requested_provider == "openai"
            assert "extra_body" not in (agent.request_overrides or {})
            assert agent.runtime_capabilities is not None
        assert agent.requested_provider == "orig-provider"
        assert agent.request_overrides == {"service_tier": "standard"}
        assert agent.runtime_capabilities == {"native_compaction": False}

    def test_fallback_supersession_skips_the_restore(self):
        # P1-2 precedence: a proactive override owns the primary attempt; once
        # _try_activate_fallback succeeds mid-scope, the fallback route
        # supersedes the override and the scope must NOT restore the
        # pre-override identity over the fallback.  Supersession is the
        # EXPLICIT consume_runtime_override handoff, never an inference.
        from agent.runtime_override import consume_runtime_override

        agent = _FakeAgent()
        with apply_runtime_override(agent, {
            "model": "override-model",
            "provider": "openai",
        }):
            # Simulate try_activate_fallback taking ownership of the route,
            # then the fallback call site performing the supersede handoff.
            agent.model = "fallback-model"
            agent.provider = "fallback-provider"
            agent._fallback_activated = True
            consume_runtime_override(agent)
        # The fallback state stands; the pre-override identity is NOT restored.
        assert agent.model == "fallback-model"
        assert agent.provider == "fallback-provider"
        assert agent._fallback_activated is True
        # The handoff also cleared the turn-scoped override.
        assert agent._runtime_override == {}

    def test_supersession_requires_the_explicit_handoff(self):
        # A route change alone (no consume_runtime_override handoff) must NOT
        # be mistaken for supersession — the scope restores normally.
        agent = _FakeAgent()
        with apply_runtime_override(agent, {"model": "override-model"}):
            agent.model = "changed-by-something-else"
            agent._fallback_activated = True
        assert agent.model == "orig-model"

    def test_bug1_turn2_on_fallback_api_key_only_override_chain_advance(self):
        # BUG-1: turn-2 is already on the fallback (_fallback_activated=True
        # at scope entry) and the override's keys are all OUTSIDE the route
        # tuple (api_key only).  The old flag-delta/4-tuple inference could
        # not see the chain advance when the fallback-2 route coincides with
        # the applied route (same model/provider/api_mode/base_url, new
        # credential), and __exit__ clobbered the fallback-2 activation with
        # the restore.  The explicit supersede handoff resolves it.
        from agent.runtime_override import consume_runtime_override

        agent = _FakeAgent()
        agent._fallback_activated = True  # previous turn landed on fallback-1
        agent._runtime_override = {"api_key": "sk-override"}
        with apply_runtime_override(agent, {"api_key": "sk-override"}):
            # Mid-scope 429: the chain advances to fallback-2.  The fallback-2
            # entry shares the pre-scope 4-tuple (only the credential differs),
            # exactly the case the old tuple comparison was blind to.
            agent.api_key = "sk-fallback-2"
            agent._client_kwargs["api_key"] = "sk-fallback-2"
            agent._fallback_activated = True
            consume_runtime_override(agent)
        # fallback-2's credential stands; the restore did NOT clobber it.
        assert agent.api_key == "sk-fallback-2"
        assert agent._client_kwargs["api_key"] == "sk-fallback-2"
        assert agent.model == "orig-model"
        assert agent.provider == "orig-provider"
        assert agent._runtime_override == {}

    def test_leak2_is_anthropic_oauth_restored_after_supersession(self):
        # LEAK-2: an OAuth agent overrides to a static key for one turn and
        # the fallback supersedes; _is_anthropic_oauth (forced False by the
        # static-key override) must be restored to the pre-override value so
        # the OAuth state is never left permanently clobbered.
        from agent.runtime_override import consume_runtime_override

        agent = _FakeAgent()
        agent._is_anthropic_oauth = True  # OAuth anthropic primary
        with apply_runtime_override(agent, {"api_key": "sk-static"}):
            assert agent._is_anthropic_oauth is False  # static key, no OAuth
            # Fallback supersedes (chat wire, which does not re-derive the flag).
            agent._fallback_activated = True
            consume_runtime_override(agent)
        assert agent._is_anthropic_oauth is True  # restored, not left False

    def test_scope_registers_and_unregisters_itself(self):
        agent = _FakeAgent()
        assert getattr(agent, "_active_runtime_override_scope", None) is None
        with apply_runtime_override(agent, {"model": "m"}):
            assert agent._active_runtime_override_scope is not None
        assert getattr(agent, "_active_runtime_override_scope", None) is None

    def test_nested_scope_outer_wins_registration(self):
        # Scope 2 (wire-time safety net) is created inside Scope 1; it must
        # not steal the registration, or the fallback handoff would find the
        # inner scope after it already exited and miss superseding Scope 1.
        from agent.runtime_override import consume_runtime_override

        agent = _FakeAgent()
        with apply_runtime_override(agent, {"model": "override-model"}):
            outer = agent._active_runtime_override_scope
            with apply_runtime_override(agent, {"api_key": "inner-key"}):
                # Inner scope did not steal the registration.
                assert agent._active_runtime_override_scope is outer
            # Inner scope exiting did not clear the outer registration either.
            assert agent._active_runtime_override_scope is outer
            agent.model = "fallback-model"
            consume_runtime_override(agent)
        assert agent.model == "fallback-model"  # outer scope was superseded
        assert getattr(agent, "_active_runtime_override_scope", None) is None

    def test_consume_runtime_override_clears_the_turn_override(self):
        from agent.runtime_override import consume_runtime_override

        agent = _FakeAgent()
        agent._runtime_override = {"model": "m"}
        consume_runtime_override(agent)
        assert agent._runtime_override == {}

    def test_consume_runtime_override_none_safe_on_bare_agent(self):
        # A bare agent (created via __new__) has neither the registration
        # attribute nor _runtime_override; the handoff must not raise.
        from agent.runtime_override import consume_runtime_override

        agent = object.__new__(_FakeAgent)
        consume_runtime_override(agent)  # must not raise AttributeError
        consume_runtime_override(None)  # must not raise on None either


# ---------------------------------------------------------------------------
# EDGE-4: api_key / api_mode cross-validation on the MERGED override
# ---------------------------------------------------------------------------

class TestApiKeyModeCrossValidation:
    """Multiple plugins' overrides merge per-key; a mismatched credential must
    never ship to the wrong wire."""

    def test_anthropic_mode_with_openai_style_key_drops_the_key(self, caplog):
        # Plugin A contributes api_mode=anthropic_messages, plugin B (later,
        # wins) contributes a provably-OpenAI api_key -> merged conflict.
        agent = _FakeAgent()
        merged = {"api_mode": "anthropic_messages", "api_key": "sk-proj-openai-abc"}
        with caplog.at_level("WARNING", logger="agent.runtime_override"):
            with apply_runtime_override(agent, merged):
                # The mismatched key was dropped before application: the
                # agent's own anthropic credential stays authoritative.
                assert agent.api_key == "orig-key"
                assert agent._anthropic_api_key == "orig-anthropic-key"
        assert merged.get("api_key") is None  # dropped from the canonical source
        assert "does not match api_mode" in caplog.text

    def test_chat_mode_with_anthropic_style_key_drops_the_key(self, caplog):
        agent = _FakeAgent()
        merged = {"api_mode": "chat_completions", "api_key": "sk-ant-api03-xyz"}
        with caplog.at_level("WARNING", logger="agent.runtime_override"):
            with apply_runtime_override(agent, merged):
                assert agent.api_key == "orig-key"
        assert merged.get("api_key") is None

    def test_matching_pair_is_kept(self):
        agent = _FakeAgent()
        merged = {"api_mode": "anthropic_messages", "api_key": "sk-ant-api03-xyz"}
        with apply_runtime_override(agent, merged):
            assert agent.api_key == "sk-ant-api03-xyz"
            assert agent._anthropic_api_key == "sk-ant-api03-xyz"

    def test_generic_sk_key_on_anthropic_wire_is_kept(self):
        # No provable family signal (e.g. "sk-anthropic"): dropping it would
        # break legitimate anthropic-intent credentials.
        agent = _FakeAgent()
        merged = {"api_mode": "anthropic_messages", "api_key": "sk-anthropic"}
        with apply_runtime_override(agent, merged):
            assert agent.api_key == "sk-anthropic"
            assert agent._anthropic_api_key == "sk-anthropic"
        assert merged.get("api_key") == "sk-anthropic"

    def test_custom_key_format_untouched(self):
        # Third-party anthropic-compatible endpoints use non-sk-* keys; those
        # carry no provable family signal and must never be dropped.
        agent = _FakeAgent()
        merged = {"api_mode": "anthropic_messages", "api_key": "minimax-token-9"}
        with apply_runtime_override(agent, merged):
            assert agent.api_key == "minimax-token-9"
        assert merged.get("api_key") == "minimax-token-9"

    def test_no_api_mode_means_no_cross_validation(self):
        agent = _FakeAgent()
        merged = {"api_key": "sk-anything"}
        with apply_runtime_override(agent, merged):
            assert agent.api_key == "sk-anything"
        assert merged.get("api_key") == "sk-anything"
