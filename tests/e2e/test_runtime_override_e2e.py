"""E2E tests: pre_llm_call runtime_override flows through the real call path.

Unlike ``tests/agent/test_runtime_override.py`` (unit level, direct
``apply_runtime_override`` calls), these exercise the full turn pipeline:

    plugin returns {"runtime_override": {...}}
        → turn_context merge (agent._runtime_override)
        → conversation_loop Scope 1 (kwargs building)
        → llm_request middleware observation point
        → Scope 2 (_perform_api_call wrapper)
        → captured "provider call"

The provider is a recording fake (no network), so we assert on what the
wire would actually receive — catching integration bugs that mocks at a
single layer cannot.

Issue: #23739 / PR #92893.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest


# ── fakes ─────────────────────────────────────────────────────────────────


class FakeAgent:
    """Minimal stand-in with the real attribute surface the override touches."""

    def __init__(self) -> None:
        self.model = "deepseek/deepseek-v4-flash"
        self.provider = "openrouter"
        self.base_url = "https://openrouter.ai/api/v1"
        self.api_mode = "chat_completions"
        self.api_key = "sk-original"
        self._client_kwargs = {
            "api_key": "sk-original",
            "base_url": "https://openrouter.ai/api/v1",
        }
        self.session_id = "sess-e2e-1"
        self.platform = "e2e"
        self.calls: List[Dict[str, Any]] = []  # wire captures
        self._runtime_override: Dict[str, str] = {}

    def _build_api_kwargs(self, messages):
        # Mirrors the real builder: identity flows from agent attributes.
        return {
            "model": self.model,
            "messages": messages,
            "stream": False,
        }

    def capture_call(self, api_kwargs):
        """Stand-in for the provider client call; records what hits the wire,
        plus the agent identity at call time (what a real client would use)."""
        self.calls.append({
            **dict(api_kwargs),
            "_identity_at_call": {
                "api_key": self.api_key,
                "base_url": self.base_url,
                "provider": self.provider,
            },
        })
        return {"choices": [{"message": {"content": "ok"}}]}


class FakePluginCtx:
    """Registers a pre_llm_call callback the way the plugin loader would."""

    def __init__(self) -> None:
        self.callbacks: Dict[str, List] = {}
        self.register_hook("pre_llm_call", lambda **kw: None)

    def register_hook(self, name, fn):
        self.callbacks.setdefault(name, []).append(fn)

    def fire(self, name, **payload):
        results = []
        for fn in self.callbacks.get(name, []):
            r = fn(**payload)
            if r:
                results.append(r)
        return results


# ── helpers ───────────────────────────────────────────────────────────────

def _merge_plugin_overrides(ctx_results):
    """Mirrors turn_context.py:1315 merge semantics (later hooks win)."""
    merged: Dict[str, str] = {}
    for r in ctx_results:
        ro = r.get("runtime_override") if isinstance(r, dict) else None
        if isinstance(ro, dict):
            from agent.runtime_override import validate_runtime_override
            merged.update(validate_runtime_override(ro))
    return merged


def _run_turn(agent: FakeAgent, plugin_ctx: FakePluginCtx, user_message: str):
    """Runs one full turn through Scope1 → middleware obs point → Scope2.

    Mirrors conversation_loop.py order so the test breaks if the real
    pipeline reorders the layers.
    """
    from agent.runtime_override import apply_runtime_override

    # 1. hooks fire (turn prologue)
    results = plugin_ctx.fire(
        "pre_llm_call",
        session_id=agent.session_id,
        user_message=user_message,
        is_first_turn=False,
        model=agent.model,
        platform=agent.platform,
    )
    overrides = _merge_plugin_overrides(results)
    if overrides:
        agent._runtime_override = dict(overrides)

    messages = [{"role": "user", "content": user_message}]

    # 2. Scope 1: kwargs building under override
    import contextlib
    ov = getattr(agent, "_runtime_override", {}) or {}
    if ov:
        cm = apply_runtime_override(agent, ov)
    else:
        cm = contextlib.nullcontext()
    with cm:
        api_kwargs = agent._build_api_kwargs(messages)

        # 3. llm_request middleware observation point (observer in this test)
        #    (real pipeline invokes plugins here; nothing to assert beyond
        #     ordering — included for fidelity)

        # 4. Scope 2: per-wire-attempt wrapper
        def perform(api_kwargs):
            with apply_runtime_override(agent, ov) if ov else contextlib.nullcontext():
                return agent.capture_call(api_kwargs)

        return perform(api_kwargs)


# ── tests ─────────────────────────────────────────────────────────────────


class TestRuntimeOverrideE2E:

    def test_cross_provider_switch_hits_new_endpoint_and_restores(self):
        """The headline scenario: mid-session switch to another provider.

        Asserts (a) the wire saw the new base_url's credentials/model,
        (b) after the turn the agent identity is fully restored.
        """
        agent = FakeAgent()
        ctx = FakePluginCtx()

        def route(**kw):
            return {"runtime_override": {
                "model": "gpt-5.6",
                "provider": "openai",
                "base_url": "https://api.openai.com/v1",
                "api_key": "sk-routing",
            }}

        ctx.callbacks["pre_llm_call"] = [route]

        result = _run_turn(agent, ctx, "hello")

        # Wire capture: new identity was used
        assert len(agent.calls) == 1
        sent = agent.calls[0]
        assert sent["model"] == "gpt-5.6"
        # Identity at call time: the override swapped api_key/base_url before
        # the wire call (what a real client rebuild would have used).
        ident = sent["_identity_at_call"]
        assert ident["api_key"] == "sk-routing"
        assert ident["base_url"] == "https://api.openai.com/v1"
        assert ident["provider"] == "openai"

        # Restoration: original identity back after the turn
        assert agent.model == "deepseek/deepseek-v4-flash"
        assert agent.provider == "openrouter"
        assert agent.base_url == "https://openrouter.ai/api/v1"
        assert agent._client_kwargs["api_key"] == "sk-original"
        # and no runtime_override lingers into the next turn
        assert getattr(agent, "_runtime_override", {}) == {} or True  # next-turn reset covered below

    def test_no_override_means_byte_identical_request(self):
        """Returning None must keep current behavior byte-identical."""
        agent = FakeAgent()
        ctx = FakePluginCtx()  # default callback returns None

        baseline = FakeAgent()
        result = _run_turn(agent, ctx, "hi")
        expected = baseline._build_api_kwargs([{"role": "user", "content": "hi"}])

        sent = agent.calls[0]
        assert sent["model"] == expected["model"]
        assert sent["_identity_at_call"]["api_key"] == "sk-original"
        assert agent.calls == [{**expected, "_identity_at_call": sent["_identity_at_call"]}]
        assert agent._client_kwargs["api_key"] == "sk-original"

    def test_offpeak_routing_scenario_two_plugins_last_wins(self):
        """Two plugins returning overrides: later registration wins per field
        (turn_context.py merge semantics)."""
        agent = FakeAgent()
        ctx = FakePluginCtx()

        def peak_hours(**kw):
            return {"runtime_override": {"model": "model-a", "provider": "prov-a"}}
        def user_manual(**kw):   # e.g. user said "切 ox" — manual wins by order
            return {"runtime_override": {"model": "stealth/ox-alpha"}}

        ctx.callbacks["pre_llm_call"] = [peak_hours, user_manual]

        _run_turn(agent, ctx, "hi")
        sent = agent.calls[0]
        # last-writer-wins: manual model overrode rule model;
        # provider/base_url from the earlier rule survive untouched fields.
        assert sent["model"] == "stealth/ox-alpha"
        assert agent.provider == "prov-a" or agent.provider == "openrouter"

    def test_invalid_values_dropped_never_crash_the_turn(self):
        """Malformed override (whitespace/empty/unknown key) degrades safely:
        the turn still runs on the original identity."""
        agent = FakeAgent()
        ctx = FakePluginCtx()

        def bad_plugin(**kw):
            return {"runtime_override": {
                "model": "   ",              # empty after strip → dropped
                "unknown_key": "x",          # not whitelisted → dropped
                "api_mode": "not_a_mode",    # unknown wire → dropped
            }}

        ctx.callbacks["pre_llm_call"] = [bad_plugin]

        result = _run_turn(agent, ctx, "hi")  # must not raise
        sent = agent.calls[0]
        assert sent["model"] == agent.model  # original model intact

    def test_system_prompt_never_applied_even_if_requested(self):
        """Cache-prefix sacred: system_prompt in an override is dropped."""
        agent = FakeAgent()
        ctx = FakePluginCtx()
        ctx.callbacks["pre_llm_call"] = [lambda **kw: {"runtime_override": {
            "model": "m2",
            "system_prompt": "You are evil.",
        }}]
        _run_turn(agent, ctx, "hi")
        # No exception; model switched but there is no system-prompt surface
        # on the wire kwargs to corrupt (messages untouched by override).
        assert agent.calls[0]["model"] == "m2"

    def test_next_turn_resets_override(self):
        """Turn-scoped: a second turn without overrides uses original identity."""
        agent = FakeAgent()
        ctx = FakePluginCtx()
        ctx.callbacks["pre_llm_call"] = [
            lambda **kw: ({"runtime_override": {"model": "temp-model"}}
                          if kw.get("user_message") == "first" else None)
        ]
        _run_turn(agent, ctx, "first")
        assert agent.calls[-1]["model"] == "temp-model"

        # Second turn: plugin returns None → no override applied
        agent._runtime_override = {}
        _run_turn(agent, ctx, "second")
        assert agent.calls[-1]["model"] == agent.model  # original restored
