"""Tests for events.gateway_integration — startup/shutdown wiring."""

import json
import time

from events import gateway_integration as gi
from events.paths import gateway_heartbeat_path
from events.subscribers.mailbox_translator import MailboxTranslator


SCHEDULE = [8, 13, 18]


class TestPickDigestTarget:
    """Covers the digest-hour catch-up decision used by the subscriber poll
    loop.  Regression shield against the 2026-04-19 ``et_hour == 7``
    equality check that silently skipped any digest whose scheduled hour
    the gateway happened to be offline for."""

    def test_no_applicable_hour_returns_none(self):
        # 5am ET — before the first scheduled hour today
        assert gi._pick_digest_target(5, "2026-04-19", "", SCHEDULE) is None

    def test_first_fire_of_the_day_returns_key(self):
        assert gi._pick_digest_target(8, "2026-04-19", "", SCHEDULE) == "2026-04-19-08"

    def test_same_hour_already_fired_returns_none(self):
        assert gi._pick_digest_target(8, "2026-04-19", "2026-04-19-08", SCHEDULE) is None

    def test_later_hour_catchup_fires_latest_only(self):
        # 1pm ET, gateway just came up, never fired today — fire 13 only,
        # NOT 8 too.  Back-to-back catch-up digests would be noise.
        assert gi._pick_digest_target(13, "2026-04-19", "", SCHEDULE) == "2026-04-19-13"

    def test_next_day_same_hour_re_fires(self):
        # Yesterday's 8am key must not suppress today's 8am.  This is why
        # the key is date-qualified, not a bare int hour.
        assert gi._pick_digest_target(8, "2026-04-20", "2026-04-19-08", SCHEDULE) == "2026-04-20-08"

    def test_gateway_offline_all_day_fires_latest_at_night(self):
        # Came up at 20h — latest applicable is 18h.  This is the canonical
        # catch-up scenario from the 2026-04-19 incident.
        assert gi._pick_digest_target(20, "2026-04-19", "", SCHEDULE) == "2026-04-19-18"

    def test_uses_module_default_schedule_when_arg_omitted(self):
        # Sanity: confirm the helper wires DIGEST_SCHEDULE_HOURS when the
        # caller doesn't pass schedule_hours — the poll loop relies on this.
        from events.subscribers.digest_composer import DIGEST_SCHEDULE_HOURS
        latest_scheduled = max(DIGEST_SCHEDULE_HOURS)
        assert gi._pick_digest_target(latest_scheduled, "2026-04-19", "") == \
            f"2026-04-19-{latest_scheduled:02d}"


def test_mailbox_translator_registered_at_startup():
    gi.startup()
    try:
        subs = gi._registry.subscribers
        assert any(isinstance(s, MailboxTranslator) for s in subs), (
            "MailboxTranslator must be registered at gateway startup"
        )
    finally:
        gi.shutdown()


def test_poll_loop_survives_unexpected_exception_in_body():
    """Outer try/except must keep the polling thread alive when something
    escapes the inner per-block try/excepts.

    Regression guard against the silent-notification failure mode: if the
    poll thread dies, all subscribers stop forever and the user sees only
    silence.  We simulate that by replacing ``registry.subscribers`` with
    an iterable that raises on every iteration — without the outer safety
    net the thread would die on the first tick.
    """
    gi.startup()
    try:
        class _PoisonPillIterable:
            def __iter__(self):
                raise RuntimeError("synthetic iteration failure for test")

        gi._registry.subscribers = _PoisonPillIterable()

        # Wait long enough for at least two 1s loop ticks to occur.
        time.sleep(2.5)

        assert gi._subscriber_thread is not None
        assert gi._subscriber_thread.is_alive(), (
            "Poll loop thread must survive exceptions that escape the "
            "inner try/excepts — otherwise notifications silently stop"
        )
    finally:
        # Restore a real list so shutdown() can iterate subscribers.
        if gi._registry is not None:
            gi._registry.subscribers = []
        gi.shutdown()


def test_poll_loop_writes_heartbeat_file():
    """The poll loop must write a heartbeat file so external watchers can
    detect gateway death.

    Consumers (mission-control, cron probes) stat the mtime and alert on
    staleness.  This test asserts the file appears shortly after startup
    and contains the documented JSON schema.
    """
    heartbeat = gateway_heartbeat_path()
    # Don't rely on a pre-existing file — previous test runs may have left one.
    if heartbeat.exists():
        prev_mtime = heartbeat.stat().st_mtime
    else:
        prev_mtime = -1.0

    gi.startup()
    try:
        # First tick initializes last_heartbeat=0, so the write should fire on
        # the first iteration (within ~1s).  Give it 2.5s to be safe.
        time.sleep(2.5)

        assert heartbeat.exists(), (
            f"Gateway heartbeat file was not written to {heartbeat}"
        )
        assert heartbeat.stat().st_mtime > prev_mtime, (
            "Heartbeat file exists but mtime didn't advance this run"
        )

        payload = json.loads(heartbeat.read_text(encoding="utf-8"))
        assert set(payload.keys()) >= {
            "ts", "pid", "subscriber_count", "uptime_seconds",
            "consecutive_outer_errors",
        }, f"Heartbeat payload missing required keys: {payload}"
        assert payload["pid"] > 0
        assert payload["subscriber_count"] >= 7  # 7 registered subscribers
        assert payload["uptime_seconds"] >= 0
        assert payload["consecutive_outer_errors"] == 0
    finally:
        gi.shutdown()
