"""Regression tests for #86570: gateway provider error connection messaging."""

import pytest

from gateway.run import (
    _GATEWAY_CONNECTION_ERROR_RE,
    _gateway_provider_error_reply,
    _looks_like_gateway_provider_error,
)


class TestGatewayConnectionErrorReply:
    def test_connection_error_strings_produce_specific_reply(self):
        samples = [
            "openai.APIConnectionError",
            "httpx.ConnectError: connection refused",
            "ConnectionError: [WinError 10061] No connection could be made",
            "Errno 111 Connection refused",
            "All connection attempts failed: Connection refused",
        ]
        for text in samples:
            assert _looks_like_gateway_provider_error(text), text
            reply = _gateway_provider_error_reply(text)
            assert "not responding" in reply.lower(), text
            assert "not running or is unreachable" in reply, text

    def test_broad_connection_phrases_still_map_once_classified(self):
        """Reply selector keeps the full phrase set; the gate does not."""
        for text in (
            "cannot connect to http://127.0.0.1:8033/v1",
            "failed to establish a new connection",
        ):
            reply = _gateway_provider_error_reply(text)
            assert "not running or is unreachable" in reply, text

    def test_prose_cannot_connect_is_not_a_provider_error(self):
        text = (
            "cannot connect to the office VPN from this cafe, "
            "so I used the backup notes instead"
        )
        assert not _looks_like_gateway_provider_error(text)

    def test_other_errors_keep_generic_reply(self):
        for text in (
            "RuntimeError: model returned empty content",
            "Exception: unknown provider",
            "HTTP 500 internal server error",
        ):
            if _looks_like_gateway_provider_error(text):
                reply = _gateway_provider_error_reply(text)
                assert "not running or is unreachable" not in reply, text

    def test_connection_regex_does_not_match_non_connection_error(self):
        assert not _GATEWAY_CONNECTION_ERROR_RE.search("Rate limited after 3 retries")
        assert not _GATEWAY_CONNECTION_ERROR_RE.search("Provider authentication failed")

    def test_auth_and_rate_limit_preserved(self):
        assert "authentication" in _gateway_provider_error_reply(
            "provider authentication failed"
        ).lower()
        assert "rate-limiting" in _gateway_provider_error_reply(
            "rate limited after 3 retries"
        ).lower()


class TestQuotaMislabelReply:
    """Quota exhaustion must not reach chat as an auth failure (#89401).

    The resolution layer wraps the quota AuthError inside a RuntimeError and
    the caller prefixes "Provider authentication failed:" — which the reply
    classifier then matched, sending operators to re-authenticate valid
    credentials. format_resolution_failure_reply distinguishes them at the
    source; the classifier then routes the quota wording to the rate-limit
    reply instead of the auth reply."""

    def _quota_runtime_error(self):
        from hermes_cli.auth import AuthError, CODEX_RATE_LIMITED_CODE

        inner = AuthError(
            "Codex provider quota exhausted (429); retry after 116168s. "
            "Credentials are still valid.",
            provider="openai-codex",
            code=CODEX_RATE_LIMITED_CODE,
            relogin_required=False,
        )
        wrapped = RuntimeError(str(inner))
        wrapped.__cause__ = inner
        return wrapped

    def test_quota_resolution_failure_is_not_auth(self):
        from hermes_cli.auth import format_resolution_failure_reply

        reply = format_resolution_failure_reply(self._quota_runtime_error())
        assert "quota" in reply.lower()
        assert "credentials are still valid" in reply
        assert "authentication" not in reply.lower()

    def test_genuine_auth_failure_keeps_auth_reply(self):
        from hermes_cli.auth import format_resolution_failure_reply

        reply = format_resolution_failure_reply(RuntimeError("invalid api key"))
        assert "authentication" in reply.lower()

    def test_quota_reply_classifies_as_rate_limit_not_auth(self):
        # End to end: the quota-worded resolution reply must fall through the
        # classifier to the rate-limit bucket, never the auth bucket.
        from hermes_cli.auth import format_resolution_failure_reply

        reply = format_resolution_failure_reply(self._quota_runtime_error())
        classified = _gateway_provider_error_reply(reply)
        assert "rate-limiting" in classified.lower()
        assert "authentication" not in classified.lower()
