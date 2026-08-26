"""Tests for channel dispatch in plugins/platforms/twilio/adapter.py.

Only RCS is registered today; these tests cover the generic
target-format dispatch mechanism (_channel_for_target and friends) that
a second channel will also go through.
"""

from __future__ import annotations

from plugins.platforms.twilio import adapter
from plugins.platforms.twilio.channels.rcs import RcsChannel


def test_phone_number_routes_to_rcs_channel():
    channel = adapter._channel_for_target("+15551234567")
    assert isinstance(channel, RcsChannel)


def test_garbage_target_matches_no_channel():
    assert adapter._channel_for_target("not-a-target") is None


def test_parse_target_ref_dispatches_phone_to_rcs():
    assert adapter.parse_target_ref("+15551234567") == ("+15551234567", None)


def test_parse_target_ref_rejects_unrecognized_format():
    assert adapter.parse_target_ref("not-a-target") is None


def test_validate_target_ref_accepts_phone_number():
    assert adapter.validate_target_ref("+15551234567") is True


def test_validate_target_ref_rejects_unrecognized_format():
    result = adapter.validate_target_ref("not-a-target")
    assert result != True  # noqa: E712 -- explicitly checking for the string diagnostic
    assert "phone number" in result


def test_union_required_env_has_no_duplicates_and_covers_rcs():
    env_vars = adapter._union_required_env()
    assert len(env_vars) == len(set(env_vars))
    assert "TWILIO_MESSAGING_SERVICE_SID" in env_vars


def test_max_message_length_matches_rcs_channel():
    assert adapter._MAX_MESSAGE_LENGTH == RcsChannel.max_message_length
