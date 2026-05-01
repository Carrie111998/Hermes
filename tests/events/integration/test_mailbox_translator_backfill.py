"""Integration test: MailboxWatcher + MailboxTranslator on real 2026-04-16 mailbox data.

Fixtures are PII-scrubbed samples of real SCORE_RESULT and SCORE_BATCH_SUMMARY
messages emitted by the matcher agent on 2026-04-16.
"""
import json
from pathlib import Path

import pytest

from events.bus import EventBus
from events.producers.mailbox_watcher import MailboxWatcher
from events.schema import EventType
from events.subscribers.mailbox_translator import MailboxTranslator

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def mailbox_tree(tmp_path):
    inbox = tmp_path / "mailbox" / "main" / "inbox"
    inbox.mkdir(parents=True)
    for fixture in FIXTURES.glob("*.json"):
        (inbox / fixture.name).write_text(fixture.read_text(encoding="utf-8"),
                                          encoding="utf-8")
    return tmp_path


def test_real_score_result_flows_through_to_job_scored(mailbox_tree, tmp_path):
    bus = EventBus(db_path=tmp_path / "event_bus.db")
    try:
        watcher = MailboxWatcher(bus, mailbox_root=mailbox_tree / "mailbox")
        translator = MailboxTranslator(bus)

        # Seed translator cursor at zero so it sees mailbox_message events
        # emitted by watcher.scan(). Without this, the 2026-04-28 head-jump
        # default in events/bus.py:subscribe() lands the cursor past the
        # emitted rows on the very first poll.
        bus._execute(
            """INSERT INTO subscriber_cursors (subscriber_id, last_rowid, updated_at)
               VALUES ('mailbox-translator', 0, datetime('now'))
               ON CONFLICT(subscriber_id) DO UPDATE SET last_rowid = 0""",
        )

        emitted = watcher.scan()
        assert emitted > 0, "MailboxWatcher should emit mailbox_message for fixtures"

        translator.poll()

        all_events = bus.query()
        types = [e.event_type for e in all_events]

        assert EventType.MAILBOX_MESSAGE in types
        assert EventType.JOB_SCORED in types, (
            "Real SCORE_RESULT fixture must produce JOB_SCORED event"
        )
    finally:
        bus.close()
