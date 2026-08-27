"""Escalation ladder + non-sliding repeat window for sustained faults.

Regression cover for the 2026-08-25 Docker outage, where postgres-sync
emitted 22 ``cron_failed_consecutive`` events at priority=critical over
4h17m (reaching consecutive_errors=15) and four were delivered — three of
those four by accident (a gateway restart wiping in-memory guard state,
and the error STRING changing), not by severity.

Two behaviours are pinned here:

  #2 LADDER   — only rungs 3, 6, 12, 24 ... reach chat/phone, and a rung
                is never re-collapsed by RepeatGuard (all rungs normalize
                to one fingerprint, so running both would restore the bug).
  #3 NON-SLIDING — WARN+CRITICAL routes dedup on a window measured from
                the first delivery, so a persistent fault costs one message
                per window instead of one message per outage.

The headline test is ``test_replays_the_20260825_outage``: it drives the
real 08-25 sequence through the real notifier and asserts on the message
count and the rungs, so a regression shows up as the actual incident
rather than as an abstract guard assertion.
"""

import json

import pytest

from events.bus import EventBus
from events.noise_guards import (
    CONSECUTIVE_FAILURE_LADDER_BASE,
    RepeatGuard,
    is_consecutive_failure_ladder_step,
    is_off_ladder_consecutive_failure,
)
from events.producers.cron_emitter import CONSECUTIVE_FAILURE_THRESHOLD
from events.routing_policy import Attention, classify
from events.schema import Event, EventType, Priority
from events.subscribers.telegram_notifier import TelegramNotifier
from events.subscribers.whatsapp_escalator import WhatsAppEscalator


# --------------------------------------------------------------- fixtures

@pytest.fixture(autouse=True)
def _isolate_notifier_batch_state(tmp_path, monkeypatch):
    """TelegramNotifier resolves its batch-state file through a module-level
    ``notifier_batch_path()`` with no injection seam, so a bare construction
    LOADS (and a flush would WRITE) the live ~/.hermes notifier_batch.json.
    None of these tests batch — cron_failed_consecutive is WARN, and only
    TRACE routes batch — but relying on that is one refactor away from a
    suite that edits production state. Patch the name where it is BOUND
    (the subscriber module), not where it is defined.
    """
    monkeypatch.setattr(
        "events.subscribers.telegram_notifier.notifier_batch_path",
        lambda: tmp_path / "notifications" / "notifier_batch.json",
    )


@pytest.fixture
def bus(tmp_path):
    return EventBus(db_path=tmp_path / "events" / "event_bus.db")


@pytest.fixture
def topics_config(tmp_path):
    path = tmp_path / "telegram" / "topics.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "group_chat_id": "-1001234567890",
        "topics": {"watchdog_alerts": {"thread_id": 100, "name": "Alerts"}},
    }))
    return path


@pytest.fixture
def verbosity_config(tmp_path):
    """Mirrors production: watchdog_alerts gates on min_priority=normal,
    which CRITICAL clears. Pinned so a future verbosity edit cannot make
    these tests pass for the wrong reason."""
    path = tmp_path / "telegram" / "verbosity.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "watchdog_alerts": {"mode": "significant_only", "min_priority": "normal"},
    }))
    return path


@pytest.fixture
def quiet_config(tmp_path):
    path = tmp_path / "notifications" / "quiet_hours.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "enabled": False,          # quiet hours are a SEPARATE axis; off here
        "start": "23:00", "end": "07:00", "timezone": "America/New_York",
    }))
    return path


def _rendered_count(message: str) -> int:
    """Pull consecutive_errors back out of a rendered CHAT message.

    The chat lane renders payload key/value lines ("consecutive_errors: 12");
    the prose form ("has failed 12 times in a row") belongs to the WhatsApp
    lane only. Parsing the real rendering is deliberate: it is what proves
    the rung is legible to the person reading Alerts.
    """
    for line in message.splitlines():
        if line.startswith("consecutive_errors:"):
            return int(line.split(":", 1)[1].strip())
    raise AssertionError(f"no consecutive_errors line in: {message!r}")


def _consec_event(count, error="Workload reported exit_code=1", job="postgres-sync"):
    return Event.create(
        EventType.CRON_FAILED_CONSECUTIVE, job,
        {"job_id": "9823bee8f270", "job_name": job,
         "consecutive_errors": count, "error": error},
    )


# ------------------------------------------------------- #2 ladder predicate

class TestLadderPredicate:
    def test_rungs_are_base_doubling(self):
        assert [n for n in range(1, 100) if is_consecutive_failure_ladder_step(n)] == [
            3, 6, 12, 24, 48, 96]

    def test_ladder_is_unbounded(self):
        """An all-night outage keeps halving its own rate rather than
        hitting a final rung and going silent (or machine-gunning)."""
        assert is_consecutive_failure_ladder_step(3 * 2 ** 20)
        assert not is_consecutive_failure_ladder_step(3 * 2 ** 20 + 3)

    def test_below_threshold_is_not_a_rung(self):
        for n in (-5, 0, 1, 2):
            assert not is_consecutive_failure_ladder_step(n)

    def test_first_rung_is_the_producers_threshold(self):
        """The first rung MUST be the first value the producer can emit.
        If these drift apart, the very first alarm of an outage is the one
        that gets dropped — the worst possible failure for this feature.
        This is the check that makes the deliberate constant duplication
        (noise_guards stays import-free) safe."""
        assert CONSECUTIVE_FAILURE_LADDER_BASE == CONSECUTIVE_FAILURE_THRESHOLD
        assert is_consecutive_failure_ladder_step(CONSECUTIVE_FAILURE_THRESHOLD)

    def test_bools_are_not_counts(self):
        """isinstance(True, int) is True and True == 1 — without the explicit
        reject, a malformed payload could be read as a near-rung."""
        assert not is_consecutive_failure_ladder_step(True)
        assert not is_consecutive_failure_ladder_step(False)

    def test_non_ints_are_not_counts(self):
        for junk in (None, "3", 3.0, [3], {"n": 3}):
            assert not is_consecutive_failure_ladder_step(junk)


class TestOffLadderSuppression:
    def test_rungs_are_delivered(self):
        for n in (3, 6, 12, 24):
            assert not is_off_ladder_consecutive_failure(_consec_event(n))

    def test_between_rungs_is_suppressed(self):
        for n in (4, 5, 7, 8, 9, 10, 11, 13, 15, 23):
            assert is_off_ladder_consecutive_failure(_consec_event(n))

    def test_other_event_types_untouched(self):
        """The gate must be inert for everything else on the bus."""
        for et in (EventType.CRON_FAILED, EventType.AGENT_ERROR,
                   EventType.GATEWAY_HEALTH, EventType.INTERVIEW_SIGNAL):
            assert not is_off_ladder_consecutive_failure(
                Event.create(et, "x", {"consecutive_errors": 5}))

    def test_unreadable_payload_still_pages(self):
        """Fail OPEN. A payload we cannot parse must not be silently eaten —
        that would turn a producer bug into an invisible outage."""
        for payload in ({}, {"consecutive_errors": None},
                        {"consecutive_errors": "many"},
                        {"consecutive_errors": True}):
            ev = Event.create(EventType.CRON_FAILED_CONSECUTIVE, "job", payload)
            assert not is_off_ladder_consecutive_failure(ev)


# ------------------------------------------------- #3 non-sliding repeat window

class TestNonSlidingWindow:
    def test_sliding_is_still_the_default(self):
        """The [SILENT] x1,522/week flood this guard was built for must keep
        collapsing to silence-until-it-changes."""
        g = RepeatGuard(window_seconds=1800)
        assert not g.is_repeat("t", "devflow-bridge: tick", now=0)
        for i in range(1, 40):
            assert g.is_repeat("t", "devflow-bridge: tick", now=i * 300)

    def test_non_sliding_window_expires_under_a_continuing_fault(self):
        """The 08-25 shape: a repeat every 15 min, forever. Sliding never
        re-delivers; non-sliding re-delivers once per window."""
        g = RepeatGuard(window_seconds=1800)
        delivered = [t for t in range(0, 7200, 900)
                     if not g.is_repeat("t", "still broken", now=t, sliding=False)]
        assert delivered == [0, 1800, 3600, 5400]

    def test_sliding_never_expires_under_the_same_stream(self):
        """Control for the test above — this is the 2026-08-25 defect,
        reproduced. Without it, the test above proves nothing about which
        discipline is responsible."""
        g = RepeatGuard(window_seconds=1800)
        delivered = [t for t in range(0, 7200, 900)
                     if not g.is_repeat("t", "still broken", now=t, sliding=True)]
        assert delivered == [0]

    def test_non_sliding_still_suppresses_inside_the_window(self):
        g = RepeatGuard(window_seconds=1800)
        assert not g.is_repeat("t", "m", now=0, sliding=False)
        assert g.is_repeat("t", "m", now=60, sliding=False)
        assert g.is_repeat("t", "m", now=1799, sliding=False)
        assert not g.is_repeat("t", "m", now=1800, sliding=False)

    def test_suppressed_key_is_not_evicted_early(self):
        """A key under active suppression must not be evicted by max_entries
        — eviction would silently hand it a FRESH window before its own
        expired, re-delivering early and undoing the point of the window."""
        g = RepeatGuard(window_seconds=1800, max_entries=4)
        assert not g.is_repeat("t", "hot", now=0, sliding=False)
        for i in range(1, 30):
            assert g.is_repeat("t", "hot", now=1, sliding=False)
            g.is_repeat("t", f"cold-{i}", now=1, sliding=False)
        # Still inside its window, and still remembered → still suppressed.
        assert g.is_repeat("t", "hot", now=1700, sliding=False)

    def test_sliding_is_keyword_only(self):
        """`now` is passed positionally throughout the existing suite; the
        keyword-only marker is what stops a future call site selecting a
        window discipline by accident."""
        g = RepeatGuard()
        with pytest.raises(TypeError):
            g.is_repeat("t", "m", 0, False)


# ------------------------------------------------------ route-level contract

class TestRouteContract:
    def test_consecutive_failure_is_warn_critical(self):
        """Both subscribers key their non-sliding branch off (WARN, CRITICAL).
        If routing ever reclassifies this event, that branch silently stops
        applying — so pin the classification here."""
        route = classify(_consec_event(3))
        assert route.attention is Attention.WARN
        assert route.priority is Priority.CRITICAL
        assert route.topic_key == "watchdog_alerts"
        assert route.wa_tier == "urgent"


# -------------------------------------------------- subscriber integration

class TestTelegramLadder:
    def _notifier(self, bus, topics_config, verbosity_config, sent):
        return TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_config,
            send_fn=lambda chat_id, thread_id, msg: sent.append((thread_id, msg)),
        )

    def test_off_ladder_events_never_reach_chat(
            self, bus, topics_config, verbosity_config):
        sent = []
        n = self._notifier(bus, topics_config, verbosity_config, sent)
        for count in (4, 5, 7, 11):
            n.handle(_consec_event(count))
        assert sent == []

    def test_consecutive_rungs_are_not_collapsed_by_the_repeat_guard(
            self, bus, topics_config, verbosity_config):
        """The load-bearing composition test. Rungs 3 and 6 arrive well
        inside the 30-min window and normalize to ONE fingerprint (digits →
        N), so if RepeatGuard still ran on them the ladder would deliver
        exactly one message and this whole change would be inert."""
        sent = []
        n = self._notifier(bus, topics_config, verbosity_config, sent)
        n.handle(_consec_event(3))
        n.handle(_consec_event(6))
        n.handle(_consec_event(12))
        assert len(sent) == 3
        # resolve_topic_thread returns the thread id as a string
        assert all(str(thread) == "100" for thread, _ in sent)

    def test_delivered_messages_state_the_count(
            self, bus, topics_config, verbosity_config):
        """A ladder is only useful if the rung is legible to the reader —
        otherwise consec=12 still looks like consec=3."""
        sent = []
        n = self._notifier(bus, topics_config, verbosity_config, sent)
        n.handle(_consec_event(12))
        assert _rendered_count(sent[0][1]) == 12

    def test_replays_the_20260825_outage(
            self, bus, topics_config, verbosity_config):
        """THE regression test. The real sequence: consec 3..15, every
        ~13-15 min, with the error string changing after the first event
        exactly as it did on the night.

        Before this change: 22 events → 4 messages, at counts 3, 4, 3, 10,
        three of them accidents of state loss or text drift, and NOTHING
        after consec=10 for the remaining 78 minutes.
        After: one message per rung, each one worse than the last.
        """
        sent = []
        n = self._notifier(bus, topics_config, verbosity_config, sent)
        first = "RuntimeError: cannot schedule new futures after interpreter shutdown"
        for count in range(3, 16):
            n.handle(_consec_event(
                count, error=first if count == 3 else "Workload reported exit_code=1"))

        counts = [_rendered_count(msg) for _, msg in sent]
        assert counts == [3, 6, 12], f"expected rungs, got {counts}"
        # And the escalation is VISIBLE: each message names a bigger number.
        assert counts == sorted(counts)

    def test_a_transient_blip_still_costs_exactly_one_message(
            self, bus, topics_config, verbosity_config):
        """The other half of the design goal. A job that fails 3 times and
        recovers must not get louder than it was before."""
        sent = []
        n = self._notifier(bus, topics_config, verbosity_config, sent)
        n.handle(_consec_event(3))
        assert len(sent) == 1

    def test_distinct_jobs_are_independent(
            self, bus, topics_config, verbosity_config):
        """Two jobs failing at once are two incidents, not one."""
        sent = []
        n = self._notifier(bus, topics_config, verbosity_config, sent)
        n.handle(_consec_event(3, job="postgres-sync"))
        n.handle(_consec_event(3, job="jaum-inbox-sweeper"))
        assert len(sent) == 2

    def test_other_warn_critical_events_re_deliver_once_per_window(
            self, bus, topics_config, verbosity_config):
        """#3 applies beyond the ladder: a sustained non-cron critical
        fault re-announces once per window instead of once per outage."""
        sent = []
        n = self._notifier(bus, topics_config, verbosity_config, sent)
        ev = lambda: Event.create(
            EventType.CONTAINER_CRASH_LOOP, "docker",
            {"container": "hindsight-db", "restarts": 9}, priority=Priority.CRITICAL)
        n.handle(ev())
        assert len(sent) == 1
        n._repeat_guard._seen.clear()          # simulate the window expiring
        n.handle(ev())
        assert len(sent) == 2


class TestWhatsAppLadder:
    def _escalator(self, bus, quiet_config, tmp_path, sent):
        return WhatsAppEscalator(
            bus,
            quiet_config_path=quiet_config,
            queue_path=tmp_path / "notifications" / "quiet_queue.json",
            send_fn=lambda msg: sent.append(msg),
        )

    def test_off_ladder_events_never_page(self, bus, quiet_config, tmp_path):
        sent = []
        e = self._escalator(bus, quiet_config, tmp_path, sent)
        for count in (4, 5, 7, 11, 13, 15):
            e.handle(_consec_event(count))
        assert sent == []
        assert not e._throttle_buffer, "off-ladder events must not even buffer"

    def test_rungs_reach_the_phone_lane(self, bus, quiet_config, tmp_path):
        """URGENT is throttle-buffered rather than sent instantly, so assert
        on the buffer — asserting on send_fn would pass vacuously."""
        sent = []
        e = self._escalator(bus, quiet_config, tmp_path, sent)
        for count in (3, 6, 12):
            e.handle(_consec_event(count))
        assert len(e._throttle_buffer) == 3

    def test_rungs_are_not_collapsed_on_the_phone_lane_either(
            self, bus, quiet_config, tmp_path):
        """The escalator keeps its OWN RepeatGuard (_wa_repeat_guard), so the
        chat-lane fix does not cover this lane — it needs its own bypass."""
        sent = []
        e = self._escalator(bus, quiet_config, tmp_path, sent)
        e.handle(_consec_event(3))
        e.handle(_consec_event(6))
        assert len(e._throttle_buffer) == 2
        assert e._wa_repeat_guard.suppressed_count == 0
