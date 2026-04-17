"""Tests for MailboxTranslator subscriber (Silence #1 fix)."""
import json
from pathlib import Path

import pytest

from events.bus import EventBus
from events.schema import Event, EventType, Priority
from events.subscribers.mailbox_translator import MailboxTranslator


@pytest.fixture
def bus(tmp_path):
    db = tmp_path / "event_bus.db"
    b = EventBus(db_path=db)
    yield b
    b.close()


def _mailbox_event(bus, message_type, payload, correlation_id="corr-1"):
    return bus.emit(
        event_type=EventType.MAILBOX_MESSAGE,
        source="test",
        payload={"message_type": message_type, "from": "matcher", "to": "main",
                 "file": f"fake_{message_type}.json", "summary": "",
                 "inner_payload": payload},
        correlation_id=correlation_id,
    )


def _recent_domain_events(bus):
    rows = bus.query()
    return [(e.event_type, e.payload) for e in rows
            if e.event_type != EventType.MAILBOX_MESSAGE]


def test_score_result_emits_job_scored(bus):
    _mailbox_event(bus, "SCORE_RESULT", {
        "score": 7.2, "recommendation": "REVIEW",
        "company": "Acme", "title": "Director Finance",
    })
    MailboxTranslator(bus).poll()
    events = _recent_domain_events(bus)
    assert any(et == EventType.JOB_SCORED for et, _ in events)
    payload = next(p for et, p in events if et == EventType.JOB_SCORED)
    assert payload["score"] == 7.2
    assert payload["company"] == "Acme"


def test_score_result_high_score_double_emits(bus):
    _mailbox_event(bus, "SCORE_RESULT", {
        "score": 9.1, "recommendation": "PROCEED",
        "company": "BigCo", "title": "VP Finance",
    })
    MailboxTranslator(bus).poll()
    events = _recent_domain_events(bus)
    types = [et for et, _ in events]
    assert EventType.JOB_SCORED in types
    assert EventType.JOB_HIGH_SCORE in types


def test_batch_summary_expands_to_per_job_events(bus):
    _mailbox_event(bus, "SCORE_BATCH_SUMMARY", {
        "scored_jobs": [
            {"score": 7.0, "company": "A", "title": "X"},
            {"score": 9.0, "company": "B", "title": "Y"},
            {"score": 5.0, "company": "C", "title": "Z"},
        ],
    })
    MailboxTranslator(bus).poll()
    events = _recent_domain_events(bus)
    scored = [p for et, p in events if et == EventType.JOB_SCORED]
    high = [p for et, p in events if et == EventType.JOB_HIGH_SCORE]
    assert len(scored) == 3
    assert len(high) == 1
    assert high[0]["company"] == "B"


def test_batch_summary_real_protocol_field_results(bus):
    """Real matcher agent emits SCORE_BATCH_SUMMARY with payload.results."""
    _mailbox_event(bus, "SCORE_BATCH_SUMMARY", {
        "results": [
            {"score": 6.0, "company": "Alpha", "title": "Dir"},
            {"score": 9.2, "company": "Beta",  "title": "VP"},
        ],
    })
    MailboxTranslator(bus).poll()
    events = _recent_domain_events(bus)
    scored = [p for et, p in events if et == EventType.JOB_SCORED]
    high = [p for et, p in events if et == EventType.JOB_HIGH_SCORE]
    assert len(scored) == 2
    assert len(high) == 1
    assert high[0]["company"] == "Beta"


def test_submit_confirm_emits_application_submitted(bus):
    _mailbox_event(bus, "SUBMIT_CONFIRM",
                   {"company": "Acme", "title": "Director", "submission_id": "s1"})
    MailboxTranslator(bus).poll()
    events = _recent_domain_events(bus)
    assert any(et == EventType.APPLICATION_SUBMITTED for et, _ in events)


def test_blocked_question_emits_application_blocked(bus):
    _mailbox_event(bus, "BLOCKED_QUESTION",
                   {"company": "Acme", "title": "Director", "question": "Eligible?"})
    MailboxTranslator(bus).poll()
    events = _recent_domain_events(bus)
    assert any(et == EventType.APPLICATION_BLOCKED for et, _ in events)


def test_pipeline_update_emits_stage_transition_only_if_different(bus):
    _mailbox_event(bus, "PIPELINE_UPDATE",
                   {"job_key": "j1", "previous_stage": "discovered", "new_stage": "scored"})
    _mailbox_event(bus, "PIPELINE_UPDATE",
                   {"job_key": "j2", "previous_stage": "X", "new_stage": "X"})
    MailboxTranslator(bus).poll()
    events = _recent_domain_events(bus)
    transitions = [p for et, p in events if et == EventType.STAGE_TRANSITION]
    assert len(transitions) == 1
    assert transitions[0]["job_key"] == "j1"


def test_error_message_emits_agent_error(bus):
    _mailbox_event(bus, "ERROR",
                   {"message": "scout failed", "source_agent": "scout"})
    MailboxTranslator(bus).poll()
    events = _recent_domain_events(bus)
    assert any(et == EventType.AGENT_ERROR for et, _ in events)


def test_unknown_message_type_produces_no_domain_event(bus):
    _mailbox_event(bus, "SOME_RANDOM_TYPE", {"foo": "bar"})
    MailboxTranslator(bus).poll()
    assert _recent_domain_events(bus) == []


def test_cursor_advances_after_poll(bus):
    _mailbox_event(bus, "SCORE_RESULT", {"score": 5.0, "company": "A", "title": "B"})
    t = MailboxTranslator(bus)
    t.poll()
    pre_events = len(bus.query())
    t.poll()
    post_events = len(bus.query())
    assert post_events == pre_events


def test_notification_interview_keyword_emits_interview_signal(bus):
    _mailbox_event(bus, "NOTIFICATION", {
        "body": "Interview scheduled with Acme next Tuesday",
        "company": "Acme",
    })
    MailboxTranslator(bus).poll()
    events = _recent_domain_events(bus)
    assert any(et == EventType.INTERVIEW_SIGNAL for et, _ in events)


def test_notification_offer_keyword_emits_offer_signal(bus):
    _mailbox_event(bus, "NOTIFICATION", {
        "body": "We are pleased to offer you the Director of Finance role",
        "company": "BigCo",
    })
    MailboxTranslator(bus).poll()
    events = _recent_domain_events(bus)
    assert any(et == EventType.OFFER_SIGNAL for et, _ in events)


def test_notification_without_keyword_emits_nothing(bus):
    _mailbox_event(bus, "NOTIFICATION", {
        "body": "Weekly pipeline update: 12 jobs discovered",
    })
    MailboxTranslator(bus).poll()
    events = _recent_domain_events(bus)
    types = [et for et, _ in events]
    assert EventType.INTERVIEW_SIGNAL not in types
    assert EventType.OFFER_SIGNAL not in types
