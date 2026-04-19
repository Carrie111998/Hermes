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

    def test_recovers_files_swept_to_processed_before_scan(self, bus, mailbox_root):
        """Simulate the jaum-inbox-sweeper race: a file arrives in inbox/ and is
        moved to processed/ before MailboxWatcher scans.  The file must still
        produce a mailbox_message event via the processed/ recovery pass,
        tagged with recovered_from=processed.
        """
        watcher = MailboxWatcher(bus, mailbox_root=mailbox_root)

        processed = mailbox_root / "main" / "processed"
        processed.mkdir(parents=True, exist_ok=True)

        # Write the message directly to processed/ — as if the sweeper had
        # already moved it from inbox/ before MailboxWatcher's first poll.
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        filename = f"{ts}_NOTIFICATION_notifier.json"
        msg = {
            "type": "NOTIFICATION",
            "from": "notifier",
            "to": "main",
            "payload": {"summary": "Morning digest body"},
        }
        (processed / filename).write_text(json.dumps(msg), encoding="utf-8")

        count = watcher.scan()
        assert count == 1

        events = bus.query(event_type=EventType.MAILBOX_MESSAGE)
        assert len(events) == 1
        assert events[0].payload["message_type"] == "NOTIFICATION"
        assert events[0].payload.get("recovered_from") == "processed"

        # A second scan must not re-emit the same file
        assert watcher.scan() == 0

    def test_dedup_inbox_and_processed_by_basename(self, bus, mailbox_root):
        """A file scanned from inbox/ then moved to processed/ between polls
        must not be emitted twice when the next scan sees it in processed/.
        """
        watcher = MailboxWatcher(bus, mailbox_root=mailbox_root)

        msg_path = _write_message(
            mailbox_root / "main" / "inbox",
            "SCORE_RESULT", "matcher",
            {"score": 9.0, "company": "Acme", "title": "VP Fin"},
        )
        assert watcher.scan() == 1

        # Simulate the sweeper moving the file to processed/
        processed = mailbox_root / "main" / "processed"
        processed.mkdir(parents=True, exist_ok=True)
        msg_path.rename(processed / msg_path.name)

        # Processed recovery scan must skip this file (already emitted)
        assert watcher.scan() == 0

        events = bus.query(event_type=EventType.MAILBOX_MESSAGE)
        assert len(events) == 1  # still just one emission

    def test_processed_recovery_respects_time_window(self, bus, mailbox_root):
        """Old files in processed/ (beyond the recovery window) must not be
        emitted — the window bounds per-poll work and prevents replaying ancient
        archives after watermark loss."""
        import os
        from events.producers.mailbox_watcher import PROCESSED_RECOVERY_WINDOW_SECONDS

        watcher = MailboxWatcher(bus, mailbox_root=mailbox_root)

        processed = mailbox_root / "scout" / "processed"
        processed.mkdir(parents=True, exist_ok=True)
        old_file = processed / "20240101T000000Z_SCOUT_DISCOVERY_scout.json"
        old_file.write_text(json.dumps({
            "type": "SCOUT_DISCOVERY", "from": "scout", "to": "tracker",
            "payload": {"jobs": []},
        }), encoding="utf-8")

        # Push mtime well beyond the recovery window
        old_ts = __import__("time").time() - (PROCESSED_RECOVERY_WINDOW_SECONDS * 2)
        os.utime(old_file, (old_ts, old_ts))

        assert watcher.scan() == 0  # skipped by mtime cutoff


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
