"""Tests for the twilio_email platform plugin adapter."""

from __future__ import annotations

import asyncio
import base64
import json
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


def _mock_response(status, *, json_body=None, text_body=None, headers=None):
    resp = MagicMock()
    resp.status = status
    resp.headers = headers or {}
    resp.json = AsyncMock(return_value=json_body or {})
    resp.text = AsyncMock(
        return_value=text_body if text_body is not None else json.dumps(json_body or {})
    )
    return resp


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in (
        "TWILIO_ACCOUNT_SID",
        "TWILIO_AUTH_TOKEN",
        "TWILIO_EMAIL_FROM",
        "TWILIO_EMAIL_FROM_NAME",
        "TWILIO_EMAIL_API_BASE",
        "TWILIO_EMAIL_HOME_CHANNEL",
    ):
        monkeypatch.delenv(key, raising=False)


def _configure(monkeypatch):
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACtestsid")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "testtoken")
    monkeypatch.setenv("TWILIO_EMAIL_FROM", "sender@example.com")


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


def test_twilio_email_api_base_defaults_to_public(monkeypatch):
    monkeypatch.delenv("TWILIO_EMAIL_API_BASE", raising=False)
    assert twilio_email._twilio_email_api_base() == "https://comms.twilio.com/v1/Emails"


def test_twilio_email_api_base_honors_override(monkeypatch):
    monkeypatch.setenv(
        "TWILIO_EMAIL_API_BASE", "https://comms.staging.twilio.com/v1/Emails/"
    )
    assert (
        twilio_email._twilio_email_api_base()
        == "https://comms.staging.twilio.com/v1/Emails"
    )


# ---------------------------------------------------------------------------
# Basic auth header


def test_basic_auth_header_encodes_sid_and_token():
    header = twilio_email._basic_auth_header("ACsid", "token123")
    assert header.startswith("Basic ")
    decoded = base64.b64decode(header.split(" ", 1)[1]).decode("ascii")
    assert decoded == "ACsid:token123"


# ---------------------------------------------------------------------------
# Readiness probes


def test_check_email_requirements_false_when_unconfigured(monkeypatch):
    assert twilio_email.check_email_requirements() is False


def test_check_email_requirements_true_when_configured(monkeypatch):
    _configure(monkeypatch)
    assert twilio_email.check_email_requirements() is True


def test_is_connected_requires_all_three(monkeypatch):
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACtestsid")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "testtoken")
    assert twilio_email._is_connected(None) is False

    monkeypatch.setenv("TWILIO_EMAIL_FROM", "sender@example.com")
    assert twilio_email._is_connected(None) is True


# ---------------------------------------------------------------------------
# connect() readiness gate (no network -- fails before any HTTP call)


def test_connect_fails_fast_without_credentials(monkeypatch):
    monkeypatch.setenv("TWILIO_EMAIL_FROM", "sender@example.com")
    adapter = twilio_email.TwilioEmailAdapter(PlatformConfig())

    connected = asyncio.run(adapter.connect())

    assert connected is False


def test_connect_fails_fast_without_from_email(monkeypatch):
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACtestsid")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "testtoken")
    adapter = twilio_email.TwilioEmailAdapter(PlatformConfig())

    connected = asyncio.run(adapter.connect())

    assert connected is False


def test_connect_succeeds_when_fully_configured(monkeypatch):
    _configure(monkeypatch)
    adapter = twilio_email.TwilioEmailAdapter(PlatformConfig())

    async def _connect_and_disconnect():
        connected = await adapter.connect()
        # connect() opens a real aiohttp.ClientSession -- close it in the same
        # event loop it was created in, or aiohttp warns about an unclosed session.
        await adapter.disconnect()
        return connected

    connected = asyncio.run(_connect_and_disconnect())

    assert connected is True


# ---------------------------------------------------------------------------
# Empty-body guard -- refuse before ever making an HTTP call, not after.


def test_send_refuses_empty_body_without_any_network_call(monkeypatch):
    _configure(monkeypatch)
    adapter = twilio_email.TwilioEmailAdapter(PlatformConfig())

    result = asyncio.run(adapter.send("customer@example.com", "   "))

    assert result.success is False
    assert "empty body" in (result.error or "")


def test_standalone_send_refuses_empty_body(monkeypatch):
    _configure(monkeypatch)

    result = asyncio.run(
        twilio_email._standalone_send(None, "customer@example.com", "   ")
    )

    assert "error" in result
    assert "empty body" in result["error"]


# ---------------------------------------------------------------------------
# Attachments


def test_build_attachments_reads_and_encodes_file(tmp_path):
    f = tmp_path / "note.txt"
    f.write_bytes(b"hello world")

    attachments, error = twilio_email._build_attachments([str(f)])

    assert error is None
    assert len(attachments) == 1
    assert attachments[0]["filename"] == "note.txt"
    assert attachments[0]["contentType"] == "text/plain"
    assert base64.b64decode(attachments[0]["content"]) == b"hello world"


def test_build_attachments_missing_file_returns_error(tmp_path):
    missing = tmp_path / "does-not-exist.pdf"

    attachments, error = twilio_email._build_attachments([str(missing)])

    assert attachments == []
    assert "not found" in error


def test_build_attachments_rejects_over_size_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(twilio_email, "MAX_ATTACHMENT_BYTES_RAW", 10)
    f = tmp_path / "big.bin"
    f.write_bytes(b"x" * 20)

    attachments, error = twilio_email._build_attachments([str(f)])

    assert attachments == []
    assert "too large" in error


def test_send_with_metadata_attachments(monkeypatch, tmp_path):
    _configure(monkeypatch)
    f = tmp_path / "report.pdf"
    f.write_bytes(b"%PDF-1.4 fake")
    adapter = twilio_email.TwilioEmailAdapter(PlatformConfig())

    mock_resp = _mock_response(202, json_body={"operationId": "op123"})
    mock_session = MagicMock()
    mock_session.close = AsyncMock()
    captured = {}

    def _capturing_post(url, json=None, headers=None):
        captured["payload"] = json
        return _AsyncCM(mock_resp)

    mock_session.post = MagicMock(side_effect=_capturing_post)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = asyncio.run(
            adapter.send(
                "customer@example.com",
                "Report attached",
                metadata={"attachments": [str(f)]},
            )
        )

    assert result.success is True
    assert result.message_id == "op123"
    sent_attachments = captured["payload"]["content"]["attachments"]
    assert sent_attachments[0]["filename"] == "report.pdf"


def test_send_document_attaches_local_file(monkeypatch, tmp_path):
    _configure(monkeypatch)
    f = tmp_path / "invoice.txt"
    f.write_bytes(b"invoice contents")
    adapter = twilio_email.TwilioEmailAdapter(PlatformConfig())

    mock_resp = _mock_response(202, json_body={"operationId": "op456"})
    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=_AsyncCM(mock_resp))
    mock_session.close = AsyncMock()

    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = asyncio.run(
            adapter.send_document(
                "customer@example.com", str(f), caption="Here's the invoice"
            )
        )

    assert result.success is True


def test_send_document_missing_file_returns_error_without_network_call(monkeypatch):
    _configure(monkeypatch)
    adapter = twilio_email.TwilioEmailAdapter(PlatformConfig())

    result = asyncio.run(
        adapter.send_document("customer@example.com", "/no/such/file.pdf")
    )

    assert result.success is False
    assert "not found" in result.error


def test_send_image_remote_url_links_in_body_not_downloaded(monkeypatch):
    _configure(monkeypatch)
    adapter = twilio_email.TwilioEmailAdapter(PlatformConfig())

    mock_resp = _mock_response(202, json_body={"operationId": "op789"})
    mock_session = MagicMock()
    captured = {}

    def _capturing_post(url, json=None, headers=None):
        captured["payload"] = json
        return _AsyncCM(mock_resp)

    mock_session.post = MagicMock(side_effect=_capturing_post)
    mock_session.close = AsyncMock()

    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = asyncio.run(
            adapter.send_image(
                "customer@example.com", "https://example.com/pic.png", caption="Look"
            )
        )

    assert result.success is True
    assert "attachments" not in captured["payload"]["content"]
    assert "https://example.com/pic.png" in captured["payload"]["content"]["text"]


def test_standalone_send_attaches_media_files(monkeypatch, tmp_path):
    _configure(monkeypatch)
    f = tmp_path / "photo.jpg"
    f.write_bytes(b"\xff\xd8\xff fake jpeg")

    mock_resp = _mock_response(202, json_body={"operationId": "op999"})
    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=_AsyncCM(mock_resp))
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = asyncio.run(
            twilio_email._standalone_send(
                None,
                "customer@example.com",
                "Photo attached",
                media_files=[(str(f), False)],
            )
        )

    assert result.get("success") is True
    assert result["message_id"] == "op999"


def test_standalone_send_missing_media_file_returns_error(monkeypatch):
    _configure(monkeypatch)

    result = asyncio.run(
        twilio_email._standalone_send(
            None,
            "customer@example.com",
            "Photo attached",
            media_files=[("/no/such/photo.jpg", False)],
        )
    )

    assert "error" in result
    assert "not found" in result["error"]


# ---------------------------------------------------------------------------
# Error-body redaction (mocked transport -- matches tests/gateway/test_sms.py
# and tests/gateway/test_whatsapp_connect.py's convention of mocking
# aiohttp.ClientSession rather than hitting the network).


def test_send_masks_email_in_error_body(monkeypatch):
    _configure(monkeypatch)
    adapter = twilio_email.TwilioEmailAdapter(PlatformConfig())

    mock_resp = _mock_response(
        400,
        text_body='{"errors":[{"message":"customer@example.com is not a valid address"}]}',
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
    _configure(monkeypatch)

    mock_resp = _mock_response(
        400,
        text_body='{"errors":[{"message":"customer@example.com is not a valid address"}]}',
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
    _configure(monkeypatch)
    adapter = twilio_email.TwilioEmailAdapter(PlatformConfig())

    mock_resp = _mock_response(202, json_body={"operationId": "op-ok"})
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
# Error clarity on a first real send -- exceptions like asyncio.TimeoutError
# stringify to "", which must not surface as a blank/useless error.


def test_send_error_is_never_blank_for_exceptions_with_empty_str(monkeypatch):
    _configure(monkeypatch)
    adapter = twilio_email.TwilioEmailAdapter(PlatformConfig())

    mock_session = MagicMock()
    mock_session.post = MagicMock(side_effect=TimeoutError())
    mock_session.close = AsyncMock()

    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = asyncio.run(
            adapter.send("customer@example.com", "Test subject\nTest body")
        )

    assert result.success is False
    assert result.error
    assert "TimeoutError" in result.error


def test_standalone_send_error_is_never_blank_for_exceptions_with_empty_str(
    monkeypatch,
):
    _configure(monkeypatch)

    with patch("aiohttp.ClientSession", side_effect=TimeoutError()):
        result = asyncio.run(
            twilio_email._standalone_send(
                None, "customer@example.com", "Test subject\nTest body"
            )
        )

    assert "error" in result
    assert "TimeoutError" in result["error"]


# ---------------------------------------------------------------------------
# A 2xx response that already means "accepted" must not be reported as a
# failed send just because its body didn't parse -- that would risk a
# retry-induced duplicate email.


def test_send_treats_unparseable_202_body_as_success_not_failure(monkeypatch):
    _configure(monkeypatch)
    adapter = twilio_email.TwilioEmailAdapter(PlatformConfig())

    mock_resp = MagicMock()
    mock_resp.status = 202
    mock_resp.headers = {}
    mock_resp.json = AsyncMock(side_effect=ValueError("not json"))
    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=_AsyncCM(mock_resp))
    mock_session.close = AsyncMock()

    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = asyncio.run(
            adapter.send("customer@example.com", "Test subject\nTest body")
        )

    assert result.success is True
    assert result.message_id == ""


def test_send_treats_empty_202_body_as_success_not_attribute_error(monkeypatch):
    # aiohttp's resp.json() returns None (no exception) for an empty body --
    # a naive `data.get(...)` on that would raise AttributeError and get
    # caught by the outer except, misreporting an accepted send as failed.
    _configure(monkeypatch)
    adapter = twilio_email.TwilioEmailAdapter(PlatformConfig())

    mock_resp = MagicMock()
    mock_resp.status = 202
    mock_resp.headers = {}
    mock_resp.json = AsyncMock(return_value=None)
    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=_AsyncCM(mock_resp))
    mock_session.close = AsyncMock()

    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = asyncio.run(
            adapter.send("customer@example.com", "Test subject\nTest body")
        )

    assert result.success is True
    assert result.message_id == ""


# ---------------------------------------------------------------------------
# Real-network smoke tests (fake credentials, confirm request
# shape/URL construction via a clean rejection from the real API, not a
# mocked transport). Confirmed live: a syntactically-fake Account SID gets a
# 401 with Twilio's standard error envelope
# ({"code":20003,"message":"Authentication Error - invalid username",...}).


@pytest.mark.integration
def test_send_reaches_twilio_email_api_and_gets_clean_rejection(monkeypatch):
    _configure(monkeypatch)
    adapter = twilio_email.TwilioEmailAdapter(PlatformConfig())

    result = asyncio.run(
        adapter.send("customer@example.com", "Test subject\nTest body")
    )

    assert result.success is False
    assert result.error and "401" in result.error


@pytest.mark.integration
def test_standalone_send_reaches_twilio_email_api_and_gets_clean_rejection(monkeypatch):
    _configure(monkeypatch)

    result = asyncio.run(
        twilio_email._standalone_send(
            None, "customer@example.com", "Test subject\nTest body"
        )
    )

    assert "error" in result
    assert "401" in result["error"]
