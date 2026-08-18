"""Enforcement read #1: applying a model-rate-limit reroute at agent init.

This is the read that makes crons work. A fresh process resolves its model,
sees the override, and starts on the replacement -- so the
``agent._primary_runtime`` snapshot taken right after ``_apply_model_override``
records the *replacement* as primary and the process never pays the 429 at
all. See ``agent/agent_init.py`` for the placement rationale.

THE CENTRAL INVARIANT: with no override, resolution is byte-identical.
Phase 1 shipped a Critical defect where a telemetry-only parameter reached a
behavioral branch and silently pinned sessions to a fallback for 60 seconds;
nine task-scoped reviews missed it. ``test_no_override_leaves_model_untouched``
below is the regression guard for that class of bug in Phase 2.
"""
import pytest
from unittest.mock import MagicMock

from agent.agent_init import _apply_model_override


class _FakeClient:
    """Stand-in for the OpenAI-compatible client resolve_provider_client
    returns. Only the attributes _apply_model_override actually reads."""

    def __init__(self, base_url="https://api.example.com/v1", api_key="test-key"):
        self.base_url = base_url
        self.api_key = api_key


def _fake_agent(*, provider, model):
    class _Agent:
        pass

    agent = _Agent()
    agent.provider = provider
    agent.model = model
    agent.base_url = "https://api.deepseek.com"
    agent.api_mode = "chat_completions"
    agent.api_key = "primary-key"
    agent.client = object()
    agent._client_kwargs = {}
    return agent


@pytest.fixture(autouse=True)
def _stub_resolve_provider_client(monkeypatch):
    """Client construction talks to real provider auth in production
    (resolve_provider_client). Stub it so these tests exercise
    _apply_model_override's swap logic in isolation rather than depending on
    live credentials -- the hermetic-test conftest strips every credential
    env var, so an unstubbed call here would always resolve to (None, None)
    and every "override applied" assertion would fail for the wrong reason.
    """

    def _fake_resolve(provider, model=None, **kwargs):
        return _FakeClient(), model

    monkeypatch.setattr(
        "agent.auxiliary_client.resolve_provider_client", _fake_resolve)
    return None


def test_no_override_leaves_model_untouched(monkeypatch):
    """THE CENTRAL INVARIANT: with no override, resolution is byte-identical."""
    from events import model_override
    monkeypatch.setattr(model_override, "get_override", lambda p, m: None)
    agent = _fake_agent(provider="deepseek", model="deepseek-v4-pro")
    _apply_model_override(agent)
    assert (agent.provider, agent.model) == ("deepseek", "deepseek-v4-pro")


def test_active_override_swaps_before_the_snapshot(monkeypatch):
    from events import model_override
    monkeypatch.setattr(model_override, "get_override", lambda p, m: {
        "replacement_provider": "openai-codex", "replacement_model": "gpt-5.6-sol"})
    agent = _fake_agent(provider="deepseek", model="deepseek-v4-pro")
    _apply_model_override(agent)
    assert (agent.provider, agent.model) == ("openai-codex", "gpt-5.6-sol")


def test_override_read_failure_never_breaks_init(monkeypatch):
    from events import model_override
    monkeypatch.setattr(model_override, "get_override",
                        lambda p, m: (_ for _ in ()).throw(RuntimeError("boom")))
    agent = _fake_agent(provider="deepseek", model="deepseek-v4-pro")
    _apply_model_override(agent)   # must not raise
    assert agent.model == "deepseek-v4-pro"


def test_anthropic_replacement_sets_native_client_state(monkeypatch):
    """CRITICAL: swapping onto an Anthropic replacement must not just flip
    api_mode -- it must also build the native client state that
    api_mode == "anthropic_messages" dispatch reads with no getattr default
    (agent._create_request_anthropic_client, run_agent.py:4404-4409). An
    agent whose ORIGINAL api_mode was chat_completions/codex has none of
    these attributes, so a string-only swap produces an AttributeError on
    the very next request -- a broken agent, not a degrade-to-primary.
    """
    from events import model_override
    monkeypatch.setattr(model_override, "get_override", lambda p, m: {
        "replacement_provider": "anthropic", "replacement_model": "claude-x"})
    agent = _fake_agent(provider="deepseek", model="deepseek-v4-pro")
    _apply_model_override(agent)

    assert agent.api_mode == "anthropic_messages"
    assert (agent.provider, agent.model) == ("anthropic", "claude-x")
    assert getattr(agent, "_anthropic_api_key", None)
    assert getattr(agent, "_anthropic_base_url", None) is not None
    assert getattr(agent, "_anthropic_client", None) is not None
    assert hasattr(agent, "_is_anthropic_oauth")


def test_unresolvable_replacement_leaves_agent_on_primary(monkeypatch):
    """IMPORTANT: the autouse _stub_resolve_provider_client fixture always
    returns a working fake client, so `if new_client is None: return` is
    unreachable anywhere else in this file. Override the stub per-test to
    exercise that named global constraint directly.
    """
    from events import model_override
    monkeypatch.setattr(model_override, "get_override", lambda p, m: {
        "replacement_provider": "openai-codex", "replacement_model": "gpt-5.6-sol"})
    monkeypatch.setattr(
        "agent.auxiliary_client.resolve_provider_client",
        lambda provider, model=None, **kwargs: (None, None),
    )
    agent = _fake_agent(provider="deepseek", model="deepseek-v4-pro")
    _apply_model_override(agent)
    assert (agent.provider, agent.model) == ("deepseek", "deepseek-v4-pro")


# =============================================================================
# Enforcement read #2: agent_runtime_helpers.restore_primary_runtime()
#
# A long-lived gateway session (agents are cached across messages -- see
# ``_agent_cache`` in gateway/run.py) must return to the real primary once a
# reroute EXPIRES, instead of staying diverted for the whole process
# lifetime. restore_primary_runtime() re-checks the override for the
# snapshotted primary (agent._primary_runtime) every time it is about to
# restore it: gone/expired -> restore proceeds as before; still active ->
# stay on the current fallback rather than bouncing back onto a model that
# is still rate-limited.
# =============================================================================

def _make_real_agent(provider="deepseek", model="deepseek-v4-pro",
                      base_url="https://api.deepseek.com",
                      fallback_model=None):
    """A real AIAgent, client construction stubbed out. restore_primary_
    runtime() touches context_compressor/credential_pool/transport-cache
    internals that a bare fake object doesn't have -- the real agent is the
    lightest way to exercise the whole restore path faithfully."""
    from unittest.mock import patch as _patch
    from run_agent import AIAgent

    with (
        _patch("run_agent.get_tool_definitions", return_value=[]),
        _patch("run_agent.check_toolset_requirements", return_value={}),
        _patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url=base_url,
            provider=provider,
            model=model,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            fallback_model=fallback_model,
        )
        agent.client = MagicMock()
    return agent


def _activated_agent():
    """Real agent that has already turn-scope-activated a fallback -- the
    state restore_primary_runtime() is called on."""
    from unittest.mock import patch as _patch

    agent = _make_real_agent(
        fallback_model={"provider": "openai-codex", "model": "gpt-5.6-sol"},
    )
    mock_client = MagicMock()
    mock_client.api_key = "fallback-key"
    mock_client.base_url = "https://api.openai.com/v1"
    with _patch("agent.auxiliary_client.resolve_provider_client",
                return_value=(mock_client, None)):
        agent._try_activate_fallback()

    assert agent._fallback_activated is True
    assert (agent.provider, agent.model) == ("openai-codex", "gpt-5.6-sol")
    return agent


def test_no_override_regression_restore_unchanged(monkeypatch):
    """THE CENTRAL INVARIANT for read #2: with no override, restore_primary_
    runtime() behaves exactly as it did before this change -- it restores
    the snapshotted primary and clears fallback state."""
    from events import model_override
    monkeypatch.setattr(model_override, "get_override", lambda p, m: None)

    agent = _activated_agent()

    from unittest.mock import patch as _patch
    with _patch("run_agent.OpenAI", return_value=MagicMock()):
        result = agent._restore_primary_runtime()

    assert result is True
    assert agent._fallback_activated is False
    assert agent._fallback_index == 0
    assert (agent.provider, agent.model) == ("deepseek", "deepseek-v4-pro")


def test_expired_override_restores_real_primary(monkeypatch):
    """(a) An override that is gone/expired by restore time must not block
    the normal restore -- the session returns to its real primary."""
    from events import model_override

    agent = _activated_agent()
    # Simulate the reroute having expired between activation and restore.
    monkeypatch.setattr(model_override, "get_override", lambda p, m: None)

    from unittest.mock import patch as _patch
    with _patch("run_agent.OpenAI", return_value=MagicMock()):
        result = agent._restore_primary_runtime()

    assert result is True
    assert (agent.provider, agent.model) == ("deepseek", "deepseek-v4-pro")


def test_unexpired_override_keeps_replacement(monkeypatch):
    """(b) A still-active override must keep the session on the replacement
    instead of bouncing back onto the still-rate-limited primary."""
    from events import model_override

    agent = _activated_agent()
    monkeypatch.setattr(model_override, "get_override", lambda p, m: {
        "replacement_provider": "openai-codex", "replacement_model": "gpt-5.6-sol",
    })

    result = agent._restore_primary_runtime()

    assert result is False
    assert agent._fallback_activated is True  # stayed diverted
    assert (agent.provider, agent.model) == ("openai-codex", "gpt-5.6-sol")


def test_override_lookup_failure_fails_open_to_restore(monkeypatch):
    """Failure-path test for the get_override stub above: a broken override
    lookup must not permanently strand a long-lived session on its
    fallback -- it fails open to the normal restore."""
    from events import model_override

    agent = _activated_agent()

    def _boom(provider, model):
        raise RuntimeError("reroute state unreadable")
    monkeypatch.setattr(model_override, "get_override", _boom)

    from unittest.mock import patch as _patch
    with _patch("run_agent.OpenAI", return_value=MagicMock()):
        result = agent._restore_primary_runtime()

    assert result is True
    assert (agent.provider, agent.model) == ("deepseek", "deepseek-v4-pro")


# =============================================================================
# Enforcement read #3: chat_completion_helpers.try_activate_fallback()
#
# A chain entry that currently has an open rate-limit episode (see
# events/rate_limit_signal._load_state()/_episode_key()) is skipped, so a
# dodge away from the primary does not land on a fallback model that is
# already known to be limited. This must NEVER be wired through the
# `reason` parameter -- see the module docstring on try_activate_fallback
# and the HARD PROHIBITION in this task's brief.
# =============================================================================

def _fake_fallback_client():
    client = MagicMock()
    client.base_url = "https://api.openai.com/v1"
    client.api_key = "sk-test"
    return client


def _agent_with_fallback_chain(chain):
    """MagicMock agent wired so the recursive
    ``agent._try_activate_fallback(...)`` skip-and-continue calls really
    walk the chain, instead of hitting an unrelated auto-generated Mock."""
    from agent import chat_completion_helpers as cch

    agent = MagicMock()
    agent.provider = "deepseek"
    agent.model = "deepseek-v4-pro"
    agent.base_url = "https://api.deepseek.com"
    agent._fallback_chain = chain
    agent._fallback_index = 0
    agent._fallback_activated = False
    agent._primary_runtime = {"provider": "deepseek"}
    agent._unavailable_fallback_keys = set()
    agent._credential_pool = None
    agent._rate_limited_until = 0
    agent._anthropic_prompt_cache_policy.return_value = (True, False)
    agent.context_compressor = None
    agent._try_activate_fallback = (
        lambda reason=None, telemetry_reason=None:
            cch.try_activate_fallback(agent, reason, telemetry_reason=telemetry_reason)
    )
    return agent


def test_chain_entry_with_open_episode_is_skipped(monkeypatch):
    """(c) A chain entry with an open rate-limit episode is skipped in favor
    of the next entry."""
    from agent import chat_completion_helpers as cch

    agent = _agent_with_fallback_chain([
        {"provider": "openai-codex", "model": "gpt-5.6-sol"},
        {"provider": "anthropic", "model": "claude-x"},
    ])
    monkeypatch.setattr(
        "events.rate_limit_signal._load_state",
        lambda: {"openai-codex/gpt-5.6-sol": {"opened_at": "2026-08-18T00:00:00Z"}},
    )
    monkeypatch.setattr(
        "agent.auxiliary_client.resolve_provider_client",
        lambda *a, **k: (_fake_fallback_client(), None),
    )

    assert cch.try_activate_fallback(agent) is True

    assert (agent.provider, agent.model) == ("anthropic", "claude-x"), (
        "the rate-limited first entry should have been skipped"
    )
    assert agent._fallback_index == 2


def test_no_open_episodes_chain_walk_unchanged(monkeypatch):
    """(d) THE CENTRAL INVARIANT for read #3: with no open episodes (and no
    override), chain-walking picks the first entry exactly as it did before
    this change."""
    from agent import chat_completion_helpers as cch

    agent = _agent_with_fallback_chain([
        {"provider": "openai-codex", "model": "gpt-5.6-sol"},
    ])
    monkeypatch.setattr("events.rate_limit_signal._load_state", lambda: {})
    monkeypatch.setattr(
        "agent.auxiliary_client.resolve_provider_client",
        lambda *a, **k: (_fake_fallback_client(), None),
    )

    assert cch.try_activate_fallback(agent) is True
    assert (agent.provider, agent.model) == ("openai-codex", "gpt-5.6-sol")
    assert agent._fallback_index == 1


def test_episode_lookup_failure_fails_open(monkeypatch):
    """Failure-path test for the _load_state stub above: a broken
    episode-state read must not block failover -- fail open (no skip)
    rather than break the one thing this chain exists to do."""
    from agent import chat_completion_helpers as cch

    agent = _agent_with_fallback_chain([
        {"provider": "openai-codex", "model": "gpt-5.6-sol"},
    ])

    def _boom():
        raise RuntimeError("rate limit state file unreadable")
    monkeypatch.setattr("events.rate_limit_signal._load_state", _boom)
    monkeypatch.setattr(
        "agent.auxiliary_client.resolve_provider_client",
        lambda *a, **k: (_fake_fallback_client(), None),
    )

    assert cch.try_activate_fallback(agent) is True
    assert (agent.provider, agent.model) == ("openai-codex", "gpt-5.6-sol")


def test_all_chain_entries_rate_limited_exhausts_chain(monkeypatch):
    """Every entry currently limited -> the chain exhausts (returns False)
    exactly like walking an empty chain, instead of silently landing on a
    model that is already known to be rate-limited."""
    from agent import chat_completion_helpers as cch

    agent = _agent_with_fallback_chain([
        {"provider": "openai-codex", "model": "gpt-5.6-sol"},
        {"provider": "anthropic", "model": "claude-x"},
    ])
    monkeypatch.setattr(
        "events.rate_limit_signal._load_state",
        lambda: {
            "openai-codex/gpt-5.6-sol": {"opened_at": "2026-08-18T00:00:00Z"},
            "anthropic/claude-x": {"opened_at": "2026-08-18T00:00:00Z"},
        },
    )

    assert cch.try_activate_fallback(agent) is False


def test_skip_does_not_key_off_reason_parameter(monkeypatch):
    """Guard against the exact Phase-1 defect this task's brief warns about:
    the open-episode skip must fire identically regardless of what `reason`
    (or `telemetry_reason`) is passed -- it must never reuse that parameter
    for its own behavior."""
    from agent.error_classifier import FailoverReason
    from agent import chat_completion_helpers as cch

    monkeypatch.setattr(
        "events.rate_limit_signal._load_state",
        lambda: {"openai-codex/gpt-5.6-sol": {"opened_at": "2026-08-18T00:00:00Z"}},
    )
    monkeypatch.setattr(
        "agent.auxiliary_client.resolve_provider_client",
        lambda *a, **k: (_fake_fallback_client(), None),
    )

    for reason, telemetry_reason in (
        (None, None),
        (FailoverReason.rate_limit, None),
        (None, FailoverReason.upstream_rate_limit),
    ):
        agent = _agent_with_fallback_chain([
            {"provider": "openai-codex", "model": "gpt-5.6-sol"},
            {"provider": "anthropic", "model": "claude-x"},
        ])
        cch.try_activate_fallback(
            agent, reason, telemetry_reason=telemetry_reason)
        assert (agent.provider, agent.model) == ("anthropic", "claude-x")
