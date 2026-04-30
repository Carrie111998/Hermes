"""End-to-end correlation_id chain for the notification reverse signal.

Pins that when a high-stakes event (interview_signal, application_failed,
etc.) flows through the full pipeline (producer -> telegram-notifier ->
audit-logger), the resulting NOTIFICATION_DELIVERED / NOTIFICATION_FAILED
event's ``correlation_id`` field equals the ORIGINAL event's
``event_id``.

Why pin this: the user-facing query "did the WhatsApp also go out for
this interview signal?" turns into a grep over audit.jsonl. The grep
joins on correlation_id. If the producer/consumer pair doesn't agree
on the join key, the answer is silently empty and the operator can't
tell. This test is the contract.

Spec: docs/superpowers/specs/2026-04-30-notification-delivered-design.md
"""
import json

import pytest

from events.bus import EventBus
from events.schema import EventType, Priority
from events.subscribers.audit_logger import AuditLogger
from events.subscribers.telegram_notifier import TelegramNotifier
from events.subscribers.whatsapp_escalator import WhatsAppEscalator


@pytest.fixture
def bus(tmp_path):
    return EventBus(db_path=tmp_path / "events" / "event_bus.db")


@pytest.fixture
def topics_config(tmp_path):
    config = {
        "group_chat_id": "-1001234567890",
        "topics": {
            "watchdog_alerts": {"thread_id": 100, "name": "Watchdog Alerts"},
            "jobflow_firehose": {"thread_id": 101, "name": "JobFlow Firehose"},
            "jobflow_decisions": {"thread_id": 102, "name": "JobFlow Decisions"},
            "scribe_daily": {"thread_id": 105, "name": "Scribe Daily"},
        },
    }
    path = tmp_path / "telegram" / "topics.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(config))
    return path


@pytest.fixture
def verbosity_config(tmp_path):
    config = {
        "jobflow_firehose": {"mode": "all"},
        "jobflow_decisions": {"mode": "all"},
        "watchdog_alerts": {"mode": "all"},
    }
    path = tmp_path / "telegram" / "verbosity.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config))
    return path


@pytest.fixture
def quiet_config(tmp_path):
    # Disable quiet hours so IMMEDIATE escalations always deliver
    # immediately and the test doesn't depend on wall-clock time.
    config = {
        "enabled": False,
        "start": "23:00",
        "end": "07:00",
        "timezone": "America/New_York",
        "breakthrough_events": ["interview_signal", "offer_signal"],
    }
    path = tmp_path / "notifications" / "quiet_hours.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(config))
    return path


@pytest.fixture
def audit_path(tmp_path):
    return tmp_path / "events" / "audit.jsonl"


def _register_backfill_cursor(bus: EventBus, subscriber_id: str) -> None:
    """Pre-register the subscriber's cursor at rowid=0 so it backfills
    events emitted later in the same test.

    The 2026-04-28 first-registration head-default in bus.subscribe()
    correctly prevents production backlog floods, but tests that
    emit-then-poll need an explicit opt-in to backfill. The bus.py
    docstring documents this escape hatch as: "If you genuinely want
    backfill, manually set last_rowid in subscriber_cursors BEFORE
    the subscriber's first poll."
    """
    bus._execute(
        "INSERT INTO subscriber_cursors(subscriber_id, last_rowid, updated_at) "
        "VALUES (?, 0, datetime('now'))",
        (subscriber_id,),
    )


class TestCorrelationIdDeliveryChain:
    """A successful Telegram delivery + audit drain produces an audit
    line for both the original event AND the reverse signal, joinable
    by correlation_id.
    """

    def test_telegram_success_chain_grep_joinable(
        self, bus, topics_config, verbosity_config, audit_path,
    ):
        notifier = TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_config,
            send_fn=lambda chat_id, thread_id, msg: None,
        )
        audit = AuditLogger(bus, audit_path=audit_path)
        _register_backfill_cursor(bus, audit.subscriber_id)

        original_id = bus.emit(
            event_type=EventType.INTERVIEW_SIGNAL, source="tracker",
            payload={"company": "Acme", "detail": "phone screen"},
            priority=Priority.CRITICAL,
        )
        original = bus.query(event_type=EventType.INTERVIEW_SIGNAL)[0]
        notifier.handle(original)

        audit.poll()

        lines = [
            json.loads(line) for line in audit_path.read_text().strip().split("\n")
            if line
        ]
        # The original interview_signal must be in the audit log
        originals = [l for l in lines if l["event_type"] == "interview_signal"]
        assert len(originals) == 1
        assert originals[0]["event_id"] == original_id

        # And the NOTIFICATION_DELIVERED reverse-signal lines (cross-post
        # produces 2 — primary + watchdog_alerts at HIGH+) must point
        # back to it via correlation_id.
        delivered = [l for l in lines if l["event_type"] == "notification_delivered"]
        assert len(delivered) == 2, (
            f"interview_signal cross-posts; expected 2 reverse signals, "
            f"got {len(delivered)}"
        )
        for d in delivered:
            assert d["correlation_id"] == original_id, (
                "delivery audit entry must point at the original event"
            )
            assert d["payload"]["original_event_id"] == original_id

    def test_telegram_failure_chain_grep_joinable(
        self, bus, topics_config, verbosity_config, audit_path,
    ):
        """Same shape as the success path — failures must also be
        joinable to the original event so an operator can ask
        "what failed and why?" and get the upstream context.
        """
        def boom(chat_id, thread_id, msg):
            raise RuntimeError("Bad Request: thread_not_found")

        notifier = TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_config,
            send_fn=boom,
        )
        audit = AuditLogger(bus, audit_path=audit_path)
        _register_backfill_cursor(bus, audit.subscriber_id)

        original_id = bus.emit(
            event_type=EventType.APPLICATION_FAILED, source="applier",
            payload={"company": "Acme", "error": "captcha"},
            priority=Priority.CRITICAL,
        )
        original = bus.query(event_type=EventType.APPLICATION_FAILED)[0]
        notifier.handle(original)

        audit.poll()

        lines = [
            json.loads(line) for line in audit_path.read_text().strip().split("\n")
            if line
        ]
        failed = [l for l in lines if l["event_type"] == "notification_failed"]
        assert len(failed) >= 1, (
            "application_failed at CRITICAL routes to watchdog_alerts; "
            "the failed delivery must produce a notification_failed entry"
        )
        for f in failed:
            assert f["correlation_id"] == original_id
            assert f["payload"]["original_event_id"] == original_id
            assert f["payload"]["error"]["kind"] == "RuntimeError"
            assert "thread_not_found" in f["payload"]["error"]["message"]

    def test_whatsapp_success_chain_grep_joinable(
        self, bus, quiet_config, tmp_path, audit_path,
    ):
        """Same join contract for the WhatsApp side. interview_signal
        is IMMEDIATE so it bypasses throttle and emits the reverse
        signal synchronously inside handle().
        """
        escalator = WhatsAppEscalator(
            bus, quiet_config_path=quiet_config,
            queue_path=tmp_path / "queue.json",
            send_fn=lambda msg: None,
        )
        audit = AuditLogger(bus, audit_path=audit_path)
        _register_backfill_cursor(bus, audit.subscriber_id)

        original_id = bus.emit(
            event_type=EventType.INTERVIEW_SIGNAL, source="tracker",
            payload={"company": "Acme", "detail": "phone screen"},
            priority=Priority.CRITICAL,
        )
        original = bus.query(event_type=EventType.INTERVIEW_SIGNAL)[0]
        escalator.handle(original)

        audit.poll()

        lines = [
            json.loads(line) for line in audit_path.read_text().strip().split("\n")
            if line
        ]
        delivered = [
            l for l in lines
            if l["event_type"] == "notification_delivered"
            and l["payload"]["platform"] == "whatsapp"
        ]
        assert len(delivered) == 1, (
            f"expected one whatsapp NOTIFICATION_DELIVERED in audit; "
            f"got {len(delivered)}"
        )
        assert delivered[0]["correlation_id"] == original_id
        assert delivered[0]["payload"]["original_event_id"] == original_id

    def test_grep_pattern_finds_original_and_delivery_for_same_correlation_id(
        self, bus, topics_config, verbosity_config, quiet_config,
        tmp_path, audit_path,
    ):
        """Operator query: ``grep <correlation_id> audit.jsonl`` must
        return the upstream event AND every delivery report (telegram +
        whatsapp + cross-posts). This is the integration test for the
        end-to-end flow Diego asked about: "did the WhatsApp ALSO go
        out?" — answerable in one grep.
        """
        notifier = TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_config,
            send_fn=lambda chat_id, thread_id, msg: None,
        )
        escalator = WhatsAppEscalator(
            bus, quiet_config_path=quiet_config,
            queue_path=tmp_path / "queue.json",
            send_fn=lambda msg: None,
        )
        audit = AuditLogger(bus, audit_path=audit_path)
        _register_backfill_cursor(bus, audit.subscriber_id)

        original_id = bus.emit(
            event_type=EventType.INTERVIEW_SIGNAL, source="tracker",
            payload={"company": "Acme", "detail": "phone screen"},
            priority=Priority.CRITICAL,
        )
        original = bus.query(event_type=EventType.INTERVIEW_SIGNAL)[0]
        notifier.handle(original)
        escalator.handle(original)

        audit.poll()

        # Simulate the operator grep: filter audit by correlation_id /
        # event_id == original_id. We expect ≥ 4 hits:
        #   1 original interview_signal
        #   2 telegram NOTIFICATION_DELIVERED (primary + cross-post)
        #   1 whatsapp NOTIFICATION_DELIVERED (IMMEDIATE breakthrough)
        lines = [
            json.loads(line) for line in audit_path.read_text().strip().split("\n")
            if line
        ]
        joined = [
            l for l in lines
            if l.get("correlation_id") == original_id
            or l.get("event_id") == original_id
        ]
        assert len(joined) >= 4, (
            f"grep must join 4+ events on correlation_id; got {len(joined)}: "
            f"{[(l.get('event_type'), l.get('payload', {}).get('platform')) for l in joined]}"
        )
        # Confirm at least one telegram and one whatsapp delivery
        platforms = {
            l.get("payload", {}).get("platform")
            for l in joined
            if l["event_type"] == "notification_delivered"
        }
        assert "telegram" in platforms
        assert "whatsapp" in platforms
