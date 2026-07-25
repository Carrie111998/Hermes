"""Provider 500/502 must be able to activate a configured fallback chain.

Regression for a live incident (2026-07-25): an upstream provider outage
returned a mix of 503 and HTTP 500. The 503s could fail over, but
``FailoverReason.server_error`` was absent from the transport-failure set, so
every 500 retried against the same dead provider until the turn's retries
exhausted and the worker crashed — 144 of that outage's errors were 500s.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]




def _transport_reasons() -> set[str]:
    """Extract the transport-failure reason set from the loop's source.

    conversation_loop imports the world, so parse the predicate rather than
    importing it — the set membership is the whole contract under test.
    """
    src = (ROOT / "agent/conversation_loop.py").read_text(encoding="utf-8")
    marker = "_is_transport_failure = classified.reason in {"
    start = src.index(marker) + len(marker)
    body = src[start : src.index("}", start)]
    return {
        line.strip().rstrip(",").split(".")[-1]
        for line in body.splitlines()
        if line.strip().startswith("FailoverReason.")
    }


def test_server_error_can_trigger_fallback():
    assert "server_error" in _transport_reasons(), (
        "HTTP 500/502 must be able to activate the fallback chain; without it a "
        "provider outage kills the turn even with a healthy chain configured"
    )


def test_overloaded_and_timeout_remain_covered():
    reasons = _transport_reasons()
    assert {"timeout", "overloaded"} <= reasons


def test_rate_limit_is_not_a_transport_failure():
    """Rate limits fail over EAGERLY (no retry gate) — keep the sets disjoint."""
    reasons = _transport_reasons()
    assert "rate_limit" not in reasons
    assert "billing" not in reasons
    assert "upstream_rate_limit" not in reasons


def test_server_error_is_still_classified_retryable():
    """The retry gate is what preserves transient-blip recovery on the primary.

    Source-checked: error_classifier pulls in the agent package at import time,
    so assert on the classification site rather than importing it.
    """
    src = (ROOT / "agent/error_classifier.py").read_text(encoding="utf-8")
    assert "return result_fn(FailoverReason.server_error, retryable=True)" in src
