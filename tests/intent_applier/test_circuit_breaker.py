import time

import pytest

from intent_applier.circuit_breaker import CircuitBreakerOpen, SimpleCircuitBreaker


class TestSimpleCircuitBreaker:
    def test_initially_closed(self):
        cb = SimpleCircuitBreaker(failure_threshold=3, reset_timeout_seconds=10)
        assert not cb.is_open()

    def test_opens_after_threshold_failures(self):
        cb = SimpleCircuitBreaker(failure_threshold=3, reset_timeout_seconds=10)
        cb.record_failure()
        cb.record_failure()
        assert not cb.is_open()
        cb.record_failure()
        assert cb.is_open()

    def test_call_raises_when_open(self):
        cb = SimpleCircuitBreaker(failure_threshold=2, reset_timeout_seconds=10)
        cb.record_failure()
        cb.record_failure()
        with pytest.raises(CircuitBreakerOpen):
            cb.guard()

    def test_success_resets_failure_count(self):
        cb = SimpleCircuitBreaker(failure_threshold=3, reset_timeout_seconds=10)
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        cb.record_failure()
        cb.record_failure()
        assert not cb.is_open()  # only 2 since last success

    def test_auto_reset_after_timeout(self):
        cb = SimpleCircuitBreaker(failure_threshold=2, reset_timeout_seconds=0.05)
        cb.record_failure()
        cb.record_failure()
        assert cb.is_open()
        time.sleep(0.1)
        assert not cb.is_open()
        # First call after reset is "half-open" — passes guard
        cb.guard()  # must not raise
