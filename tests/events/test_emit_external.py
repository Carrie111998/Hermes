"""Tests for events.emit_external — the out-of-process emit path.

Added 2026-07-27 alongside BOOT_SUMMARY. ~/laptop-start.ps1 shells out to this
module instead of posting raw text at a Telegram thread, so a regression here
puts the boot summary back to being the one message in the feed with no
priority dot, icon or header.
"""
import io
import json

import pytest

from events.bus import EventBus
from events.emit_external import main
from events.schema import EventType, Priority


@pytest.fixture
def bus(tmp_path, monkeypatch):
    db = tmp_path / "event_bus.db"
    monkeypatch.setattr("events.paths.events_db_path", lambda: db)
    return EventBus(db_path=db)


def _run(monkeypatch, argv, payload):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    return main(argv)


def test_emits_a_boot_summary_onto_the_bus(bus, tmp_path, monkeypatch):
    monkeypatch.setattr("events.emit_external.EventBus",
                        lambda *a, **k: bus)
    rc = _run(monkeypatch,
              ["--type", "boot_summary", "--source", "laptop-start"],
              {"boot_id": "20260723-110217", "state": "done",
               "total": 22, "done": 22, "failed": 0,
               "failures": [], "anomalies": ["task-329-kill: killed"]})
    assert rc == 0
    events = bus.query()
    assert len(events) == 1
    assert events[0].event_type is EventType.BOOT_SUMMARY
    assert events[0].source == "laptop-start"
    assert events[0].payload["boot_id"] == "20260723-110217"
    # No explicit --priority: the type's own default applies.
    assert events[0].priority is Priority.HIGH


def test_priority_override_is_applied(bus, monkeypatch):
    monkeypatch.setattr("events.emit_external.EventBus", lambda *a, **k: bus)
    rc = _run(monkeypatch,
              ["--type", "boot_summary", "--source", "laptop-start",
               "--priority", "critical"],
              {"state": "failed"})
    assert rc == 0
    assert bus.query()[0].priority is Priority.CRITICAL


def test_unknown_event_type_is_rejected(bus, monkeypatch):
    monkeypatch.setattr("events.emit_external.EventBus", lambda *a, **k: bus)
    assert _run(monkeypatch, ["--type", "nope", "--source", "x"], {"a": 1}) == 2
    assert bus.query() == []


def test_unknown_priority_is_rejected_not_silently_downgraded(bus, monkeypatch):
    """Priority.from_string falls back to NORMAL for unknown labels, which
    would turn a typo into a quietly-demoted alert. Reject instead."""
    monkeypatch.setattr("events.emit_external.EventBus", lambda *a, **k: bus)
    rc = _run(monkeypatch,
              ["--type", "boot_summary", "--source", "x",
               "--priority", "urgent"], {"a": 1})
    assert rc == 2
    assert bus.query() == []


def test_non_object_payload_is_rejected(bus, monkeypatch):
    monkeypatch.setattr("events.emit_external.EventBus", lambda *a, **k: bus)
    assert _run(monkeypatch, ["--type", "boot_summary", "--source", "x"],
                ["not", "an", "object"]) == 2


def test_empty_stdin_is_rejected(bus, monkeypatch):
    monkeypatch.setattr("events.emit_external.EventBus", lambda *a, **k: bus)
    monkeypatch.setattr("sys.stdin", io.StringIO("   "))
    assert main(["--type", "boot_summary", "--source", "x"]) == 2


def test_malformed_json_is_rejected(bus, monkeypatch):
    monkeypatch.setattr("events.emit_external.EventBus", lambda *a, **k: bus)
    monkeypatch.setattr("sys.stdin", io.StringIO("{not json"))
    assert main(["--type", "boot_summary", "--source", "x"]) == 2
