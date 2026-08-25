"""Focused tests for provider-level model-REQUEST concurrency.

Verifies that ``provider_max_concurrent_requests`` (an optional REQUEST-level
throttle, independent of the AGENT-level ``delegation.max_concurrent_children``
cap) serializes concurrent provider model calls when configured, and is a
transparent pass-through when unset — so existing behavior is unchanged.

Concurrency is asserted via wall-clock timing, not merely "calls happened", so
that a subtle sequential fallback cannot masquerade as concurrency.
"""

import threading
import time

from unittest.mock import patch

from agent import chat_completion_helpers as m


def _fake_unlocked(agent, api_kwargs, *, make_client):
    """Deterministic stand-in for the guarded blocking HTTP request."""
    return api_kwargs


def _spawn_calls(wrapper, n=3):
    """Run n parallel calls through the wrapper on worker threads."""
    results = [None] * n
    threads = []
    for i in range(n):
        t = threading.Thread(
            target=lambda i=i: results.__setitem__(
                i, wrapper(None, {"i": i}, make_client=None)
            ),
            daemon=True,
        )
        t.start()
        threads.append(t)
    for t in threads:
        t.join(timeout=15)
    return results


def test_wrapper_is_pass_through_when_limit_unset():
    """With no limit configured the wrapper must call the unlocked body and
    return its result unchanged (behavior identical to before the change)."""
    called = []

    def unlocked(agent, api_kwargs, *, make_client):
        called.append(api_kwargs)
        return "result"

    with patch.object(m, "_get_request_semaphore", return_value=None), \
         patch.object(m, "_dispatch_nonstreaming_api_request_unlocked", side_effect=unlocked):
        out = m._dispatch_nonstreaming_api_request(None, {"k": 1}, make_client="mc")
        assert out == "result"
        assert called == [{"k": 1}]


def test_semaphore_none_when_config_unset_or_bad():
    """Invalid / unset values must yield a no-op (None) semaphore, preserving
    the original unlimited behavior."""
    for bad in (None, 0, -3, "5", True):
        with patch("hermes_cli.config.load_config_readonly",
                   return_value={"provider_max_concurrent_requests": bad}):
            assert m._get_request_semaphore() is None


def test_limit_serializes_concurrent_requests():
    """With limit=1, N parallel worker calls must run sequentially (serialized
    by the request semaphore). With limit=3 they overlap."""

    def run(limit, unlock):
        before = time.monotonic()
        with patch("hermes_cli.config.load_config_readonly",
                   return_value={"provider_max_concurrent_requests": limit}), \
             patch.object(m, "_dispatch_nonstreaming_api_request_unlocked", side_effect=unlock):
            results = _spawn_calls(m._dispatch_nonstreaming_api_request, n=3)
        wall = time.monotonic() - before
        return wall, results

    def sleeper(agent, api_kwargs, *, make_client):
        time.sleep(0.25)  # simulate a blocking provider HTTP round-trip
        return api_kwargs

    # limit=1 => 3 sequential sleeps of 0.25s => clearly >= 0.6s
    m._request_semaphore = None
    m._request_semaphore_limit = None
    wall1, results1 = run(1, sleeper)
    assert results1 == [{"i": 0}, {"i": 1}, {"i": 2}], results1
    assert wall1 >= 0.6, f"expected serialization, got {wall1:.2f}s"

    # limit=3 => 3 overlapping sleeps => clearly < 0.6s
    m._request_semaphore = None
    m._request_semaphore_limit = None
    wall3, results3 = run(3, sleeper)
    assert results3 == [{"i": 0}, {"i": 1}, {"i": 2}], results3
    assert wall3 < 0.6, f"expected overlap, got {wall3:.2f}s"


def test_limit_is_independent_of_agent_concurrency_config():
    """The request-level throttle must not read or depend on
    delegation.max_concurrent_children; the two levels stay independent."""
    with patch("hermes_cli.config.load_config_readonly",
               return_value={
                   "provider_max_concurrent_requests": 2,
                   "delegation": {"max_concurrent_children": 5},
               }):
        sem = m._get_request_semaphore()
        assert sem is not None
        assert sem._value == 2

    m._request_semaphore = None
    m._request_semaphore_limit = None
    with patch("hermes_cli.config.load_config_readonly",
               return_value={
                   "provider_max_concurrent_requests": None,
                   "delegation": {"max_concurrent_children": 5},
               }):
        assert m._get_request_semaphore() is None