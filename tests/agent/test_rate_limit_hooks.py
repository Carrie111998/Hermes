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
    """The two sites that know their reason must pass it — as TELEMETRY.

    Without this, the eager empty-response fallback (documented in its own
    comment as 'a common rate-limit symptom') and the non-retryable branch
    both fail over invisibly.

    The assertion demands ``telemetry_reason=``, not ``reason=``. ``reason=``
    reaches a behavioral branch that arms ``agent._rate_limited_until`` for 60s
    and keeps the agent pinned to its fallback (see
    test_telemetry_reason_does_not_arm_the_cooldown), which would break Phase
    1's promise that it cannot change which model answers a call. Do not
    "restore" reason= here.
    """
    import inspect
    from agent import conversation_loop

    src = inspect.getsource(conversation_loop)

    # The eager empty/malformed-response fallback.
    assert "_try_activate_fallback(telemetry_reason=FailoverReason.upstream_rate_limit)" in src, \
        "the eager empty/malformed-response fallback must attribute itself"
    assert "_try_activate_fallback(reason=FailoverReason.upstream_rate_limit)" not in src, \
        "that site must attribute via telemetry_reason=, never reason= — " \
        "reason= arms the 60s fallback-pinning cooldown"

    # The non-retryable branch (site 2). This needs its own explicit check,
    # not just the bare==6 count below: reverting this site from
    # telemetry_reason=classified.reason back to reason=classified.reason
    # does not touch a single bare "_try_activate_fallback()" call, so
    # bare==6 stays green through exactly this regression. The literal
    # string "_try_activate_fallback(reason=classified.reason)" also already
    # appears twice elsewhere in this file, at two pre-existing, untouched,
    # genuinely-behavioral sites (rate-limit/billing failover, auth
    # failover) — so a blanket "must be absent" would false-fail on correct
    # code. Pin the count instead: it must stay at exactly those 2. A revert
    # of site 2 raises it to 3.
    assert "_try_activate_fallback(telemetry_reason=classified.reason)" in src, \
        "the non-retryable branch must attribute itself via telemetry_reason="
    site2_reason_count = src.count("_try_activate_fallback(reason=classified.reason)")
    assert site2_reason_count == 2, (
        f"expected exactly 2 legitimate reason=classified.reason sites "
        f"(rate-limit/billing and auth failover), found {site2_reason_count} — "
        "the non-retryable branch (site 2) appears to have reverted from "
        "telemetry_reason= to reason=, which re-arms the 60s fallback-pinning "
        "cooldown; bare==6 alone would not have caught this"
    )

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


def test_telemetry_reason_does_not_arm_the_cooldown(captured, monkeypatch):
    """C1 regression guard: attribution must not change WHICH MODEL ANSWERS.

    ``agent._rate_limited_until`` is read by
    ``agent_runtime_helpers.restore_primary_runtime()``: while it is in the
    future, the agent STAYS ON THE FALLBACK instead of restoring the primary.
    So arming it is not telemetry, it is routing.

    Phase 1 promised to be read-only with respect to model selection. Threading
    ``reason=`` into a call site purely to attribute it silently broke that
    promise — the two converted conversation_loop sites went from a 0s cooldown
    to a 60s one. This test pins both halves of the split: telemetry_reason
    attributes without arming, reason still arms.
    """
    import time
    from agent.error_classifier import FailoverReason
    from agent import chat_completion_helpers as cch

    # --- telemetry_reason alone: attributed, but NO behavior change.
    agent = _agent_with_chain([])
    assert cch.try_activate_fallback(
        agent, telemetry_reason=FailoverReason.upstream_rate_limit) is False
    assert agent._rate_limited_until == 0, (
        "telemetry_reason armed the fallback-pinning cooldown — this is C1, "
        "the agent will now stay on its fallback for 60s because of a "
        "telemetry-only argument"
    )
    assert len(captured) == 1, "telemetry_reason must still be attributed"

    # --- reason: the pre-existing BEHAVIORAL contract, unchanged.
    behavioral = _agent_with_chain([])
    before = time.monotonic()
    assert cch.try_activate_fallback(
        behavioral, reason=FailoverReason.rate_limit) is False
    assert behavioral._rate_limited_until >= before + 59, (
        "reason= must still arm the 60s cooldown — the split must not have "
        "disarmed the real behavioral path"
    )


def test_exhausted_chain_guard_still_reads_reason_only(captured):
    """C1, second half: telemetry_reason must not skip the #24996 guard.

    When a non-rate-limit failure walks off the end of a NON-EMPTY chain,
    try_activate_fallback arms a short 5s cooldown so the next turn's
    restore_primary_runtime does not reset _fallback_index=0 and re-marshal the
    whole context across every provider again. That guard is skipped for
    rate-limit-class reasons (they arm their own 60s cooldown instead).

    If telemetry_reason leaked into that condition, an attribution-only caller
    would silently disable the replay-storm guard.
    """
    import time
    from agent.error_classifier import FailoverReason
    from agent import chat_completion_helpers as cch

    agent = _agent_with_chain([{"provider": "x", "model": "y"}])
    agent._fallback_index = 1          # already walked off the end
    before = time.monotonic()

    assert cch.try_activate_fallback(
        agent, telemetry_reason=FailoverReason.rate_limit) is False

    assert agent._rate_limited_until >= before, (
        "the #24996 exhausted-chain cooldown was skipped because "
        "telemetry_reason reached a behavioral branch"
    )
    assert agent._rate_limited_until < before + 30, (
        "expected the 5s exhausted-chain guard, not the 60s rate-limit "
        "cooldown — telemetry_reason must not arm the latter"
    )


def test_a1_hook_reports_when_only_telemetry_reason_is_supplied(
        captured, monkeypatch):
    """A1 must attribute a site that passes telemetry_reason only.

    ``telemetry_reason or reason`` — a site supplying neither stays invisible,
    a site supplying either is reported.
    """
    from agent.error_classifier import FailoverReason
    from agent import chat_completion_helpers as cch

    agent = _agent_with_chain([{"provider": "openai-codex",
                                "model": "gpt-5.6-sol"}])
    monkeypatch.setattr(
        "agent.auxiliary_client.resolve_provider_client",
        lambda *a, **k: (_fake_client(), "gpt-5.6-sol"),
    )
    agent._anthropic_prompt_cache_policy.return_value = (True, False)
    agent.context_compressor = None

    assert cch.try_activate_fallback(
        agent, telemetry_reason=FailoverReason.upstream_rate_limit) is True

    assert len(captured) == 1, "A1 ignored telemetry_reason"
    assert captured[0]["outcome"] == "diverted"
    assert captured[0]["reason"] == FailoverReason.upstream_rate_limit.value
    assert captured[0]["provider"] == "deepseek"
    assert captured[0]["fallback_model"] == "gpt-5.6-sol"
    # ...and it still did not touch routing.
    assert agent._rate_limited_until == 0


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


def test_nous_signal_fires_even_when_the_state_write_fails(
        captured, tmp_path, monkeypatch):
    """M3: A3 must not be coupled to an unrelated disk write.

    The hook sat INSIDE the try that persists the Nous breaker file, so an
    atomic_replace failure (full disk, the Windows reader race) was swallowed
    by that handler and the detector never fired — the rate limit went
    unreported because of a write that has nothing to do with it.
    """
    from agent import nous_rate_guard
    monkeypatch.setattr(nous_rate_guard, "_state_path",
                        lambda: str(tmp_path / "nous.json"))
    monkeypatch.setattr(
        nous_rate_guard, "atomic_replace",
        lambda *a, **k: (_ for _ in ()).throw(
            PermissionError(13, "Access is denied")),
    )

    nous_rate_guard.record_nous_rate_limit(
        headers={"x-ratelimit-reset-requests-1h": "1800"}
    )

    assert len(captured) == 1, (
        "A3 did not fire — the detector is gated on the breaker-file write "
        "succeeding"
    )
    assert captured[0]["detector"] == "nous_guard"
    assert captured[0]["resets_at"], "reset time must still be propagated"


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
