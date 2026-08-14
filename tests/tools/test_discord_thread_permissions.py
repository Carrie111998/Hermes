"""Tests for plugins.platforms.discord.thread_permissions."""

import pytest

from plugins.platforms.discord.thread_permissions import (
    CREATE_PRIVATE_THREADS,
    CREATE_PUBLIC_THREADS,
    SEND_MESSAGES_IN_THREADS,
    ThreadPermissionError,
    can_create_thread,
    classify_thread_permission_failure,
    fallback_eligible,
)

FULL_PUBLIC = CREATE_PUBLIC_THREADS | SEND_MESSAGES_IN_THREADS
FULL_PRIVATE = CREATE_PRIVATE_THREADS | SEND_MESSAGES_IN_THREADS


class TestBitConstants:
    def test_values_match_discord(self):
        assert CREATE_PUBLIC_THREADS == 1 << 43
        assert CREATE_PRIVATE_THREADS == 1 << 44
        assert SEND_MESSAGES_IN_THREADS == 1 << 38


class TestCanCreateThread:
    def test_public_create_with_send(self):
        assert can_create_thread(FULL_PUBLIC, private=False) is True

    def test_private_create_with_send(self):
        assert can_create_thread(FULL_PRIVATE, private=True) is True

    def test_public_requires_public_bit(self):
        assert (
            can_create_thread(
                CREATE_PRIVATE_THREADS | SEND_MESSAGES_IN_THREADS,
                private=False,
            )
            is False
        )

    def test_private_requires_private_bit(self):
        assert (
            can_create_thread(
                CREATE_PUBLIC_THREADS | SEND_MESSAGES_IN_THREADS,
                private=True,
            )
            is False
        )

    def test_send_threads_required_for_public(self):
        assert can_create_thread(CREATE_PUBLIC_THREADS, private=False) is False

    def test_send_threads_required_for_private(self):
        assert can_create_thread(CREATE_PRIVATE_THREADS, private=True) is False

    def test_zero_bits_never_allowed(self):
        assert can_create_thread(0, private=False) is False
        assert can_create_thread(0, private=True) is False

    def test_invalid_bits_raise(self):
        for bad in (-1, "abc", 1.5, None, True):
            with pytest.raises(ThreadPermissionError):
                can_create_thread(bad, private=False)
            with pytest.raises(ThreadPermissionError):
                can_create_thread(bad, private=True)


class TestFallbackEligible:
    def test_send_bit_means_eligible(self):
        assert fallback_eligible(SEND_MESSAGES_IN_THREADS) is True
        assert fallback_eligible(FULL_PUBLIC) is True
        assert fallback_eligible(FULL_PRIVATE) is True

    def test_missing_send_bit_means_not_eligible(self):
        assert fallback_eligible(0) is False
        assert fallback_eligible(CREATE_PUBLIC_THREADS) is False
        assert fallback_eligible(CREATE_PRIVATE_THREADS) is False

    def test_invalid_bits_raise(self):
        for bad in (-1, "abc", 1.5, None, False):
            with pytest.raises(ThreadPermissionError):
                fallback_eligible(bad)


class TestClassifyThreadPermissionFailure:
    def test_ok_public(self):
        assert classify_thread_permission_failure(FULL_PUBLIC, private=False) == "ok"

    def test_ok_private(self):
        assert classify_thread_permission_failure(FULL_PRIVATE, private=True) == "ok"

    def test_missing_create_private(self):
        bits = CREATE_PUBLIC_THREADS | SEND_MESSAGES_IN_THREADS
        assert classify_thread_permission_failure(bits, private=True) == "missing_create_private"

    def test_missing_create_public(self):
        bits = CREATE_PRIVATE_THREADS | SEND_MESSAGES_IN_THREADS
        assert classify_thread_permission_failure(bits, private=False) == "missing_create_public"

    def test_missing_send_threads(self):
        assert (
            classify_thread_permission_failure(CREATE_PUBLIC_THREADS, private=False)
            == "missing_send_threads"
        )
        assert (
            classify_thread_permission_failure(CREATE_PRIVATE_THREADS, private=True)
            == "missing_send_threads"
        )

    def test_no_fallback_when_nothing_available(self):
        assert classify_thread_permission_failure(0, private=True) == "no_fallback"
        assert classify_thread_permission_failure(0, private=False) == "no_fallback"
        # Irrelevant create bit for the requested visibility, and no send bit.
        assert (
            classify_thread_permission_failure(CREATE_PRIVATE_THREADS, private=False)
            == "no_fallback"
        )

    def test_invalid_bits_raise(self):
        for bad in (-1, "abc", 1.5, None, True):
            with pytest.raises(ThreadPermissionError):
                classify_thread_permission_failure(bad, private=False)


class TestThreadPermissionError:
    def test_is_value_error(self):
        assert issubclass(ThreadPermissionError, ValueError)

    def test_negative_bits_raise(self):
        with pytest.raises(ThreadPermissionError):
            can_create_thread(-1, private=False)
        with pytest.raises(ThreadPermissionError):
            fallback_eligible(-1)
        with pytest.raises(ThreadPermissionError):
            classify_thread_permission_failure(-1, private=True)
