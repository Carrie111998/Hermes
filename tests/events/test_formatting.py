"""Tests for events.formatting emoji + header helpers."""
from events.formatting import (
    PRIORITY_EMOJI, EVENT_TYPE_EMOJI, MAILBOX_INNER_EMOJI,
    SEPARATOR,
    priority_dot, event_icon,
    format_header, format_event_message, format_whatsapp_message,
    format_whatsapp_header,
)
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
    for et in EventType:
        assert EVENT_TYPE_EMOJI.get(et), f"missing icon for {et.type_string}"


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
    """WATCHDOG_BURST gets a distinct icon (🌀) so operators can scan for it."""
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
        assert "degraded" in body
        assert "15m" in body

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
