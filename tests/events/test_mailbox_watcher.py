"""Tests for events.producers.mailbox_watcher — MailboxWatcher."""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from events.bus import EventBus
from events.schema import EventType
from events.producers.mailbox_watcher import MailboxWatcher

MIRRORED_TYPES = {"SCORE_RESULT", "TAILOR_REQUEST", "SUBMIT_REQUEST", "SCOUT_DISCOVERY"}


@pytest.fixture
def bus(tmp_path):
    return EventBus(db_path=tmp_path / "events" / "event_bus.db")


@pytest.fixture
def mailbox_root(tmp_path):
    root = tmp_path / "mailbox"
    for profile in ("main", "scout", "matcher", "tracker"):
        (root / profile / "inbox").mkdir(parents=True)
    return root


def _write_message(inbox: Path, msg_type: str, sender: str, payload: dict) -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    filename = f"{ts}_{msg_type}_{sender}.json"
    msg = {"type": msg_type, "from": sender, "to": inbox.parent.name, "payload": payload}
    path = inbox / filename
    path.write_text(json.dumps(msg), encoding="utf-8")
    return path


class TestMailboxWatcher:
    def test_detects_new_messages(self, bus, mailbox_root):
        watcher = MailboxWatcher(bus, mailbox_root=mailbox_root)

        _write_message(
            mailbox_root / "tracker" / "inbox",
            "SCOUT_DISCOVERY", "scout",
            {"jobs": [{"title": "VP Finance"}]},
        )

        count = watcher.scan()
        assert count == 1

        events = bus.query(event_type=EventType.MAILBOX_MESSAGE)
        assert len(events) == 1
        assert events[0].payload["message_type"] == "SCOUT_DISCOVERY"
        assert events[0].payload["from"] == "scout"
        assert events[0].payload["to"] == "tracker"

    def test_skips_already_seen(self, bus, mailbox_root):
        watcher = MailboxWatcher(bus, mailbox_root=mailbox_root)

        _write_message(mailbox_root / "main" / "inbox", "SCORE_RESULT", "matcher", {})

        assert watcher.scan() == 1
        assert watcher.scan() == 0  # same file, already seen

    def test_filters_non_protocol_files(self, bus, mailbox_root):
        watcher = MailboxWatcher(bus, mailbox_root=mailbox_root)

        # Write a file that doesn't match the naming convention
        (mailbox_root / "main" / "inbox" / "random.txt").write_text("not a message")

        assert watcher.scan() == 0

    def test_filters_sweeper_files(self, bus, mailbox_root):
        watcher = MailboxWatcher(bus, mailbox_root=mailbox_root)

        # Sweeper operations use non-standard types
        _write_message(mailbox_root / "main" / "inbox", "SWEEP_COMPLETE", "system", {})

        assert watcher.scan() == 0

    def test_persists_watermark(self, bus, mailbox_root):
        watcher1 = MailboxWatcher(bus, mailbox_root=mailbox_root)
        _write_message(mailbox_root / "main" / "inbox", "SCORE_RESULT", "matcher", {})
        watcher1.scan()

        # New watcher instance loads watermark from disk
        watcher2 = MailboxWatcher(bus, mailbox_root=mailbox_root)
        _write_message(mailbox_root / "main" / "inbox", "TAILOR_REQUEST", "main", {})

        assert watcher2.scan() == 1  # only the new message


def test_mailbox_watcher_forwards_inner_payload(tmp_path):
    import json
    from events.bus import EventBus
    from events.producers.mailbox_watcher import MailboxWatcher
    inbox = tmp_path / "mailbox" / "main" / "inbox"
    inbox.mkdir(parents=True)
    msg = {
        "type": "SCORE_RESULT",
        "from": "matcher", "to": "main",
        "correlation_id": "abc",
        "payload": {"score": 8.8, "company": "X"},
    }
    (inbox / "20260416T1_SCORE_RESULT_matcher.json").write_text(json.dumps(msg))
    bus = EventBus(db_path=tmp_path / "db.sqlite")
    MailboxWatcher(bus, mailbox_root=tmp_path / "mailbox").scan()
    events = bus.query()
    assert len(events) == 1
    assert events[0].payload.get("inner_payload") == {"score": 8.8, "company": "X"}
    bus.close()


def test_summarize_covers_key_message_types(tmp_path):
    from events.bus import EventBus
    from events.producers.mailbox_watcher import MailboxWatcher
    bus = EventBus(db_path=tmp_path / "db.sqlite")
    try:
        w = MailboxWatcher(bus, mailbox_root=tmp_path / "mailbox")

        assert w._summarize({"type": "NOTIFICATION",
                             "payload": {"summary": "New interview offer"}}) == "New interview offer"
        assert w._summarize({"type": "NOTIFICATION",
                             "payload": {"body": "Body text here", "summary": ""}}) == "Body text here"
        assert w._summarize({"type": "SCORE_RESULT",
                             "payload": {"score": 8.9, "company": "Acme", "title": "VP Fin"}}) == "score 8.9 for Acme (VP Fin)"
        assert "pending -> scored" in w._summarize({"type": "PIPELINE_UPDATE",
                             "payload": {"previous_stage": "pending", "new_stage": "scored", "job_key": "j1"}})
        alias_summary = w._summarize({
            "type": "PIPELINE_UPDATE",
            "payload": {
                "from_stage": "approved_for_tailor",
                "to_stage": "materials_ready",
                "job_id": "job-48",
            },
        })
        assert "approved_for_tailor -> materials_ready" in alias_summary
        assert "job-48" in alias_summary
        assert "3 days" in w._summarize({"type": "FOLLOWUP_ALERT",
                             "payload": {"days_since_application": 3, "company": "X"}})
        assert "Acme / VP" in w._summarize({"type": "SUBMIT_CONFIRM",
                             "payload": {"company": "Acme", "title": "VP"}})
    finally:
        bus.close()
