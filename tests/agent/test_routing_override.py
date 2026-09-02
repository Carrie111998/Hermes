"""Unit tests for the per-turn routing-override interface extension.

A ``pre_llm_call`` hook result may carry a
``{"route": {"model": ..., "provider": ...}}`` override that the runtime applies
BEFORE the turn's client is built, turning a plugin routing DECISION into an
actual model swap.

Design (see agent/routing_override.py):
  - ``extract_routing_override(results)`` pulls the first well-formed override
    out of the list of pre_llm_call hook results.
  - ``apply_routing_override(agent, override)`` actuates the swap by delegating
    to the agent's existing, tested ``switch_model`` machinery, then makes the
    swap TURN-SCOPED via a dedicated flag plus a snapshot of the pre-swap
    ``_primary_runtime`` (reverted next turn by ``restore_primary_runtime``).
    Reactive-fallback state is deliberately left untouched. Fail-safe: any error
    leaves the agent untouched.
"""
import sys
import types
from unittest.mock import MagicMock

import pytest

from agent.routing_override import (
    apply_routing_override,
    extract_routing_override,
)


def _stub_resolver(monkeypatch, fn):
    """Inject a fake ``agent.auxiliary_client`` so the lazy
    ``from agent.auxiliary_client import resolve_provider_client`` in
    apply_routing_override picks up ``fn`` without importing the heavy real
    module (and its runtime deps) at unit-test time."""
    mod = types.ModuleType("agent.auxiliary_client")
    mod.resolve_provider_client = fn
    monkeypatch.setitem(sys.modules, "agent.auxiliary_client", mod)


@pytest.fixture(autouse=True)
def _inert_resolver(monkeypatch):
    """Default every test to a resolver that resolves nothing, so a cross-provider
    route never reaches the real provider registry. Tests that care about
    resolution install their own stub over this one."""
    _stub_resolver(monkeypatch, lambda *a, **k: (None, None))


# ------------------------------------------------ extract_routing_override ----

def test_extract_none_when_no_results():
    assert extract_routing_override([]) is None


def test_extract_none_when_only_context():
    results = [{"context": "[Current model: x]"}, "some string"]
    assert extract_routing_override(results) is None


def test_extract_pulls_route_dict():
    results = [{"context": "line", "route": {"model": "deepseek/deepseek-v3.2",
                                             "provider": "openrouter"}}]
    ov = extract_routing_override(results)
    assert ov == {"model": "deepseek/deepseek-v3.2", "provider": "openrouter"}


def test_extract_requires_model_key():
    # A route dict with no model is not actionable.
    assert extract_routing_override([{"route": {"provider": "openrouter"}}]) is None


def test_extract_first_wins():
    results = [
        {"route": {"model": "a", "provider": "p1"}},
        {"route": {"model": "b", "provider": "p2"}},
    ]
    assert extract_routing_override(results)["model"] == "a"


# ------------------------------------------------- apply_routing_override -----

def _fake_agent():
    agent = MagicMock()
    agent.model = "claude-sonnet-5"
    agent.provider = "anthropic"
    agent._primary_runtime = {"model": "claude-sonnet-5", "provider": "anthropic"}
    agent._fallback_activated = False
    # Set explicitly: a MagicMock would otherwise auto-create these on read and
    # make "was it scoped?" assertions vacuously truthy.
    agent._routing_override_active = False
    agent._routing_override_saved_primary = None

    # switch_model mutates model/provider like the real one, and (like the real
    # one) persists _primary_runtime — we assert the override code re-scopes.
    def _switch(new_model, new_provider, **kw):
        agent.model = new_model
        agent.provider = new_provider
        agent._primary_runtime = {"model": new_model, "provider": new_provider}
        return {"ok": True}

    agent.switch_model.side_effect = _switch
    return agent


def test_apply_actuates_the_swap():
    agent = _fake_agent()
    ok = apply_routing_override(agent, {"model": "deepseek/deepseek-v3.2",
                                        "provider": "openrouter"})
    assert ok is True
    assert agent.model == "deepseek/deepseek-v3.2"
    assert agent.provider == "openrouter"
    agent.switch_model.assert_called_once()


def test_apply_is_turn_scoped_with_dedicated_flag():
    """Scoping uses a DEDICATED flag, NOT reactive-fallback state.

    _primary_runtime stays == the routed runtime (so in-turn recovery paths do
    not jump tiers), the pre-route snapshot is stashed for the revert,
    _routing_override_active is set, and _fallback_activated is left untouched
    (arming it would corrupt reactive cooldown accounting).
    """
    agent = _fake_agent()
    apply_routing_override(agent, {"model": "deepseek/deepseek-v3.2",
                                   "provider": "openrouter"})
    # _primary_runtime is the ROUTED runtime during the routed turn.
    assert agent._primary_runtime == {"model": "deepseek/deepseek-v3.2",
                                      "provider": "openrouter"}
    # The pre-route snapshot is stashed for restore_primary_runtime to revert.
    assert agent._routing_override_saved_primary == {"model": "claude-sonnet-5",
                                                     "provider": "anthropic"}
    assert agent._routing_override_active is True
    # Reactive-fallback state is NOT touched.
    assert agent._fallback_activated is False


def test_second_apply_in_one_turn_keeps_the_pristine_snapshot():
    """A second apply before any revert must not overwrite the snapshot with the
    first route's own runtime — that would make the revert a no-op."""
    agent = _fake_agent()
    apply_routing_override(agent, {"model": "a", "provider": "p1"})
    apply_routing_override(agent, {"model": "b", "provider": "p2"})
    assert agent._routing_override_saved_primary == {"model": "claude-sonnet-5",
                                                     "provider": "anthropic"}


def test_apply_noop_when_target_equals_current():
    """No swap when the override already matches the live model (avoid churn)."""
    agent = _fake_agent()
    ok = apply_routing_override(agent, {"model": "claude-sonnet-5",
                                        "provider": "anthropic"})
    assert ok is False
    agent.switch_model.assert_not_called()
    # _fallback_activated untouched — nothing was swapped.
    assert agent._fallback_activated is False


def test_apply_fail_safe_on_switch_error():
    """If switch_model raises, the agent is left untouched and we return False."""
    agent = _fake_agent()
    agent.switch_model.side_effect = RuntimeError("bad key")
    ok = apply_routing_override(agent, {"model": "deepseek/deepseek-v3.2",
                                        "provider": "openrouter"})
    assert ok is False
    # Original model/provider preserved (switch_model itself rolls back; the
    # applier must not leave a half-state or re-raise into the turn loop).
    assert agent.model == "claude-sonnet-5"
    assert getattr(agent, "_routing_override_active", False) is False


def test_apply_ignores_empty_override():
    agent = _fake_agent()
    assert apply_routing_override(agent, {}) is False
    assert apply_routing_override(agent, None) is False
    agent.switch_model.assert_not_called()


# ---------------- base_url resolution on cross-provider routes ----------------
#
# A plugin typically emits {"model": ..., "provider": ...} with NO base_url.
# switch_model REFUSES a cross-provider switch with no resolved base_url — it
# raises rather than keep the previous provider's endpoint (#47828). Its normal
# callers resolve the URL before calling it; a plugin route does not. So without
# pre-resolution the route never takes effect at all: the ValueError is caught by
# apply_routing_override's fail-safe and the turn stays on the configured
# primary. apply_routing_override must therefore resolve the new provider's
# canonical endpoint up front when the override omits it AND the provider
# changes. test_real_switch_model_refuses_cross_provider_without_base_url below
# pins that upstream behavior against the real function.


class _FakeClient:
    def __init__(self, base_url, api_key):
        self.base_url = base_url
        self.api_key = api_key


def test_apply_resolves_base_url_when_provider_changes(monkeypatch):
    agent = _fake_agent()  # primary: anthropic
    seen = {}

    def _resolve(provider, model=None, **kw):
        seen["provider"] = provider
        seen["model"] = model
        return _FakeClient("https://openrouter.ai/api/v1/", "sk-or-test"), model

    _stub_resolver(monkeypatch, _resolve)

    ok = apply_routing_override(
        agent, {"model": "deepseek/deepseek-v3.2", "provider": "openrouter"}
    )
    assert ok is True
    assert seen["provider"] == "openrouter"
    _, kwargs = agent.switch_model.call_args
    # The resolved endpoint (and key) must be forwarded so switch_model does NOT
    # inherit the Anthropic primary's base_url.
    assert kwargs.get("base_url") == "https://openrouter.ai/api/v1/"
    assert kwargs.get("api_key") == "sk-or-test"


def test_apply_does_not_resolve_when_provider_unchanged(monkeypatch):
    """Same-provider route (e.g. anthropic sonnet → anthropic haiku) must not
    inject base_url — switch_model correctly keeps the same-provider base_url
    and re-derives api_mode. Forcing a resolve here is needless and risky."""
    agent = _fake_agent()  # primary: anthropic

    def _resolve(*a, **k):
        raise AssertionError("resolve_provider_client should not be called")

    _stub_resolver(monkeypatch, _resolve)

    ok = apply_routing_override(
        agent, {"model": "claude-haiku-4-5-20251001", "provider": "anthropic"}
    )
    assert ok is True
    _, kwargs = agent.switch_model.call_args
    assert "base_url" not in kwargs


def test_apply_preserves_explicit_base_url(monkeypatch):
    """An override that DOES carry base_url wins — no resolve, no overwrite."""
    agent = _fake_agent()

    def _resolve(*a, **k):
        raise AssertionError("resolve_provider_client should not be called")

    _stub_resolver(monkeypatch, _resolve)

    apply_routing_override(
        agent,
        {
            "model": "deepseek/deepseek-v3.2",
            "provider": "openrouter",
            "base_url": "https://custom.example/v1",
        },
    )
    _, kwargs = agent.switch_model.call_args
    assert kwargs["base_url"] == "https://custom.example/v1"


def test_apply_resolve_failure_is_fail_safe(monkeypatch):
    """A resolver error is swallowed, not propagated.

    apply_routing_override falls through to switch_model without a base_url and
    lets it decide. (With the real switch_model a cross-provider call in that
    state raises, and the except below turns it into "stay on the configured
    primary" — never an exception out of the turn.)"""
    agent = _fake_agent()

    def _resolve(*a, **k):
        raise RuntimeError("registry down")

    _stub_resolver(monkeypatch, _resolve)

    ok = apply_routing_override(
        agent, {"model": "deepseek/deepseek-v3.2", "provider": "openrouter"}
    )
    assert ok is True
    agent.switch_model.assert_called_once()


# ---------------- premise check against the real switch_model ----------------


def test_real_switch_model_refuses_cross_provider_without_base_url():
    """Pin the upstream behavior the pre-resolution above exists for.

    Uses the REAL ``switch_model``, not a fake: a provider change with no
    base_url must raise, and must roll the agent back untouched.
    """
    from agent import agent_runtime_helpers

    class _BareAgent:
        def _read_reasoning_echo_from_config(self):
            return False

    a = _BareAgent()
    a.model = "gpt-5"
    a.provider = "openai"
    a.base_url = "https://api.openai.com/v1"

    with pytest.raises(ValueError, match="no base_url resolved"):
        agent_runtime_helpers.switch_model(a, "deepseek/deepseek-v3.2", "openrouter")

    # Atomic rollback: nothing about the agent changed.
    assert a.model == "gpt-5"
    assert a.provider == "openai"
    assert a.base_url == "https://api.openai.com/v1"
