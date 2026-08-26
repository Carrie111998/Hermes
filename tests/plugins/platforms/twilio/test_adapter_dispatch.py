"""Tests for channel dispatch in plugins/platforms/twilio/adapter.py.

RCS and Email share one registered platform ("twilio"); these tests cover
the target-format-based routing between them, which is the behavior added
by hosting both channels under a single adapter.
"""

from __future__ import annotations

from plugins.platforms.twilio import adapter
from plugins.platforms.twilio.channels.email import EmailChannel
from plugins.platforms.twilio.channels.rcs import RcsChannel


def test_phone_number_routes_to_rcs_channel():
    channel = adapter._channel_for_target("+15551234567")
    assert isinstance(channel, RcsChannel)


def test_email_address_routes_to_email_channel():
    channel = adapter._channel_for_target("customer@example.com")
    assert isinstance(channel, EmailChannel)


def test_garbage_target_matches_no_channel():
    assert adapter._channel_for_target("not-a-target") is None


def test_parse_target_ref_dispatches_phone_to_rcs():
    assert adapter.parse_target_ref("+15551234567") == ("+15551234567", None)


def test_parse_target_ref_dispatches_email_to_email_channel():
    assert adapter.parse_target_ref("customer@example.com") == (
        "customer@example.com",
        None,
    )


def test_parse_target_ref_rejects_unrecognized_format():
    assert adapter.parse_target_ref("not-a-target") is None


def test_validate_target_ref_accepts_both_formats():
    assert adapter.validate_target_ref("+15551234567") is True
    assert adapter.validate_target_ref("customer@example.com") is True


def test_validate_target_ref_rejects_unrecognized_format():
    result = adapter.validate_target_ref("not-a-target")
    assert result != True  # noqa: E712 -- explicitly checking for the string diagnostic
    assert "phone number" in result and "email" in result


def test_union_required_env_has_no_duplicates_and_covers_both_channels():
    env_vars = adapter._union_required_env()
    assert len(env_vars) == len(set(env_vars))
    assert "TWILIO_MESSAGING_SERVICE_SID" in env_vars
    assert "SENDGRID_API_KEY" in env_vars


def test_max_message_length_is_the_larger_of_the_two_channels():
    assert adapter._MAX_MESSAGE_LENGTH == max(
        RcsChannel.max_message_length, EmailChannel.max_message_length
    )
