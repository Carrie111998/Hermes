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
        # Real AIAgents always have this (run_agent.py:1423); the swap now
        # re-runs it for the replacement, so the stand-in must too.
        def _anthropic_prompt_cache_policy(self, *, provider=None, base_url=None,
                                           api_mode=None, model=None):
            return (False, False)

    agent = _Agent()
    agent.provider = provider
    agent.model = model
    agent.base_url = "https://api.deepseek.com"
    agent.api_mode = "chat_completions"
    agent.api_key = "primary-key"
    agent.client = object()
    agent._client_kwargs = {}
    agent._use_prompt_caching = False
    agent._use_native_cache_layout = False
    agent._config_context_length = None
    agent._credential_pool = None
    agent.context_compressor = None
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


# =============================================================================
# C1: _apply_model_override was a PARTIAL swap.
#
# The other two model-swap paths -- try_activate_fallback
# (agent/chat_completion_helpers.py) and swap_model
# (agent/agent_runtime_helpers.py) -- both reset _config_context_length
# (#22387), re-run _anthropic_prompt_cache_policy, rebind the context
# compressor, clear the transport cache and rebind the credential pool.
# _apply_model_override did NONE of them, and because agent._primary_runtime
# is snapshotted immediately AFTER it, the stale values were blessed as
# "primary" and re-applied by restore_primary_runtime() every subsequent
# turn. It never self-corrected.
# =============================================================================

def _anthropic_primary_agent():
    """A real agent whose primary is Anthropic-family, so
    _use_prompt_caching and _use_native_cache_layout are both True at init
    -- the state that turns a divert into a 400 on every call."""
    from unittest.mock import patch as _patch
    from run_agent import AIAgent

    with (
        _patch("run_agent.get_tool_definitions", return_value=[]),
        _patch("run_agent.check_toolset_requirements", return_value={}),
        _patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://api.anthropic.com",
            provider="anthropic",
            api_mode="anthropic_messages",
            model="claude-sonnet-4-6",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    return agent


@pytest.fixture
def _stub_context_length(monkeypatch):
    """The compressor rebind resolves the replacement's context window, which
    can hit a live /models probe. Pin it so these tests measure the rebind,
    not the network."""
    monkeypatch.setattr(
        "agent.model_metadata.get_model_context_length",
        lambda *a, **k: 64000,
    )
    return 64000


def test_chat_completions_replacement_clears_anthropic_cache_policy(
        monkeypatch, _stub_context_length):
    """THE CRITICAL ONE. An Anthropic-family primary has
    _use_prompt_caching/_use_native_cache_layout True. Divert to a
    chat_completions provider without recomputing them and
    agent/conversation_loop.py keeps calling
    apply_anthropic_cache_control(..., native_anthropic=True), injecting
    cache_control blocks into every message sent to a NON-Anthropic
    endpoint: a 400 on EVERY call, caused solely by the override -- in the
    one feature whose contract is "never a blocked model call".

    Mutation check: deleting the two-line policy re-run in
    _apply_model_override leaves both flags True and fails this test.
    """
    from events import model_override

    agent = _anthropic_primary_agent()
    # Precondition -- without it a passing assertion below could just mean
    # "the flags were already False", which would prove nothing.
    assert agent._use_prompt_caching is True
    assert agent._use_native_cache_layout is True

    monkeypatch.setattr(model_override, "get_override", lambda p, m: {
        "replacement_provider": "deepseek", "replacement_model": "deepseek-v4-pro"})

    _apply_model_override(agent)

    assert (agent.provider, agent.model) == ("deepseek", "deepseek-v4-pro")
    assert agent.api_mode == "chat_completions"
    assert agent._use_prompt_caching is False, (
        "cache_control blocks would be sent to a non-Anthropic endpoint")
    assert agent._use_native_cache_layout is False


def test_replacement_is_reflected_in_the_primary_runtime_snapshot(
        monkeypatch, _stub_context_length):
    """The snapshot is taken right after the swap, so whatever the swap
    leaves stale becomes permanent: restore_primary_runtime() re-applies
    _primary_runtime['use_prompt_caching'] at the top of every turn."""
    from events import model_override
    from agent.agent_init import _snapshot_primary_runtime

    agent = _anthropic_primary_agent()
    monkeypatch.setattr(model_override, "get_override", lambda p, m: {
        "replacement_provider": "deepseek", "replacement_model": "deepseek-v4-pro"})

    _apply_model_override(agent)
    rt = _snapshot_primary_runtime(agent)

    assert rt["use_prompt_caching"] is False
    assert rt["use_native_cache_layout"] is False
    assert rt["compressor_model"] == "deepseek-v4-pro"
    assert rt["compressor_provider"] == "deepseek"


def test_compressor_is_rebound_off_the_rate_limited_model(
        monkeypatch, _stub_context_length):
    """Left unrebound, the compressor keeps the OLD model's
    model/provider/base_url/api_key -- so summarization still calls the
    RATE-LIMITED model, precisely on the long contexts where the 429 hurts
    most.

    Mutation check: deleting the context_compressor.update_model() call
    leaves the compressor on claude-sonnet-4-6/anthropic and fails here.
    """
    from events import model_override

    agent = _anthropic_primary_agent()
    assert agent.context_compressor.model == "claude-sonnet-4-6"

    monkeypatch.setattr(model_override, "get_override", lambda p, m: {
        "replacement_provider": "deepseek", "replacement_model": "deepseek-v4-pro"})

    _apply_model_override(agent)

    assert agent.context_compressor.model == "deepseek-v4-pro"
    assert agent.context_compressor.provider == "deepseek"
    assert agent.context_compressor.api_mode == "chat_completions"
    assert agent.context_compressor.context_length == 64000


def test_config_context_length_and_transport_cache_are_cleared(
        monkeypatch, _stub_context_length):
    """#22387: the per-config context_length is the PRIMARY's; inheriting it
    sizes compression to the wrong window. The transport cache is keyed on
    the previous endpoint."""
    from events import model_override

    agent = _anthropic_primary_agent()
    agent._config_context_length = 204800
    agent._transport_cache["sentinel"] = object()

    monkeypatch.setattr(model_override, "get_override", lambda p, m: {
        "replacement_provider": "deepseek", "replacement_model": "deepseek-v4-pro"})

    _apply_model_override(agent)

    assert agent._config_context_length is None
    assert "sentinel" not in agent._transport_cache


def test_no_override_leaves_every_derived_field_untouched(
        monkeypatch, _stub_context_length):
    """THE CENTRAL INVARIANT, extended to the five fields this fix adds:
    with no override, nothing about the agent changes."""
    from events import model_override

    agent = _anthropic_primary_agent()
    agent._config_context_length = 204800
    monkeypatch.setattr(model_override, "get_override", lambda p, m: None)

    _apply_model_override(agent)

    assert (agent.provider, agent.model) == ("anthropic", "claude-sonnet-4-6")
    assert agent._use_prompt_caching is True
    assert agent._use_native_cache_layout is True
    assert agent._config_context_length == 204800
    assert agent.context_compressor.model == "claude-sonnet-4-6"
    assert getattr(agent, "_override_origin", None) is None


def test_a_failure_mid_swap_leaves_the_configured_primary_intact(monkeypatch):
    """Fail-open contract: degrade to the configured primary, never run
    half-swapped. A blown context-length probe must not leave the agent on
    the new model with the primary's cache policy."""
    from events import model_override

    agent = _anthropic_primary_agent()
    monkeypatch.setattr(model_override, "get_override", lambda p, m: {
        "replacement_provider": "deepseek", "replacement_model": "deepseek-v4-pro"})

    def _boom(*a, **k):
        raise RuntimeError("model metadata unavailable")
    monkeypatch.setattr("agent.model_metadata.get_model_context_length", _boom)

    _apply_model_override(agent)   # must not raise

    assert (agent.provider, agent.model) == ("anthropic", "claude-sonnet-4-6")
    assert agent.api_mode == "anthropic_messages"
    assert agent._use_prompt_caching is True
    assert agent.context_compressor.model == "claude-sonnet-4-6"
    assert getattr(agent, "_override_origin", None) is None


# =============================================================================
# I1: enforcement read #2 was structurally INERT for the case read #1 creates.
#
# When _apply_model_override fires, agent._primary_runtime IS the replacement
# -- so restore_primary_runtime()'s get_override(rt["provider"], rt["model"])
# looks up the REPLACEMENT's key and can never match. The gateway caches
# agents in _agent_cache and evicts them only on the idle sweep, so a session
# diverted at init kept routing to the replacement after the override expired
# AND after `hermes overrides clear`: the 24h TTL cap ("no permanent override
# is expressible through this API") defeated for exactly the long-lived
# process the file-backed design exists for.
# =============================================================================

def _init_diverted_agent(monkeypatch, replacement=("openai-codex", "gpt-5.6-sol")):
    """A long-lived agent that was diverted at INIT (read #1) and whose
    _primary_runtime snapshot was therefore taken AFTER the swap -- exactly
    the init ordering in agent/agent_init.py."""
    from events import model_override
    from agent.agent_init import _snapshot_primary_runtime

    agent = _make_real_agent()
    monkeypatch.setattr(model_override, "get_override", lambda p, m: (
        {"replacement_provider": replacement[0], "replacement_model": replacement[1]}
        if (p, m) == ("deepseek", "deepseek-v4-pro") else None
    ))
    monkeypatch.setattr(
        "agent.model_metadata.get_model_context_length", lambda *a, **k: 64000)

    _apply_model_override(agent)
    agent._primary_runtime = _snapshot_primary_runtime(agent)

    assert (agent.provider, agent.model) == replacement
    assert agent._primary_runtime["model"] == replacement[1], (
        "precondition: the snapshot records the REPLACEMENT as primary")
    assert agent._fallback_activated is False, (
        "precondition: an init divert activates no fallback -- the case the "
        "old _fallback_activated gate returned on every turn")
    return agent


def test_expired_init_override_restores_the_configured_primary(monkeypatch):
    """The blocker: a long-lived agent diverted at init, override then
    expired/cleared -> the next restore_primary_runtime() must land on the
    CONFIGURED primary, not the replacement.

    Mutation check: without the _override_origin stash + re-check, the
    lookup uses the replacement's key, returns None, the
    _fallback_activated gate returns False first anyway, and the agent stays
    on openai-codex/gpt-5.6-sol forever.
    """
    from events import model_override
    from unittest.mock import patch as _patch

    agent = _init_diverted_agent(monkeypatch)

    # `hermes overrides clear`, or simply 24h elapsing.
    monkeypatch.setattr(model_override, "get_override", lambda p, m: None)

    with _patch("run_agent.OpenAI", return_value=MagicMock()):
        result = agent._restore_primary_runtime()

    assert result is True
    assert (agent.provider, agent.model) == ("deepseek", "deepseek-v4-pro")
    assert agent.base_url == "https://api.deepseek.com"
    assert getattr(agent, "_override_origin", None) is None


def test_unexpired_init_override_keeps_the_replacement(monkeypatch):
    """The other side of the invariant: while the override is still valid the
    session must stay diverted. Routing changes iff a VALID override matches
    -- in both directions."""
    agent = _init_diverted_agent(monkeypatch)
    # get_override is still the _init_diverted_agent stub: active for
    # deepseek/deepseek-v4-pro, None for anything else.

    result = agent._restore_primary_runtime()

    assert result is False
    assert (agent.provider, agent.model) == ("openai-codex", "gpt-5.6-sol")
    assert getattr(agent, "_override_origin", None) is not None


def test_unreadable_store_does_not_yank_a_live_reroute(monkeypatch):
    """Fail-open direction for the revert check: a store that cannot be read
    must not bounce a working session back onto the model it was diverted
    off. Retried next turn."""
    from events import model_override

    agent = _init_diverted_agent(monkeypatch)

    def _boom(provider, model):
        raise RuntimeError("reroute state unreadable")
    monkeypatch.setattr(model_override, "get_override", _boom)

    result = agent._restore_primary_runtime()

    assert result is False
    assert (agent.provider, agent.model) == ("openai-codex", "gpt-5.6-sol")


def test_explicit_model_switch_retires_the_override_stash(monkeypatch):
    """An explicit /model switch supersedes the reroute: the stash must not
    later clobber the user's deliberate choice with the pre-override
    snapshot."""
    from unittest.mock import patch as _patch

    agent = _init_diverted_agent(monkeypatch)
    assert getattr(agent, "_override_origin", None) is not None

    mock_client = MagicMock()
    mock_client.api_key = "chosen-key"
    mock_client.base_url = "https://api.example.com/v1"
    with (
        _patch("agent.auxiliary_client.resolve_provider_client",
               return_value=(mock_client, None)),
        _patch("agent.model_metadata.get_model_context_length",
               return_value=64000),
        _patch("run_agent.OpenAI", return_value=MagicMock()),
    ):
        agent.switch_model(
            "some-other-model", "openrouter",
            base_url="https://openrouter.ai/api/v1",
        )

    assert getattr(agent, "_override_origin", None) is None


# =============================================================================
# Re-review finding: reasoning_config is a SIXTH item of the C1 class.
#
# The C1 fix mirrored five things the two reference swap paths do
# (try_activate_fallback, agent/chat_completion_helpers.py:1936-1945;
# switch_model, agent/agent_runtime_helpers.py:2344-2360). Both references
# ALSO re-resolve reasoning_config for the new model -- _apply_model_override
# did not, and _snapshot_primary_runtime did not carry it either, so a
# revert never restored it (it is resolved once at init for the configured
# PRIMARY and flows straight into every request build). A divert from a
# primary carrying e.g. {"effort": "high"} to a replacement that rejects the
# parameter reproduces C1's exact "400 on every call, caused solely by the
# override" shape.
# =============================================================================

def test_divert_reresolves_reasoning_config_for_the_replacement(
        monkeypatch, _stub_context_length):
    """Mirrors try_activate_fallback (#21256) and switch_model: the
    replacement model's reasoning_config must be re-resolved from config,
    not inherited from the primary.

    Mutation check: deleting the reasoning_config re-resolution block in
    _apply_model_override leaves agent.reasoning_config at the primary's
    stale {"enabled": True, "effort": "medium"} and fails this test.
    """
    from events import model_override
    from unittest.mock import patch as _patch

    agent = _anthropic_primary_agent()
    agent.reasoning_config = {"enabled": True, "effort": "medium"}

    monkeypatch.setattr(model_override, "get_override", lambda p, m: {
        "replacement_provider": "deepseek", "replacement_model": "deepseek-v4-pro"})

    fake_cfg = {
        "agent": {
            "reasoning_effort": "medium",
            "reasoning_overrides": {"deepseek-v4-pro": "high"},
        },
    }
    with _patch("hermes_cli.config.load_config", return_value=fake_cfg):
        _apply_model_override(agent)

    assert agent.reasoning_config == {"enabled": True, "effort": "high"}, (
        "the replacement's per-model reasoning_overrides entry must win, "
        "not the primary's stale reasoning_config"
    )


def test_divert_reasoning_config_resolution_failure_keeps_current(
        monkeypatch, _stub_context_length):
    """A config-load failure while resolving the replacement's
    reasoning_config must not kill the swap (mirrors both references'
    try/except) -- and must not silently null out reasoning_config either,
    it just leaves the prior value in place."""
    from events import model_override
    from unittest.mock import patch as _patch

    agent = _anthropic_primary_agent()
    agent.reasoning_config = {"enabled": True, "effort": "medium"}

    monkeypatch.setattr(model_override, "get_override", lambda p, m: {
        "replacement_provider": "deepseek", "replacement_model": "deepseek-v4-pro"})

    def _boom(*a, **k):
        raise RuntimeError("config unavailable")
    with _patch("hermes_cli.config.load_config", _boom):
        _apply_model_override(agent)   # must not raise

    assert (agent.provider, agent.model) == ("deepseek", "deepseek-v4-pro"), (
        "the swap itself must still succeed"
    )
    assert agent.reasoning_config == {"enabled": True, "effort": "medium"}, (
        "on resolution failure, reasoning_config must be left untouched"
    )


def test_revert_restores_the_primarys_reasoning_config(monkeypatch):
    """The other half: once the override expires and restore_primary_
    runtime() puts the agent back on its configured primary, the PRIMARY's
    reasoning_config must come back too -- not stay pinned to whatever the
    replacement re-resolved to.

    Mutation check: without "reasoning_config" in _snapshot_primary_runtime's
    returned dict, restore_primary_runtime's "if saved_reasoning is not
    None" guard never fires and the replacement's resolved value survives
    the revert.
    """
    from events import model_override
    from agent.agent_init import _snapshot_primary_runtime
    from unittest.mock import patch as _patch

    agent = _make_real_agent(provider="deepseek", model="deepseek-v4-pro",
                              base_url="https://api.deepseek.com")
    agent.reasoning_config = {"enabled": True, "effort": "low"}

    monkeypatch.setattr(model_override, "get_override", lambda p, m: (
        {"replacement_provider": "openai-codex", "replacement_model": "gpt-5.6-sol"}
        if (p, m) == ("deepseek", "deepseek-v4-pro") else None
    ))
    monkeypatch.setattr(
        "agent.model_metadata.get_model_context_length", lambda *a, **k: 64000)

    fake_cfg = {
        "agent": {
            "reasoning_effort": "medium",
            "reasoning_overrides": {"gpt-5.6-sol": "xhigh"},
        },
    }
    with _patch("hermes_cli.config.load_config", return_value=fake_cfg):
        _apply_model_override(agent)
        agent._primary_runtime = _snapshot_primary_runtime(agent)

    assert agent.reasoning_config == {"enabled": True, "effort": "xhigh"}, (
        "precondition: the replacement's reasoning_config was resolved")

    # `hermes overrides clear`, or simply 24h elapsing.
    monkeypatch.setattr(model_override, "get_override", lambda p, m: None)

    with _patch("run_agent.OpenAI", return_value=MagicMock()):
        result = agent._restore_primary_runtime()

    assert result is True
    assert (agent.provider, agent.model) == ("deepseek", "deepseek-v4-pro")
    assert agent.reasoning_config == {"enabled": True, "effort": "low"}, (
        "the primary's reasoning_config must be restored, not left on the "
        "replacement's re-resolved value"
    )
