"""Tests for tools.discord_api.moderation (feature A4)."""

from datetime import datetime, timedelta, timezone

import pytest

from tools.discord_api.moderation import (
    MAX_DELETE_MESSAGE_DAYS,
    MAX_REASON_LENGTH,
    MAX_TIMEOUT_SECONDS,
    ModerationError,
    ban_member_request,
    kick_member_request,
    remove_timeout_request,
    timeout_member_request,
    unban_member_request,
)

GUILD = "123456789012345678"
USER = "234567890123456789"


class TestSnowflakeValidation:
    @pytest.mark.parametrize(
        "value",
        [
            "abc",
            "-5",
            "12.5",
            "",
            "   ",
            "0",
            0,
            -1,
            1.5,
            None,
            True,
            [],
            {},
        ],
    )
    def test_invalid_snowflakes_raise(self, value):
        with pytest.raises(ModerationError):
            timeout_member_request(value, USER, duration_seconds=60)

    def test_invalid_user_id_raises(self):
        with pytest.raises(ModerationError):
            timeout_member_request(GUILD, "not-a-snowflake", duration_seconds=60)

    def test_snowflake_accepts_int_and_str(self):
        req = timeout_member_request(123456789012345678, USER, duration_seconds=60)
        assert req["path"] == f"/guilds/123456789012345678/members/{USER}"

    def test_moderation_error_is_value_error(self):
        assert issubclass(ModerationError, ValueError)


class TestTimeoutMemberRequest:
    def test_basic(self):
        req = timeout_member_request(GUILD, USER, duration_seconds=3600)
        assert req["method"] == "PATCH"
        assert req["path"] == f"/guilds/{GUILD}/members/{USER}"
        until = req["payload"]["communication_disabled_until"]
        assert isinstance(until, str)
        assert until.endswith("Z")

    def test_iso_timestamp_is_now_plus_duration(self):
        before = datetime.now(timezone.utc)
        req = timeout_member_request(GUILD, USER, duration_seconds=60)
        after = datetime.now(timezone.utc)
        until = datetime.fromisoformat(
            req["payload"]["communication_disabled_until"].replace("Z", "+00:00")
        )
        assert until.astimezone(timezone.utc).tzinfo is not None
        assert before + timedelta(seconds=60) - timedelta(seconds=5) <= until
        assert until <= after + timedelta(seconds=60) + timedelta(seconds=5)

    def test_duration_bounds(self):
        timeout_member_request(GUILD, USER, duration_seconds=1)
        timeout_member_request(GUILD, USER, duration_seconds=MAX_TIMEOUT_SECONDS)

    @pytest.mark.parametrize(
        "duration",
        [0, -1, MAX_TIMEOUT_SECONDS + 1, 1.5, "60", None, True],
    )
    def test_invalid_duration_raises(self, duration):
        with pytest.raises(ModerationError):
            timeout_member_request(GUILD, USER, duration_seconds=duration)

    def test_reason_in_header(self):
        req = timeout_member_request(GUILD, USER, duration_seconds=60, reason="spam")
        assert req["headers"]["X-Audit-Log-Reason"] == "spam"

    def test_no_reason_no_header(self):
        req = timeout_member_request(GUILD, USER, duration_seconds=60)
        assert "headers" not in req

    def test_reason_boundary(self):
        timeout_member_request(
            GUILD, USER, duration_seconds=60, reason="x" * MAX_REASON_LENGTH
        )
        with pytest.raises(ModerationError):
            timeout_member_request(
                GUILD, USER, duration_seconds=60, reason="x" * (MAX_REASON_LENGTH + 1)
            )

    def test_non_string_reason_raises(self):
        with pytest.raises(ModerationError):
            timeout_member_request(GUILD, USER, duration_seconds=60, reason=123)


class TestRemoveTimeoutRequest:
    def test_basic(self):
        req = remove_timeout_request(GUILD, USER)
        assert req["method"] == "PATCH"
        assert req["path"] == f"/guilds/{GUILD}/members/{USER}"
        assert req["payload"] == {"communication_disabled_until": None}

    def test_invalid_snowflake_raises(self):
        with pytest.raises(ModerationError):
            remove_timeout_request("nope", USER)


class TestKickMemberRequest:
    def test_basic(self):
        req = kick_member_request(GUILD, USER)
        assert req["method"] == "DELETE"
        assert req["path"] == f"/guilds/{GUILD}/members/{USER}"
        assert "params" not in req

    def test_reason_as_query_param(self):
        req = kick_member_request(GUILD, USER, reason="rule 1")
        assert req["params"] == {"reason": "rule 1"}

    def test_reason_boundary(self):
        kick_member_request(GUILD, USER, reason="x" * MAX_REASON_LENGTH)
        with pytest.raises(ModerationError):
            kick_member_request(GUILD, USER, reason="x" * (MAX_REASON_LENGTH + 1))


class TestBanMemberRequest:
    def test_basic(self):
        req = ban_member_request(GUILD, USER)
        assert req["method"] == "PUT"
        assert req["path"] == f"/guilds/{GUILD}/bans/{USER}"
        assert req["payload"] == {"delete_message_days": 0}

    def test_delete_message_days_bounds(self):
        ban_member_request(GUILD, USER, delete_message_days=0)
        ban_member_request(GUILD, USER, delete_message_days=MAX_DELETE_MESSAGE_DAYS)

    @pytest.mark.parametrize(
        "days",
        [-1, MAX_DELETE_MESSAGE_DAYS + 1, 1.5, "2", None, True],
    )
    def test_invalid_delete_message_days_raises(self, days):
        with pytest.raises(ModerationError):
            ban_member_request(GUILD, USER, delete_message_days=days)

    def test_reason_in_payload(self):
        req = ban_member_request(GUILD, USER, delete_message_days=3, reason="spam")
        assert req["payload"] == {"delete_message_days": 3, "reason": "spam"}

    def test_reason_omitted_when_none(self):
        req = ban_member_request(GUILD, USER, delete_message_days=2)
        assert req["payload"] == {"delete_message_days": 2}

    def test_reason_boundary(self):
        ban_member_request(GUILD, USER, reason="x" * MAX_REASON_LENGTH)
        with pytest.raises(ModerationError):
            ban_member_request(GUILD, USER, reason="x" * (MAX_REASON_LENGTH + 1))


class TestUnbanMemberRequest:
    def test_basic(self):
        req = unban_member_request(GUILD, USER)
        assert req["method"] == "DELETE"
        assert req["path"] == f"/guilds/{GUILD}/bans/{USER}"
        assert "payload" not in req

    def test_invalid_snowflake_raises(self):
        with pytest.raises(ModerationError):
            unban_member_request(GUILD, 0)
