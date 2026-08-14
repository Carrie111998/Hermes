"""Tests for the Discord streaming-delivery state machine (feature M7)."""

import pytest

from plugins.platforms.discord.delivery_state import (
    DeliveryState,
    DeliveryStateError,
)


def make_stream():
    """Helper: fresh state machine advanced to STREAMING."""
    ds = DeliveryState()
    ds.begin()
    ds.start_stream()
    return ds


# ----------------------------------------------------------------------
# Happy path
# ----------------------------------------------------------------------

def test_initial_state_is_idle():
    ds = DeliveryState()
    assert ds.state == DeliveryState.IDLE


def test_begin_transitions_to_typing():
    ds = DeliveryState()
    ds.begin()
    assert ds.state == DeliveryState.TYPING


def test_happy_path_begin_start_append_finish():
    ds = make_stream()
    assert ds.state == DeliveryState.STREAMING

    ds.append("Hello, ")
    ds.append("world!")
    assert ds.state == DeliveryState.STREAMING
    assert ds.delivered_chunks == 2
    assert ds.has_content is True

    ds.finish()
    assert ds.state == DeliveryState.DONE
    assert ds.delivered_chunks == 2
    assert ds.has_content is True


# ----------------------------------------------------------------------
# Invalid transitions raise DeliveryStateError
# ----------------------------------------------------------------------

def test_begin_raises_when_not_idle():
    ds = DeliveryState()
    ds.begin()
    with pytest.raises(DeliveryStateError):
        ds.begin()


def test_begin_raises_after_done():
    ds = make_stream()
    ds.append("hello")
    ds.finish()
    with pytest.raises(DeliveryStateError):
        ds.begin()


def test_start_stream_requires_typing():
    ds = DeliveryState()
    with pytest.raises(DeliveryStateError):
        ds.start_stream()


def test_append_requires_streaming():
    ds = DeliveryState()
    with pytest.raises(DeliveryStateError):
        ds.append("nope")


def test_finish_requires_streaming():
    ds = DeliveryState()
    with pytest.raises(DeliveryStateError):
        ds.finish()


def test_delivery_state_error_is_value_error():
    assert issubclass(DeliveryStateError, ValueError)


# ----------------------------------------------------------------------
# Duplicate-final-response guard
# ----------------------------------------------------------------------

def test_duplicate_finish_raises():
    ds = make_stream()
    ds.append("content")
    ds.finish()
    assert ds.state == DeliveryState.DONE
    with pytest.raises(DeliveryStateError):
        ds.finish()


def test_operations_after_finish_raise():
    ds = make_stream()
    ds.append("content")
    ds.finish()
    with pytest.raises(DeliveryStateError):
        ds.append("more")
    with pytest.raises(DeliveryStateError):
        ds.start_stream()


# ----------------------------------------------------------------------
# fail(err) from active states
# ----------------------------------------------------------------------

def test_fail_from_streaming():
    ds = make_stream()
    ds.append("partial")
    ds.fail(RuntimeError("boom"))
    assert ds.state == DeliveryState.FAILED
    assert isinstance(ds.error, RuntimeError)
    # no further transitions from FAILED
    with pytest.raises(DeliveryStateError):
        ds.append("more")
    with pytest.raises(DeliveryStateError):
        ds.finish()


def test_fail_from_typing():
    ds = DeliveryState()
    ds.begin()
    ds.fail(ValueError("bad"))
    assert ds.state == DeliveryState.FAILED
    assert isinstance(ds.error, ValueError)


def test_fail_from_idle_raises():
    ds = DeliveryState()
    with pytest.raises(DeliveryStateError):
        ds.fail("nope")


def test_fail_after_done_raises():
    ds = make_stream()
    ds.append("x")
    ds.finish()
    with pytest.raises(DeliveryStateError):
        ds.fail("nope")


# ----------------------------------------------------------------------
# reset()
# ----------------------------------------------------------------------

def test_reset_returns_to_idle():
    ds = make_stream()
    ds.append("abc")
    ds.finish()
    ds.reset()
    assert ds.state == DeliveryState.IDLE
    assert ds.delivered_chunks == 0
    assert ds.has_content is False


def test_reset_makes_machine_reusable():
    ds = make_stream()
    ds.append("first run")
    ds.finish()
    ds.reset()
    ds.begin()
    assert ds.state == DeliveryState.TYPING
    ds.start_stream()
    ds.append("second run")
    ds.finish()
    assert ds.state == DeliveryState.DONE
    assert ds.delivered_chunks == 1


def test_reset_clears_error():
    ds = make_stream()
    ds.fail(OSError("network"))
    ds.reset()
    assert ds.state == DeliveryState.IDLE
    assert ds.error is None


# ----------------------------------------------------------------------
# has_content / empty-content handling
# ----------------------------------------------------------------------

def test_has_content_tracks_non_empty_chunks():
    ds = make_stream()
    assert ds.has_content is False
    ds.append("")
    assert ds.has_content is False
    ds.append("data")
    assert ds.has_content is True
    ds.append("")
    assert ds.has_content is True  # empty chunk never drops prior content


def test_empty_append_is_noop():
    ds = make_stream()
    ds.append("")
    assert ds.delivered_chunks == 0
    ds.append("real")
    assert ds.delivered_chunks == 1
    assert ds.has_content is True


def test_delivered_chunks_counts_only_non_empty():
    ds = make_stream()
    ds.append("a")
    ds.append("")
    ds.append("b")
    assert ds.delivered_chunks == 2


def test_empty_content_finish_allowed_when_nothing_appended():
    ds = make_stream()
    ds.finish()  # empty finish with no non-empty chunks is allowed
    assert ds.state == DeliveryState.DONE
    assert ds.has_content is False
    assert ds.delivered_chunks == 0


def test_content_is_never_silently_dropped_at_finish():
    ds = make_stream()
    ds.append("kept")
    ds.append("")  # trailing empty chunk must not erase prior content
    ds.finish()
    assert ds.state == DeliveryState.DONE
    assert ds.has_content is True
    assert ds.delivered_chunks == 1
