"""Tests for the twilio_email platform plugin adapter."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.twilio_email import adapter as twilio_email


class _AsyncCM:
    """Minimal async context manager returning a fixed value.

    Mirrors tests/gateway/test_whatsapp_connect.py::_AsyncCM.
    """

    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *exc):
        return False


def _mock_error_response(status, text_body):
    resp = MagicMock()
    resp.status = status
    resp.text = AsyncMock(return_value=text_body)
    resp.headers = {}
    return resp


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in (
        "SENDGRID_API_KEY",
        "SENDGRID_FROM_EMAIL",
        "SENDGRID_FROM_NAME",
        "SENDGRID_API_BASE",
        "SENDGRID_HOME_CHANNEL",
    ):
        monkeypatch.delenv(key, raising=False)


# ---------------------------------------------------------------------------
# Target parsing / validation


def test_parse_target_ref_accepts_email():
    assert twilio_email.parse_target_ref("customer@example.com") == (
        "customer@example.com",
        None,
    )


def test_parse_target_ref_rejects_phone_number():
    assert twilio_email.parse_target_ref("+15551234567") is None


def test_validate_target_ref_accepts_email():
    assert twilio_email.validate_target_ref("customer@example.com") is True


def test_validate_target_ref_rejects_garbage():
    result = twilio_email.validate_target_ref("not-an-email")
    assert result != True  # noqa: E712 -- explicitly checking for the string diagnostic
    assert "not a valid email address" in result


# ---------------------------------------------------------------------------
# Subject/body split


def test_split_subject_and_body_with_newline():
    subject, body = twilio_email._split_subject_and_body(
        "Order shipped\nYour package is on its way."
    )
    assert subject == "Order shipped"
    assert body == "Your package is on its way."


def test_split_subject_and_body_single_line_gets_default_subject():
    subject, body = twilio_email._split_subject_and_body(
        "Just one line, no subject split"
    )
    assert subject == twilio_email.DEFAULT_SUBJECT
    assert body == "Just one line, no subject split"


def test_split_subject_and_body_blank_second_line_falls_back():
    # First line present but nothing meaningful follows -- don't manufacture
    # an empty body from a title-only message.
    subject, body = twilio_email._split_subject_and_body("Just a title\n   \n")
    assert subject == twilio_email.DEFAULT_SUBJECT


# ---------------------------------------------------------------------------
# Masking


def test_mask_email():
    assert twilio_email._mask_email("customer@example.com") == "c******r@example.com"


def test_mask_email_short_local_part():
    assert twilio_email._mask_email("ab@example.com") == "**@example.com"


# ---------------------------------------------------------------------------
# API base override


def test_sendgrid_api_base_defaults_to_public(monkeypatch):
    monkeypatch.delenv("SENDGRID_API_BASE", raising=False)
    assert twilio_email._sendgrid_api_base() == "https://api.sendgrid.com/v3"


def test_sendgrid_api_base_honors_override(monkeypatch):
    monkeypatch.setenv("SENDGRID_API_BASE", "https://api.staging.sendgrid.com/v3/")
    assert twilio_email._sendgrid_api_base() == "https://api.staging.sendgrid.com/v3"


# ---------------------------------------------------------------------------
# Readiness probes


def test_check_email_requirements_false_when_unconfigured(monkeypatch):
    monkeypatch.delenv("SENDGRID_API_KEY", raising=False)
    monkeypatch.delenv("SENDGRID_FROM_EMAIL", raising=False)
    assert twilio_email.check_email_requirements() is False


def test_check_email_requirements_true_when_configured(monkeypatch):
    monkeypatch.setenv("SENDGRID_API_KEY", "SG.testkey")
    monkeypatch.setenv("SENDGRID_FROM_EMAIL", "sender@example.com")
    assert twilio_email.check_email_requirements() is True


def test_is_connected_requires_both_key_and_sender(monkeypatch):
    monkeypatch.setenv("SENDGRID_API_KEY", "SG.testkey")
    monkeypatch.delenv("SENDGRID_FROM_EMAIL", raising=False)
    assert twilio_email._is_connected(None) is False

    monkeypatch.setenv("SENDGRID_FROM_EMAIL", "sender@example.com")
    assert twilio_email._is_connected(None) is True


# ---------------------------------------------------------------------------
# connect() readiness gate (no network -- fails before any HTTP call)


def test_connect_fails_fast_without_from_email(monkeypatch):
    monkeypatch.setenv("SENDGRID_API_KEY", "SG.testkey")
    monkeypatch.delenv("SENDGRID_FROM_EMAIL", raising=False)
    adapter = twilio_email.TwilioEmailAdapter(PlatformConfig())

    connected = asyncio.run(adapter.connect())

    assert connected is False


def test_connect_succeeds_when_from_email_set(monkeypatch):
    monkeypatch.setenv("SENDGRID_API_KEY", "SG.testkey")
    monkeypatch.setenv("SENDGRID_FROM_EMAIL", "sender@example.com")
    adapter = twilio_email.TwilioEmailAdapter(PlatformConfig())

    connected = asyncio.run(adapter.connect())

    assert connected is True


# ---------------------------------------------------------------------------
# Empty-body guard -- refuse before ever making an HTTP call, not after.


def test_send_refuses_empty_body_without_any_network_call(monkeypatch):
    monkeypatch.setenv("SENDGRID_API_KEY", "SG.testkey")
    monkeypatch.setenv("SENDGRID_FROM_EMAIL", "sender@example.com")
    adapter = twilio_email.TwilioEmailAdapter(PlatformConfig())

    # Single-line, all-whitespace content: default subject kicks in, body
    # after stripping is empty -- must be refused rather than silently
    # sending a blank email.
    result = asyncio.run(adapter.send("customer@example.com", "   "))

    assert result.success is False
    assert "empty body" in (result.error or "")


def test_standalone_send_refuses_empty_body(monkeypatch):
    monkeypatch.setenv("SENDGRID_API_KEY", "SG.testkey")
    monkeypatch.setenv("SENDGRID_FROM_EMAIL", "sender@example.com")

    result = asyncio.run(
        twilio_email._standalone_send(None, "customer@example.com", "   ")
    )

    assert "error" in result
    assert "empty body" in result["error"]


# ---------------------------------------------------------------------------
# Error-body redaction (mocked transport -- matches tests/gateway/test_sms.py
# and tests/gateway/test_whatsapp_connect.py's convention of mocking
# aiohttp.ClientSession rather than hitting the network).


def test_send_masks_email_in_error_body(monkeypatch):
    monkeypatch.setenv("SENDGRID_API_KEY", "SG.testkey")
    monkeypatch.setenv("SENDGRID_FROM_EMAIL", "sender@example.com")
    adapter = twilio_email.TwilioEmailAdapter(PlatformConfig())

    mock_resp = _mock_error_response(
        400, '{"errors":[{"message":"customer@example.com is not a valid address"}]}'
    )
    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=_AsyncCM(mock_resp))
    mock_session.close = AsyncMock()

    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = asyncio.run(
            adapter.send("customer@example.com", "Test subject\nTest body")
        )

    assert result.success is False
    assert "customer@example.com" not in (result.error or "")
    assert "c******r@example.com" in (result.error or "")


def test_standalone_send_masks_email_in_error_body(monkeypatch):
    monkeypatch.setenv("SENDGRID_API_KEY", "SG.testkey")
    monkeypatch.setenv("SENDGRID_FROM_EMAIL", "sender@example.com")

    mock_resp = _mock_error_response(
        400, '{"errors":[{"message":"customer@example.com is not a valid address"}]}'
    )
    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=_AsyncCM(mock_resp))

    with patch("aiohttp.ClientSession", return_value=_AsyncCM(mock_session)):
        result = asyncio.run(
            twilio_email._standalone_send(
                None, "customer@example.com", "Test subject\nTest body"
            )
        )

    assert "error" in result
    assert "customer@example.com" not in result["error"]
    assert "c******r@example.com" in result["error"]


def test_send_rejects_non_string_metadata_subject_without_crashing(monkeypatch):
    # Regression test: a non-string metadata["subject"] (e.g. a caller bug
    # passing an int) must not crash _sanitize_subject() with a TypeError --
    # it should fall back to the first-line convention instead.
    monkeypatch.setenv("SENDGRID_API_KEY", "SG.testkey")
    monkeypatch.setenv("SENDGRID_FROM_EMAIL", "sender@example.com")
    adapter = twilio_email.TwilioEmailAdapter(PlatformConfig())

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.headers = {"X-Message-Id": "abc123"}
    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=_AsyncCM(mock_resp))
    mock_session.close = AsyncMock()

    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = asyncio.run(
            adapter.send(
                "customer@example.com",
                "Order shipped\nYour package is on its way.",
                metadata={"subject": 12345},
            )
        )

    assert result.success is True


# ---------------------------------------------------------------------------
# Real-network smoke tests (fake credentials, confirm request
# shape/URL construction via a clean auth rejection from the real API,
# not a mocked transport).


@pytest.mark.integration
def test_send_reaches_sendgrid_and_gets_clean_auth_rejection(monkeypatch):
    monkeypatch.setenv("SENDGRID_API_KEY", "SG.not-a-real-key-for-shape-test-only")
    monkeypatch.setenv("SENDGRID_FROM_EMAIL", "sender@example.com")
    adapter = twilio_email.TwilioEmailAdapter(PlatformConfig())

    result = asyncio.run(
        adapter.send("customer@example.com", "Test subject\nTest body")
    )

    assert result.success is False
    assert "401" in (result.error or "") or "Unauthorized" in (result.error or "")


@pytest.mark.integration
def test_standalone_send_reaches_sendgrid_and_gets_clean_auth_rejection(monkeypatch):
    monkeypatch.setenv("SENDGRID_API_KEY", "SG.not-a-real-key-for-shape-test-only")
    monkeypatch.setenv("SENDGRID_FROM_EMAIL", "sender@example.com")

    result = asyncio.run(
        twilio_email._standalone_send(
            None, "customer@example.com", "Test subject\nTest body"
        )
    )

    assert "error" in result
    assert "401" in result["error"] or "Unauthorized" in result["error"]
