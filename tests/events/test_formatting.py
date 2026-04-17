"""Tests for events.formatting emoji + header helpers."""
from events.formatting import (
    PRIORITY_EMOJI, EVENT_TYPE_EMOJI, MAILBOX_INNER_EMOJI,
    SEPARATOR,
    priority_dot, event_icon,
    format_header, format_event_message, format_whatsapp_message,
)
from events.schema import Event, EventType, Priority


def _make_event(event_type, priority=None, source="test", payload=None,
                timestamp="2026-04-17T05:02:39+00:00"):
    return Event(
        event_id="x", event_type=event_type, source=source,
        timestamp=timestamp, priority=priority or event_type.default_priority,
        payload=payload or {},
    )


def test_priority_dots_cover_all_levels():
    for p in Priority:
        assert priority_dot(p), f"missing dot for {p}"


def test_event_icons_cover_all_types():
    for et in EventType:
        assert EVENT_TYPE_EMOJI.get(et), f"missing icon for {et.type_string}"


def test_event_icon_uses_inner_type_for_mailbox_message():
    e = _make_event(EventType.MAILBOX_MESSAGE,
                    payload={"message_type": "SCORE_RESULT"})
    assert event_icon(e) == "📊"


def test_event_icon_falls_back_to_mailbox_generic_for_unknown_inner():
    e = _make_event(EventType.MAILBOX_MESSAGE,
                    payload={"message_type": "UNKNOWN_TYPE"})
    assert event_icon(e) == "📨"


def test_format_header_for_agent_error():
    e = _make_event(EventType.AGENT_ERROR, source="mailbox:sentinel")
    assert format_header(e) == "🟠 ⚠️ AGENT_ERROR — mailbox:sentinel · 05:02 UTC"


def test_format_header_for_interview_signal_is_critical():
    e = _make_event(EventType.INTERVIEW_SIGNAL, source="mailbox:notifier")
    h = format_header(e)
    assert h.startswith("🔴")
    assert "🗓️" in h
    assert "INTERVIEW_SIGNAL" in h
    assert "mailbox:notifier" in h


def test_format_header_for_mailbox_message_shows_inner_type_and_routing():
    e = _make_event(EventType.MAILBOX_MESSAGE,
                    payload={"message_type": "SCORE_RESULT",
                             "from": "matcher", "to": "main"})
    h = format_header(e)
    assert "📊" in h and "SCORE_RESULT" in h and "matcher → main" in h


def test_format_event_message_includes_separator():
    e = _make_event(EventType.CRON_COMPLETED, source="polish-verify")
    msg = format_event_message(e, "body here")
    assert SEPARATOR in msg
    assert "body here" in msg


def test_format_whatsapp_message_has_no_separator():
    e = _make_event(EventType.OFFER_SIGNAL, source="mailbox:notifier")
    msg = format_whatsapp_message(e, "You have an offer from Acme")
    assert SEPARATOR not in msg
    assert "OFFER_SIGNAL" in msg
    assert "You have an offer from Acme" in msg
