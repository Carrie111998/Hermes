import pytest

from reply_handlers.parser import parse, CommandIntent, ParseError


class TestParseHappyPath:
    def test_approve_with_reason(self):
        result = parse("/approve linkedin-4392439748 reason=VP role matches")
        assert result == CommandIntent(
            verb="approve",
            job_id="linkedin-4392439748",
            reason="VP role matches",
        )

    def test_reject_with_reason(self):
        result = parse("/reject job-123 reason=too junior")
        assert result == CommandIntent(verb="reject", job_id="job-123", reason="too junior")

    def test_archive_no_reason(self):
        result = parse("/archive linkedin-99999")
        assert result == CommandIntent(verb="archive", job_id="linkedin-99999", reason=None)

    def test_verb_case_insensitive(self):
        result = parse("/APPROVE job-1")
        assert result is not None and result.verb == "approve"

    def test_leading_trailing_whitespace(self):
        result = parse("  /approve job-1  ")
        assert result is not None and result.job_id == "job-1"


class TestParseNonCommand:
    def test_plain_text_returns_none(self):
        assert parse("hello there") is None

    def test_empty_string_returns_none(self):
        assert parse("") is None

    def test_unknown_slash_command_returns_none(self):
        # /start, /help, etc. are not our commands; let other handlers take them
        assert parse("/start") is None
        assert parse("/help") is None


class TestParseErrors:
    def test_approve_without_job_id_raises(self):
        with pytest.raises(ParseError, match="job_id"):
            parse("/approve")

    def test_reject_without_job_id_raises(self):
        with pytest.raises(ParseError, match="job_id"):
            parse("/reject")
