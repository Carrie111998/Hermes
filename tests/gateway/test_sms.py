"""Tests for SMS (Twilio) platform integration.

Covers config loading, format/truncate, echo prevention,
requirements check, toolset verification, and Twilio signature validation.
"""

import base64
import hashlib
import hmac
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import Platform, PlatformConfig


# ── Config loading ──────────────────────────────────────────────────

class TestSmsConfigLoading:
    """Verify _apply_env_overrides wires SMS correctly."""

    def test_env_overrides_create_sms_config(self):
        from gateway.config import load_gateway_config

        env = {
            "TWILIO_ACCOUNT_SID": "ACtest123",
            "TWILIO_AUTH_TOKEN": "token_abc",
            "TWILIO_PHONE_NUMBER": "+15551234567",
        }
        with patch.dict(os.environ, env, clear=False):
            config = load_gateway_config()
            assert Platform.SMS in config.platforms
            pc = config.platforms[Platform.SMS]
            assert pc.enabled is True
            assert pc.api_key == "token_abc"

    def test_env_overrides_set_home_channel(self):
        from gateway.config import load_gateway_config

        env = {
            "TWILIO_ACCOUNT_SID": "ACtest123",
            "TWILIO_AUTH_TOKEN": "token_abc",
            "TWILIO_PHONE_NUMBER": "+15551234567",
            "SMS_HOME_CHANNEL": "+15559876543",
            "SMS_HOME_CHANNEL_NAME": "My Phone",
        }
        with patch.dict(os.environ, env, clear=False):
            config = load_gateway_config()
            hc = config.platforms[Platform.SMS].home_channel
            assert hc is not None
            assert hc.chat_id == "+15559876543"
            assert hc.name == "My Phone"
            assert hc.platform == Platform.SMS

# ── Format / truncate ───────────────────────────────────────────────

class TestSmsFormatAndTruncate:
    """Test SmsAdapter.format_message strips markdown."""

    def _make_adapter(self):
        from plugins.platforms.sms.adapter import SmsAdapter

        env = {
            "TWILIO_ACCOUNT_SID": "ACtest",
            "TWILIO_AUTH_TOKEN": "tok",
            "TWILIO_PHONE_NUMBER": "+15550001111",
        }
        with patch.dict(os.environ, env):
            pc = PlatformConfig(enabled=True, api_key="tok")
            adapter = object.__new__(SmsAdapter)
            adapter.config = pc
            adapter._platform = Platform.SMS
            adapter._account_sid = "ACtest"
            adapter._auth_token = "tok"
            adapter._from_number = "+15550001111"
        return adapter

    def test_strips_bold(self):
        adapter = self._make_adapter()
        assert adapter.format_message("**hello**") == "hello"

    def test_strips_italic(self):
        adapter = self._make_adapter()
        assert adapter.format_message("*world*") == "world"

    def test_strips_code_blocks(self):
        adapter = self._make_adapter()
        result = adapter.format_message("```python\nprint('hi')\n```")
        assert "```" not in result
        assert "print('hi')" in result

    def test_strips_inline_code(self):
        adapter = self._make_adapter()
        assert adapter.format_message("`code`") == "code"

    def test_strips_headers(self):
        adapter = self._make_adapter()
        assert adapter.format_message("## Title") == "Title"

    def test_strips_links(self):
        adapter = self._make_adapter()
        assert adapter.format_message("[click](https://example.com)") == "click"

    def test_collapses_newlines(self):
        adapter = self._make_adapter()
        result = adapter.format_message("a\n\n\n\nb")
        assert result == "a\n\nb"


# ── Echo prevention ────────────────────────────────────────────────

class TestSmsEchoPrevention:
    """Adapter should ignore messages from its own number."""

    def test_own_number_detection(self):
        """The adapter stores _from_number for echo prevention."""
        from plugins.platforms.sms.adapter import SmsAdapter

        env = {
            "TWILIO_ACCOUNT_SID": "ACtest",
            "TWILIO_AUTH_TOKEN": "tok",
            "TWILIO_PHONE_NUMBER": "+15550001111",
        }
        with patch.dict(os.environ, env):
            pc = PlatformConfig(enabled=True, api_key="tok")
            adapter = SmsAdapter(pc)
            assert adapter._from_number == "+15550001111"


# ── Requirements check ─────────────────────────────────────────────

class TestSmsRequirements:
    def test_check_sms_requirements_missing_sid(self):
        from plugins.platforms.sms.adapter import check_sms_requirements

        env = {"TWILIO_AUTH_TOKEN": "tok"}
        with patch.dict(os.environ, env, clear=True):
            assert check_sms_requirements() is False

    def test_check_sms_requirements_missing_token(self):
        from plugins.platforms.sms.adapter import check_sms_requirements

        env = {"TWILIO_ACCOUNT_SID": "ACtest"}
        with patch.dict(os.environ, env, clear=True):
            assert check_sms_requirements() is False

    def test_check_sms_requirements_both_set(self):
        from plugins.platforms.sms.adapter import check_sms_requirements

        env = {
            "TWILIO_ACCOUNT_SID": "ACtest",
            "TWILIO_AUTH_TOKEN": "tok",
        }
        with patch.dict(os.environ, env, clear=False):
            # Only returns True if aiohttp is also importable
            result = check_sms_requirements()
            try:
                import aiohttp  # noqa: F401
                assert result is True
            except ImportError:
                assert result is False


# ── Toolset verification ───────────────────────────────────────────

# ── Webhook host configuration ─────────────────────────────────────

class TestWebhookHostConfig:
    """Verify SMS_WEBHOOK_HOST env var and default."""

    def test_default_host_is_localhost(self):
        from plugins.platforms.sms.adapter import DEFAULT_WEBHOOK_HOST
        assert DEFAULT_WEBHOOK_HOST == "127.0.0.1"

    def test_host_from_env(self):
        from plugins.platforms.sms.adapter import SmsAdapter

        env = {
            "TWILIO_ACCOUNT_SID": "ACtest",
            "TWILIO_AUTH_TOKEN": "tok",
            "TWILIO_PHONE_NUMBER": "+15550001111",
            "SMS_WEBHOOK_HOST": "127.0.0.1",
        }
        with patch.dict(os.environ, env):
            pc = PlatformConfig(enabled=True, api_key="tok")
            adapter = SmsAdapter(pc)
            assert adapter._webhook_host == "127.0.0.1"

    def test_webhook_url_from_env(self):
        from plugins.platforms.sms.adapter import SmsAdapter

        env = {
            "TWILIO_ACCOUNT_SID": "ACtest",
            "TWILIO_AUTH_TOKEN": "tok",
            "TWILIO_PHONE_NUMBER": "+15550001111",
            "SMS_WEBHOOK_URL": "https://example.com/webhooks/twilio",
        }
        with patch.dict(os.environ, env):
            pc = PlatformConfig(enabled=True, api_key="tok")
            adapter = SmsAdapter(pc)
            assert adapter._webhook_url == "https://example.com/webhooks/twilio"

    def test_webhook_url_stripped(self):
        from plugins.platforms.sms.adapter import SmsAdapter

        env = {
            "TWILIO_ACCOUNT_SID": "ACtest",
            "TWILIO_AUTH_TOKEN": "tok",
            "TWILIO_PHONE_NUMBER": "+15550001111",
            "SMS_WEBHOOK_URL": "  https://example.com/webhooks/twilio  ",
        }
        with patch.dict(os.environ, env):
            pc = PlatformConfig(enabled=True, api_key="tok")
            adapter = SmsAdapter(pc)
            assert adapter._webhook_url == "https://example.com/webhooks/twilio"


# ── Startup guard (fail-closed) ────────────────────────────────────

class TestStartupGuard:
    """Adapter must refuse to start without SMS_WEBHOOK_URL."""

    def _make_adapter(self, extra_env=None):
        from plugins.platforms.sms.adapter import SmsAdapter

        env = {
            "TWILIO_ACCOUNT_SID": "ACtest",
            "TWILIO_AUTH_TOKEN": "tok",
            "TWILIO_PHONE_NUMBER": "+15550001111",
        }
        if extra_env:
            env.update(extra_env)
        with patch.dict(os.environ, env, clear=False):
            pc = PlatformConfig(enabled=True, api_key="tok")
            adapter = SmsAdapter(pc)
        return adapter

    @pytest.mark.asyncio
    async def test_refuses_start_without_webhook_url(self):
        adapter = self._make_adapter()
        result = await adapter.connect()
        assert result is False

    @pytest.mark.asyncio
    async def test_missing_webhook_url_is_non_retryable(self):
        adapter = self._make_adapter()
        await adapter.connect()
        assert adapter.has_fatal_error is True
        assert adapter.fatal_error_retryable is False
        assert "sms_missing_webhook_url" == adapter.fatal_error_code

    @pytest.mark.asyncio
    async def test_missing_phone_number_is_non_retryable(self):
        from plugins.platforms.sms.adapter import SmsAdapter

        env = {
            "TWILIO_ACCOUNT_SID": "ACtest",
            "TWILIO_AUTH_TOKEN": "tok",
            "TWILIO_PHONE_NUMBER": "",
            "SMS_WEBHOOK_URL": "",
        }
        with patch.dict(os.environ, env, clear=True):
            pc = PlatformConfig(enabled=True, api_key="tok")
            adapter = SmsAdapter(pc)
        await adapter.connect()
        assert adapter.has_fatal_error is True
        assert adapter.fatal_error_retryable is False
        assert adapter.fatal_error_code == "sms_missing_phone_number"

    @pytest.mark.asyncio
    async def test_insecure_flag_does_not_set_fatal_error(self):
        mock_session = AsyncMock()
        with patch.dict(os.environ, {"SMS_INSECURE_NO_SIGNATURE": "true"}), \
             patch("aiohttp.web.AppRunner") as mock_runner_cls, \
             patch("aiohttp.web.TCPSite") as mock_site_cls, \
             patch("aiohttp.ClientSession", return_value=mock_session):
            mock_runner_cls.return_value.setup = AsyncMock()
            mock_runner_cls.return_value.cleanup = AsyncMock()
            mock_site_cls.return_value.start = AsyncMock()
            adapter = self._make_adapter()
            result = await adapter.connect()
            assert result is True
            assert adapter.has_fatal_error is False
            await adapter.disconnect()

    @pytest.mark.asyncio
    async def test_insecure_flag_allows_start_without_url(self):
        mock_session = AsyncMock()
        with patch.dict(os.environ, {"SMS_INSECURE_NO_SIGNATURE": "true"}), \
             patch("aiohttp.web.AppRunner") as mock_runner_cls, \
             patch("aiohttp.web.TCPSite") as mock_site_cls, \
             patch("aiohttp.ClientSession", return_value=mock_session):
            mock_runner_cls.return_value.setup = AsyncMock()
            mock_runner_cls.return_value.cleanup = AsyncMock()
            mock_site_cls.return_value.start = AsyncMock()
            adapter = self._make_adapter()
            result = await adapter.connect()
            assert result is True
            await adapter.disconnect()

    @pytest.mark.asyncio
    async def test_webhook_url_allows_start(self):
        mock_session = AsyncMock()
        with patch("aiohttp.web.AppRunner") as mock_runner_cls, \
             patch("aiohttp.web.TCPSite") as mock_site_cls, \
             patch("aiohttp.ClientSession", return_value=mock_session):
            mock_runner_cls.return_value.setup = AsyncMock()
            mock_runner_cls.return_value.cleanup = AsyncMock()
            mock_site_cls.return_value.start = AsyncMock()
            adapter = self._make_adapter(
                extra_env={"SMS_WEBHOOK_URL": "https://example.com/webhooks/twilio"}
            )
            result = await adapter.connect()
            assert result is True
            await adapter.disconnect()


# ── Twilio signature validation ────────────────────────────────────

def _compute_twilio_signature(auth_token, url, params):
    """Reference implementation of Twilio's signature algorithm."""
    data_to_sign = url
    for key in sorted(params.keys()):
        data_to_sign += key + params[key]
    mac = hmac.new(
        auth_token.encode("utf-8"),
        data_to_sign.encode("utf-8"),
        hashlib.sha1,
    )
    return base64.b64encode(mac.digest()).decode("utf-8")


class TestTwilioSignatureValidation:
    """Unit tests for SmsAdapter._validate_twilio_signature."""

    def _make_adapter(self, auth_token="test_token_secret"):
        from plugins.platforms.sms.adapter import SmsAdapter

        env = {
            "TWILIO_ACCOUNT_SID": "ACtest",
            "TWILIO_AUTH_TOKEN": auth_token,
            "TWILIO_PHONE_NUMBER": "+15550001111",
        }
        with patch.dict(os.environ, env):
            pc = PlatformConfig(enabled=True, api_key=auth_token)
            adapter = SmsAdapter(pc)
        return adapter

    def test_valid_signature_accepted(self):
        adapter = self._make_adapter()
        url = "https://example.com/webhooks/twilio"
        params = {"From": "+15551234567", "Body": "hello", "To": "+15550001111"}
        sig = _compute_twilio_signature("test_token_secret", url, params)
        assert adapter._validate_twilio_signature(url, params, sig) is True

    def test_invalid_signature_rejected(self):
        adapter = self._make_adapter()
        url = "https://example.com/webhooks/twilio"
        params = {"From": "+15551234567", "Body": "hello"}
        assert adapter._validate_twilio_signature(url, params, "badsig") is False

    def test_wrong_token_rejected(self):
        adapter = self._make_adapter(auth_token="correct_token")
        url = "https://example.com/webhooks/twilio"
        params = {"From": "+15551234567", "Body": "hello"}
        sig = _compute_twilio_signature("wrong_token", url, params)
        assert adapter._validate_twilio_signature(url, params, sig) is False

    def test_params_sorted_by_key(self):
        """Signature must be computed with params sorted alphabetically."""
        adapter = self._make_adapter()
        url = "https://example.com/webhooks/twilio"
        params = {"Zebra": "last", "Alpha": "first", "Middle": "mid"}
        sig = _compute_twilio_signature("test_token_secret", url, params)
        assert adapter._validate_twilio_signature(url, params, sig) is True

    def test_empty_param_values_included(self):
        """Blank values must be included in signature computation."""
        adapter = self._make_adapter()
        url = "https://example.com/webhooks/twilio"
        params = {"From": "+15551234567", "Body": "", "SmsStatus": "received"}
        sig = _compute_twilio_signature("test_token_secret", url, params)
        assert adapter._validate_twilio_signature(url, params, sig) is True

    def test_url_matters(self):
        """Different URLs produce different signatures."""
        adapter = self._make_adapter()
        params = {"Body": "hello"}
        sig = _compute_twilio_signature(
            "test_token_secret", "https://a.com/webhooks/twilio", params
        )
        assert adapter._validate_twilio_signature(
            "https://b.com/webhooks/twilio", params, sig
        ) is False

    def test_port_variant_443_matches_without_port(self):
        """Signature for https URL with :443 validates against URL without port."""
        adapter = self._make_adapter()
        params = {"From": "+15551234567", "Body": "hello"}
        sig = _compute_twilio_signature(
            "test_token_secret", "https://example.com:443/webhooks/twilio", params
        )
        assert adapter._validate_twilio_signature(
            "https://example.com/webhooks/twilio", params, sig
        ) is True

    def test_port_variant_without_port_matches_443(self):
        """Signature for https URL without port validates against URL with :443."""
        adapter = self._make_adapter()
        params = {"From": "+15551234567", "Body": "hello"}
        sig = _compute_twilio_signature(
            "test_token_secret", "https://example.com/webhooks/twilio", params
        )
        assert adapter._validate_twilio_signature(
            "https://example.com:443/webhooks/twilio", params, sig
        ) is True

    def test_non_standard_port_no_variant(self):
        """Non-standard port must NOT match URL without port."""
        adapter = self._make_adapter()
        params = {"From": "+15551234567", "Body": "hello"}
        sig = _compute_twilio_signature(
            "test_token_secret", "https://example.com/webhooks/twilio", params
        )
        assert adapter._validate_twilio_signature(
            "https://example.com:8080/webhooks/twilio", params, sig
        ) is False

    def test_port_variant_http_80(self):
        """Port variant also works for http with port 80."""
        adapter = self._make_adapter()
        params = {"From": "+15551234567", "Body": "hello"}
        sig = _compute_twilio_signature(
            "test_token_secret", "http://example.com:80/webhooks/twilio", params
        )
        assert adapter._validate_twilio_signature(
            "http://example.com/webhooks/twilio", params, sig
        ) is True


# ── Webhook signature enforcement (handler-level) ──────────────────

class TestWebhookSignatureEnforcement:
    """Integration tests for signature validation in _handle_webhook."""

    def _make_adapter(self, webhook_url=""):
        from plugins.platforms.sms.adapter import SmsAdapter

        env = {
            "TWILIO_ACCOUNT_SID": "ACtest",
            "TWILIO_AUTH_TOKEN": "test_token_secret",
            "TWILIO_PHONE_NUMBER": "+15550001111",
            "SMS_WEBHOOK_URL": webhook_url,
        }
        with patch.dict(os.environ, env):
            pc = PlatformConfig(enabled=True, api_key="test_token_secret")
            adapter = SmsAdapter(pc)
        adapter._message_handler = AsyncMock()
        return adapter

    def _mock_request(self, body, headers=None, content_length=None):
        request = MagicMock()
        request.read = AsyncMock(return_value=body)
        request.headers = headers or {}
        request.content_length = content_length
        return request

    @pytest.mark.asyncio
    async def test_insecure_flag_skips_validation(self):
        """With SMS_INSECURE_NO_SIGNATURE=true and no URL, requests are accepted."""
        env = {"SMS_INSECURE_NO_SIGNATURE": "true"}
        with patch.dict(os.environ, env):
            adapter = self._make_adapter(webhook_url="")
        body = b"From=%2B15551234567&To=%2B15550001111&Body=hello&MessageSid=SM123"
        request = self._mock_request(body)
        resp = await adapter._handle_webhook(request)
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_insecure_flag_with_url_still_validates(self):
        """When both SMS_WEBHOOK_URL and SMS_INSECURE_NO_SIGNATURE are set,
        validation stays active (URL takes precedence)."""
        adapter = self._make_adapter(webhook_url="https://example.com/webhooks/twilio")
        body = b"From=%2B15551234567&To=%2B15550001111&Body=hello&MessageSid=SM123"
        request = self._mock_request(body, headers={})
        resp = await adapter._handle_webhook(request)
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_missing_signature_returns_403(self):
        adapter = self._make_adapter(webhook_url="https://example.com/webhooks/twilio")
        body = b"From=%2B15551234567&To=%2B15550001111&Body=hello&MessageSid=SM123"
        request = self._mock_request(body, headers={})
        resp = await adapter._handle_webhook(request)
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_invalid_signature_returns_403(self):
        adapter = self._make_adapter(webhook_url="https://example.com/webhooks/twilio")
        body = b"From=%2B15551234567&To=%2B15550001111&Body=hello&MessageSid=SM123"
        request = self._mock_request(body, headers={"X-Twilio-Signature": "invalid"})
        resp = await adapter._handle_webhook(request)
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_valid_signature_returns_200(self):
        webhook_url = "https://example.com/webhooks/twilio"
        adapter = self._make_adapter(webhook_url=webhook_url)
        params = {
            "From": "+15551234567",
            "To": "+15550001111",
            "Body": "hello",
            "MessageSid": "SM123",
        }
        sig = _compute_twilio_signature("test_token_secret", webhook_url, params)
        body = b"From=%2B15551234567&To=%2B15550001111&Body=hello&MessageSid=SM123"
        request = self._mock_request(body, headers={"X-Twilio-Signature": sig})
        resp = await adapter._handle_webhook(request)
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_port_variant_signature_returns_200(self):
        """Signature computed with :443 should pass when URL configured without port."""
        webhook_url = "https://example.com/webhooks/twilio"
        adapter = self._make_adapter(webhook_url=webhook_url)
        params = {
            "From": "+15551234567",
            "To": "+15550001111",
            "Body": "hello",
            "MessageSid": "SM123",
        }
        sig = _compute_twilio_signature(
            "test_token_secret", "https://example.com:443/webhooks/twilio", params
        )
        body = b"From=%2B15551234567&To=%2B15550001111&Body=hello&MessageSid=SM123"
        request = self._mock_request(body, headers={"X-Twilio-Signature": sig})
        resp = await adapter._handle_webhook(request)
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_webhook_rejects_oversized_body_via_content_length(self):
        """POST with Content-Length exceeding 64 KiB returns 413 before reading."""
        adapter = self._make_adapter(webhook_url="")
        body = b"From=%2B15551234567&To=%2B15550001111&Body=hello&MessageSid=SM123"
        request = self._mock_request(body, content_length=65_537)
        resp = await adapter._handle_webhook(request)
        assert resp.status == 413
        # request.read must NOT have been called — we bailed on Content-Length
        request.read.assert_not_called()

    @pytest.mark.asyncio
    async def test_webhook_rejects_oversized_body_via_read_length(self):
        """POST whose actual read size exceeds 64 KiB returns 413.

        Covers the case where Content-Length is absent (chunked transfer) but
        the body still exceeds the cap.
        """
        adapter = self._make_adapter(webhook_url="")
        oversized = b"x" * 65_537
        request = self._mock_request(oversized, content_length=None)
        resp = await adapter._handle_webhook(request)
        assert resp.status == 413


# ── Out-of-process cron delivery (_standalone_send) ──────────────────

class TestSmsStandaloneSendMarkdown:
    """Guards the markdown strip on the out-of-process SMS send path.

    ``tools/send_message_tool.py`` reaches this path via the platform
    registry (``_registry_standalone_send("sms", ...)``) for cron and
    other out-of-process delivery.  It had NO test coverage, which is why
    a missing ``import re`` sat here undetected: the strip helper raised
    ``NameError`` on its first substitution, unconditionally, for every
    outbound message — not just markdown-bearing ones.

    The adapter class path already used the shared
    ``gateway.platforms.helpers.strip_markdown``; only this module-level
    duplicate had drifted.
    """

    def _strip(self, text: str) -> str:
        from plugins.platforms.sms import adapter

        return adapter.strip_markdown(text)

    def test_strip_is_callable_and_does_not_raise(self):
        """Regression: this raised NameError: name 're' is not defined."""
        assert self._strip("**bold** and *italic*") == "bold and italic"

    def test_strip_removes_common_markdown(self):
        assert self._strip("# Heading\ntext") == "Heading\ntext"
        assert self._strip("`code`") == "code"
        assert self._strip("[label](https://example.com)") == "label"

    def test_snake_case_identifiers_survive(self):
        """The shared helper's word-boundary guards keep identifiers intact.

        The module-level duplicate this replaced used bare ``_(.+?)_`` with
        no boundary guards, so it collapsed ``snake_case_names`` to
        ``snakecasenames`` — real corruption for any SMS discussing code.
        Measured, both ways, before this test was written.

        Pinned so a future "simplification" back to the naive pattern fails
        here rather than silently in outbound messages.
        """
        assert self._strip("use snake_case_names") == "use snake_case_names"
        assert self._strip("my_var and other_var") == "my_var and other_var"

    def test_fenced_code_language_is_stripped(self):
        """The duplicate's ``[a-z]*`` fence charset left residue behind.

        ```` ```Python ```` kept the word "Python" in the message body and
        ```` ```c++ ```` left "++"; the shared helper's
        ``[a-zA-Z0-9_+-]*`` handles both.
        """
        assert self._strip("```Python\ncode\n```") == "code"
        assert self._strip("```c++\nx\n```") == "x"

    def test_dunder_is_still_flattened_known_limitation(self):
        """``__init__`` → ``init`` in BOTH implementations.

        Not a regression introduced here and not fixed here: it is
        pre-existing ``strip_markdown`` behaviour shared with every other
        plain-text platform (iMessage, Feishu).  Pinned so the limitation
        is visible and any future fix is a deliberate, cross-platform
        change rather than an accident.
        """
        assert self._strip("call __init__ first") == "call init first"


# ── Out-of-process delivery, end to end ──────────────────────────────

class _RecordingFormData:
    """Stand-in for aiohttp.FormData that records the fields Twilio would get."""

    def __init__(self):
        self.fields = {}

    def add_field(self, name, value, **kwargs):
        self.fields[name] = value


class _FakeResponse:
    status = 201

    async def json(self):
        return {"sid": "SM_fake_123"}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    last_data = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def post(self, url, data=None, headers=None, **kwargs):
        _FakeSession.last_data = data
        return _FakeResponse()


class TestSmsStandaloneSendDelivery:
    """Drives ``_standalone_send`` itself rather than the strip helper alone.

    ``TestSmsStandaloneSendMarkdown`` above pins what the shared helper does
    to a string.  These two pin that the send path actually *calls* it and
    that the result reaches Twilio, which is the part that was broken: the
    strip call sits outside ``_standalone_send``'s ``try/except``, so the
    ``NameError`` aborted the send before any HTTP request was issued.
    """

    def test_standalone_strip_matches_in_process_format_message(self):
        """Standalone and in-process SMS paths must render identically.

        The out-of-process duplicate drifting away from the adapter class's
        shared helper is the original defect; this is the invariant that
        would have caught it without knowing the helper was missing ``re``.
        """
        from plugins.platforms.sms.adapter import SmsAdapter, strip_markdown

        adapter = object.__new__(SmsAdapter)
        adapter.config = PlatformConfig(enabled=True, api_key="tok")
        adapter._platform = Platform.SMS
        sample = "**b** _i_ `c`\n# H\n[l](https://e.com)\nkeep_this_name"
        assert strip_markdown(sample) == adapter.format_message(sample)

    @pytest.mark.asyncio
    async def test_standalone_send_strips_markdown_in_twilio_body(self):
        """The Body posted to Twilio is stripped — and no NameError escapes."""
        import aiohttp

        from plugins.platforms.sms.adapter import _standalone_send

        env = {
            "TWILIO_ACCOUNT_SID": "ACtest123",
            "TWILIO_PHONE_NUMBER": "+15551234567",
        }
        _FakeSession.last_data = None
        with patch.dict(os.environ, env, clear=False), \
                patch.object(aiohttp, "ClientSession", _FakeSession), \
                patch.object(aiohttp, "FormData", _RecordingFormData), \
                patch.object(aiohttp, "ClientTimeout", lambda **kw: None):
            result = await _standalone_send(
                PlatformConfig(enabled=True, api_key="token_abc"),
                "+15559876543",
                "**urgent** see `logs`",
            )

        assert result.get("success") is True, result
        assert _FakeSession.last_data is not None, "Twilio POST was never issued"
        assert _FakeSession.last_data.fields["Body"] == "urgent see logs"
