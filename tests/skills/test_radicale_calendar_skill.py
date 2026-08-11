"""Tests for optional-skills/productivity/radicale-calendar's CLI helpers.

Pure logic only (date/datetime parsing, event JSON shaping, field-clear
semantics) - no live network calls, no real caldav server.
"""

import sys
from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# radicale_cli.py imports the `caldav` package at module scope. That's an
# opt-in extra (`hermes-agent[caldav]`), not part of [dev]/[all], so skip
# cleanly rather than erroring out test collection where it isn't installed.
pytest.importorskip("caldav")

SCRIPTS_DIR = (
    Path(__file__).resolve().parents[2]
    / "optional-skills"
    / "productivity"
    / "radicale-calendar"
    / "scripts"
)
sys.path.insert(0, str(SCRIPTS_DIR))

import radicale_cli as rc  # noqa: E402


def test_parse_dt_accepts_z_suffix():
    assert rc._parse_dt("2026-08-15T14:00:00Z") == datetime.fromisoformat(
        "2026-08-15T14:00:00+00:00"
    )


def test_parse_dt_accepts_explicit_offset():
    assert rc._parse_dt("2026-08-15T14:00:00+02:00") == datetime.fromisoformat(
        "2026-08-15T14:00:00+02:00"
    )


def test_parse_date_bare_iso():
    assert rc._parse_date("2026-08-15") == date(2026, 8, 15)


def test_set_or_clear_none_leaves_field_untouched():
    ical = {"summary": "Original"}
    rc._set_or_clear(ical, "summary", None)
    assert ical["summary"] == "Original"


def test_set_or_clear_empty_string_deletes_field():
    ical = {"summary": "Original"}
    rc._set_or_clear(ical, "summary", "")
    assert "summary" not in ical


def test_set_or_clear_value_sets_field():
    ical = {}
    rc._set_or_clear(ical, "location", "New Place")
    assert ical["location"] == "New Place"


def test_event_json_timed_event():
    comp = {
        "uid": "abc-123",
        "summary": "Dentist",
        "dtstart": MagicMock(dt=datetime(2026, 8, 15, 14, 0)),
        "dtend": MagicMock(dt=datetime(2026, 8, 15, 15, 0)),
        "location": "Clinic",
        "description": "Check-up",
        "rrule": None,
    }
    event = MagicMock()
    event.icalendar_component = comp
    event.parent.get_display_name.return_value = "default"

    result = rc._event_json(event)

    assert result["uid"] == "abc-123"
    assert result["summary"] == "Dentist"
    assert result["start"] == "2026-08-15T14:00:00"
    assert result["end"] == "2026-08-15T15:00:00"
    assert result["all_day"] is False
    assert result["recurring"] is None


def test_event_json_all_day_recurring_event():
    comp = {
        "uid": "bday-1",
        "summary": "Birthday",
        "dtstart": MagicMock(dt=date(2026, 4, 13)),
        "dtend": MagicMock(dt=date(2026, 4, 14)),
        "location": "",
        "description": "",
        "rrule": MagicMock(to_ical=lambda: b"FREQ=YEARLY"),
    }
    event = MagicMock()
    event.icalendar_component = comp
    event.parent.get_display_name.return_value = "default"

    result = rc._event_json(event)

    assert result["all_day"] is True
    assert result["recurring"] == "FREQ=YEARLY"
    assert result["location"] is None
    assert result["description"] is None
