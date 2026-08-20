"""Tests for MailboxTranslator subscriber (Silence #1 fix)."""
import json

import pytest

from events.bus import EventBus
from events.schema import EventType
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


def _translate(bus):
    """Construct a MailboxTranslator and poll from the start of the bus.

    These tests emit the mailbox_message BEFORE constructing the translator, so
    the construction-time cursor seed (at current head) would skip it. Force the
    cursor to 0 (read-from-start) so the just-emitted message is consumed.
    """
    t = MailboxTranslator(bus)
    bus._execute(
        "INSERT OR REPLACE INTO subscriber_cursors "
        "(subscriber_id, last_rowid, updated_at) VALUES (?, 0, datetime('now'))",
        (t.subscriber_id,),
    )
    return t.poll()


def test_score_result_emits_job_scored(bus):
    _mailbox_event(bus, "SCORE_RESULT", {
        "score": 7.2, "recommendation": "REVIEW",
        "company": "Acme", "title": "Director Finance",
    })
    _translate(bus)
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
    _translate(bus)
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
    _translate(bus)
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
    _translate(bus)
    events = _recent_domain_events(bus)
    scored = [p for et, p in events if et == EventType.JOB_SCORED]
    high = [p for et, p in events if et == EventType.JOB_HIGH_SCORE]
    assert len(scored) == 2
    assert len(high) == 1
    assert high[0]["company"] == "Beta"


def test_blocked_question_emits_application_blocked(bus):
    _mailbox_event(bus, "BLOCKED_QUESTION",
                   {"company": "Acme", "title": "Director", "question": "Eligible?"})
    _translate(bus)
    events = _recent_domain_events(bus)
    assert any(et == EventType.APPLICATION_BLOCKED for et, _ in events)


def test_pipeline_update_emits_stage_transition_only_if_different(bus):
    _mailbox_event(bus, "PIPELINE_UPDATE",
                   {"job_key": "j1", "previous_stage": "discovered", "new_stage": "scored"})
    _mailbox_event(bus, "PIPELINE_UPDATE",
                   {"job_key": "j2", "previous_stage": "X", "new_stage": "X"})
    _translate(bus)
    events = _recent_domain_events(bus)
    transitions = [p for et, p in events if et == EventType.STAGE_TRANSITION]
    assert len(transitions) == 1
    assert transitions[0]["job_key"] == "j1"


def test_pipeline_update_emits_on_first_stage_assignment(bus):
    """First assignment has previous_stage=None but IS a real transition."""
    _mailbox_event(bus, "PIPELINE_UPDATE",
                   {"job_key": "j3", "previous_stage": None, "new_stage": "discovered"})
    _translate(bus)
    events = _recent_domain_events(bus)
    transitions = [p for et, p in events if et == EventType.STAGE_TRANSITION]
    assert len(transitions) == 1
    assert transitions[0]["job_key"] == "j3"
    assert transitions[0]["new_stage"] == "discovered"


def test_pipeline_update_emits_prior_stage_key_for_formatter(bus):
    """The TelegramNotifier formatter reads `prior_stage` (matching the
    pipeline_state.manager producer + the '? →' symptom). The mailbox path
    must emit that canonical key, not only `previous_stage`, or the prior
    stage renders as '?'.
    """
    _mailbox_event(bus, "PIPELINE_UPDATE",
                   {"job_key": "j1", "previous_stage": "discovered", "new_stage": "scored"})
    _translate(bus)
    events = _recent_domain_events(bus)
    t = next(p for et, p in events if et == EventType.STAGE_TRANSITION)
    assert t["prior_stage"] == "discovered"
    # legacy alias kept for back-compat with any older consumer
    assert t["previous_stage"] == "discovered"


def test_pipeline_update_populates_actor_from_mailbox_sender(bus):
    """The '(by ?)' symptom: mailbox-sourced transitions never set `actor`,
    so the formatter falls back to '?'. Actor must default to the sending
    agent (mailbox 'from'), canonicalised.
    """
    _mailbox_event(bus, "PIPELINE_UPDATE",
                   {"job_key": "j1", "new_stage": "archived"})
    _translate(bus)
    events = _recent_domain_events(bus)
    t = next(p for et, p in events if et == EventType.STAGE_TRANSITION)
    assert t["actor"] == "matcher"


def test_pipeline_update_explicit_actor_wins_over_sender(bus):
    """An explicit actor in the mailbox payload takes precedence over the
    sender attribution."""
    _mailbox_event(bus, "PIPELINE_UPDATE",
                   {"job_key": "j1", "new_stage": "applied", "actor": "diego"})
    _translate(bus)
    events = _recent_domain_events(bus)
    t = next(p for et, p in events if et == EventType.STAGE_TRANSITION)
    assert t["actor"] == "diego"


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
    _translate(bus)
    events = _recent_domain_events(bus)
    transitions = [p for et, p in events if et == EventType.STAGE_TRANSITION]
    assert len(transitions) == 1
    assert transitions[0]["job_key"] == "48f36c8d-ad38-4bf4-aaf5-d38be28a97e3"
    assert transitions[0]["job_id"] == "48f36c8d-ad38-4bf4-aaf5-d38be28a97e3"
    assert transitions[0]["previous_stage"] == "approved_for_tailor"
    assert transitions[0]["new_stage"] == "materials_ready"
    assert transitions[0]["company"] == "Citi"
    assert transitions[0]["title"].startswith("LMS Deposit Strategy")


def test_pipeline_update_enriches_title_company_from_pipeline_state(bus, tmp_path, monkeypatch):
    """Matcher's batch PIPELINE_UPDATE messages carry only job_key + new_stage
    (no title/company), so the Telegram head fell back to a bare UUID. The
    translator best-effort backfills title/company from pipeline state.
    """
    import events.subscribers.mailbox_translator as mt
    pipeline = tmp_path / "pipeline.json"
    pipeline.write_text(json.dumps({"jobs": [
        {"job_id": "e0752a61", "title": "Director Finance", "company": "Acme"},
    ]}), encoding="utf-8")
    monkeypatch.setattr(mt, "PIPELINE_PATH", pipeline)

    _mailbox_event(bus, "PIPELINE_UPDATE", {"job_key": "e0752a61", "new_stage": "archived"})
    _translate(bus)
    events = _recent_domain_events(bus)
    t = next(p for et, p in events if et == EventType.STAGE_TRANSITION)
    assert t["title"] == "Director Finance"
    assert t["company"] == "Acme"


def test_pipeline_update_enriches_from_dict_keyed_pipeline_state(bus, tmp_path, monkeypatch):
    """Regression: on this host Tracker projects `jobs` as a dict keyed by
    job_id, not a list. The backfill iterated `jobs` as a list, so `for j in
    <dict>` yielded keys (strings), every `j.get()` raised, and the broad
    except returned {} — title/company never populated and the Telegram head
    stayed a bare UUID. This fixture mirrors the real dict shape.
    """
    import events.subscribers.mailbox_translator as mt
    pipeline = tmp_path / "pipeline.json"
    pipeline.write_text(json.dumps({"jobs": {
        "e0752a61": {"job_id": "e0752a61", "title": "Director Finance", "company": "Acme"},
    }}), encoding="utf-8")
    monkeypatch.setattr(mt, "PIPELINE_PATH", pipeline)

    _mailbox_event(bus, "PIPELINE_UPDATE", {"job_key": "e0752a61", "new_stage": "archived"})
    _translate(bus)
    events = _recent_domain_events(bus)
    t = next(p for et, p in events if et == EventType.STAGE_TRANSITION)
    assert t["title"] == "Director Finance"
    assert t["company"] == "Acme"


def test_pipeline_update_enrichment_does_not_override_message_fields(bus, tmp_path, monkeypatch):
    """Title/company already in the message win over pipeline-state values."""
    import events.subscribers.mailbox_translator as mt
    pipeline = tmp_path / "pipeline.json"
    pipeline.write_text(json.dumps({"jobs": [
        {"job_id": "j9", "title": "Stale Title", "company": "StaleCo"},
    ]}), encoding="utf-8")
    monkeypatch.setattr(mt, "PIPELINE_PATH", pipeline)

    _mailbox_event(bus, "PIPELINE_UPDATE", {
        "job_key": "j9", "new_stage": "applied", "title": "Fresh Title", "company": "FreshCo"})
    _translate(bus)
    events = _recent_domain_events(bus)
    t = next(p for et, p in events if et == EventType.STAGE_TRANSITION)
    assert t["title"] == "Fresh Title"
    assert t["company"] == "FreshCo"


def test_pipeline_update_enrichment_best_effort_when_no_pipeline_file(bus, tmp_path, monkeypatch):
    """Missing/unreadable pipeline state must never break emission."""
    import events.subscribers.mailbox_translator as mt
    monkeypatch.setattr(mt, "PIPELINE_PATH", tmp_path / "does_not_exist.json")

    _mailbox_event(bus, "PIPELINE_UPDATE", {"job_key": "j1", "new_stage": "scored"})
    _translate(bus)
    events = _recent_domain_events(bus)
    t = next(p for et, p in events if et == EventType.STAGE_TRANSITION)
    assert t["new_stage"] == "scored"
    assert "title" not in t and "company" not in t


def test_error_message_emits_agent_error(bus):
    _mailbox_event(bus, "ERROR",
                   {"message": "scout failed", "source_agent": "scout"})
    _translate(bus)
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
        _translate(isolated_bus)
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
        _translate(isolated_bus)
        clusters = isolated_bus.query(event_type=EventType.AGENT_FAILURE_CLUSTER)
        assert clusters == []

    def test_three_different_types_no_cluster(self, isolated_bus):
        self._emit_error(isolated_bus, "scout", "captcha")
        self._emit_error(isolated_bus, "scout", "HTTP 401 Unauthorized")
        self._emit_error(isolated_bus, "scout", "Request timed out")
        _translate(isolated_bus)
        clusters = isolated_bus.query(event_type=EventType.AGENT_FAILURE_CLUSTER)
        assert clusters == []

    def test_different_sources_dont_cluster_together(self, isolated_bus):
        self._emit_error(isolated_bus, "scout", "captcha")
        self._emit_error(isolated_bus, "matcher", "captcha")
        self._emit_error(isolated_bus, "applier", "captcha")
        _translate(isolated_bus)
        clusters = isolated_bus.query(event_type=EventType.AGENT_FAILURE_CLUSTER)
        # No single source crossed the threshold of 3.
        assert clusters == []

    def test_existing_agent_error_still_emits(self, isolated_bus):
        """Adding the cluster wiring must not regress the existing AGENT_ERROR."""
        self._emit_error(isolated_bus, "scout", "captcha")
        _translate(isolated_bus)
        agent_errors = isolated_bus.query(event_type=EventType.AGENT_ERROR)
        assert len(agent_errors) == 1

    def test_error_and_cluster_preserve_structured_diagnostics(
        self, isolated_bus,
    ):
        secret = "sk-testabcdefghijklmnop"
        inner = {
            "message": f"connection refused Authorization: Bearer {secret}",
            "source_agent": "tracker",
            "error_code": "PG_CONNECT_REFUSED",
            "phase": "postgres_sync",
            "deadline_seconds": 1800,
            "exception_type": "OperationalError",
        }
        for _ in range(3):
            isolated_bus.emit(
                event_type=EventType.MAILBOX_MESSAGE,
                source="test",
                payload={
                    "message_type": "ERROR",
                    "from": "tracker",
                    "to": "main",
                    "file": "fake_error_tracker.json",
                    "summary": "",
                    "inner_payload": inner,
                },
            )
        _translate(isolated_bus)

        agent_error = isolated_bus.query(event_type=EventType.AGENT_ERROR)[0]
        assert agent_error.payload["error_code"] == "PG_CONNECT_REFUSED"
        assert agent_error.payload["phase"] == "postgres_sync"
        assert agent_error.payload["deadline_seconds"] == 1800
        assert agent_error.payload["exception_type"] == "OperationalError"
        assert secret not in json.dumps(agent_error.payload)

        cluster = isolated_bus.query(
            event_type=EventType.AGENT_FAILURE_CLUSTER,
        )[0]
        assert cluster.payload["failure_type"] == "network"
        assert cluster.payload["count"] == 3
        assert cluster.payload["error_code"] == "PG_CONNECT_REFUSED"
        assert cluster.payload["phase"] == "postgres_sync"
        assert cluster.payload["deadline_seconds"] == 1800
        assert cluster.payload["exception_type"] == "OperationalError"
        assert secret not in cluster.payload["latest_cause"]

    def test_error_omits_nested_diagnostics_and_preserves_explicit_cause(
        self, isolated_bus,
    ):
        secret = "sk-testabcdefghijklmnop"
        inner = {
            "message": "wrapper failed",
            "latest_cause": f"connection refused Authorization: Bearer {secret}",
            "source_agent": "tracker",
            "error_code": {"token": secret},
            "phase": ["postgres_sync"],
            "deadline_seconds": [1800],
            "exception_type": {"name": "OperationalError"},
        }
        translator = MailboxTranslator(isolated_bus)
        for _ in range(3):
            translator._record_error_for_clustering(
                outer_payload={"from": "tracker", "to": "main"},
                inner=inner,
                correlation_id=None,
            )
        error_payload = translator._translate("ERROR", inner)[0][1]

        assert set(error_payload) == {"message", "source_agent", "latest_cause"}
        assert error_payload["message"] == "wrapper failed"
        assert "connection refused" in error_payload["latest_cause"]
        assert secret not in json.dumps(error_payload)

        cluster = isolated_bus.query(
            event_type=EventType.AGENT_FAILURE_CLUSTER,
        )[0].payload
        assert "connection refused" in cluster["latest_cause"]
        assert "wrapper failed" not in cluster["latest_cause"]
        assert secret not in json.dumps(cluster)

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


class TestScoreEventDedup:
    """The matcher writes BOTH a per-job SCORE_RESULT file AND a
    SCORE_BATCH_SUMMARY whose results[] repeats the same job, so every
    scored job produced two JOB_SCORED (and two JOB_HIGH_SCORE when
    >= threshold) bus events ~60ms apart — one full payload, one with
    dimensions:null (the batch rows carry no dimensions). Observed on
    the live bus 2026-07-10..07-18 (e.g. Amex 'Director – Treasury
    Deposits' 2026-07-12 14:14:03.98 + 14:14:04.04). The translator —
    the sole producer of these event types — must dedupe by job
    identity + score within a window.
    """

    def test_score_result_then_batch_summary_same_job_emits_once(self, bus):
        _mailbox_event(bus, "SCORE_RESULT", {
            "job_id": "4388377347", "score": 9.2, "recommendation": "PROCEED",
            "company": "JPMC", "title": "FRM Lead",
            "dimensions": {"title_match": {"raw": 10.0}},
        })
        _mailbox_event(bus, "SCORE_BATCH_SUMMARY", {
            "results": [
                {"job_id": "4388377347", "score": 9.2,
                 "recommendation": "PROCEED", "company": "JPMC",
                 "title": "FRM Lead"},
            ],
        }, correlation_id="corr-2")
        _translate(bus)
        events = _recent_domain_events(bus)
        scored = [p for et, p in events if et == EventType.JOB_SCORED]
        high = [p for et, p in events if et == EventType.JOB_HIGH_SCORE]
        assert len(scored) == 1
        assert len(high) == 1
        # First (full) emission wins — dimensions survive.
        assert high[0]["dimensions"] is not None

    def test_batch_summary_then_score_result_same_job_emits_once(self, bus):
        """Real 2026-07-18 14:12 order: the batch summary lands first."""
        _mailbox_event(bus, "SCORE_BATCH_SUMMARY", {
            "results": [
                {"job_id": "b3760968", "score": 8.8, "company": "JPMC",
                 "title": "Quant Treasury"},
            ],
        })
        _mailbox_event(bus, "SCORE_RESULT", {
            "job_id": "b3760968", "score": 8.8, "company": "JPMC",
            "title": "Quant Treasury",
            "dimensions": {"title_match": {"raw": 9.0}},
        }, correlation_id="corr-2")
        _translate(bus)
        events = _recent_domain_events(bus)
        assert len([1 for et, _ in events if et == EventType.JOB_SCORED]) == 1
        assert len([1 for et, _ in events if et == EventType.JOB_HIGH_SCORE]) == 1

    def test_dedup_spans_polls_of_same_translator(self, bus):
        """The straggler shape (Transamerica 2026-07-11: third duplicate
        3 minutes after the pair) arrives in a LATER poll cycle."""
        _mailbox_event(bus, "SCORE_RESULT", {
            "job_id": "j1", "score": 9.1, "company": "A", "title": "T"})
        t = MailboxTranslator(bus)
        bus._execute(
            "INSERT OR REPLACE INTO subscriber_cursors "
            "(subscriber_id, last_rowid, updated_at) VALUES (?, 0, datetime('now'))",
            (t.subscriber_id,),
        )
        t.poll()
        _mailbox_event(bus, "HIGH_SCORE_ALERT", {
            "job_id": "j1", "score": 9.1, "company": "A", "title": "T"},
            correlation_id="corr-2")
        t.poll()
        events = _recent_domain_events(bus)
        assert len([1 for et, _ in events if et == EventType.JOB_HIGH_SCORE]) == 1

    def test_different_jobs_same_score_both_emit(self, bus):
        _mailbox_event(bus, "SCORE_BATCH_SUMMARY", {
            "results": [
                {"job_id": "j1", "score": 9.0, "company": "Central Bank", "title": "ALM"},
                {"job_id": "j2", "score": 9.0, "company": "BlackRock", "title": "Head of AI"},
            ],
        })
        _translate(bus)
        events = _recent_domain_events(bus)
        assert len([1 for et, _ in events if et == EventType.JOB_HIGH_SCORE]) == 2

    def test_rescore_with_different_score_still_emits(self, bus):
        """A changed score is new information, never suppressed."""
        _mailbox_event(bus, "SCORE_RESULT", {
            "job_id": "j1", "score": 9.0, "company": "A", "title": "T"})
        _mailbox_event(bus, "SCORE_RESULT", {
            "job_id": "j1", "score": 9.3, "company": "A", "title": "T"},
            correlation_id="corr-2")
        _translate(bus)
        events = _recent_domain_events(bus)
        assert len([1 for et, _ in events if et == EventType.JOB_HIGH_SCORE]) == 2

    def test_dedup_falls_back_to_company_title_without_ids(self, bus):
        _mailbox_event(bus, "SCORE_RESULT", {
            "score": 9.0, "company": "Acme", "title": "VP"})
        _mailbox_event(bus, "SCORE_RESULT", {
            "score": 9.0, "company": "Acme", "title": "VP"},
            correlation_id="corr-2")
        _translate(bus)
        events = _recent_domain_events(bus)
        assert len([1 for et, _ in events if et == EventType.JOB_HIGH_SCORE]) == 1

    def test_window_expiry_allows_reemission(self, bus, monkeypatch):
        """A genuine rescore in a later matcher run (observed 2h apart)
        must emit again once the window has passed."""
        import events.subscribers.mailbox_translator as mt
        monkeypatch.setattr(mt, "SCORE_DEDUP_WINDOW_SECONDS", 0.0)
        _mailbox_event(bus, "SCORE_RESULT", {
            "job_id": "j1", "score": 9.0, "company": "A", "title": "T"})
        _mailbox_event(bus, "SCORE_RESULT", {
            "job_id": "j1", "score": 9.0, "company": "A", "title": "T"},
            correlation_id="corr-2")
        _translate(bus)
        events = _recent_domain_events(bus)
        assert len([1 for et, _ in events if et == EventType.JOB_HIGH_SCORE]) == 2


def test_score_payload_carries_job_id_and_backfills_job_key(bus):
    """The matcher sends `job_id`, not `job_key` — every score event on the
    live bus had job_key=None and an empty bus job_id column, defeating any
    downstream keying. The payload must carry job_id and backfill job_key."""
    _mailbox_event(bus, "SCORE_RESULT", {
        "job_id": "4388377347", "score": 7.0, "company": "JPMC", "title": "Lead"})
    _translate(bus)
    events = _recent_domain_events(bus)
    p = next(p for et, p in events if et == EventType.JOB_SCORED)
    assert p["job_id"] == "4388377347"
    assert p["job_key"] == "4388377347"
    row = next(e for e in bus.query()
               if e.event_type == EventType.JOB_SCORED)
    assert row.job_id == "4388377347"


def test_unknown_message_type_produces_no_domain_event(bus):
    _mailbox_event(bus, "SOME_RANDOM_TYPE", {"foo": "bar"})
    _translate(bus)
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
    _translate(bus)
    events = _recent_domain_events(bus)
    assert any(et == EventType.INTERVIEW_SIGNAL for et, _ in events)


def test_notification_offer_keyword_emits_offer_signal(bus):
    _mailbox_event(bus, "NOTIFICATION", {
        "body": "We are pleased to offer you the Director of Finance role",
        "company": "BigCo",
    })
    _translate(bus)
    events = _recent_domain_events(bus)
    assert any(et == EventType.OFFER_SIGNAL for et, _ in events)


def test_notification_without_keyword_emits_nothing(bus):
    _mailbox_event(bus, "NOTIFICATION", {
        "body": "Weekly pipeline update: 12 jobs discovered",
    })
    _translate(bus)
    events = _recent_domain_events(bus)
    types = [et for et, _ in events]
    assert EventType.INTERVIEW_SIGNAL not in types
    assert EventType.OFFER_SIGNAL not in types


class TestBlockedQuestionLegacyEnvelopeBackstop:
    """The translator must salvage BLOCKED_QUESTION envelopes that predate the
    producer-side contract fix.

    Every one of the 38 `application_blocked` events on the live bus between
    2026-07-20 19:14:29 and 2026-08-19 01:06:40 carried `payload={}`: the
    applier writes BLOCKED_QUESTION straight into ~/.hermes/mailbox/main/inbox
    with keys `job_id`/`api_job_id`/`questions`/`failureMessage`, and
    `_copy_fields(inner, ["company","title","job_key","question"])` drops every
    key whose value is None. The CRITICAL WhatsApp page therefore rendered
    "Application blocked at ?: needs your input"
    (whatsapp_escalator.py:379) and the Telegram summary was the bare literal
    "Blocked question" (MailboxWatcher._summarize reads payload["question"]).

    The producer is fixed separately; this is the backstop, so a stale producer,
    a replayed envelope, or a third emitter can never re-empty the payload.
    """

    # Shape copied from a real envelope,
    # mailbox/main/processed/20260819T010544_BLOCKED_QUESTION_applier_f9e6d24a.json
    # (identifiers zero-filled -- a real job UUID under an `api_*` key trips
    # the gitleaks generic-api-key rule on entropy).
    def _legacy(self, **over):
        payload = {
            "job_id": "bcff95e9-c08e-40f4-ac5d-e32c4f947d0e",
            "api_job_id": "56874381-0000-0000-0000-000000000000",
            "attempt_id": "applier-dry-run-20260819T010048Z-56874381",
            "questions": [
                {"label": "First Name*", "type": "text", "selector": "#first_name"},
                {"label": "Email*", "type": "text", "selector": "#email"},
            ],
            "failureMessage": (
                "Greenhouse still has unanswered required fields after standard "
                "profile and document filling."
            ),
            "screenshots": [],
            "artifacts": [],
        }
        payload.update(over)
        return payload

    def _blocked(self, bus):
        events = _recent_domain_events(bus)
        return next(p for et, p in events if et == EventType.APPLICATION_BLOCKED)

    def test_question_is_built_from_the_questions_list(self, bus):
        _mailbox_event(bus, "BLOCKED_QUESTION", self._legacy())
        _translate(bus)
        question = self._blocked(bus)["question"]
        assert "First Name*" in question
        assert "Email*" in question

    def test_job_key_is_aliased_from_job_id(self, bus):
        _mailbox_event(bus, "BLOCKED_QUESTION", self._legacy())
        _translate(bus)
        assert self._blocked(bus)["job_key"] == "bcff95e9-c08e-40f4-ac5d-e32c4f947d0e"

    def test_job_key_falls_back_to_api_job_id(self, bus):
        _mailbox_event(bus, "BLOCKED_QUESTION", self._legacy(job_id=None))
        _translate(bus)
        assert self._blocked(bus)["job_key"] == "56874381-0000-0000-0000-000000000000"

    def test_question_falls_back_to_failure_message(self, bus):
        _mailbox_event(bus, "BLOCKED_QUESTION", self._legacy(questions=[]))
        _translate(bus)
        assert "unanswered required fields" in self._blocked(bus)["question"]

    def test_question_is_never_absent_even_for_an_empty_envelope(self, bus):
        """`_summarize` and the WhatsApp page both key on `question`; a missing
        key is what produced "needs your input" for a month."""
        _mailbox_event(bus, "BLOCKED_QUESTION", {})
        _translate(bus)
        assert self._blocked(bus)["question"].strip()

    def test_question_is_truncated_to_the_summary_budget(self, bus):
        _mailbox_event(bus, "BLOCKED_QUESTION", self._legacy(questions=[
            {"label": "Q%d %s" % (i, "x" * 40)} for i in range(20)
        ]))
        _translate(bus)
        assert len(self._blocked(bus)["question"]) <= 200

    def test_company_and_title_are_backfilled_from_pipeline_state(
        self, bus, tmp_path, monkeypatch
    ):
        import events.subscribers.mailbox_translator as mt
        pipeline = tmp_path / "pipeline.json"
        pipeline.write_text(json.dumps({"jobs": [
            {"job_id": "bcff95e9-c08e-40f4-ac5d-e32c4f947d0e",
             "title": "Director Finance", "company": "Petra Funds Group"},
        ]}), encoding="utf-8")
        monkeypatch.setattr(mt, "PIPELINE_PATH", pipeline)

        _mailbox_event(bus, "BLOCKED_QUESTION", self._legacy())
        _translate(bus)
        out = self._blocked(bus)
        assert out["company"] == "Petra Funds Group"
        assert out["title"] == "Director Finance"

    def test_backfill_is_best_effort_when_pipeline_state_is_missing(
        self, bus, tmp_path, monkeypatch
    ):
        import events.subscribers.mailbox_translator as mt
        monkeypatch.setattr(mt, "PIPELINE_PATH", tmp_path / "nope.json")
        _mailbox_event(bus, "BLOCKED_QUESTION", self._legacy())
        _translate(bus)
        assert self._blocked(bus)["question"]

    def test_a_wellformed_envelope_is_passed_through_untouched(self, bus):
        """The fixed producer already emits all four keys — the backstop must
        not rewrite what it sends."""
        _mailbox_event(bus, "BLOCKED_QUESTION", self._legacy(
            job_key="ext-1", company="Acme Corp", title="VP Finance",
            question="The ATS dry run needs answers: Work authorization?",
        ))
        _translate(bus)
        out = self._blocked(bus)
        assert out["job_key"] == "ext-1"
        assert out["company"] == "Acme Corp"
        assert out["title"] == "VP Finance"
        assert out["question"] == "The ATS dry run needs answers: Work authorization?"

    def test_the_rendered_whatsapp_page_head_is_no_longer_the_placeholder(self, bus):
        """Mirrors whatsapp_escalator.py:379 exactly."""
        _mailbox_event(bus, "BLOCKED_QUESTION", self._legacy(
            company="Petra Funds Group",
        ))
        _translate(bus)
        out = self._blocked(bus)
        text = "Application blocked at %s: %s" % (
            out.get("company", "?"), out.get("question", "needs your input"))
        assert text != "Application blocked at ?: needs your input"
        assert text.startswith("Application blocked at Petra Funds Group: ")
        # Not just the company — the tail must be the real question, never the
        # "needs your input" default that stood in for a month.
        assert "needs your input" not in text
        assert "First Name*" in text


# ---------------------------------------------------------------------------
# The BLOCKED_QUESTION empty-payload defect was one instance of a family: every
# mailbox producer speaks job_id / files / questions, while the translator's
# _copy_fields contracts ask for job_key / artifacts / question, and _copy_fields
# drops any key whose value is None. Measured on the live bus 2026-08-19:
#   application_ready   6 events, ALL payload {}
#   followup_due        3 events, ALL payload {}
#   tailor_completed   97 events, payload ['company','title'] only
# ---------------------------------------------------------------------------

_PIPELINE = {"jobs": [
    {"job_id": "8446590b", "title": "Reporting Director", "company": "Deloitte"},
    {"job_id": "8de4877d", "title": "Director of Strategic Finance", "company": "Tala"},
]}


@pytest.fixture
def pipeline_state(tmp_path, monkeypatch):
    import events.subscribers.mailbox_translator as mt
    path = tmp_path / "pipeline.json"
    path.write_text(json.dumps(_PIPELINE), encoding="utf-8")
    monkeypatch.setattr(mt, "PIPELINE_PATH", path)
    return path


class TestApplicationReadyIdentity:
    """The applier's DRY_RUN_COMPLETE carries job_id/api_job_id/screenshots,
    so all four contract keys were dropped and the HIGH "approve?" page read
    "Dry-run complete for ? . Approve submission? Reply YES or NO."
    (whatsapp_escalator.py:383) -- Diego asked to approve an UNNAMED job.

    All six live `application_ready` events came from SUBMIT_REQUEST, which no
    longer produces this event at all: it is a command, not an outcome (see
    TestSubmitRequestIsNotAnApprovalPrompt). DRY_RUN_COMPLETE is the truthful
    producer, and it has the same broken vocabulary, so the identity repair
    these tests cover is what makes the lane usable when the first clean dry
    run lands.
    """

    def _ready(self, bus):
        return next(p for et, p in _recent_domain_events(bus)
                    if et == EventType.APPLICATION_READY)

    def test_job_key_is_aliased_from_job_id(self, bus, pipeline_state):
        _mailbox_event(bus, "DRY_RUN_COMPLETE", {
            "job_id": "8446590b", "status": "review_reached",
            "review_url": "https://x", "approval_required": True})
        _translate(bus)
        assert self._ready(bus)["job_key"] == "8446590b"

    def test_company_and_title_backfilled_from_pipeline_state(self, bus, pipeline_state):
        _mailbox_event(bus, "DRY_RUN_COMPLETE", {"job_id": "8446590b"})
        _translate(bus)
        out = self._ready(bus)
        assert out["company"] == "Deloitte"
        assert out["title"] == "Reporting Director"

    def test_dry_run_complete_aliases_screenshots_to_artifacts(self, bus, pipeline_state):
        """The applier's DRY_RUN_COMPLETE has no `files`; its evidence is
        `screenshots` plus an already-correct `artifacts`."""
        _mailbox_event(bus, "DRY_RUN_COMPLETE", {
            "job_id": "8de4877d", "api_job_id": "api-1",
            "screenshots": ["04_landing.png"], "review_url": "https://x"})
        _translate(bus)
        assert self._ready(bus)["artifacts"] == ["04_landing.png"]

    def test_producer_artifacts_win_over_the_alias(self, bus, pipeline_state):
        _mailbox_event(bus, "DRY_RUN_COMPLETE", {
            "job_id": "8de4877d", "artifacts": [{"type": "trace"}],
            "screenshots": ["shot.png"]})
        _translate(bus)
        assert self._ready(bus)["artifacts"] == [{"type": "trace"}]

    def test_job_key_falls_back_to_api_job_id(self, bus, pipeline_state):
        _mailbox_event(bus, "DRY_RUN_COMPLETE", {"api_job_id": "api-only-7"})
        _translate(bus)
        assert self._ready(bus)["job_key"] == "api-only-7"

    def test_the_rendered_approval_page_names_the_job(self, bus, pipeline_state):
        """Mirrors whatsapp_escalator.py:383 exactly."""
        _mailbox_event(bus, "DRY_RUN_COMPLETE", {"job_id": "8de4877d"})
        _translate(bus)
        p = self._ready(bus)
        text = (f"Dry-run complete for {p.get('company', '?')} "
                f"{p.get('title', '')}. Approve submission? Reply YES or NO.")
        assert text.startswith(
            "Dry-run complete for Tala Director of Strategic Finance.")


class TestFollowupDueFanOut:
    """FOLLOWUP_ALERT is a BATCH -- a `jobs` (or `candidates`) list plus a
    count -- while the translator expected one job per message, so every key
    was dropped and one useless "Follow-up due for ? -- 14+ days" page was
    emitted per SCAN rather than one per job.

    Aliasing alone cannot fix this; it needs fan-out. All three real shapes
    seen on the bus use a DIFFERENT per-job day key: days_since (2026-08-07),
    days_inactive (08-08), days_since_stage (08-09).
    """

    def _due(self, bus):
        return [p for et, p in _recent_domain_events(bus)
                if et == EventType.FOLLOWUP_DUE]

    def test_a_batch_emits_one_event_per_job(self, bus):
        _mailbox_event(bus, "FOLLOWUP_ALERT", {
            "checked_at": "2026-08-07T14:06:06Z", "cutoff_days": 14,
            "jobs": [
                {"job_id": "j1", "company": "JPMorganChase", "title": "TMO",
                 "days_since": 120},
                {"job_id": "j2", "company": "Affirm", "title": "VP Treasury",
                 "days_since": 120},
                {"job_id": "j3", "company": "Raptor", "title": "VP FP&A",
                 "days_since": 122},
            ]})
        _translate(bus)
        assert len(self._due(bus)) == 3

    def test_each_fanned_out_event_identifies_its_own_job(self, bus):
        _mailbox_event(bus, "FOLLOWUP_ALERT", {"jobs": [
            {"job_id": "j1", "company": "JPMorganChase", "title": "TMO",
             "days_since": 120},
            {"job_id": "j2", "company": "Affirm", "title": "VP Treasury",
             "days_since": 99},
        ]})
        _translate(bus)
        by_company = {p["company"]: p for p in self._due(bus)}
        assert by_company["Affirm"]["job_key"] == "j2"
        assert by_company["Affirm"]["title"] == "VP Treasury"
        assert by_company["JPMorganChase"]["job_key"] == "j1"

    def test_the_candidates_key_fans_out_too(self, bus):
        """The 2026-08-08 tracker shape uses `candidates`, not `jobs`."""
        _mailbox_event(bus, "FOLLOWUP_ALERT", {
            "total_candidates": 2, "threshold_days": 14,
            "candidates": [
                {"job_id": "c1", "company": "StoneX Group Inc.",
                 "title": "Head of Cash", "days_inactive": 26},
                {"job_id": "c2", "company": "Morgan Stanley",
                 "title": "Lending Lead", "days_inactive": 24},
            ]})
        _translate(bus)
        assert {p["company"] for p in self._due(bus)} == {
            "StoneX Group Inc.", "Morgan Stanley"}

    @pytest.mark.parametrize("key", [
        "days_since", "days_inactive", "days_since_stage",
        "days_since_application"])
    def test_days_is_populated_from_every_real_day_key(self, bus, key):
        """BOTH renderers read payload['days'] (whatsapp_escalator.py:393,
        digest_composer.py:312), NOT days_since_application -- so even a
        producer honouring the old contract rendered the bare default 14."""
        _mailbox_event(bus, "FOLLOWUP_ALERT", {"jobs": [
            {"job_id": "j1", "company": "Tala", "title": "Dir", key: 21}]})
        _translate(bus)
        assert self._due(bus)[0]["days"] == 21

    def test_days_falls_back_to_the_batch_threshold(self, bus):
        _mailbox_event(bus, "FOLLOWUP_ALERT", {
            "threshold_days": 30,
            "jobs": [{"job_id": "j1", "company": "Tala", "title": "Dir"}]})
        _translate(bus)
        assert self._due(bus)[0]["days"] == 30

    def test_an_empty_batch_emits_nothing(self, bus):
        """A scan that found nothing due must not page. The old code emitted
        one payload-{} event for it."""
        _mailbox_event(bus, "FOLLOWUP_ALERT",
                       {"checked_at": "2026-08-09T14:10:00Z",
                        "due_count": 0, "jobs": []})
        _translate(bus)
        assert self._due(bus) == []

    def test_a_batchless_envelope_still_emits_one_event(self, bus):
        """Back-compat: a producer with neither `jobs` nor `candidates` keeps
        the single-event behaviour rather than going silent."""
        _mailbox_event(bus, "FOLLOWUP_ALERT",
                       {"job_key": "j9", "company": "Tala",
                        "days_since_application": 15})
        _translate(bus)
        due = self._due(bus)
        assert len(due) == 1
        assert due[0]["company"] == "Tala"

    def test_company_and_title_backfilled_per_job(self, bus, pipeline_state):
        _mailbox_event(bus, "FOLLOWUP_ALERT",
                       {"jobs": [{"job_id": "8de4877d", "days_since": 20}]})
        _translate(bus)
        out = self._due(bus)[0]
        assert out["company"] == "Tala"
        assert out["title"] == "Director of Strategic Finance"

    def test_the_rendered_followup_page_names_job_and_days(self, bus):
        """Mirrors whatsapp_escalator.py:393 exactly."""
        _mailbox_event(bus, "FOLLOWUP_ALERT", {"jobs": [
            {"job_id": "j1", "company": "Deloitte", "title": "Dir",
             "days_since": 120}]})
        _translate(bus)
        p = self._due(bus)[0]
        text = (f"Follow-up due for {p.get('company', '?')} "
                f"\u2014 {p.get('days', 14)}+ days no response")
        assert text == "Follow-up due for Deloitte \u2014 120+ days no response"


class TestTailorCompletedIdentity:
    """97 tailor_completed events carry company/title but no job_key and no
    artifacts: TAILOR_COMPLETE speaks `job_id` and `files`. Without job_key the
    bus `job_id` column is NULL, so the event cannot be correlated to its job.
    """

    def _tailored(self, bus):
        return next(p for et, p in _recent_domain_events(bus)
                    if et == EventType.TAILOR_COMPLETED)

    def _envelope(self):
        return {
            "job_id": "c0207191", "company": "VirtualVocations",
            "title": "Deputy Chief Data Scientist",
            "materials_path": "C:/x/c0207191",
            "files": ["resume.pdf", "cover-letter.pdf", "metadata.json"],
            "submission_authorized": False,
        }

    def test_job_key_is_aliased_from_job_id(self, bus):
        _mailbox_event(bus, "TAILOR_COMPLETE", self._envelope())
        _translate(bus)
        assert self._tailored(bus)["job_key"] == "c0207191"

    def test_artifacts_are_aliased_from_files(self, bus):
        _mailbox_event(bus, "TAILOR_COMPLETE", self._envelope())
        _translate(bus)
        assert self._tailored(bus)["artifacts"] == [
            "resume.pdf", "cover-letter.pdf", "metadata.json"]

    def test_producer_company_and_title_are_not_overwritten(self, bus, pipeline_state):
        """The tailor DOES send company/title; pipeline state must not win."""
        env = self._envelope()
        env["job_id"] = "8de4877d"  # resolves to Tala in pipeline_state
        _mailbox_event(bus, "TAILOR_COMPLETE", env)
        _translate(bus)
        out = self._tailored(bus)
        assert out["company"] == "VirtualVocations"
        assert out["title"] == "Deputy Chief Data Scientist"

    def test_the_bus_job_id_column_is_populated(self, bus):
        """emit() keys the job_id column off job_key -- that is what makes the
        event correlatable at all."""
        _mailbox_event(bus, "TAILOR_COMPLETE", self._envelope())
        _translate(bus)
        row = next(e for e in bus.query()
                   if e.event_type == EventType.TAILOR_COMPLETED)
        assert row.job_id == "c0207191"


class TestSubmitRequestIsNotAnApprovalPrompt:
    """SUBMIT_REQUEST is a COMMAND (main -> applier), not an outcome.

    Its payload is a dry-run *request* -- `mode: dry_run`,
    `submission_authorized: false`, and an `applier-dry-run:` idempotency key
    (jobflow_dispatch/contracts.py:50-53). Mapping it to APPLICATION_READY
    fired the HIGH ACT/ACTION_REQUIRED page
    "Dry-run complete for X. Approve submission? Reply YES or NO."
    (whatsapp_escalator.py:383) at the moment the dry run was REQUESTED --
    before the applier had run anything. Repairing the payload only made that
    claim well-named instead of anonymous; the claim itself is false.

    APPLICATION_READY is a _PENDING_EVENT_TYPE (events/outcomes.py:77) meaning
    "waiting on Diego". A just-issued SUBMIT_REQUEST is waiting on the APPLIER.
    The applier's own DRY_RUN_COMPLETE is the truthful producer, and it is
    already mirrored (mailbox_watcher.py MIRRORED_MESSAGE_TYPES) and already
    handled by the same branch.
    """

    def test_submit_request_emits_no_domain_event(self, bus, pipeline_state):
        _mailbox_event(bus, "SUBMIT_REQUEST", {
            "job_id": "8446590b", "materials_path": "C:/x",
            "mode": "dry_run", "submission_authorized": False})
        _translate(bus)
        assert _recent_domain_events(bus) == []

    def test_the_applier_dry_run_still_pages_for_approval(self, bus, pipeline_state):
        """Removing the command must not silence the real approval prompt."""
        _mailbox_event(bus, "DRY_RUN_COMPLETE", {
            "job_id": "8446590b", "status": "review_reached",
            "approval_required": True})
        _translate(bus)
        ready = [p for et, p in _recent_domain_events(bus)
                 if et == EventType.APPLICATION_READY]
        assert len(ready) == 1
        assert ready[0]["company"] == "Deloitte"


class TestSubmitResultIsTranslated:
    """The applier reports every real submission as SUBMIT_RESULT
    (tmp_ready_sweep_cron.py:811,834) and the translator had no branch for it,
    so the only `application_submitted` event ever on the bus was a synthetic
    Mission-Control drill. SUBMIT_CONFIRM -- the branch that DID exist -- is
    Diego's go-ahead, not an outcome.

    APPLICATION_SUBMITTED is a _SUCCESS_EVENT_TYPE and APPLICATION_FAILED a
    CRITICAL failure (events/outcomes.py), so the two SUBMIT_RESULT statuses
    must not collapse into one event type.
    """

    def _only(self, bus):
        _translate(bus)
        events = _recent_domain_events(bus)
        assert len(events) == 1, events
        return events[0]

    def _submitted(self):
        return {
            "job_id": "8de4877d", "api_job_id": "api-9",
            "status": "submitted", "confirmation_id": "CONF-123",
            "error": None, "attempt_id": "att-1",
            "screenshots": ["09_confirmation.png"],
            "artifacts": ["09_confirmation.png", "dom.html"],
        }

    def test_a_successful_submit_emits_application_submitted(self, bus):
        _mailbox_event(bus, "SUBMIT_RESULT", self._submitted())
        et, _ = self._only(bus)
        assert et == EventType.APPLICATION_SUBMITTED

    def test_the_confirmation_id_becomes_the_submission_id(self, bus):
        _mailbox_event(bus, "SUBMIT_RESULT", self._submitted())
        assert self._only(bus)[1]["submission_id"] == "CONF-123"

    def test_it_carries_job_identity_and_artifacts(self, bus):
        _mailbox_event(bus, "SUBMIT_RESULT", self._submitted())
        p = self._only(bus)[1]
        assert p["job_key"] == "8de4877d"
        assert p["artifacts"] == ["09_confirmation.png", "dom.html"]

    def test_company_and_title_are_backfilled(self, bus, pipeline_state):
        _mailbox_event(bus, "SUBMIT_RESULT", self._submitted())
        p = self._only(bus)[1]
        assert p["company"] == "Tala"
        assert p["title"] == "Director of Strategic Finance"

    def test_a_failed_submit_emits_application_failed_not_submitted(self, bus):
        """Success and failure share the message type; only `status` differs.
        Treating both as APPLICATION_SUBMITTED would book a failure as a
        _SUCCESS_EVENT_TYPE."""
        _mailbox_event(bus, "SUBMIT_RESULT", {
            "job_id": "8de4877d", "status": "failed",
            "error": "Browser submit failed", "failureCode": "SUBMIT_FAILED",
            "confirmation_id": None})
        et, p = self._only(bus)
        assert et == EventType.APPLICATION_FAILED
        assert p["error"] == "Browser submit failed"

    def test_the_rendered_failure_page_names_job_and_reason(self, bus, pipeline_state):
        """Mirrors whatsapp_escalator.py:381 exactly."""
        _mailbox_event(bus, "SUBMIT_RESULT", {
            "job_id": "8de4877d", "status": "failed",
            "error": "Browser submit failed"})
        p = self._only(bus)[1]
        text = (f"Application failed for {p.get('company', '?')}: "
                f"{p.get('error', 'unknown error')}")
        assert text == "Application failed for Tala: Browser submit failed"

    def test_the_bus_job_id_column_is_populated(self, bus):
        _mailbox_event(bus, "SUBMIT_RESULT", self._submitted())
        _translate(bus)
        row = next(e for e in bus.query()
                   if e.event_type == EventType.APPLICATION_SUBMITTED)
        assert row.job_id == "8de4877d"


class TestSubmitConfirmIsNotAnOutcome:
    """SUBMIT_CONFIRM is Diego's AUTHORIZATION, not an accomplished submission.

    "Dry-run -> Jaum review -> SUBMIT_CONFIRM -> actual submit"
    (curator/orchestrator.py:151) places it strictly BEFORE the applier acts,
    and protocol.md:49 gives its whole payload as `{job_id, approved: true}`.
    Mapping it to APPLICATION_SUBMITTED -- a _SUCCESS_EVENT_TYPE
    (events/outcomes.py:89) -- booked the job as a completed success the
    instant Diego said yes. The damage is durable, not cosmetic:
    memory_writer.py:186 appends "Application submitted for <title> at
    <company>" to that job's GBrain timeline, which is append-only, and
    digest_composer.py:371 counts it in the daily "Applier: N submitted" line.

    Since a4e6ade172 the truthful producer of that event type is SUBMIT_RESULT
    with `status == "submitted"`, so keeping this branch made one authorized
    application emit APPLICATION_SUBMITTED TWICE -- once falsely at the
    go-ahead, once truthfully at the report.

    Removed rather than repointed at an "authorization received" event type:
    Diego AUTHORS SUBMIT_CONFIRM (SOUL.md:58; he clicks Approve --
    profiles/applier/workspace/WORKFLOW.md:73), so such an event would page
    him about his own click. Nothing is lost -- the message still reaches the
    bus as `mailbox_message` for audit, with its own `_summarize` case
    (mailbox_watcher.py:282), and the dispatch route that wakes the applier is
    a separate table (jobflow_dispatch/contracts.py:55).
    """

    def test_submit_confirm_emits_no_domain_event(self, bus, pipeline_state):
        """The real payload the protocol defines."""
        _mailbox_event(bus, "SUBMIT_CONFIRM",
                       {"job_id": "8de4877d", "approved": True})
        _translate(bus)
        assert _recent_domain_events(bus) == []

    def test_a_confirm_naming_the_job_is_still_not_a_submission(self, bus):
        """The removed branch keyed on exactly these fields, so a producer
        that started sending them must not resurrect the false success."""
        _mailbox_event(bus, "SUBMIT_CONFIRM", {
            "company": "Acme", "title": "Director", "job_key": "8de4877d",
            "submission_id": "s1"})
        _translate(bus)
        assert _recent_domain_events(bus) == []

    def test_one_authorized_application_emits_one_submitted_event(
        self, bus, pipeline_state
    ):
        """The whole authorized lane: Diego confirms, the applier reports.
        Two producers for one _SUCCESS_EVENT_TYPE double-counted the digest
        and wrote the GBrain timeline entry twice."""
        _mailbox_event(bus, "SUBMIT_CONFIRM",
                       {"job_id": "8de4877d", "approved": True})
        _mailbox_event(bus, "SUBMIT_RESULT", {
            "job_id": "8de4877d", "status": "submitted",
            "confirmation_id": "CONF-123"})
        _translate(bus)
        submitted = [p for et, p in _recent_domain_events(bus)
                     if et == EventType.APPLICATION_SUBMITTED]
        assert len(submitted) == 1
        assert submitted[0]["submission_id"] == "CONF-123"
        assert submitted[0]["company"] == "Tala"


class TestBlockedQuestionOptions:
    """`options` must reach Diego independently of the 200-char `question`.

    A Workday listbox answer is matched against the tenant's own option text,
    so an answer that is not verbatim one of those labels is never clicked and
    the run re-stalls looking exactly like nobody answered. The applier emits
    the labels (`question_options`, 2026-08-20) and protocol.md documents the
    key, but the translator copied only company/title/job_key/question -- so
    the only way through was to inline them in `question`, where the real
    Capital One list measured exactly 200 chars with zero margin.
    """

    # The Capital One list, from the applier-side helper's docstring.
    OPTIONS = [
        "Internet",
        "Contacted by Recruiter",
        "Job Fair",
        "Employee Referral",
    ]

    def _blocked(self, bus):
        events = _recent_domain_events(bus)
        return next(p for et, p in events if et == EventType.APPLICATION_BLOCKED)

    def test_flat_options_key_is_carried_through(self, bus):
        _mailbox_event(bus, "BLOCKED_QUESTION", {
            "job_key": "j1",
            "question": "Answer needed for How Did You Hear About Us?",
            "options": self.OPTIONS,
        })
        _translate(bus)
        assert self._blocked(bus)["options"] == self.OPTIONS

    def test_options_survive_a_question_at_the_summary_budget(self, bus):
        """The point of a separate key: truncation must not eat the choices."""
        _mailbox_event(bus, "BLOCKED_QUESTION", {
            "job_key": "j1",
            "questions": [{"label": "Q%d %s" % (i, "x" * 40)} for i in range(20)],
            "options": self.OPTIONS,
        })
        _translate(bus)
        out = self._blocked(bus)
        assert len(out["question"]) <= 200
        assert out["options"] == self.OPTIONS

    def test_options_are_recovered_from_the_per_question_lists(self, bus):
        """Envelopes written before the flat key existed, and the
        notifier-bridge producer, only carry `questions[i].options`."""
        _mailbox_event(bus, "BLOCKED_QUESTION", {
            "job_key": "j1",
            "unansweredQuestions": [
                {"label": "First Name*", "type": "text"},
                {"label": "How Did You Hear About Us?", "type": "listbox",
                 "options": self.OPTIONS},
            ],
        })
        _translate(bus)
        assert self._blocked(bus)["options"] == self.OPTIONS

    def test_a_producer_supplied_list_wins_over_the_per_question_one(self, bus):
        _mailbox_event(bus, "BLOCKED_QUESTION", {
            "job_key": "j1",
            "options": ["Internet"],
            "questions": [{"label": "Q", "options": ["Job Fair"]}],
        })
        _translate(bus)
        assert self._blocked(bus)["options"] == ["Internet"]

    def test_free_text_question_carries_no_options_key(self, bus):
        """Absent is not the same claim as "the ATS offers nothing to pick";
        a free-text answer has to stay possible, and an empty list would reach
        the TelegramNotifier generic fallback as `options: []`."""
        _mailbox_event(bus, "BLOCKED_QUESTION", {
            "job_key": "j1",
            "questions": [{"label": "Why do you want this role?", "type": "text"}],
        })
        _translate(bus)
        assert "options" not in self._blocked(bus)

    def test_blank_and_duplicate_labels_are_dropped(self, bus):
        _mailbox_event(bus, "BLOCKED_QUESTION", {
            "job_key": "j1",
            "options": ["Internet", "  ", "Internet", " Job Fair "],
        })
        _translate(bus)
        assert self._blocked(bus)["options"] == ["Internet", "Job Fair"]

    def test_a_non_list_options_value_is_ignored_not_raised(self, bus):
        _mailbox_event(bus, "BLOCKED_QUESTION", {
            "job_key": "j1", "options": "Internet, Job Fair"})
        _translate(bus)
        assert "options" not in self._blocked(bus)
