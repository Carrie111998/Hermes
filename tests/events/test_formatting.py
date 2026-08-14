"""Tests for events.formatting emoji + header helpers."""

from enum import Enum

import pytest

from events.formatting import (
    PRIORITY_EMOJI, EVENT_TYPE_EMOJI, MAILBOX_INNER_EMOJI,
    SEPARATOR,
    priority_dot, header_dot, event_icon,
    format_header, format_event_message, format_whatsapp_message,
    format_whatsapp_header,
)
from events.routing_policy import classify
from events.schema import Event, EventType, Priority


def _make_event(event_type, priority=None, source="test", payload=None,
                timestamp="2026-04-17T05:02:39+00:00"):
    return Event(
        event_id="x", event_type=event_type, source=source,
        timestamp=timestamp, priority=priority or event_type.default_priority,
        payload=payload or {},
    )


def test_priority_dots_cover_all_levels():
    for p in Priority:
        assert priority_dot(p), f"missing dot for {p}"


def test_event_icons_cover_all_types():
    """Every EventType member needs an icon.

    Totality is now STRUCTURAL — the icon is a required EventType field and
    EVENT_TYPE_EMOJI is derived from it — so this can only fail if the derived
    table stops deriving. Kept as the canary for exactly that.

    Collect-then-assert, not assert-in-loop: the in-loop form short-circuited
    on the FIRST miss, so the failure message named one type at every
    recurrence of this drift (2026-04-27 x2, 2026-05-29, 2026-08-11) while
    12-13 were actually gone. See events/coverage.py for the standing check.
    """
    missing = [et.type_string for et in EventType if not EVENT_TYPE_EMOJI.get(et)]
    assert not missing, (
        f"{len(missing)} of {len(list(EventType))} EventType members have no "
        f"EVENT_TYPE_EMOJI entry: {', '.join(missing)}"
    )


def test_event_icons_are_unique_within_a_telegram_topic():
    """Two events on the SAME Telegram topic must not share a glyph.

    Coverage is now guaranteed by construction (EventType requires a non-empty
    icon at class creation), but nothing asserts that icons are
    *distinguishable* — so the table can be 100% covered and still unreadable.
    The standard the EventType docstring states is per-topic, not global ("pick
    a glyph disjoint from its neighbours in the same Telegram topic"), because
    an operator only ever scans one topic's feed at a time; two identical
    glyphs in different topics never appear side by side and are deliberately
    allowed.

    Caught for real on 2026-08-11: 💥 was on both ``cron_failed`` (2026-04-17)
    and ``agent_loop_fault`` (2026-05-29), i.e. two failure events landing in
    watchdog_alerts that a reader could not tell apart at a glance. It had
    gone unnoticed for ~2.5 months. The near-miss cost was higher: efc77632f
    added 12 DevFlow icons colliding with six pre-existing types (four of them
    on a shared topic) and no test objected — it was caught only by a human
    AST diff while resolving it against a competing fix (b0adeb34c).

    Cross-topic duplicates are intentionally NOT asserted here; as of this
    writing ✅ (application_submitted / critic_auto_applied) and 🟢
    (devflow.build_succeeded / gateway_started) are both fine.
    """
    from collections import defaultdict

    from events.routing_policy import _POLICY

    by_topic_glyph = defaultdict(list)
    for event_type, glyph in EVENT_TYPE_EMOJI.items():
        spec = _POLICY.get(event_type)
        if spec is None:
            continue  # unrouted: never rendered into a topic feed
        by_topic_glyph[(spec.topic_key, glyph)].append(event_type.type_string)

    collisions = {k: sorted(v) for k, v in by_topic_glyph.items() if len(v) > 1}
    assert not collisions, (
        "EVENT_TYPE_EMOJI reuses a glyph within a single Telegram topic, so "
        "an operator scanning that feed cannot tell the events apart: "
        + "; ".join(
            f"{glyph} on {topic} -> {', '.join(names)}"
            for (topic, glyph), names in sorted(collisions.items())
        )
    )


def test_event_type_emoji_is_derived_from_the_enum():
    """EVENT_TYPE_EMOJI must stay a view over EventType.icon, not a second
    hand-maintained table. Four drifts (2026-04-27 x2, 2026-05-29, 2026-08-11)
    came from it being independently edited; re-introducing a literal dict here
    re-opens that failure mode, so pin both the values and the fact that it
    cannot be written to.
    """
    assert dict(EVENT_TYPE_EMOJI) == {et: et.icon for et in EventType}
    with pytest.raises(TypeError):
        EVENT_TYPE_EMOJI[EventType.CRON_STARTED] = "💩"  # read-only view


class TestIconGuardIsArmed:
    """Prove the structural guard actually fires.

    Without these, a green suite is equally consistent with "the guard works"
    and "the guard was silently removed" — the icons would all still be present
    either way. Each case reproduces one shape of the drift against a throwaway
    enum that reuses EventType's real __init__.
    """

    @staticmethod
    def _define(*member_value):
        class _Throwaway(Enum):
            __init__ = EventType.__init__
            SAMPLE = member_value

        return _Throwaway

    def test_omitting_the_icon_is_a_type_error(self):
        """The 2026-08-11 shape: a member added with only (string, priority).

        Enum's member construction rejects it at CLASS-CREATION time, so
        ``import events.schema`` fails — and every producer imports it.
        """
        with pytest.raises(TypeError, match="icon"):
            self._define("sample", Priority.LOW)

    def test_empty_icon_is_rejected(self):
        """Closes the obvious escape hatch: silencing the TypeError with "".

        That would restore exactly the old failure (event_icon() -> "").
        """
        with pytest.raises(ValueError, match="non-empty"):
            self._define("sample", Priority.LOW, "")

    def test_whitespace_only_icon_is_rejected(self):
        with pytest.raises(ValueError, match="non-empty"):
            self._define("sample", Priority.LOW, "   ")

    def test_a_real_icon_is_accepted(self):
        """Negative-test control: the guard rejects blanks, not everything."""
        assert self._define("sample", Priority.LOW, "🧪").SAMPLE.icon == "🧪"


def test_event_icon_uses_inner_type_for_mailbox_message():
    e = _make_event(EventType.MAILBOX_MESSAGE,
                    payload={"message_type": "SCORE_RESULT"})
    assert event_icon(e) == "📊"


def test_event_icon_falls_back_to_mailbox_generic_for_unknown_inner():
    e = _make_event(EventType.MAILBOX_MESSAGE,
                    payload={"message_type": "UNKNOWN_TYPE"})
    assert event_icon(e) == "📨"


def test_event_icon_for_secret_detected_is_padlock():
    """SR-408 regression (2026-04-19): SECRET_DETECTED must have a distinct
    icon. Before the fix, the missing EVENT_TYPE_EMOJI entry made event_icon()
    return "" — headers rendered as "🟠  SECRET_DETECTED — …" (double space,
    no visual hook) which operators scanning a chat flood could not
    disambiguate from generic HIGH events. The padlock 🔐 is the scan token.
    """
    e = _make_event(EventType.SECRET_DETECTED,
                    payload={"rule_id": "aws-access-token",
                             "file_path": "C:/Users/diego/.env",
                             "line_no": 5,
                             "match_preview": "AKIA****XYZ1234"})
    assert event_icon(e) == "🔐"


def test_format_header_for_agent_error():
    e = _make_event(EventType.AGENT_ERROR, source="mailbox:sentinel")
    assert format_header(e) == "🟠 ⚠️ AGENT_ERROR — mailbox:sentinel · 05:02 UTC"


def test_format_header_gateway_health_up_is_green():
    """A GATEWAY_HEALTH recovery ('up'/back-running) reads as green (🟢), not
    the amber HIGH dot it shares with the 'down' outage. Operator request
    2026-07-18: an 'X is back up' line must be visually distinct from an
    outage at a glance."""
    e = _make_event(EventType.GATEWAY_HEALTH, source="system",
                    payload={"platform": "whatsapp", "status": "up", "detail": ""})
    assert format_header(e) == "🟢 🛰️ GATEWAY_HEALTH — system · 05:02 UTC"


def test_format_header_gateway_health_down_stays_amber():
    """The 'down' side is unchanged — still the amber HIGH dot."""
    e = _make_event(EventType.GATEWAY_HEALTH, source="system",
                    payload={"platform": "whatsapp", "status": "down",
                             "detail": "connection refused"})
    assert format_header(e) == "🟠 🛰️ GATEWAY_HEALTH — system · 05:02 UTC"


def test_format_whatsapp_header_gateway_health_up_is_green():
    """The green-on-recovery override applies to the WhatsApp surface too."""
    e = _make_event(EventType.GATEWAY_HEALTH, source="system",
                    payload={"platform": "whatsapp", "status": "up", "detail": ""})
    assert format_whatsapp_header(e) == "🟢 🛰️ GATEWAY HEALTH — system · 05:02 UTC"


@pytest.mark.parametrize(
    ("payload", "priority", "expected"),
    [
        ({"counters": {"exit_code": 1}, "reason": "success"}, Priority.LOW,
         "🟠 FAILED "),
        ({"exit_code": 1}, Priority.CRITICAL, "🔴 FAILED "),
        ({"status": "partial"}, Priority.LOW, "🟠 DEGRADED "),
        ({"status": "pending"}, Priority.HIGH, "🟡 PENDING "),
        ({"reason": "success"}, Priority.NORMAL, "🟢 SUCCEEDED "),
        ({"status": "healthy", "before": "down"}, Priority.HIGH,
         "🟢 RECOVERED "),
        ({"summary": "tick"}, Priority.LOW, "🟡 UNKNOWN "),
        ({"reason": "no_work"}, Priority.LOW, "🟢 NO WORK "),
    ],
)
def test_verdict_header_has_consistent_marker_and_text_label(
    payload, priority, expected,
):
    e = _make_event(
        EventType.AGENT_ITERATION,
        priority=priority,
        payload=payload,
    )
    route = classify(e)

    assert format_header(e, verdict=route.verdict).startswith(expected)
    assert format_whatsapp_header(e, verdict=route.verdict).startswith(expected)


def test_no_work_header_label_has_no_underscore():
    event = _make_event(
        EventType.AGENT_ITERATION,
        priority=Priority.LOW,
        source="critic",
        payload={"agent": "critic", "reason": "no_work"},
    )
    route = classify(event)
    header = format_header(event, verdict=route.verdict)
    whatsapp_header = format_whatsapp_header(event, verdict=route.verdict)

    for rendered in (header, whatsapp_header):
        assert rendered.startswith("🟢 NO WORK ")
        assert "NO_WORK" not in rendered
        assert "UNKNOWN" not in rendered


def test_failure_evidence_blocks_recovery_green():
    e = _make_event(
        EventType.GATEWAY_HEALTH,
        priority=Priority.HIGH,
        payload={"status": "up", "before": "down", "exit_code": 1},
    )
    route = classify(e)

    assert format_header(e, route.verdict).startswith("🟠 FAILED ")
    assert format_whatsapp_header(e, route.verdict).startswith("🟠 FAILED ")


class TestRecoveryHeaderDots:
    """Recovery lines wear a green dot (2026-07-27).

    Diego, reading the Telegram feed: "messages saying that components are up
    with an amber indication (should be green)". header_dot() only greened
    GATEWAY_HEALTH 'up' and CODE_DRIFT 'resolved'; every other recovery fell
    through to priority_dot(). GATEWAY_STARTED and WATCHDOG_RECOVERED are
    recoveries by definition (their icon is already green), and a
    WATCHDOG_PROBE_TRANSITION to 'healthy' is one conditionally. The dot
    contradicted the icon beside it.

    PRESENTATIONAL ONLY — priority, routing and escalation are untouched, the
    same scoping as the 2026-07-18 GATEWAY_HEALTH change.
    """

    def test_gateway_started_is_green(self):
        e = _make_event(EventType.GATEWAY_STARTED, source="gateway",
                        payload={"pid": 62724, "boot_reason": "manual"})
        assert header_dot(e) == PRIORITY_EMOJI[Priority.LOW]

    def test_watchdog_recovered_is_green(self):
        e = _make_event(EventType.WATCHDOG_RECOVERED, source="watchdog",
                        payload={"component": "mempalace"})
        assert header_dot(e) == PRIORITY_EMOJI[Priority.LOW]

    def test_always_recovery_types_stay_green_at_any_priority(self):
        """These two are recoveries by definition — a producer that stamps a
        louder priority must not repaint the dot amber."""
        for et in (EventType.GATEWAY_STARTED, EventType.WATCHDOG_RECOVERED):
            e = _make_event(et, priority=Priority.CRITICAL, source="watchdog")
            assert header_dot(e) == PRIORITY_EMOJI[Priority.LOW], et

    def test_probe_transition_to_healthy_is_green(self):
        e = _make_event(EventType.WATCHDOG_PROBE_TRANSITION, source="watchdog",
                        payload={"probe": "gbrain-http", "before": "degraded",
                                 "after": "healthy"})
        assert header_dot(e) == PRIORITY_EMOJI[Priority.LOW]

    def test_probe_transition_to_degraded_keeps_its_priority_dot(self):
        """The conditional half only fires on the recovery side."""
        e = _make_event(EventType.WATCHDOG_PROBE_TRANSITION, source="watchdog",
                        payload={"probe": "gbrain-http", "before": "healthy",
                                 "after": "degraded"})
        assert header_dot(e) == PRIORITY_EMOJI[Priority.HIGH]

    def test_probe_transition_without_payload_keeps_its_priority_dot(self):
        e = _make_event(EventType.WATCHDOG_PROBE_TRANSITION, source="watchdog")
        assert header_dot(e) == PRIORITY_EMOJI[Priority.HIGH]

    def test_unrelated_event_still_uses_its_priority_dot(self):
        """The restructure must not green anything outside the two tables."""
        e = _make_event(EventType.AGENT_ERROR, source="mailbox:sentinel")
        assert header_dot(e) == priority_dot(e.priority)

    def test_recovery_dot_does_not_change_event_priority(self):
        """Presentational only: rendering a header must leave the event's
        own priority (and therefore routing/escalation) alone."""
        e = _make_event(EventType.WATCHDOG_RECOVERED, source="watchdog")
        before = e.priority
        format_header(e)
        assert e.priority is before

    def test_whatsapp_header_greens_recoveries_too(self):
        e = _make_event(EventType.GATEWAY_STARTED, source="gateway")
        assert format_whatsapp_header(e).startswith(PRIORITY_EMOJI[Priority.LOW])


def test_format_header_for_interview_signal_is_critical():
    e = _make_event(EventType.INTERVIEW_SIGNAL, source="mailbox:notifier")
    h = format_header(e)
    assert h.startswith("🔴")
    assert "🗓️" in h
    assert "INTERVIEW_SIGNAL" in h
    assert "mailbox:notifier" in h


def test_format_header_for_mailbox_message_shows_inner_type_and_routing():
    e = _make_event(EventType.MAILBOX_MESSAGE,
                    payload={"message_type": "SCORE_RESULT",
                             "from": "matcher", "to": "main"})
    h = format_header(e)
    assert "📊" in h and "SCORE_RESULT" in h and "matcher → main" in h


def test_format_event_message_includes_separator():
    e = _make_event(EventType.CRON_COMPLETED, source="polish-verify")
    msg = format_event_message(e, "body here")
    assert SEPARATOR in msg
    assert "body here" in msg


def test_format_whatsapp_message_has_no_separator():
    e = _make_event(EventType.OFFER_SIGNAL, source="mailbox:notifier")
    msg = format_whatsapp_message(e, "You have an offer from Acme")
    assert SEPARATOR not in msg
    # 2026-07-11: WhatsApp headers use plain-language titles, not enum names.
    assert "JOB OFFER" in msg
    assert "OFFER_SIGNAL" not in msg
    assert "You have an offer from Acme" in msg


def test_watchdog_burst_renders_with_burst_icon():
    """WATCHDOG_BURST gets a distinct icon (🌊) so operators can scan for it."""
    e = Event.create(
        event_type=EventType.WATCHDOG_BURST,
        source="watchdog",
        payload={
            "count": 22,
            "trigger": "burst_threshold",
            "transitions": [
                {"probe": "docker", "tier": "critical", "before": "up", "after": "down"},
                {"probe": "postgres", "tier": "critical", "before": "up", "after": "down"},
            ],
        },
        priority=Priority.HIGH,
    )
    icon = event_icon(e)
    assert icon != ""
    # Sanity: it must NOT be the same as the single-probe icon (🔄), so a
    # human scanning Telegram can tell a burst apart from a single transition.
    assert EVENT_TYPE_EMOJI[EventType.WATCHDOG_BURST] != EVENT_TYPE_EMOJI[EventType.WATCHDOG_PROBE_TRANSITION]
    # Regression guard: never let the burst icon collide with a priority dot.
    # WATCHDOG_BURST is HIGH-priority by default, so a 🟠 icon would render
    # adjacent to a 🟠 priority dot in format_header() output. Generalizes
    # the convention SECRET_DETECTED's docstring articulates.
    from events.formatting import PRIORITY_EMOJI
    assert EVENT_TYPE_EMOJI[EventType.WATCHDOG_BURST] not in PRIORITY_EMOJI.values()


class TestPlainLanguageBodies:
    """2026-07-11 operator feedback: WhatsApp escalations rendered as raw
    payload JSON truncated at 200 chars ('what the heck is a
    WATCHDOG_BURST????'). These helpers must produce complete sentences
    with zero payload-JSON leakage on every surface."""

    def test_format_duration(self):
        from events.formatting import format_duration
        assert format_duration(45) == "45s"
        assert format_duration(509) == "8m 29s"
        assert format_duration(300) == "5m"
        assert format_duration(7500) == "2h 5m"
        assert format_duration(None) == "None"  # graceful, not crashing
        # Day tier (2026-07-18): multi-day ages (e.g. a partial-backlog oldest
        # of ~2.8d) read as "2d 19h", not an unbounded hour count.
        assert format_duration(86400) == "1d"
        assert format_duration(244601) == "2d 19h"

    def test_humanize_health_detail_connection_refused(self):
        from events.formatting import humanize_health_detail
        raw = ("HTTPConnectionPool(host='127.0.0.1', port=3000): Max retries "
               "exceeded with url: /health (Caused by NewConnectionError("
               "\"HTTPConnection(host='127.0.0.1', port=3000): Failed to "
               "establish a new connection\"))")
        out = humanize_health_detail(raw)
        assert "connection refused" in out
        assert "127.0.0.1:3000" in out
        assert "HTTPConnectionPool" not in out

    def test_humanize_health_detail_read_timeout(self):
        from events.formatting import humanize_health_detail
        raw = ("HTTPConnectionPool(host='127.0.0.1', port=3000): "
               "Read timed out. (read timeout=5)")
        out = humanize_health_detail(raw)
        assert "timed out" in out
        assert "127.0.0.1:3000" in out
        assert "HTTPConnectionPool" not in out

    def test_humanize_health_detail_passthrough_and_empty(self):
        from events.formatting import humanize_health_detail
        assert humanize_health_detail("") == ""
        assert humanize_health_detail("HTTP 503") == "health endpoint returned HTTP 503"
        assert humanize_health_detail("some other failure") == "some other failure"

    def test_watchdog_burst_body_leads_with_failures(self):
        from events.formatting import watchdog_burst_body
        body = watchdog_burst_body({
            "count": 3,
            "transitions": [
                {"probe": "Container: devflow-api", "tier": "optional",
                 "category": "infra", "before": "healthy", "after": "down"},
                {"probe": "Bridge: hermes->devflow lag", "tier": "important",
                 "category": "hermes", "before": "healthy", "after": "down"},
                {"probe": "Postgres :5437", "tier": "critical",
                 "category": "infra", "before": "healthy", "after": "down",
                 "detail": "CONNECT_TIMEOUT"},
            ],
        })
        # Critical listed first, optional aggregated into a count.
        lines = body.splitlines()
        assert "3 health checks failing" in lines[0]
        assert "Postgres :5437" in lines[1] and "[critical]" in lines[1]
        assert "CONNECT_TIMEOUT" in lines[1]
        assert "Bridge: hermes->devflow lag" in lines[2]
        assert "Container: devflow-api" not in body
        assert "+1 low-priority probe flap" in body
        # No payload JSON leakage.
        assert "{" not in body and "watchdog_type" not in body

    def test_watchdog_burst_body_recovery_only(self):
        from events.formatting import watchdog_burst_body
        body = watchdog_burst_body({
            "count": 2,
            "transitions": [
                {"probe": "A", "tier": "important", "before": "down", "after": "healthy"},
                {"probe": "B", "tier": "critical", "before": "unknown", "after": "healthy"},
            ],
        })
        assert "recoveries" in body
        assert "A" in body and "B" in body

    def test_watchdog_burst_body_telegram_lists_optional(self):
        from events.formatting import watchdog_burst_body
        body = watchdog_burst_body({
            "count": 1,
            "transitions": [
                {"probe": "Container: devflow-api", "tier": "optional",
                 "before": "healthy", "after": "down"},
            ],
        }, aggregate_optional=False)
        assert "Container: devflow-api" in body

    def test_watchdog_burst_body_caps_listing(self):
        from events.formatting import watchdog_burst_body
        transitions = [
            {"probe": f"probe-{i}", "tier": "important",
             "before": "healthy", "after": "down"}
            for i in range(9)
        ]
        body = watchdog_burst_body({"count": 9, "transitions": transitions},
                                   max_listed=5)
        assert "…and 4 more failing" in body

    def test_watchdog_burst_body_empty_transitions(self):
        from events.formatting import watchdog_burst_body
        body = watchdog_burst_body({"count": 7, "transitions": []})
        assert "7" in body and "{" not in body

    def test_watchdog_burst_body_all_skipped_is_not_a_failure(self):
        # 2026-07-11 pass-overrun storms: after="unknown" means the probe was
        # SKIPPED (monitor over budget), not that the service failed. A burst
        # of pure skips must not read as "55 health checks failing".
        from events.formatting import watchdog_burst_body
        transitions = [
            {"probe": f"probe-{i}", "tier": "important", "before": "healthy",
             "after": "unknown",
             "detail": "skipped: pass exceeded 50s budget (probe not run this tick)"}
            for i in range(55)
        ]
        body = watchdog_burst_body({"count": 55, "transitions": transitions})
        assert "failing" not in body
        assert "skipped" in body and "time budget" in body
        assert "Nothing is known to be down" in body

    def test_watchdog_burst_body_mixed_skips_stay_out_of_failing(self):
        from events.formatting import watchdog_burst_body
        body = watchdog_burst_body({
            "count": 3,
            "transitions": [
                {"probe": "Postgres :5437", "tier": "critical",
                 "before": "healthy", "after": "down", "detail": "CONNECT_TIMEOUT"},
                {"probe": "skipped-A", "tier": "important",
                 "before": "healthy", "after": "unknown",
                 "detail": "skipped: pass exceeded 50s budget"},
                {"probe": "skipped-B", "tier": "important",
                 "before": "healthy", "after": "unknown",
                 "detail": "skipped: pass exceeded 50s budget"},
            ],
        })
        assert "1 health check failing" in body
        assert "Postgres :5437" in body
        assert "2 probes skipped this pass" in body
        assert "not real failures" in body

    def test_watchdog_self_degraded_body_over_budget(self):
        from events.formatting import watchdog_self_degraded_body
        body = watchdog_self_degraded_body({
            "reason": "monitor pass over budget",
            "skipped_probes": 55,
            "sample_detail": "skipped: pass exceeded 50s budget",
        })
        assert "monitor pass over budget" in body
        assert "55 probes were not checked" in body
        assert "unchanged, not down" in body
        assert "{" not in body

    def test_watchdog_self_degraded_body_stale_status(self):
        from events.formatting import watchdog_self_degraded_body
        body = watchdog_self_degraded_body({
            "reason": "laptop-monitor status.json stale",
            "age_seconds": 900,
        })
        assert "Laptop monitor stopped updating its health snapshot" in body
        assert r"C:\Users\diego\architecture-map\status.json" in body
        assert "15m" in body
        assert "Service health shown by the watchdog may be out of date" in body
        assert "No action is usually needed" in body
        assert "If this persists" in body

    def test_silence_alert_body(self):
        from events.formatting import silence_alert_body
        body = silence_alert_body({
            "source": "devflow-bridge",
            "expected_cadence_seconds": 300,
            "time_since_last_seconds": 509,
            "last_seen": "2026-07-11T17:55:10.218930+00:00",
            "severity": "silent",
        })
        assert "devflow-bridge went quiet" in body
        assert "8m 29s" in body
        assert "5m" in body
        assert "17:55 UTC" in body
        assert "{" not in body

    def test_silence_alert_body_never_seen(self):
        from events.formatting import silence_alert_body
        body = silence_alert_body({
            "source": "sentinel",
            "expected_cadence_seconds": 600,
            "time_since_last_seconds": None,
            "last_seen": None,
            "severity": "never_seen",
        })
        assert "sentinel" in body and "never" in body

    def test_failure_cluster_body(self):
        from events.formatting import failure_cluster_body
        body = failure_cluster_body({
            "source": "mailbox:tailor",
            "cluster_size": 4,
            "last_event_type": "cron_failed",
            "last_timestamp": "2026-07-11T18:03:00+00:00",
        })
        assert "mailbox:tailor" in body
        assert "4 times in a row" in body
        assert "cron_failed" in body

    def test_failure_cluster_body_prefers_canonical_diagnostics(self):
        from events.formatting import failure_cluster_body
        body = failure_cluster_body({
            "source": "tracker",
            "failure_type": "network",
            "count": 3,
            "last_seen": "2026-08-02T18:03:00+00:00",
            "exception_type": "OperationalError",
            "error_code": "PG_CONNECT_REFUSED",
            "phase": "postgres_sync",
            "deadline_seconds": 1800,
            "latest_cause": "connection refused at 127.0.0.1:5434",
        })

        assert "?" not in body
        assert "tracker has failed 3 times in a row" in body
        assert "network" in body
        assert "OperationalError" in body
        assert body.index("network") < body.index("OperationalError")
        assert "PG_CONNECT_REFUSED" in body
        assert "postgres_sync" in body
        assert "30m" in body
        assert "connection refused at 127.0.0.1:5434" in body
        assert "18:03 UTC" in body

    def test_failure_cluster_body_does_not_invent_optional_details(self):
        from events.formatting import failure_cluster_body
        body = failure_cluster_body({
            "source": "scout",
            "failure_type": "timeout",
            "count": 3,
            "first_seen": "2026-08-02T17:55:00+00:00",
            "last_seen": "2026-08-02T18:03:00+00:00",
        })

        assert "?" not in body
        assert "timeout" in body
        assert "error code" not in body.lower()
        assert "phase" not in body.lower()
        assert "deadline" not in body.lower()
        assert "cause" not in body.lower()

    def test_failure_cluster_body_omits_missing_required_values(self):
        from events.formatting import failure_cluster_body
        body = failure_cluster_body({"source": "scout"})

        assert body == "scout has failed repeatedly.\nSomething is stuck — needs a look."
        assert "multiple" not in body
        assert "failure)" not in body

    def test_partial_backlog_body_explains_and_advises(self):
        from events.formatting import partial_backlog_body
        body = partial_backlog_body({
            "count": 10,
            "threshold": 3,
            "oldest_age_seconds": 244601.4,
            "capped_count": 10,
            "sample_job_ids": [
                "1764098e-1101-4c04-aba0-909c96d977bd",
                "39806922-3dc2-4573-8602-8f1de837954e",
                "0425ac22-09d8-4c0d-9df2-44238764200e",
            ],
        })
        # Says what's wrong, in plain language — not raw payload keys.
        assert "10" in body
        assert "Postgres" in body            # where the sync failed
        assert "re-drive" in body            # what's safe to do
        assert "2d 19h" in body              # humanized oldest age, not seconds
        assert "1764098e" in body            # a triage id survives
        # Zero raw-payload leakage: none of the cryptic field names appear.
        assert "oldest_age_seconds" not in body
        assert "capped_count" not in body
        assert "sample_job_ids" not in body
        assert "244601" not in body          # seconds humanized away
        assert "{" not in body

    def test_partial_backlog_body_singular(self):
        from events.formatting import partial_backlog_body
        body = partial_backlog_body({
            "count": 1, "threshold": 3, "oldest_age_seconds": 90.0,
            "capped_count": 1, "sample_job_ids": ["abcd1234-0000"],
        })
        assert "1 tracker update is" in body   # singular grammar
        assert "1m 30s" in body

    def test_partial_backlog_body_notes_when_sample_is_truncated(self):
        from events.formatting import partial_backlog_body
        body = partial_backlog_body({
            "count": 42, "threshold": 3, "oldest_age_seconds": 5.0,
            "capped_count": 2,
            "sample_job_ids": ["aaaa1111-x", "bbbb2222-y"],
        })
        # When count exceeds the shown sample, say so.
        assert "of 42" in body

    def test_partial_backlog_body_survives_missing_fields(self):
        from events.formatting import partial_backlog_body
        # Never crash on a degraded payload.
        body = partial_backlog_body({})
        assert body
        assert "{" not in body

    def test_whatsapp_header_uses_plain_title_for_watchdog_burst(self):
        from events.formatting import format_whatsapp_header
        e = _make_event(EventType.WATCHDOG_BURST, source="watchdog",
                        priority=Priority.HIGH)
        h = format_whatsapp_header(e)
        assert "SYSTEM HEALTH ALERT" in h
        assert "WATCHDOG_BURST" not in h
        assert "watchdog" in h  # source retained

    def test_whatsapp_header_falls_back_to_enum_for_unmapped_types(self):
        from events.formatting import format_whatsapp_header
        e = _make_event(EventType.CRON_COMPLETED, source="polish-verify")
        assert "CRON_COMPLETED" in format_whatsapp_header(e)


def test_resource_pressure_has_distinct_icon():
    """RESOURCE_PRESSURE (2026-06-11 pagefile-burst remediation) must have a
    present, distinct icon so an operator scanning watchdog_alerts can spot a
    commit/disk/pagefile-pressure alert at a glance. Like SECRET_DETECTED and
    WATCHDOG_BURST, the icon must not collide with a priority dot (the event is
    HIGH-priority, so a colored-dot icon would render adjacent to its own dot).
    """
    e = Event.create(
        event_type=EventType.RESOURCE_PRESSURE,
        source="system",
        payload={
            "reasons": ["commit_high"],
            "commit_pct": 98.4,
            "disk_c_free_gb": 12.3,
        },
        priority=Priority.HIGH,
    )
    icon = event_icon(e)
    assert icon, "RESOURCE_PRESSURE must have a non-empty icon"
    assert icon not in PRIORITY_EMOJI.values()


class TestCodeDriftBody:
    def _payload(self, **kw):
        p = {
            "status": "drifting", "state": "behind",
            "head": "aaaaaaaaa", "main": "bbbbbbbbb",
            "behind_count": 3, "ahead_count": 0, "dirty": False,
            "missed_subjects": ["c1 fix one", "c2 fix two"],
            "repo": "C:/Users/diego/.hermes/agent-src",
        }
        p.update(kw)
        return p

    def test_behind_body_is_plain_language(self):
        from events.formatting import code_drift_body
        body = code_drift_body(self._payload())
        assert "LAGS main by 3 commit(s)" in body
        assert "c1 fix one" in body
        assert "merge --ff-only main" in body
        assert "restart the gateway" in body
        # No raw dict/list splat.
        assert "{" not in body and "[" not in body

    def test_dirty_flag_rendered(self):
        from events.formatting import code_drift_body
        assert "DIRTY" in code_drift_body(self._payload(dirty=True))

    def test_ahead_body(self):
        from events.formatting import code_drift_body
        body = code_drift_body(self._payload(
            state="ahead", behind_count=0, ahead_count=2, missed_subjects=[]))
        assert "AHEAD of main by 2 commit(s)" in body

    def test_diverged_body(self):
        from events.formatting import code_drift_body
        body = code_drift_body(self._payload(state="diverged"))
        assert "DIVERGED" in body

    def test_resolved_body(self):
        from events.formatting import code_drift_body
        body = code_drift_body({"status": "resolved", "head": "bbbbbbbbb",
                                "main": "bbbbbbbbb", "repo": "x"})
        assert "back in sync" in body
        assert "bbbbbbbbb" in body

    def test_master_trunk_remediation_names_master_not_main(self):
        """~/.hermes has no `main` branch: telling the operator to run
        `merge --ff-only main` there hands them a fatal command."""
        from events.formatting import code_drift_body
        body = code_drift_body(self._payload(
            repo="C:/Users/diego/.hermes", repo_name="hermes",
            trunk="bbbbbbbbb", trunk_name="master",
        ))
        assert "LAGS master by 3 commit(s)" in body
        assert "merge --ff-only master" in body
        assert "ff-only main" not in body

    def test_trunk_missing_body_names_ref_and_is_unmeasurable(self):
        from events.formatting import code_drift_body
        body = code_drift_body({
            "status": "warn", "state": "trunk_missing",
            "key": "hermes", "repo": "C:/Users/diego/.hermes",
            "trunk_ref": "refs/heads/main", "branch": "master",
            "head": "aaaaaaaaa", "main": "",
        })
        assert "refs/heads/main" in body
        assert "C:/Users/diego/.hermes" in body
        assert "UNMEASURABLE" in body
        assert "in sync" not in body

    def test_behind_body_names_branch_trunk_and_executed_files(self):
        from events.formatting import code_drift_body
        body = code_drift_body({
            "status": "drifting", "state": "behind", "key": "hermes",
            "repo": "C:/Users/diego/.hermes",
            "trunk_ref": "refs/heads/master",
            "branch": "feat/manifest-router", "behind_count": 62,
            "head": "aaaaaaaaa", "main": "bbbbbbbbb",
            "executed_changed": ["scripts/gateway_watchdog.py"],
        })
        assert "feat/manifest-router" in body
        assert "master" in body
        assert "62" in body
        assert "scripts/gateway_watchdog.py" in body
        assert "re-point" in body

    def test_executed_files_are_capped_at_five(self):
        from events.formatting import code_drift_body
        body = code_drift_body({
            "status": "drifting", "state": "behind", "repo": "x",
            "trunk_ref": "refs/heads/master", "branch": "master",
            "behind_count": 9,
            "executed_changed": [f"scripts/s{i}.py" for i in range(20)],
        })
        assert body.count("scripts/s") == 5

    def test_resolved_header_dot_is_green(self):
        """A CODE_DRIFT resolution reads as green, mirroring the
        GATEWAY_HEALTH 'up' override — recovery, not an alert."""
        from events.formatting import header_dot
        e = _make_event(EventType.CODE_DRIFT, source="system",
                        payload={"status": "resolved"})
        assert header_dot(e) == PRIORITY_EMOJI[Priority.LOW]


class TestBootSummaryBody:
    """BOOT_SUMMARY bodies (2026-07-27).

    laptop-start.ps1 used to post its boot report as raw text straight at the
    watchdog_alerts thread, so it was the one message in the feed with no
    priority dot, icon or source/timestamp header. Routing it through the bus
    fixes the header but hands `failures`/`anomalies` — both LISTS — to the
    notifier's generic fallback, which splats Python list reprs. These pin the
    plain-language body that replaces it.
    """

    def _payload(self, **over):
        p = {
            "boot_id": "20260727-132212", "state": "failed",
            "total": 22, "done": 20, "failed": 2, "skipped": 1,
            "failures": ["[critical] gbrain-http: port 7483 never opened",
                         "[important] mempalace: timed out after 90s"],
            "anomalies": ["task-329-kill: soak task killed at 578s"],
        }
        p.update(over)
        return p

    def test_headline_carries_boot_id_state_and_counts(self):
        from events.formatting import boot_summary_body
        body = boot_summary_body(self._payload())
        head = body.splitlines()[0]
        assert "20260727-132212" in head
        assert "FAILED" in head
        assert "20/22" in head
        assert "2 failed" in head
        assert "1 skipped" in head

    def test_failures_and_anomalies_are_listed_as_lines(self):
        from events.formatting import boot_summary_body
        body = boot_summary_body(self._payload())
        assert "gbrain-http: port 7483 never opened" in body
        assert "mempalace: timed out after 90s" in body
        assert "task-329-kill: soak task killed at 578s" in body
        # Never a raw Python list repr — that is the bug this replaces.
        assert "['" not in body and "']" not in body

    def test_long_lists_are_bounded_with_a_remainder_line(self):
        from events.formatting import boot_summary_body
        body = boot_summary_body(self._payload(
            failures=[f"[critical] svc-{i}: down" for i in range(12)]))
        assert "svc-0: down" in body
        assert "svc-11" not in body
        assert "7 more" in body

    def test_clean_boot_still_yields_a_body(self):
        """The producer only emits on trouble, but a hand-run emit with no
        failures must not produce an empty message (an empty body would strand
        the header alone in the feed)."""
        from events.formatting import boot_summary_body
        body = boot_summary_body(self._payload(
            state="done", done=22, failed=0, skipped=0,
            failures=[], anomalies=[]))
        assert body.strip()
        assert "22/22" in body

    def test_empty_payload_does_not_raise(self):
        from events.formatting import boot_summary_body
        assert boot_summary_body({}).strip()

    def test_icon_matches_the_laptop_start_fallback_glyph(self):
        """laptop-start.ps1's non-bus fallback header hardcodes U+1F97E so a
        fallback message looks like the bus-rendered one. If this icon changes,
        that fallback must change with it."""
        e = _make_event(EventType.BOOT_SUMMARY, source="laptop-start")
        assert event_icon(e) == "\U0001F97E"

    def test_whatsapp_header_uses_plain_language(self):
        e = _make_event(EventType.BOOT_SUMMARY, source="laptop-start")
        header = format_whatsapp_header(e)
        assert "BOOT_SUMMARY" not in header
        assert "BOOT PROBLEMS" in header


# --- resource_pressure severity bands (2026-08-14) --------------------------

def _pressure_payload(free_gb, band, edge, change="band_change"):
    return {
        "reasons": ["disk_low", "disk_critical"],
        "commit_used_gb": 83.32, "commit_limit_gb": 127.2, "commit_pct": 65.5,
        "phys_used_pct": 75.8, "phys_available_gb": 15.3,
        "pagefile_allocated_gb": 64.0, "pagefile_growth_gb_10min": 0.0,
        "disk_c_free_gb": free_gb, "disk_band": band,
        "disk_band_edge_gb": edge, "change": change,
        "thresholds": {"disk_free_gb": 45.0, "disk_free_gb_critical": 25.0},
    }


def test_resource_pressure_body_leads_with_the_band_and_names_the_disk():
    from events.formatting import resource_pressure_body
    body = resource_pressure_body(_pressure_payload(2.4, "imminent", 3))
    assert "IMMINENT" in body.upper()
    assert "2.4" in body


def test_band_changes_the_fingerprint_but_digits_alone_still_do_not():
    """The whole point of the 2026-08-14 band work.

    Measured before it: ONE fingerprint covered every disk_low event from
    56.63 GB free down to 0.0 GB, because normalize_for_fingerprint collapses
    digit runs. Severity has to live in LETTERS to be visible to the guard --
    while two readings inside the SAME band must still collapse, or a filling
    disk goes back to one message per tick.
    """
    from events.formatting import resource_pressure_body
    from events.noise_guards import normalize_for_fingerprint

    severe = resource_pressure_body(_pressure_payload(10.0, "severe", 12))
    severe_later = resource_pressure_body(_pressure_payload(7.5, "severe", 12))
    imminent = resource_pressure_body(_pressure_payload(2.4, "imminent", 3))

    assert normalize_for_fingerprint(severe) == normalize_for_fingerprint(severe_later)
    assert normalize_for_fingerprint(severe) != normalize_for_fingerprint(imminent)


def test_resource_pressure_body_without_a_band_still_renders():
    """Non-disk episodes (commit/phys/pagefile) carry disk_band=None."""
    from events.formatting import resource_pressure_body
    payload = _pressure_payload(300.0, None, None)
    payload["reasons"] = ["phys_high"]
    body = resource_pressure_body(payload)
    assert "phys_high" in body
    assert "None" not in body
