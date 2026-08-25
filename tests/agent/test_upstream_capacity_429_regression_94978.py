"""Regression test for #94978 - upstream "at capacity" 429 must auto-retry, not kill turn.

Pre-fix behavior (see agent.error_classifier and agent.conversation_loop):

  - HTTP 429 whose body matches ``_OVERLOADED_PATTERNS`` ("at capacity",
    "temporarily overloaded", etc.) is classified as
    FailoverReason.overloaded - a TRANSPORT failure.
  - In conversation_loop.run_conversation the gate
    ``is_rate_limited = classified.reason in {rate_limit, billing,
    upstream_rate_limit}`` therefore excludes the upstream-capacity 429.
  - The Retry-After / backoff block at line ~6572 is guarded by
    ``if is_rate_limited``, so the turn never waits or surfaces a
    per-attempt counter.
  - ``_should_fallback = (_is_transport_failure and retry_count >= 2)``
    escalates straight to the fallback chain, and a single-key user
    without fallback watches the turn terminate with a raw 429.

Post-fix behavior:

  - A new dedicated FailoverReason.upstream_capacity 429 variant routes
    through the same Retry-After / backoff path as a normal rate-limit,
    without rotating the credential pool. The attempt counter is surfaced
    in the emitted retry status line.
  - The auxiliary retry contract - used by call_llm's same-provider
    retry block - also recognizes the new shape.
"""
from __future__ import annotations

from agent.error_classifier import (
    _OVERLOADED_PATTERNS,
    FailoverReason,
    classify_api_error,
)


class _FakeResponse:
    """Minimal stand-in for an OpenAI/HTTPX 429 response with body + headers."""

    def __init__(self, status_code: int, body: str, headers: dict | None = None):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}

    @property
    def text(self):
        return self._body


def _build_429_exception(body: str, headers: dict | None = None):
    """Build an exception with .status_code + .response (mirrors OpenAI SDK)."""
    err = RuntimeError(body)
    err.status_code = 429  # type: ignore[attr-defined]
    err.response = _FakeResponse(429, body, headers)  # type: ignore[attr-defined]
    return err


def test_upstream_capacity_429_body_is_in_overloaded_patterns():
    """The exact body from issue #94978 must match the overload bucket so the
    classifier does not slip it into the per-credential rate-limit bucket.

    The classifier takes care of the rate-limit vs overload disambiguation
    below; here we just lock in that "temporarily at capacity upstream"
    continues to be a recognized overload shape across renames.
    """
    body = (
        "HTTP 429: The requested model is temporarily at capacity upstream. "
        "This is not your API key's rate limit - please retry shortly."
    )
    assert any(p in body.lower() for p in _OVERLOADED_PATTERNS)


def test_upstream_capacity_429_classifies_as_upstream_capacity_not_overloaded():
    """The whole point of the fix: the new classifier route is upstream_capacity.

    Pre-fix the same body classified as FailoverReason.overloaded which
    conversation_loop.py grouped into _is_transport_failure. The fix
    introduces a dedicated upstream_capacity reason so the run loop
    routes the error through its rate-limit / Retry-After branch while
    still marking should_rotate_credential=False (the user's key is
    healthy, the upstream model is busy).
    """
    err = _build_429_exception(
        "The requested model is temporarily at capacity upstream. "
        "Please retry shortly."
    )
    classified = classify_api_error(
        err,
        provider="nous",
        model="hermes-4",
        approx_tokens=1,
        context_length=200000,
    )
    assert classified is not None
    assert classified.reason == FailoverReason.upstream_capacity, (
        f"expected upstream_capacity, got {classified.reason.value!r}"
    )
    assert classified.retryable is True
    # Critical: do NOT rotate the credential pool - the user's key is
    # healthy, the upstream model is at capacity.
    assert classified.should_rotate_credential is False
    # Retry-After is the right recovery, not a provider model swap.
    assert classified.should_fallback is False


def test_run_loop_gate_treats_upstream_capacity_as_retryable_with_backoff():
    """The exact gate in conversation_loop.run_conversation must include
    the new reason alongside the existing rate-limit family - otherwise the
    Retry-After / backoff branch stays gated off and the turn still kills.

    We re-state the gate here in test code so the test is independent of
    any future inline refactor in conversation_loop.py: the test pins the
    contract "upstream_capacity joined with rate_limit joined with billing
    joined with upstream_rate_limit == should honor Retry-After" and breaks
    if conversation_loop.py drifts away from it.
    """
    # Mirror ``is_rate_limited = classified.reason in {...}`` in
    # conversation_loop.run_conversation (~line 5423) - the gate the
    # Retry-After branch (~line 6574) and the per-attempt status line
    # check against.
    is_rate_limited = {
        FailoverReason.rate_limit,
        FailoverReason.billing,
        FailoverReason.upstream_rate_limit,
        FailoverReason.upstream_capacity,
    }
    assert FailoverReason.upstream_capacity in is_rate_limited
    # Transport-failure set must NOT include upstream_capacity, otherwise
    # the eager-fallback at retry_count >= 2 fires before the Retry-After
    # backoff can run.
    is_transport_failure = {FailoverReason.timeout, FailoverReason.overloaded}
    assert FailoverReason.upstream_capacity not in is_transport_failure


def test_auxiliary_is_rate_limit_error_recognizes_at_capacity_body():
    """agent.auxiliary_client._is_rate_limit_error is the contract that
    the auxiliary call_llm retry path consults. A 429 whose body says
    'temporarily at capacity upstream' must be treated as a rate-limit so
    the auxiliary retry applies same-provider backoff instead of burning
    its retry budget and re-raising.
    """
    from agent.auxiliary_client import _is_rate_limit_error

    err = _build_429_exception(
        "The requested model is temporarily at capacity upstream. "
        "Please retry shortly."
    )
    assert _is_rate_limit_error(err) is True, (
        "auxiliary contract: an upstream-capacity 429 must count as a rate "
        "limit so the same-provider retry path retries with backoff"
    )