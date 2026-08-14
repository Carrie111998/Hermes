"""Positive controls for the rate-limit detector hooks.

Each test FORCES the condition and asserts the signal fires. A test that only
asserts "no alert when healthy" would pass identically whether the hook is
correctly wired or entirely absent — which is the exact failure mode these
guard against.
"""
import pytest
from unittest.mock import MagicMock


@pytest.fixture
def captured(monkeypatch):
    calls = []

    def _fake_record(**kw):
        calls.append(kw)
        return True

    monkeypatch.setattr("events.rate_limit_signal.record", _fake_record)
    return calls


def test_fallback_swap_records_diverted(captured, monkeypatch):
    """A1: a rate-limit failover that lands on a fallback records diverted."""
    from agent.error_classifier import FailoverReason
    from agent import chat_completion_helpers as cch

    agent = _agent_with_chain([{"provider": "openai-codex",
                                "model": "gpt-5.6-sol"}])
    monkeypatch.setattr(
        "agent.auxiliary_client.resolve_provider_client",
        lambda *a, **k: (_fake_client(), "gpt-5.6-sol"),
    )
    # Unrelated to the hook under test: the real swap path tuple-unpacks
    # agent._anthropic_prompt_cache_policy(...)'s return value. A bare
    # MagicMock's default __iter__ yields nothing, so without this stub the
    # unpack raises, gets swallowed by try_activate_fallback's own broad
    # except, and the function returns via the (also-mocked)
    # agent._try_activate_fallback retry instead of completing normally.
    # Stubbing this — and disabling the optional context_compressor branch,
    # which is unrelated production config-reading code — lets the swap
    # reach its real `return True` so the hook under test actually runs on
    # the intended success path.
    agent._anthropic_prompt_cache_policy.return_value = (True, False)
    agent.context_compressor = None

    assert cch.try_activate_fallback(
        agent, reason=FailoverReason.rate_limit) is True

    assert len(captured) == 1, "hook did not fire — A1 is unwired"
    assert captured[0]["outcome"] == "diverted"
    assert captured[0]["provider"] == "deepseek"
    assert captured[0]["model"] == "deepseek-v4-pro"
    assert captured[0]["fallback_model"] == "gpt-5.6-sol"
    assert captured[0]["detector"] == "runtime"


def test_chain_exhausted_records_act_outcome(captured):
    """A2: walking off the end of the chain records chain_exhausted."""
    from agent.error_classifier import FailoverReason
    from agent import chat_completion_helpers as cch

    agent = _agent_with_chain([])           # empty chain, nothing to fall to
    agent._fallback_chain = []
    agent._fallback_index = 0

    assert cch.try_activate_fallback(
        agent, reason=FailoverReason.rate_limit) is False

    assert len(captured) == 1, "hook did not fire — A2 is unwired"
    assert captured[0]["outcome"] in {"chain_exhausted", "no_fallback"}


def test_non_rate_limit_failover_does_not_record(captured):
    """A server_error failover must NOT masquerade as a rate limit."""
    from agent.error_classifier import FailoverReason
    from agent import chat_completion_helpers as cch

    agent = _agent_with_chain([])
    cch.try_activate_fallback(agent, reason=FailoverReason.server_error)
    assert captured == []


def _fake_client():
    client = MagicMock()
    client.base_url = "https://api.openai.com/v1"
    client.api_key = "sk-test"
    return client


def _agent_with_chain(chain):
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
    return agent
