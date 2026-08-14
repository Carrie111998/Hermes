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


def test_reason_is_threaded_at_known_sites():
    """The two sites that know their reason must pass it.

    Without this, the eager empty-response fallback (documented in its own
    comment as 'a common rate-limit symptom') and the non-retryable branch
    both fail over invisibly.
    """
    import inspect
    from agent import conversation_loop

    src = inspect.getsource(conversation_loop)

    # The eager empty/malformed-response fallback.
    assert "_try_activate_fallback(reason=FailoverReason.upstream_rate_limit)" in src, \
        "conversation_loop.py:1605 must pass a reason"

    # Verified counts before this task: 8 bare, 2 carrying a reason.
    # This task converts exactly 2, leaving 6 deliberately bare (the sites
    # that genuinely do not know why they are failing over). An exact match
    # makes a regression in EITHER direction fail: a reason-carrying site
    # falling back to bare, or someone "helpfully" inventing a reason at a
    # site that does not know one.
    bare = src.count("_try_activate_fallback()")
    assert bare == 6, (
        f"expected exactly 6 deliberately-bare call sites, found {bare} — "
        "either a reason-carrying site regressed, or a reason was invented "
        "at a site that cannot know it (which manufactures false alerts)"
    )


def test_nous_rate_limit_records_signal(captured, tmp_path, monkeypatch):
    """A3: the Nous guard is the only detector that sees a Nous 429."""
    from agent import nous_rate_guard
    monkeypatch.setattr(nous_rate_guard, "_state_path",
                        lambda: str(tmp_path / "nous.json"))

    nous_rate_guard.record_nous_rate_limit(
        headers={"x-ratelimit-reset-requests-1h": "1800"}
    )

    assert len(captured) == 1, "hook did not fire — A3 is unwired"
    assert captured[0]["detector"] == "nous_guard"
    assert captured[0]["provider"] == "nous"
    assert captured[0]["reason"] == "rate_limit"
    assert captured[0]["resets_at"], "reset time must be propagated"


def test_pool_exhaustion_records_signal(captured, monkeypatch, tmp_path):
    """B: burning a pool entry on a 429 records pool_exhausted."""
    from agent.credential_pool import CredentialPool, PooledCredential

    entry = PooledCredential.from_dict("deepseek", {
        "id": "key-1", "api_key": "sk-test-1",
    })
    pool = CredentialPool("deepseek", [entry])
    monkeypatch.setattr(pool, "_persist", lambda **kw: None)

    pool._mark_exhausted(entry, status_code=429)

    assert len(captured) == 1, "hook did not fire — B is unwired"
    assert captured[0]["detector"] == "credential_pool"
    assert captured[0]["provider"] == "deepseek"
    assert captured[0]["reason"] == "pool_exhausted"


def test_auth_failure_is_not_reported_as_a_rate_limit(captured, monkeypatch):
    """A 401 is an auth problem. Reporting it as a limit would send you
    hunting a phantom outage."""
    from agent.credential_pool import CredentialPool, PooledCredential

    entry = PooledCredential.from_dict("deepseek", {
        "id": "key-1", "api_key": "sk-test-1",
    })
    pool = CredentialPool("deepseek", [entry])
    monkeypatch.setattr(pool, "_persist", lambda **kw: None)

    pool._mark_exhausted(entry, status_code=401)
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


def test_successful_call_clears_open_episode(state_file_agent):
    """D: a success on a limited provider closes the episode exactly once."""
    from events.rate_limit_signal import record, clear, _load_state

    record(provider="deepseek", model="deepseek-v4-pro",
           reason="rate_limit", detector="runtime",
           fallback_provider="openai-codex", fallback_model="gpt-5.6-sol",
           bus=_NullBus())
    assert _load_state(), "precondition: episode must be open"

    assert clear(provider="deepseek", model="deepseek-v4-pro",
                 bus=_NullBus()) is True
    assert _load_state() == {}

    # Idempotent: a second success must not emit a second RECOVERED.
    assert clear(provider="deepseek", model="deepseek-v4-pro",
                 bus=_NullBus()) is False


def test_clear_hook_is_present_in_conversation_loop():
    """Positive control for the WIRING, not just the function."""
    import inspect
    from agent import conversation_loop
    src = inspect.getsource(conversation_loop)
    assert "rate_limit_signal import clear" in src, \
        "D hook is unwired — clear() is never called from the success path"


class _NullBus:
    def emit(self, **kw):
        return "evt"


@pytest.fixture
def state_file_agent(tmp_path, monkeypatch):
    p = tmp_path / "rate_limit_state.json"
    monkeypatch.setattr("events.rate_limit_signal._state_path", lambda: p)
    from events import rate_limit_signal
    rate_limit_signal.reset_state_cache()
    return p
