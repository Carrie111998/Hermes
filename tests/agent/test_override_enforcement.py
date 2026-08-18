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
