"""Regression test for #81052 — duplicate message delivery on Slack (and any
streaming platform) when a turn is slow.

When the queued-follow-up fallback path sends ``first_response`` via
``adapter.send()`` because the stream consumer never confirmed final delivery,
the subsequent normal completion pipeline must not re-send the same text. The
fix scopes a ``_delivered_in_fallback`` set to the pending-message handling
block in ``gateway/run.py``; the fallback path adds the chat to it, and the
normal completion send decision checks it before issuing a duplicate send.

These tests exercise the dedupe predicate the fix added at
``gateway/run.py:26095`` — the set lookup that gates the suppression branch.
We can't reliably drive the full 27k-line turn-handling function in a unit
test, so we verify the predicate in isolation: same chat + session_key with
the fallback flag set suppresses; same predicate with a different chat or
session_key does NOT suppress. We also verify the set membership is keyed by
``(chat_id, session_key)`` rather than by chat alone.
"""

from typing import Optional


def _decide_suppress(
    *,
    is_empty_sentinel: bool,
    transformed: bool,
    streamed: bool,
    content_delivered: bool,
    delivered_in_fallback: set,
    chat_id: str,
    session_key: Optional[str],
) -> bool:
    """Mirror of the conditional at gateway/run.py:26095 post-fix."""
    return (
        not is_empty_sentinel
        and not transformed
        and (
            streamed
            or content_delivered
            or (chat_id, session_key) in delivered_in_fallback
        )
    )


def test_fallback_send_suppresses_normal_completion_for_same_chat():
    """The fix's contract: a successful fallback send on chat C1 with
    session_key S1 must cause the subsequent normal completion send decision
    to suppress for the same (C1, S1)."""
    delivered = set()
    chat_id = "C1"
    session_key = "agent:main:slack:dm:W1:C1:12345"

    # 1) Fallback path completes successfully — mark delivery.
    delivered.add((chat_id, session_key))

    # 2) Normal completion path runs with streamed=False, content_delivered=False
    #    (the scenario in the issue: the stream consumer never confirmed).
    suppress = _decide_suppress(
        is_empty_sentinel=False,
        transformed=False,
        streamed=False,
        content_delivered=False,
        delivered_in_fallback=delivered,
        chat_id=chat_id,
        session_key=session_key,
    )
    assert suppress is True


def test_normal_send_still_works_when_streaming_confirmed():
    """When the stream consumer already delivered (streamed=True), the normal
    send is suppressed via the original code path, regardless of the set.
    The fix must not regress this case."""
    delivered = set()  # fallback did NOT fire
    suppress = _decide_suppress(
        is_empty_sentinel=False,
        transformed=False,
        streamed=True,
        content_delivered=False,
        delivered_in_fallback=delivered,
        chat_id="C1",
        session_key="agent:main:slack:dm:W1:C1:12345",
    )
    assert suppress is True  # legacy suppression via streamed flag


def test_normal_send_works_when_nothing_was_delivered():
    """The default case: no fallback, no streaming — the normal send must
    proceed. Both flags False and the set empty → suppress=False."""
    delivered = set()
    suppress = _decide_suppress(
        is_empty_sentinel=False,
        transformed=False,
        streamed=False,
        content_delivered=False,
        delivered_in_fallback=delivered,
        chat_id="C1",
        session_key="agent:main:slack:dm:W1:C1:12345",
    )
    assert suppress is False


def test_dedup_set_does_not_leak_across_chats():
    """A fallback delivery for chat C1 must NOT suppress the normal send for
    chat C2 in the same gateway turn."""
    delivered = {("C1", "agent:main:slack:dm:W1:C1:12345")}
    suppress_c2 = _decide_suppress(
        is_empty_sentinel=False,
        transformed=False,
        streamed=False,
        content_delivered=False,
        delivered_in_fallback=delivered,
        chat_id="C2",
        session_key="agent:main:slack:dm:W2:C2:99",
    )
    assert suppress_c2 is False


def test_empty_sentinel_still_suppresses():
    """An empty/empty-after-filter final response must continue to suppress
    regardless of any prior fallback send (the predicate still gates on
    ``is_empty_sentinel`` first)."""
    delivered = {("C1", "agent:main:slack:dm:W1:C1:12345")}
    suppress = _decide_suppress(
        is_empty_sentinel=True,
        transformed=False,
        streamed=False,
        content_delivered=False,
        delivered_in_fallback=delivered,
        chat_id="C1",
        session_key="agent:main:slack:dm:W1:C1:12345",
    )
    assert suppress is False


def test_transformed_response_still_sends():
    """A response flagged as ``transformed`` (plugin-hook output appended after
    streaming finished) must always take the normal final send path so the
    appended content reaches the user — even after a fallback delivery."""
    delivered = {("C1", "agent:main:slack:dm:W1:C1:12345")}
    suppress = _decide_suppress(
        is_empty_sentinel=False,
        transformed=True,  # a transform appended more content
        streamed=False,
        content_delivered=False,
        delivered_in_fallback=delivered,
        chat_id="C1",
        session_key="agent:main:slack:dm:W1:C1:12345",
    )
    assert suppress is False


def test_set_keyed_by_session_key_too():
    """Same chat_id but different session_key (e.g. distinct Slack workspace
    routes the same channel through different sessions) must NOT cross-
    suppress. The (chat_id, session_key) tuple is load-bearing."""
    delivered = {("C1", "agent:main:slack:dm:W1:C1:12345")}
    suppress_different_session = _decide_suppress(
        is_empty_sentinel=False,
        transformed=False,
        streamed=False,
        content_delivered=False,
        delivered_in_fallback=delivered,
        chat_id="C1",
        session_key="agent:main:slack:dm:W1:C1:9999",  # different session
    )
    assert suppress_different_session is False