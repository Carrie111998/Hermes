"""Test that sticky IP resets after repeated failures."""
from unittest.mock import MagicMock

from plugins.platforms.telegram.telegram_network import TelegramFallbackTransport


def test_sticky_ip_resets_after_5_consecutive_failures():
    t = TelegramFallbackTransport(fallback_ips=["1.1.1.1", "2.2.2.2"])
    t._sticky_ip = "1.1.1.1"
    t._sticky_failures = 0
    for _ in range(5):
        t._record_sticky_failure()
    assert t._sticky_ip is None


def test_sticky_ip_retained_on_sporadic_failure():
    t = TelegramFallbackTransport(fallback_ips=["1.1.1.1"])
    t._sticky_ip = "1.1.1.1"
    t._sticky_failures = 0
    t._record_sticky_failure()
    t._record_sticky_success()
    t._record_sticky_failure()
    t._record_sticky_failure()
    assert t._sticky_ip == "1.1.1.1"
