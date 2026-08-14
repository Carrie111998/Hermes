"""Tests for the Discord interaction ACK + error discipline state machine."""

import pytest

from plugins.platforms.discord.interaction_ack import (
    ACK_WINDOW_SECONDS,
    InteractionAckError,
    InteractionLifecycle,
    InteractionState,
)


def test_ack_happy_path():
    lc = InteractionLifecycle(created_at=100.0)
    lc.ack(now=102.0)
    assert lc.state is InteractionState.ACKED


def test_ack_exactly_at_deadline_succeeds():
    lc = InteractionLifecycle(created_at=100.0)
    lc.ack(now=100.0 + ACK_WINDOW_SECONDS)
    assert lc.state is InteractionState.ACKED


def test_ack_after_deadline_marks_expired_and_raises():
    lc = InteractionLifecycle(created_at=100.0)
    with pytest.raises(InteractionAckError):
        lc.ack(now=100.0 + ACK_WINDOW_SECONDS + 0.000001)
    assert lc.state is InteractionState.EXPIRED


def test_ack_when_not_pending_raises():
    lc = InteractionLifecycle(created_at=100.0)
    lc.ack(now=101.0)
    with pytest.raises(InteractionAckError):
        lc.ack(now=101.5)
    assert lc.state is InteractionState.ACKED


def test_ack_error_is_value_error():
    lc = InteractionLifecycle(created_at=100.0)
    with pytest.raises(ValueError):
        lc.ack(now=104.0)


def test_defer_happy_path():
    lc = InteractionLifecycle(created_at=100.0)
    lc.defer(now=102.0)
    assert lc.state is InteractionState.DEFERRED


def test_defer_after_deadline_marks_expired_and_raises():
    lc = InteractionLifecycle(created_at=100.0)
    with pytest.raises(InteractionAckError):
        lc.defer(now=100.0 + ACK_WINDOW_SECONDS + 0.000001)
    assert lc.state is InteractionState.EXPIRED


def test_respond_once_from_acked():
    lc = InteractionLifecycle(created_at=100.0)
    lc.ack(now=101.0)
    lc.respond(now=102.0)
    assert lc.state is InteractionState.RESPONDED


def test_respond_once_from_deferred():
    lc = InteractionLifecycle(created_at=100.0)
    lc.defer(now=101.0)
    lc.respond(now=120.0)  # response itself is not bounded by the 3s window
    assert lc.state is InteractionState.RESPONDED


def test_respond_twice_raises_single_response_invariant():
    lc = InteractionLifecycle(created_at=100.0)
    lc.ack(now=101.0)
    lc.respond(now=101.5)
    with pytest.raises(InteractionAckError):
        lc.respond(now=102.0)
    assert lc.state is InteractionState.RESPONDED


def test_respond_twice_from_deferred_raises():
    lc = InteractionLifecycle(created_at=100.0)
    lc.defer(now=101.0)
    lc.respond(now=101.5)
    with pytest.raises(InteractionAckError):
        lc.respond(now=102.0)


def test_respond_without_ack_raises():
    lc = InteractionLifecycle(created_at=100.0)
    with pytest.raises(InteractionAckError):
        lc.respond(now=101.0)
    assert lc.state is InteractionState.PENDING


def test_respond_after_expiry_raises():
    lc = InteractionLifecycle(created_at=100.0)
    with pytest.raises(InteractionAckError):
        lc.ack(now=104.0)
    with pytest.raises(InteractionAckError):
        lc.respond(now=104.5)
    assert lc.state is InteractionState.EXPIRED


def test_is_within_ack_window_true_inside_window():
    lc = InteractionLifecycle(created_at=100.0)
    assert lc.is_within_ack_window(now=100.0) is True
    assert lc.is_within_ack_window(now=102.9) is True


def test_is_within_ack_window_boundary_exactly_three_seconds():
    lc = InteractionLifecycle(created_at=100.0)
    assert lc.is_within_ack_window(now=100.0 + ACK_WINDOW_SECONDS) is True


def test_is_within_ack_window_false_outside_window():
    lc = InteractionLifecycle(created_at=100.0)
    assert lc.is_within_ack_window(now=100.0 + ACK_WINDOW_SECONDS + 0.000001) is False
    assert lc.is_within_ack_window(now=200.0) is False
