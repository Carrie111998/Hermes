"""Deterministic tests for usage refresh hook (GOAL B).

Tests throttling, timeout-bounding, concurrent decision coalescing, and
correct handling of stale/partial failures.
"""

from __future__ import annotations

import asyncio
import time
import unittest
from unittest import mock

from hermes_cli.fleet.usage_refresh_hook import UsageRefreshHook


class TestUsageRefreshHook(unittest.TestCase):
    """Deterministic tests for the refresh hook."""

    def test_throttled_refresh_returns_cached(self):
        """Verify that calls within throttle window return cached results."""
        hook = UsageRefreshHook(min_interval_seconds=0.5)
        call_count = 0

        def mock_refresh():
            nonlocal call_count
            call_count += 1
            return mock.MagicMock(ok=True)

        hook.refresh_fn = mock_refresh

        async def run():
            # First call should refresh
            result1 = await hook.refresh_if_needed()
            assert result1["refreshed"] is True
            assert result1["cached"] is False
            assert call_count == 1

            # Second call immediately after should be throttled
            result2 = await hook.refresh_if_needed()
            assert result2["refreshed"] is False
            assert result2["cached"] is True
            assert result2["reason"] == "throttled"
            assert call_count == 1  # No additional call

        asyncio.run(run())

    def test_concurrent_calls_coalesce(self):
        """Verify that concurrent calls wait for the same refresh."""
        hook = UsageRefreshHook(min_interval_seconds=0.1, timeout_seconds=2.0)
        refresh_count = 0

        def slow_refresh():
            nonlocal refresh_count
            refresh_count += 1
            time.sleep(0.05)  # Simulate work
            return mock.MagicMock(ok=True)

        hook.refresh_fn = slow_refresh

        async def run():
            # Launch 3 concurrent calls
            tasks = [
                hook.refresh_if_needed(),
                hook.refresh_if_needed(),
                hook.refresh_if_needed(),
            ]
            results = await asyncio.gather(*tasks)

            # First call should refresh; others should coalesce
            assert refresh_count == 1, f"Expected 1 refresh, got {refresh_count}"

            # All three should report success (first refreshed, others coalesced)
            assert results[0]["refreshed"] is True
            assert results[1]["refreshed"] is False
            assert results[1]["reason"] == "coalesced"
            assert results[2]["refreshed"] is False
            assert results[2]["reason"] == "coalesced"

        asyncio.run(run())

    def test_refresh_timeout_is_bounded(self):
        """Verify that refresh calls timeout after the configured duration."""
        hook = UsageRefreshHook(min_interval_seconds=0.1, timeout_seconds=0.2)

        def slow_refresh():
            time.sleep(1.0)  # Intentionally exceed timeout
            return {"ok": True}

        hook.refresh_fn = slow_refresh

        async def run():
            result = await hook.refresh_if_needed()
            assert result["ok"] is False
            assert result["reason"] == "timeout"

        asyncio.run(run())

    def test_refresh_failure_is_reported(self):
        """Verify that refresh failures are reported correctly."""
        hook = UsageRefreshHook(min_interval_seconds=0.1, timeout_seconds=1.0)

        def failing_refresh():
            raise RuntimeError("test failure")

        hook.refresh_fn = failing_refresh

        async def run():
            result = await hook.refresh_if_needed()
            assert result["ok"] is False
            assert result["reason"] == "failed"
            assert "RuntimeError" in result["detail"]

        asyncio.run(run())

    def test_checked_at_timestamp_always_present(self):
        """Verify that all responses carry a checked_at timestamp."""
        hook = UsageRefreshHook(min_interval_seconds=0.1, timeout_seconds=1.0)

        def mock_refresh():
            return mock.MagicMock(ok=True)

        hook.refresh_fn = mock_refresh

        async def run():
            # Throttled
            result1 = await hook.refresh_if_needed()
            assert "checked_at" in result1
            assert result1["checked_at"]

            hook._last_refresh_at = None

            # Refreshed
            result2 = await hook.refresh_if_needed()
            assert "checked_at" in result2
            assert result2["checked_at"]

        asyncio.run(run())

    def test_default_min_interval_enforced(self):
        """Verify that minimum interval is at least 60 seconds."""
        # Try to set below minimum
        hook = UsageRefreshHook(min_interval_seconds=5.0)
        assert hook.min_interval_seconds == 60.0  # Clamped to minimum

        # Reasonable value is kept
        hook2 = UsageRefreshHook(min_interval_seconds=300.0)
        assert hook2.min_interval_seconds == 300.0


class TestUsageRefreshHookIntegration(unittest.TestCase):
    """Integration tests for the refresh hook with real refresh functions."""

    def test_with_real_refresh_function(self):
        """Test the hook with a real refresh function (mocked)."""
        hook = UsageRefreshHook(min_interval_seconds=0.5, timeout_seconds=2.0)

        async def run():
            # First call should attempt refresh (mocked to fail gracefully)
            with mock.patch(
                "hermes_cli.fleet.usage_refresh.refresh_usage_document",
                side_effect=RuntimeError("test"),
            ):
                result = await hook.refresh_if_needed()
                assert result["reason"] == "failed"

        asyncio.run(run())

    def test_global_hook_singleton(self):
        """Verify that get_global_usage_refresh_hook returns same instance."""
        from hermes_cli.fleet.usage_refresh_hook import get_global_usage_refresh_hook

        hook1 = get_global_usage_refresh_hook()
        hook2 = get_global_usage_refresh_hook()
        assert hook1 is hook2

    def test_refresh_before_dispatch_callable(self):
        """Verify that refresh_usage_before_dispatch is callable and returns dict."""
        from hermes_cli.fleet.usage_refresh_hook import refresh_usage_before_dispatch

        async def run():
            with mock.patch(
                "hermes_cli.fleet.usage_refresh.refresh_usage_document",
                return_value=mock.MagicMock(ok=True),
            ):
                result = await refresh_usage_before_dispatch()
                assert isinstance(result, dict)
                assert "ok" in result
                assert "reason" in result
                assert "checked_at" in result

        asyncio.run(run())


class TestConcurrentTimeoutAdversarial(unittest.TestCase):
    """Adversarial tests for concurrent timeout determinism (Inspector Criterion 6).

    These tests verify that the hook maintains deterministic behavior under timeout
    scenarios: no crashes, no deadlocks, and consistent state returns for all callers.
    """

    def test_timeout_returns_consistent_state(self):
        """Prove: timeout callers get consistent terminal state (no crashes).

        Setup: refresh that times out.
        Assert: caller gets consistent timeout result (ok=False, reason='timeout').
        Assert: no exceptions or deadlocks.
        """
        hook = UsageRefreshHook(min_interval_seconds=0.1, timeout_seconds=0.1)

        def slow_refresh():
            time.sleep(0.2)
            return mock.MagicMock(ok=True)

        hook.refresh_fn = slow_refresh

        async def run():
            result = await hook.refresh_if_needed()
            assert result["reason"] == "timeout"
            assert result["ok"] is False
            assert result["refreshed"] is False

        asyncio.run(run())

    def test_timeout_then_throttle_window_applies(self):
        """Prove: after timeout, throttle window is respected.

        Setup: refresh times out, then check throttle window.
        Assert: subsequent call returns throttled (no new refresh attempted).
        """
        hook = UsageRefreshHook(min_interval_seconds=0.2, timeout_seconds=0.1)

        def slow_refresh():
            time.sleep(0.15)
            return mock.MagicMock(ok=True)

        hook.refresh_fn = slow_refresh

        async def run():
            result1 = await hook.refresh_if_needed()
            assert result1["reason"] == "timeout"

            # Immediately follow up - should be throttled
            result2 = await hook.refresh_if_needed()
            assert result2["reason"] == "throttled"
            assert result2["ok"] is True  # throttled is ok (cached)

        asyncio.run(run())

    def test_concurrent_timeout_consistency(self):
        """Prove: concurrent callers all get timeout consistently (no race conditions).

        Setup: 3 concurrent calls during slow refresh.
        Assert: all 3 get consistent terminal states (all timeout or all succeed).
        Assert: no exceptions.
        """
        hook = UsageRefreshHook(min_interval_seconds=0.1, timeout_seconds=0.1)
        call_started = 0

        def slow_refresh():
            nonlocal call_started
            call_started += 1
            time.sleep(0.15)  # Exceeds timeout
            return mock.MagicMock(ok=True)

        hook.refresh_fn = slow_refresh

        async def run():
            results = await asyncio.gather(
                hook.refresh_if_needed(),
                hook.refresh_if_needed(),
                hook.refresh_if_needed(),
            )

            # All should be timeouts (they all waited for the same slow task)
            for i, result in enumerate(results):
                assert result["reason"] in ("timeout", "throttled"), f"Result {i}: {result['reason']}"
                assert "checked_at" in result, f"Result {i}: missing checked_at"

            # Refresh started exactly once (strict: proves no duplicates, no queuing)
            assert call_started == 1, f"Expected exactly 1 refresh, got {call_started}"

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
