"""Merged message bursts must quote the newest message, not the first.

``merge_pending_message_event`` folds a rapid burst of user messages into the
single queued event that the next turn consumes.  The queued event's
``message_id`` is what ``_reply_anchor_for_event`` hands to adapters as
``reply_to``, so keeping the first message's id made the agent answer message #3
while visibly quoting message #1.
"""

from gateway.platforms.base import (
    MessageEvent,
    MessageType,
    _reply_anchor_for_event,
    merge_pending_message_event,
)


def _event(text, message_id, message_type=MessageType.TEXT, media=()):
    return MessageEvent(
        text=text,
        message_type=message_type,
        message_id=message_id,
        media_urls=list(media),
        media_types=["image/jpeg"] * len(media),
    )


def test_text_burst_anchors_on_last_message():
    pending = {}
    for text, message_id in (("first", "M1"), ("second", "M2"), ("third", "M3")):
        merge_pending_message_event(
            pending, "sess", _event(text, message_id), merge_text=True
        )

    merged = pending["sess"]
    assert merged.text == "first\nsecond\nthird"
    assert merged.message_id == "M3"
    assert _reply_anchor_for_event(merged) == "M3"


def test_photo_burst_anchors_on_last_message():
    pending = {}
    merge_pending_message_event(
        pending, "sess", _event("", "P1", MessageType.PHOTO, ["a.jpg"])
    )
    merge_pending_message_event(
        pending, "sess", _event("caption", "P2", MessageType.PHOTO, ["b.jpg"])
    )

    merged = pending["sess"]
    assert merged.media_urls == ["a.jpg", "b.jpg"]
    assert merged.message_id == "P2"


def test_text_then_media_anchors_on_last_message():
    pending = {}
    merge_pending_message_event(pending, "sess", _event("look at this", "T1"))
    merge_pending_message_event(
        pending, "sess", _event("", "P9", MessageType.PHOTO, ["c.jpg"])
    )

    merged = pending["sess"]
    assert merged.message_id == "P9"
    assert merged.message_type == MessageType.PHOTO


def test_anchorless_followup_keeps_previous_anchor():
    """Synthetic events carry no id; clearing the anchor would drop threading."""
    pending = {}
    merge_pending_message_event(pending, "sess", _event("real message", "T1"))
    merge_pending_message_event(
        pending, "sess", _event("synthetic", None), merge_text=True
    )

    assert pending["sess"].message_id == "T1"


def test_first_event_is_stored_unchanged():
    pending = {}
    event = _event("solo", "S1")
    merge_pending_message_event(pending, "sess", event)

    assert pending["sess"] is event
    assert pending["sess"].message_id == "S1"
