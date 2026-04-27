"""End-to-end smoke test: mailbox file -> domain event -> Telegram delivery stub."""
import json

from events.bus import EventBus
from events.producers.mailbox_watcher import MailboxWatcher
from events.schema import EventType
from events.subscribers.mailbox_translator import MailboxTranslator
from events.subscribers.telegram_notifier import TelegramNotifier


def test_full_stack_score_result_reaches_telegram(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    inbox = tmp_path / "mailbox" / "main" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "20260416T_SCORE_RESULT_matcher.json").write_text(json.dumps({
        "type": "SCORE_RESULT",
        "from": "matcher", "to": "main",
        "correlation_id": "abc",
        "payload": {"score": 8.9, "company": "Acme", "title": "VP Fin",
                    "recommendation": "PROCEED"},
    }))

    (tmp_path / "telegram").mkdir()
    # v2 topic keys (Hermes Telegram cutover 20260424T233627Z). Thread IDs
    # chosen so the JOB_SCORED → jobflow_firehose delivery lands at thread 11
    # (the existing assertion below). JOB_HIGH_SCORE (score 8.9 ≥ 8.75
    # threshold) routes to jobflow_decisions; mailbox_message defaults to
    # scribe_daily.
    (tmp_path / "telegram" / "topics.json").write_text(json.dumps({
        "group_chat_id": "-100xxx",
        "topics": {
            "jobflow_firehose": {"thread_id": 11, "name": "JobFlow Firehose"},
            "jobflow_decisions": {"thread_id": 12, "name": "JobFlow Decisions"},
            "watchdog_alerts": {"thread_id": 9, "name": "Watchdog Alerts"},
            "security_and_system": {"thread_id": 15, "name": "Security & System"},
            "scribe_daily": {"thread_id": 14, "name": "Scribe Daily"},
            "devflow_firehose": {"thread_id": 10},
            "devflow_decisions": {"thread_id": 13},
            "curator_digest": {"thread_id": 16},
            "critic_proposals": {"thread_id": 17},
        }
    }))
    (tmp_path / "telegram" / "verbosity.json").write_text(json.dumps({
        "jobflow_firehose": {"mode": "all"}, "watchdog_alerts": {"mode": "all"},
    }))

    bus = EventBus(db_path=tmp_path / "events" / "event_bus.db")
    try:
        delivered = []
        send_fn = lambda chat_id, thread_id, msg: delivered.append(
            (chat_id, thread_id, msg))

        watcher = MailboxWatcher(bus)
        translator = MailboxTranslator(bus)
        notifier = TelegramNotifier(bus, send_fn=send_fn)

        watcher.scan()
        translator.poll()
        notifier.poll()

        assert len(delivered) >= 1, f"expected at least one delivery, got: {delivered}"
        firehose_deliveries = [d for d in delivered if str(d[1]) == "11"]
        assert len(firehose_deliveries) >= 1, (
            f"expected jobflow_firehose topic delivery, got: {delivered}"
        )
    finally:
        bus.close()
