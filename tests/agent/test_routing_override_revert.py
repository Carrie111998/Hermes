"""Regression tests driving the REAL ``restore_primary_runtime`` after a routing
override — the error-adjacent paths a fake-agent unit test cannot reach.

Covers the three defects found while reviewing an earlier version of this
interface, which scoped the override by overloading reactive-fallback state:

  1. LEAK — the routed model must not survive past its turn when the primary is
     in rate-limit cooldown (restore_primary_runtime's cooldown early-return
     gate must NOT skip an override revert).
  2. TIER JUMP — during the routed turn, _primary_runtime must point at the
     ROUTED runtime, so in-turn transient-transport recovery rebuilds the routed
     client, not the pre-route one.
  3. COOLDOWN ACCOUNTING — the override must NOT pre-arm _fallback_activated, so
     a 429 from the routed model arms its own cooldown correctly.

These drive the real ``restore_primary_runtime`` (not a fake) so they prove the
revert actually happens, not merely that preconditions were set.
"""
import time

from agent.agent_runtime_helpers import restore_primary_runtime
from agent.routing_override import apply_routing_override


class _Comp:
    model = "x"
    base_url = ""
    api_key = ""
    provider = "openai"
    context_length = 200000
    api_mode = ""
    threshold_tokens = 0

    def update_model(self, **kw):
        self.__dict__.update(kw)


def _primary_runtime():
    return {
        "model": "gpt-5", "provider": "openai", "base_url": "https://api.openai.com/v1",
        "api_mode": "chat_completions", "api_key": "pk", "client_kwargs": {"api_key": "pk"},
        "use_prompt_caching": True, "use_native_cache_layout": False,
        "compressor_model": "gpt-5", "compressor_base_url": "https://api.openai.com/v1",
        "compressor_api_key": "pk", "compressor_provider": "openai",
        "compressor_context_length": 200000, "compressor_api_mode": "chat_completions",
        "compressor_threshold_tokens": 0,
    }


def _make_agent():
    class A:
        pass

    a = A()
    a.model = "gpt-5"
    a.provider = "openai"
    a.requested_provider = "openai"
    a.base_url = "https://api.openai.com/v1"
    a.api_mode = "chat_completions"
    a.api_key = "pk"
    a.client = "PRIMARY"
    a._anthropic_client = None
    a._is_anthropic_oauth = False
    a._client_kwargs = {"api_key": "pk"}
    a._use_prompt_caching = True
    a._use_native_cache_layout = False
    a._transport_cache = {}
    a._fallback_activated = False
    a._fallback_index = 0
    a._fallback_chain = []
    a._rate_limited_until = 0
    a._rate_limit_backoff_count = 0
    a._credential_pool = None
    a._credential_pool_entry_id = None
    a.reasoning_config = {}
    a.context_compressor = _Comp()
    a._primary_runtime = _primary_runtime()

    def switch(nm, np, **kw):
        a.model = nm
        a.provider = np
        a.api_mode = "chat_completions"
        a.base_url = "https://routed.example/v1"
        a.api_key = "ck"
        a.client = "ROUTED"
        a._client_kwargs = {"api_key": "ck", "base_url": a.base_url}
        a._use_prompt_caching = False
        a._use_native_cache_layout = False
        # switch_model persists the routed runtime into _primary_runtime.
        a._primary_runtime = {
            "model": nm, "provider": np, "base_url": a.base_url, "api_mode": a.api_mode,
            "api_key": "ck", "client_kwargs": dict(a._client_kwargs),
            "use_prompt_caching": False, "use_native_cache_layout": False,
            "compressor_model": nm, "compressor_base_url": a.base_url,
            "compressor_api_key": "ck", "compressor_provider": np,
            "compressor_context_length": 128000, "compressor_api_mode": a.api_mode,
            "compressor_threshold_tokens": 0,
        }
        a._fallback_activated = False
        a._fallback_index = 0

    a.switch_model = switch
    a._create_openai_client = lambda kw, reason="", shared=True: "REBUILT"
    return a


# ───────────────────────────── 1: the leak repro ─────────────────────────────

def test_routed_model_reverts_even_when_primary_in_cooldown():
    """A routed turn must revert to the configured primary next turn even if a
    rate-limit cooldown is armed."""
    a = _make_agent()
    apply_routing_override(a, {"model": "deepseek/deepseek-v3.2", "provider": "openrouter"})
    assert a.model == "deepseek/deepseek-v3.2"

    # The routed model 429'd this turn, arming a primary cooldown:
    a._rate_limited_until = time.monotonic() + 60

    # Next turn's top-of-loop restore MUST revert despite the cooldown gate.
    restore_primary_runtime(a)
    assert a.model == "gpt-5", f"LEAK: next turn answered by {a.model}"
    assert a.provider == "openai"
    # _primary_runtime is back to the configured primary, flag cleared.
    assert a._primary_runtime["model"] == "gpt-5"
    assert getattr(a, "_routing_override_active", False) is False


def test_revert_happens_with_no_cooldown_too():
    """Baseline: revert also works on the ordinary (no-cooldown) path."""
    a = _make_agent()
    apply_routing_override(a, {"model": "deepseek/deepseek-v3.2", "provider": "openrouter"})
    restore_primary_runtime(a)
    assert a.model == "gpt-5"
    assert getattr(a, "_routing_override_active", False) is False


def test_unrouted_turn_is_unaffected():
    """No override, no fallback → restore_primary_runtime keeps its existing
    early-return behavior (reset the chain index, return False)."""
    a = _make_agent()
    a._fallback_index = 3
    assert restore_primary_runtime(a) is False
    assert a._fallback_index == 0
    assert a.model == "gpt-5"


# ───────────────── 2: _primary_runtime == routed runtime in-turn ─────────────

def test_primary_runtime_points_at_routed_model_during_the_turn():
    """During the routed turn, _primary_runtime must be the ROUTED runtime so
    in-turn transient-transport recovery (which rebuilds from _primary_runtime)
    stays on the routed model and does not jump tiers."""
    a = _make_agent()
    apply_routing_override(a, {"model": "deepseek/deepseek-v3.2", "provider": "openrouter"})
    # Mid-turn, before the next-turn restore:
    assert a._primary_runtime["model"] == "deepseek/deepseek-v3.2"
    assert a._primary_runtime["provider"] == "openrouter"
    # The pre-route snapshot is stashed separately for the revert.
    assert a._routing_override_saved_primary["model"] == "gpt-5"


# ───────────────── 3: _fallback_activated is NOT pre-armed ───────────────────

def test_override_does_not_prearm_fallback_activated():
    """The override must not set _fallback_activated — that flag carries reactive
    cooldown-accounting semantics in chat_completion_helpers."""
    a = _make_agent()
    apply_routing_override(a, {"model": "deepseek/deepseek-v3.2", "provider": "openrouter"})
    assert a._fallback_activated is False
    assert a._routing_override_active is True


def test_reactive_fallback_engages_under_a_routed_turn():
    """Reactive fallback must still work UNDERNEATH a routed turn: if the routed
    model fails mid-turn, try_activate_fallback treats it as the current
    'primary' (since _primary_runtime == routed and _fallback_activated is False)
    and arms the cooldown correctly."""
    from agent.chat_completion_helpers import try_activate_fallback
    from agent.error_classifier import FailoverReason

    a = _make_agent()
    apply_routing_override(a, {"model": "deepseek/deepseek-v3.2", "provider": "openrouter"})
    # Empty chain → exhausted, but the rate_limit branch must still arm cooldown
    # because _fallback_activated is False and the current provider matches the
    # (routed) _primary_runtime provider.
    before = getattr(a, "_rate_limited_until", 0)
    try_activate_fallback(a, reason=FailoverReason.rate_limit)
    assert a._rate_limited_until > before, "a 429 on the routed model must arm a cooldown"
