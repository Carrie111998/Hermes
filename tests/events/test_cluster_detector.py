"""Tests for events/cluster_detector.py.

Two layers:
  - classify_failure_type(error_text) — regex-based classifier (pure)
  - FailureClusterDetector — sliding window per source with file-backed state
"""

import json
from pathlib import Path

import pytest


# --------------------------------------------------------------------------
# classify_failure_type
# --------------------------------------------------------------------------

class TestClassifyFailureType:
    @pytest.fixture
    def classify(self):
        from events.cluster_detector import classify_failure_type
        return classify_failure_type

    def test_empty_string_returns_unknown(self, classify):
        assert classify("") == "unknown"

    def test_none_returns_unknown(self, classify):
        assert classify(None) == "unknown"

    def test_captcha_in_message(self, classify):
        assert classify("Bailing: CAPTCHA detected on login page") == "captcha"

    def test_timeout_word(self, classify):
        assert classify("Request timed out after 30s") == "timeout"
        assert classify("operation timeout exceeded") == "timeout"

    def test_auth_codes_and_words(self, classify):
        assert classify("HTTP 401 Unauthorized") == "auth"
        assert classify("403 Forbidden") == "auth"
        assert classify("authentication failed") == "auth"

    def test_rate_limit(self, classify):
        assert classify("HTTP 429 Too Many Requests") == "rate_limit"
        assert classify("rate limit exceeded; retry after 60s") == "rate_limit"

    def test_network(self, classify):
        assert classify("ECONNRESET") == "network"
        assert classify("network connection refused") == "network"

    def test_parse(self, classify):
        assert classify("json.decoder.JSONDecodeError: Expecting value") == "parse"
        assert classify("failed to parse response") == "parse"

    def test_model_error(self, classify):
        assert classify("Anthropic API error: model overloaded") == "model_error"
        assert classify("OpenAI returned 500 internal server error") == "model_error"

    def test_model_error_bidirectional(self, classify):
        # Vendor-then-error (forward) and error-then-vendor (reverse) both match
        assert classify("Error from anthropic API") == "model_error"
        assert classify("got error: openai timeout") == "model_error"

    def test_unrecognized_falls_through(self, classify):
        assert classify("file not found: /tmp/foo") == "unknown"

    def test_case_insensitive(self, classify):
        assert classify("CAPTCHA") == "captcha"
        assert classify("Timeout") == "timeout"

    def test_first_match_wins_ordering(self, classify):
        # If both "timeout" and "captcha" appear, captcha should win
        # (more specific signal). Ordering documented in classifier.
        assert classify("captcha challenge timeout") == "captcha"


# --------------------------------------------------------------------------
# FailureClusterDetector
# --------------------------------------------------------------------------

class TestFailureClusterDetector:
    @pytest.fixture
    def detector(self, tmp_path):
        from events.cluster_detector import FailureClusterDetector
        return FailureClusterDetector(state_path=tmp_path / "state.json")

    def test_first_failure_returns_none(self, detector):
        assert detector.record("scout", success=False, error_text="timeout") is None

    def test_two_same_type_failures_return_none(self, detector):
        assert detector.record("scout", success=False, error_text="timeout") is None
        assert detector.record("scout", success=False, error_text="timeout") is None

    def test_three_same_type_returns_cluster(self, detector):
        detector.record("scout", success=False, error_text="timeout")
        detector.record("scout", success=False, error_text="timed out")
        result = detector.record("scout", success=False, error_text="timeout")
        assert result is not None
        assert result.source == "scout"
        assert result.failure_type == "timeout"
        assert result.count == 3

    def test_cluster_preserves_latest_structured_details(self, detector):
        first = {
            "error_code": "OLD_CODE",
            "phase": "startup",
            "deadline_seconds": 60,
            "exception_type": "ConnectionError",
        }
        latest = {
            "error_code": "PG_CONNECT_REFUSED",
            "phase": "postgres_sync",
            "deadline_seconds": 1800,
            "exception_type": "OperationalError",
        }
        detector.record(
            "tracker", success=False, error_text="connection refused",
            details=first,
        )
        detector.record(
            "tracker", success=False, error_text="connection refused",
            details=first,
        )
        cluster = detector.record(
            "tracker", success=False, error_text="connection refused",
            details=latest,
        )

        assert cluster is not None
        assert cluster.count == 3
        assert cluster.last_details == latest

    def test_details_are_json_safe_and_unknown_fields_are_omitted(
        self, detector,
    ):
        details = {
            "error_code": "PG_CONNECT_REFUSED",
            "phase": "postgres_sync",
            "deadline_seconds": 1800,
            "exception_type": "OperationalError",
            "latest_cause": "connection refused",
            "unknown_field": "do not persist",
            "not_json_safe": object(),
        }
        for _ in range(2):
            detector.record(
                "tracker", success=False, error_text="connection refused",
                details=details,
            )
        cluster = detector.record(
            "tracker", success=False, error_text="connection refused",
            details=details,
        )

        assert cluster is not None
        assert cluster.last_details == {
            "error_code": "PG_CONNECT_REFUSED",
            "phase": "postgres_sync",
            "deadline_seconds": 1800,
            "exception_type": "OperationalError",
            "latest_cause": "connection refused",
        }
        json.loads(detector.state_path.read_text(encoding="utf-8"))

    def test_nested_or_wrong_type_details_are_omitted(self, detector):
        details = {
            "error_code": {"token": "sk-testabcdefghijklmnop"},
            "phase": ["postgres_sync"],
            "deadline_seconds": [1800],
            "exception_type": {"name": "OperationalError"},
            "latest_cause": {"password": "super-secret-value"},
        }
        for _ in range(2):
            detector.record(
                "tracker", success=False, error_text="connection refused",
                details=details,
            )
        cluster = detector.record(
            "tracker", success=False, error_text="connection refused",
            details=details,
        )

        assert cluster is not None
        assert cluster.last_details == {}
        persisted = detector.state_path.read_text(encoding="utf-8")
        assert "sk-testabcdefghijklmnop" not in persisted
        assert "super-secret-value" not in persisted

    def test_three_different_types_returns_none(self, detector):
        detector.record("scout", success=False, error_text="timeout")
        detector.record("scout", success=False, error_text="captcha")
        result = detector.record("scout", success=False, error_text="HTTP 401")
        assert result is None

    def test_success_clears_window(self, detector):
        detector.record("scout", success=False, error_text="timeout")
        detector.record("scout", success=False, error_text="timeout")
        detector.record("scout", success=True)
        detector.record("scout", success=False, error_text="timeout")
        result = detector.record("scout", success=False, error_text="timeout")
        assert result is None  # only 2 in the new window

    def test_per_source_isolation(self, detector):
        detector.record("scout", success=False, error_text="timeout")
        detector.record("matcher", success=False, error_text="timeout")
        detector.record("scout", success=False, error_text="timeout")
        result = detector.record("scout", success=False, error_text="timeout")
        assert result is not None
        assert result.source == "scout"

    def test_state_persists_across_instances(self, tmp_path):
        from events.cluster_detector import FailureClusterDetector
        path = tmp_path / "state.json"
        d1 = FailureClusterDetector(state_path=path)
        d1.record("scout", success=False, error_text="timeout")
        d1.record("scout", success=False, error_text="timeout")
        d2 = FailureClusterDetector(state_path=path)
        result = d2.record("scout", success=False, error_text="timeout")
        assert result is not None
        assert result.failure_type == "timeout"

    def test_window_caps_at_window_size(self, detector):
        for _ in range(10):
            detector.record("scout", success=False, error_text="timeout")
        with open(detector.state_path) as f:
            state = json.load(f)
        assert len(state["scout"]) == detector.window_size

    def test_cluster_uses_last_threshold_entries(self, detector):
        # Drift from captcha → timeout: the 3 most recent (timeouts) cluster
        detector.record("scout", success=False, error_text="captcha")
        detector.record("scout", success=False, error_text="timeout")
        detector.record("scout", success=False, error_text="timeout")
        result = detector.record("scout", success=False, error_text="timeout")
        assert result is not None
        assert result.failure_type == "timeout"

    def test_malformed_state_file_resets_silently(self, tmp_path):
        from events.cluster_detector import FailureClusterDetector
        path = tmp_path / "state.json"
        path.write_text("not json")
        d = FailureClusterDetector(state_path=path)
        # First record after reset should not crash and should behave like
        # a fresh state.
        assert d.record("scout", success=False, error_text="timeout") is None

    def test_first_seen_is_oldest_in_cluster_window(self, detector):
        first = detector.record("scout", success=False, error_text="timeout")
        assert first is None
        detector.record("scout", success=False, error_text="timeout")
        result = detector.record("scout", success=False, error_text="timeout")
        # first_seen should be the timestamp of the FIRST of the three
        # clustered entries — verify by checking it's <= last_seen
        assert result.first_seen <= result.last_seen
