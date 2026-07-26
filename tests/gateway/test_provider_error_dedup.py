"""Tests for provider-error dedup between status and final response (issue #72131)."""

import pytest

from gateway.run import (
    _record_provider_error_status,
    _sanitize_gateway_final_response,
    _should_dedup_final_response,
    _last_sent_provider_error_status,
    _deduped_provider_error,
)


class TestProviderErrorDedup:
    """Verify that _sanitize_gateway_final_response suppresses duplicates
    when the same provider-error text was already sent as a status message
    via the plain-send fallback (adapter without send_or_update_status).
    """

    def setup_method(self):
        """Reset module-level dedup state before each test."""
        import gateway.run as _run
        _run._last_sent_provider_error_status = None
        _run._deduped_provider_error = False

    # -- recording --

    def test_record_sets_tracking(self):
        _record_provider_error_status("⏱️ rate limited")
        assert _should_dedup_final_response("⏱️ rate limited") is True
        assert _should_dedup_final_response("something else") is False

    def test_record_resets_dedup_flag(self):
        import gateway.run as _run
        _run._deduped_provider_error = True
        _record_provider_error_status("⏱️ rate limited")
        assert _run._deduped_provider_error is False

    # -- dedup logic --

    def test_no_prior_status_no_dedup(self):
        """Without a prior status, no dedup should fire."""
        assert _should_dedup_final_response("anything") is False

    def test_identical_text_dedups(self):
        _record_provider_error_status(
            "⏱️ The model provider is rate-limiting requests. "
            "Please wait a moment and try again."
        )
        assert _should_dedup_final_response(
            "⏱️ The model provider is rate-limiting requests. "
            "Please wait a moment and try again."
        ) is True

    def test_different_text_no_dedup(self):
        _record_provider_error_status("⏱️ rate limited")
        assert _should_dedup_final_response("⚠️ auth failed") is False

    # -- sanitize dedup --

    @pytest.mark.parametrize("platform", ["whatsapp", "discord", "slack", "signal", "matrix"])
    def test_sanitize_dedups_identical_provider_error(self, platform):
        """When the same provider-error text was sent as a status, the final
        response should be empty (deduped)."""
        raw = "API call failed after 3 retries: HTTP 429 rate limited"

        # Simulate: status was sent via plain-send fallback
        _record_provider_error_status(
            "⏱️ The model provider is rate-limiting requests. "
            "Please wait a moment and try again."
        )

        # Now the final response comes in — should be deduped
        result = _sanitize_gateway_final_response(platform, raw)
        assert result == ""

        import gateway.run as _run
        assert _run._deduped_provider_error is True

    @pytest.mark.parametrize("platform", ["whatsapp", "discord", "slack", "signal", "matrix"])
    def test_sanitize_passes_when_no_prior_status(self, platform):
        """Without a prior status, provider errors should be sanitized normally."""
        raw = "API call failed after 3 retries: HTTP 429 rate limited"

        result = _sanitize_gateway_final_response(platform, raw)
        assert result == (
            "⏱️ The model provider is rate-limiting requests. "
            "Please wait a moment and try again."
        )

    @pytest.mark.parametrize("platform", ["whatsapp", "discord", "slack", "signal", "matrix"])
    def test_sanitize_passes_when_text_differs(self, platform):
        """If the final response text differs from the status, pass it through."""
        raw = "API call failed after 3 retries: HTTP 401 unauthorized"

        # A different error was sent as status
        _record_provider_error_status(
            "⏱️ The model provider is rate-limiting requests. "
            "Please wait a moment and try again."
        )

        result = _sanitize_gateway_final_response(platform, raw)
        # Auth error produces different text
        assert "authentication failed" in result.lower() or "provider" in result.lower()

    def test_local_platform_not_deduped(self):
        """Local/CLI platforms pass raw text unchanged — no dedup needed."""
        raw = "API call failed after 3 retries: HTTP 429 rate limited"

        _record_provider_error_status(
            "⏱️ The model provider is rate-limiting requests. "
            "Please wait a moment and try again."
        )

        result = _sanitize_gateway_final_response("local", raw)
        # Local platforms keep raw text
        assert "429" in result
        assert "rate" in result.lower()
