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
    (tmp_path / "telegram" / "topics.json").write_text(json.dumps({
        "group_chat_id": "-100xxx",
        "topics": {
            "matcher": {"thread_id": 11, "name": "Matcher / Scores"},
            "alerts": {"thread_id": 9, "name": "Alerts"},
            "system": {"thread_id": 15, "name": "System"},
            "scout": {"thread_id": 10}, "tailor_applier": {"thread_id": 12},
            "tracker": {"thread_id": 13}, "digests": {"thread_id": 14},
            "agent_comms": {"thread_id": 16},
        }
    }))
    (tmp_path / "telegram" / "verbosity.json").write_text(json.dumps({
        "matcher": {"mode": "all"}, "alerts": {"mode": "all"},
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
        matcher_deliveries = [d for d in delivered if str(d[1]) == "11"]
        assert len(matcher_deliveries) >= 1, (
            f"expected matcher topic delivery, got: {delivered}"
        )
    finally:
        bus.close()
