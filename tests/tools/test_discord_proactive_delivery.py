"""Tests for W4: Discord proactive/home/cron delivery targeting.

Covers home target resolution (deliver_home requires a home channel, no
silent origin fallback per #7206), continuable-thread resolution, profile
adapter resolution (found / missing / fail-closed), and snowflake
validation.
"""

import pytest

from plugins.platforms.discord.proactive_delivery import (
    DeliveryTarget,
    ProactiveDeliveryError,
    continuable_thread_target,
    resolve_home_target,
    resolve_profile_adapter,
)

VALID = "123456789012345678"
VALID_2 = "987654321098765432"

INVALID_SNOWFLAKES = [
    "",
    "abc",
    "12a34",
    "12.5",
    "-123",
    " 123",
    "99999999999999999999",  # exceeds signed 64-bit range
]


class TestProactiveDeliveryError:
    def test_is_value_error_subclass(self):
        assert issubclass(ProactiveDeliveryError, ValueError)


class TestResolveHomeTarget:
    def test_deliver_home_uses_home_channel(self):
        target = resolve_home_target(VALID, VALID_2, deliver_home=True)
        assert target == DeliveryTarget(channel_id=VALID_2, thread_id=None)

    def test_deliver_home_works_without_origin(self):
        target = resolve_home_target(None, VALID, deliver_home=True)
        assert target == DeliveryTarget(channel_id=VALID, thread_id=None)

    def test_deliver_home_without_home_raises_no_silent_fallback(self):
        # No silent origin fallback (#7206).
        with pytest.raises(ProactiveDeliveryError):
            resolve_home_target(VALID, None, deliver_home=True)

    def test_deliver_home_invalid_home_raises(self):
        with pytest.raises(ProactiveDeliveryError):
            resolve_home_target(VALID, "not-a-snowflake", deliver_home=True)

    def test_origin_used_when_deliver_home_false(self):
        target = resolve_home_target(VALID, VALID_2, deliver_home=False)
        assert target == DeliveryTarget(channel_id=VALID, thread_id=None)

    def test_origin_none_when_deliver_home_false(self):
        target = resolve_home_target(None, VALID_2, deliver_home=False)
        assert target == DeliveryTarget(channel_id=None, thread_id=None)

    def test_origin_invalid_raises_when_deliver_home_false(self):
        with pytest.raises(ProactiveDeliveryError):
            resolve_home_target("nope", None, deliver_home=False)


class TestContinuableThreadTarget:
    def test_valid_thread_id_used(self):
        target = continuable_thread_target(VALID, cron_thread_identity=VALID_2)
        assert target == DeliveryTarget(channel_id=None, thread_id=VALID)

    def test_none_thread_id_falls_back_to_cron_identity(self):
        target = continuable_thread_target(None, cron_thread_identity=VALID_2)
        assert target == DeliveryTarget(channel_id=None, thread_id=VALID_2)

    def test_both_none_returns_empty_target(self):
        target = continuable_thread_target(None, cron_thread_identity=None)
        assert target == DeliveryTarget(channel_id=None, thread_id=None)

    def test_invalid_thread_id_falls_back_to_cron_identity(self):
        # A stale/non-snowflake thread id does not crash the delivery; the
        # cron identity recorded for the job is used instead.
        target = continuable_thread_target("stale", cron_thread_identity=VALID_2)
        assert target == DeliveryTarget(channel_id=None, thread_id=VALID_2)

    def test_invalid_cron_identity_raises_when_used(self):
        with pytest.raises(ProactiveDeliveryError):
            continuable_thread_target(None, cron_thread_identity="bad")

    def test_invalid_cron_identity_raises_on_fallback(self):
        with pytest.raises(ProactiveDeliveryError):
            continuable_thread_target("stale", cron_thread_identity="bad")

    def test_valid_thread_wins_over_invalid_cron_identity(self):
        target = continuable_thread_target(VALID, cron_thread_identity="bad")
        assert target == DeliveryTarget(channel_id=None, thread_id=VALID)


class TestResolveProfileAdapter:
    def test_adapter_found(self):
        adapters = {"main": "adapter-main", "alt": "adapter-alt"}
        assert resolve_profile_adapter("alt", adapters) == "adapter-alt"

    def test_missing_profile_raises(self):
        with pytest.raises(ProactiveDeliveryError):
            resolve_profile_adapter("missing", {"main": "adapter-main"})

    def test_missing_profile_raises_fail_closed_on_empty(self):
        # Fail-closed: no silent fallback to a default adapter.
        with pytest.raises(ProactiveDeliveryError):
            resolve_profile_adapter("main", {})

    def test_non_dict_adapters_raises(self):
        with pytest.raises(ProactiveDeliveryError):
            resolve_profile_adapter("main", ["not", "a", "dict"])

    def test_non_str_adapter_value_raises(self):
        with pytest.raises(ProactiveDeliveryError):
            resolve_profile_adapter("main", {"main": 123})


class TestSnowflakeValidation:
    @pytest.mark.parametrize("bad", INVALID_SNOWFLAKES)
    def test_invalid_home_channel_raises(self, bad):
        with pytest.raises(ProactiveDeliveryError):
            resolve_home_target(None, bad, deliver_home=True)

    @pytest.mark.parametrize("bad", INVALID_SNOWFLAKES)
    def test_invalid_origin_channel_raises(self, bad):
        with pytest.raises(ProactiveDeliveryError):
            resolve_home_target(bad, None, deliver_home=False)

    @pytest.mark.parametrize("bad", INVALID_SNOWFLAKES)
    def test_invalid_cron_thread_identity_raises(self, bad):
        with pytest.raises(ProactiveDeliveryError):
            continuable_thread_target(None, cron_thread_identity=bad)

    def test_non_str_snowflake_rejected(self):
        with pytest.raises(ProactiveDeliveryError):
            resolve_home_target(123456, None, deliver_home=False)

    def test_zero_is_valid_snowflake(self):
        target = resolve_home_target("0", None, deliver_home=False)
        assert target == DeliveryTarget(channel_id="0", thread_id=None)
