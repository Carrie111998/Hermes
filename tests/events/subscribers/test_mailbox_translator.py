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


def _seed_translator_cursor(bus: EventBus) -> None:
    """Force MailboxTranslator's cursor to 0 so it sees mailbox_message events
    emitted BEFORE the first poll. The bus's first-registration default
    (bus.py subscribe(), 2026-04-28) jumps to head-of-bus to prevent backlog
    floods on real deploys; these tests emit before constructing the
    translator. Idempotent so safe to call before every poll site."""
    bus._execute(
        """INSERT INTO subscriber_cursors (subscriber_id, last_rowid, updated_at)
           VALUES ('mailbox-translator', 0, datetime('now'))
           ON CONFLICT(subscriber_id) DO UPDATE SET last_rowid = 0""",
    )


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
    _seed_translator_cursor(bus)
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
    _seed_translator_cursor(bus)
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
    _seed_translator_cursor(bus)
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
    _seed_translator_cursor(bus)
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
    _seed_translator_cursor(bus)
    MailboxTranslator(bus).poll()
    events = _recent_domain_events(bus)
    assert any(et == EventType.APPLICATION_SUBMITTED for et, _ in events)


def test_blocked_question_emits_application_blocked(bus):
    _mailbox_event(bus, "BLOCKED_QUESTION",
                   {"company": "Acme", "title": "Director", "question": "Eligible?"})
    _seed_translator_cursor(bus)
    MailboxTranslator(bus).poll()
    events = _recent_domain_events(bus)
    assert any(et == EventType.APPLICATION_BLOCKED for et, _ in events)


def test_pipeline_update_emits_stage_transition_only_if_different(bus):
    _mailbox_event(bus, "PIPELINE_UPDATE",
                   {"job_key": "j1", "previous_stage": "discovered", "new_stage": "scored"})
    _mailbox_event(bus, "PIPELINE_UPDATE",
                   {"job_key": "j2", "previous_stage": "X", "new_stage": "X"})
    _seed_translator_cursor(bus)
    MailboxTranslator(bus).poll()
    events = _recent_domain_events(bus)
    transitions = [p for et, p in events if et == EventType.STAGE_TRANSITION]
    assert len(transitions) == 1
    assert transitions[0]["job_key"] == "j1"


def test_pipeline_update_emits_on_first_stage_assignment(bus):
    """First assignment has previous_stage=None but IS a real transition."""
    _mailbox_event(bus, "PIPELINE_UPDATE",
                   {"job_key": "j3", "previous_stage": None, "new_stage": "discovered"})
    _seed_translator_cursor(bus)
    MailboxTranslator(bus).poll()
    events = _recent_domain_events(bus)
    transitions = [p for et, p in events if et == EventType.STAGE_TRANSITION]
    assert len(transitions) == 1
    assert transitions[0]["job_key"] == "j3"
    assert transitions[0]["new_stage"] == "discovered"


def test_pipeline_update_accepts_tailor_alias_fields(bus):
    _mailbox_event(bus, "PIPELINE_UPDATE", {
        "job_id": "48f36c8d-ad38-4bf4-aaf5-d38be28a97e3",
        "from_stage": "approved_for_tailor",
        "to_stage": "materials_ready",
        "metadata": {
            "company": "Citi",
            "title": "LMS Deposit Strategy & Analytics - NAM and LATAM Balance Sheet Lead - Director",
        },
    })
    _seed_translator_cursor(bus)
    MailboxTranslator(bus).poll()
    events = _recent_domain_events(bus)
    transitions = [p for et, p in events if et == EventType.STAGE_TRANSITION]
    assert len(transitions) == 1
    assert transitions[0]["job_key"] == "48f36c8d-ad38-4bf4-aaf5-d38be28a97e3"
    assert transitions[0]["job_id"] == "48f36c8d-ad38-4bf4-aaf5-d38be28a97e3"
    assert transitions[0]["previous_stage"] == "approved_for_tailor"
    assert transitions[0]["new_stage"] == "materials_ready"
    assert transitions[0]["company"] == "Citi"
    assert transitions[0]["title"].startswith("LMS Deposit Strategy")


def test_error_message_emits_agent_error(bus):
    _mailbox_event(bus, "ERROR",
                   {"message": "scout failed", "source_agent": "scout"})
    _seed_translator_cursor(bus)
    MailboxTranslator(bus).poll()
    events = _recent_domain_events(bus)
    assert any(et == EventType.AGENT_ERROR for et, _ in events)


class TestMailboxErrorFeedsClusterDetector:
    """ERROR mailbox messages must feed the FailureClusterDetector so that
    agents reporting failures via structured mailbox messages (rather than
    failing the cron exit code) still trigger AGENT_FAILURE_CLUSTER and the
    Critic post-hoc retro.

    Closes the gap identified in 2026-04-26-agent-failure-cluster-wiring:
    the cron-emitter path was already wired (Task 3) but the mailbox path
    was not, so any agent that swallowed its own exception and emitted a
    structured ERROR mailbox message was invisible to the cluster signal.
    """

    @pytest.fixture
    def isolated_bus(self, tmp_path, monkeypatch):
        """Bus with an isolated detector state path (not the live ~/.hermes one)."""
        state_path = tmp_path / "events" / "failure_cluster_state.json"
        monkeypatch.setattr(
            "events.subscribers.mailbox_translator.failure_cluster_state_path",
            lambda: state_path,
        )
        b = EventBus(db_path=tmp_path / "event_bus.db")
        yield b
        b.close()

    def _emit_error(self, bus, source_agent, message):
        bus.emit(
            event_type=EventType.MAILBOX_MESSAGE,
            source="test",
            payload={
                "message_type": "ERROR",
                "from": source_agent,
                "to": "main",
                "file": f"fake_error_{source_agent}.json",
                "summary": "",
                "inner_payload": {
                    "message": message,
                    "source_agent": source_agent,
                },
            },
        )

    def test_three_same_type_error_messages_emit_cluster(self, isolated_bus):
        for _ in range(3):
            self._emit_error(isolated_bus, "scout", "Bailing: CAPTCHA detected")
        _seed_translator_cursor(isolated_bus)
        MailboxTranslator(isolated_bus).poll()
        clusters = isolated_bus.query(event_type=EventType.AGENT_FAILURE_CLUSTER)
        assert len(clusters) == 1, (
            f"Expected 1 cluster from 3 same-type mailbox ERRORs; got {len(clusters)}"
        )
        evt = clusters[0]
        assert evt.payload["failure_type"] == "captcha"
        assert evt.payload["count"] == 3
        # source attribution: cluster source must be the failing agent,
        # not "mailbox:scout" (which is the bus 'source' on the AGENT_ERROR
        # emission).  The cluster signal is *about the agent*, not the
        # transport.
        assert evt.source == "scout"
        assert evt.payload["source"] == "scout"

    def test_two_same_type_errors_no_cluster(self, isolated_bus):
        for _ in range(2):
            self._emit_error(isolated_bus, "scout", "Bailing: CAPTCHA detected")
        _seed_translator_cursor(isolated_bus)
        MailboxTranslator(isolated_bus).poll()
        clusters = isolated_bus.query(event_type=EventType.AGENT_FAILURE_CLUSTER)
        assert clusters == []

    def test_three_different_types_no_cluster(self, isolated_bus):
        self._emit_error(isolated_bus, "scout", "captcha")
        self._emit_error(isolated_bus, "scout", "HTTP 401 Unauthorized")
        self._emit_error(isolated_bus, "scout", "Request timed out")
        _seed_translator_cursor(isolated_bus)
        MailboxTranslator(isolated_bus).poll()
        clusters = isolated_bus.query(event_type=EventType.AGENT_FAILURE_CLUSTER)
        assert clusters == []

    def test_different_sources_dont_cluster_together(self, isolated_bus):
        self._emit_error(isolated_bus, "scout", "captcha")
        self._emit_error(isolated_bus, "matcher", "captcha")
        self._emit_error(isolated_bus, "applier", "captcha")
        _seed_translator_cursor(isolated_bus)
        MailboxTranslator(isolated_bus).poll()
        clusters = isolated_bus.query(event_type=EventType.AGENT_FAILURE_CLUSTER)
        # No single source crossed the threshold of 3.
        assert clusters == []

    def test_existing_agent_error_still_emits(self, isolated_bus):
        """Adding the cluster wiring must not regress the existing AGENT_ERROR."""
        self._emit_error(isolated_bus, "scout", "captcha")
        _seed_translator_cursor(isolated_bus)
        MailboxTranslator(isolated_bus).poll()
        agent_errors = isolated_bus.query(event_type=EventType.AGENT_ERROR)
        assert len(agent_errors) == 1

    def _record(self, translator, source_agent, message):
        """Drive _record_error_for_clustering directly so the test does
        NOT depend on the bus subscribe/poll path.

        Why bypass poll(): bus.subscribe defaults a missing cursor to the
        current bus head (added 2026-04-28 to prevent first-registration
        backlog floods), which means events emitted BEFORE a subscriber's
        first poll are skipped.  The other tests in this class predate
        that change and currently fail on main for the same reason; they
        are out of scope for the watchdog-cluster-dedup work.  Hitting the
        method under test directly keeps the canonical-mapping coverage
        independent from that pre-existing fixture bug.
        """
        translator._record_error_for_clustering(
            outer_payload={"from": source_agent, "to": "main"},
            inner={"message": message, "source_agent": source_agent},
            correlation_id=None,
        )

    def test_long_source_agent_name_collapses_to_canonical(self, isolated_bus):
        """If an inner payload's source_agent uses a long-prefix shape
        ('jobflow-applier', 'sentinel-vip-evening'), the cluster source
        must collapse to the canonical agent identity so it dedupes
        against the cron-emitter path.

        Background: profiles/critic/workspace/watchdog-dedup-proposal-2026-04-29.md
        Option A.  Mailbox messages typically carry already-canonical short
        names, but a defensive canonical mapping prevents cluster-source
        drift if any caller ever emits a long form.
        """
        translator = MailboxTranslator(isolated_bus)
        for _ in range(3):
            self._record(translator, "jobflow-applier", "captcha")
        clusters = isolated_bus.query(event_type=EventType.AGENT_FAILURE_CLUSTER)
        assert len(clusters) == 1
        assert clusters[0].source == "applier"
        assert clusters[0].payload["source"] == "applier"

    def test_sentinel_vip_variants_share_window(self, isolated_bus):
        """Three structured ERROR mailbox messages naming
        sentinel-vip-evening, sentinel-vip-midday, sentinel-vip-morning
        must all flow into the SAME cluster window (canonical 'sentinel'),
        rather than three separate single-entry windows that never reach
        threshold."""
        translator = MailboxTranslator(isolated_bus)
        self._record(translator, "sentinel-vip-evening", "timeout")
        self._record(translator, "sentinel-vip-midday", "timed out")
        self._record(translator, "sentinel-vip-morning", "timeout")
        clusters = isolated_bus.query(event_type=EventType.AGENT_FAILURE_CLUSTER)
        assert len(clusters) == 1
        assert clusters[0].source == "sentinel"

    def test_canonical_short_name_unchanged(self, isolated_bus):
        """An already-canonical short name ('applier') must pass through
        unchanged after canonicalisation -- this is the existing happy
        path the proposal must not break."""
        translator = MailboxTranslator(isolated_bus)
        for _ in range(3):
            self._record(translator, "applier", "captcha")
        clusters = isolated_bus.query(event_type=EventType.AGENT_FAILURE_CLUSTER)
        assert len(clusters) == 1
        assert clusters[0].source == "applier"


def test_unknown_message_type_produces_no_domain_event(bus):
    _mailbox_event(bus, "SOME_RANDOM_TYPE", {"foo": "bar"})
    _seed_translator_cursor(bus)
    MailboxTranslator(bus).poll()
    assert _recent_domain_events(bus) == []


def test_cursor_advances_after_poll(bus):
    _mailbox_event(bus, "SCORE_RESULT", {"score": 5.0, "company": "A", "title": "B"})
    _seed_translator_cursor(bus)
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
    _seed_translator_cursor(bus)
    MailboxTranslator(bus).poll()
    events = _recent_domain_events(bus)
    assert any(et == EventType.INTERVIEW_SIGNAL for et, _ in events)


def test_notification_offer_keyword_emits_offer_signal(bus):
    _mailbox_event(bus, "NOTIFICATION", {
        "body": "We are pleased to offer you the Director of Finance role",
        "company": "BigCo",
    })
    _seed_translator_cursor(bus)
    MailboxTranslator(bus).poll()
    events = _recent_domain_events(bus)
    assert any(et == EventType.OFFER_SIGNAL for et, _ in events)


def test_notification_without_keyword_emits_nothing(bus):
    _mailbox_event(bus, "NOTIFICATION", {
        "body": "Weekly pipeline update: 12 jobs discovered",
    })
    _seed_translator_cursor(bus)
    MailboxTranslator(bus).poll()
    events = _recent_domain_events(bus)
    types = [et for et, _ in events]
    assert EventType.INTERVIEW_SIGNAL not in types
    assert EventType.OFFER_SIGNAL not in types
