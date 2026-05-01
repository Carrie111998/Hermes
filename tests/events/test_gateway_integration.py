"""Tests for events.gateway_integration — startup/shutdown wiring."""

import json
import time

import pytest

# Force cron.scheduler to load from the worktree BEFORE any test calls
# gi.startup() — startup transitively imports obs/oauth_llm.py which
# inserts ~/.hermes/agent-src at sys.path[0], shadowing the cwd-relative
# worktree path. Importing cron.scheduler here pins the cached module to
# the worktree's version, which is what subsequent patch.object() calls
# need to find flush_inflight_aborts.
from cron import scheduler as _cron_scheduler  # noqa: F401  -- side-effect import
from events import gateway_integration as gi
from events.paths import gateway_heartbeat_path
from events.schema import EventType
from events.subscribers.mailbox_translator import MailboxTranslator


SCHEDULE = [8, 13, 18]


class TestPickDigestTarget:
    """Covers the digest-hour catch-up decision used by the subscriber poll
    loop.

    First-and-latest semantics (2026-04-19): returns ONE key per call,
    preferring the first missed scheduled hour of today so the morning
    overnight summary never gets lost after an outage, then the latest
    missed hour on the next tick once the first has fired.  Middle hours
    are skipped by design.

    Regression shield against two distinct bugs:
      1. The legacy ``et_hour == 7`` equality check that silently skipped
         any digest whose scheduled hour the gateway was offline for.
      2. The 2026-04-19 "latest-only" rule that permanently dropped the
         morning overnight summary after a mid-day restart.
    """

    def test_no_applicable_hour_returns_none(self):
        # 5am ET — before the first scheduled hour today
        assert gi._pick_digest_target(5, "2026-04-19", set(), SCHEDULE) is None

    def test_first_fire_of_the_day_returns_first_key(self):
        assert gi._pick_digest_target(8, "2026-04-19", set(), SCHEDULE) == "2026-04-19-08"

    def test_same_hour_already_fired_returns_none(self):
        assert gi._pick_digest_target(8, "2026-04-19", {"2026-04-19-08"}, SCHEDULE) is None

    def test_first_unfired_preferred_over_latest(self):
        # 1pm ET, none fired today.  First-and-latest: fire the first (8am)
        # FIRST so the morning overnight summary is preserved — NOT the
        # latest (13h).  The next tick will fire the latest once this one
        # has landed in fired_keys.
        assert gi._pick_digest_target(13, "2026-04-19", set(), SCHEDULE) == "2026-04-19-08"

    def test_after_first_fired_latest_is_returned(self):
        # Previous tick caught up the 8am digest; this tick fires the latest.
        assert gi._pick_digest_target(
            13, "2026-04-19", {"2026-04-19-08"}, SCHEDULE,
        ) == "2026-04-19-13"

    def test_first_and_latest_both_fired_returns_none(self):
        # Both done for today — nothing to fire until tomorrow.
        assert gi._pick_digest_target(
            18, "2026-04-19", {"2026-04-19-08", "2026-04-19-18"}, SCHEDULE,
        ) is None

    def test_middle_hour_is_skipped_when_first_fired(self):
        # At 20h with only 8 fired, fire 18 next — NOT 13.  The middle hour
        # is permanently skipped: the latest digest's pipeline snapshot
        # covers current state better than a stale middle-of-day replay.
        assert gi._pick_digest_target(
            20, "2026-04-19", {"2026-04-19-08"}, SCHEDULE,
        ) == "2026-04-19-18"

    def test_next_day_same_hour_re_fires(self):
        # Yesterday's 8am key must not suppress today's 8am.  Caller prunes
        # fired_keys to today-only before passing in; this asserts that if
        # the set is empty for today, we fire normally.
        assert gi._pick_digest_target(8, "2026-04-20", set(), SCHEDULE) == "2026-04-20-08"

    def test_gateway_offline_all_day_catchup_fires_first_first(self):
        # Came up at 20h with nothing fired — fire 8 first (morning overnight
        # summary).  The next tick will fire 18 (latest).  The middle 13 is
        # permanently skipped.  This is the canonical catch-up scenario from
        # the 2026-04-19 morning-digest-loss incident.
        assert gi._pick_digest_target(20, "2026-04-19", set(), SCHEDULE) == "2026-04-19-08"

    def test_uses_module_default_schedule_when_arg_omitted(self):
        # Sanity: confirm the helper wires DIGEST_SCHEDULE_HOURS when the
        # caller doesn't pass schedule_hours — the poll loop relies on this.
        from events.subscribers.digest_composer import DIGEST_SCHEDULE_HOURS
        first_scheduled = sorted(DIGEST_SCHEDULE_HOURS)[0]
        assert gi._pick_digest_target(first_scheduled, "2026-04-19", set()) == \
            f"2026-04-19-{first_scheduled:02d}"


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


def test_poll_loop_flushes_telegram_notifier_batches():
    """The LOW-priority batch flush must be driven from the poll loop's timer,
    not only from inside ``TelegramNotifier.handle()``.

    Regression guard against the 2026-04-19 observation that a batched LOW
    mailbox_message (the smoke-test NOTIFICATION) sat in ``notifier_batch.json``
    for 10+ minutes past its 300s threshold because no other events arrived to
    trigger an inner-``handle()`` flush.  On a mostly-quiet bus, users would
    wait indefinitely for a message the notifier believes is already "batched
    for up to 5 minutes".
    """
    from unittest.mock import MagicMock
    from events.subscribers.telegram_notifier import TelegramNotifier

    gi.startup()
    try:
        notifier = next(
            s for s in gi._registry.subscribers
            if isinstance(s, TelegramNotifier)
        )
        flush_mock = MagicMock()
        notifier._flush_stale_batches = flush_mock

        # The first poll-loop tick satisfies ``now - last_batch_flush >= 60``
        # (last_batch_flush starts at 0, time.monotonic() is always larger),
        # so the flush fires within the first 1s iteration.  Give it 2s to
        # absorb scheduling jitter on slow runners.
        time.sleep(2.0)

        assert flush_mock.called, (
            "Poll loop did not invoke TelegramNotifier._flush_stale_batches — "
            "LOW-priority batched messages will sit past their 300s threshold "
            "until the next incoming event arrives."
        )
    finally:
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


class TestShutdownEmitsCronAborted:
    """Guard #1 (2026-04-30): shutdown() must drain the in-flight cron
    registry into cron_aborted events BEFORE closing the bus, so
    audit.jsonl never accumulates dangling cron_started rows after a
    gateway restart.

    Patches use ``patch.object`` against the loaded module rather than
    string-based dotted names because the editable-install MAPPING and
    sys.path-based PathFinder can resolve ``cron.scheduler`` to different
    files. ``patch.object(scheduler, ...)`` patches the actual loaded
    module — the same one ``gi.shutdown()`` will resolve via the regular
    Python import system.
    """

    def test_shutdown_calls_flush_inflight_aborts_with_gateway_shutdown_reason(self):
        """Wiring contract: shutdown() invokes flush_inflight_aborts with
        the literal "gateway_shutdown" reason string."""
        from unittest.mock import patch
        from cron import scheduler

        gi.startup()
        try:
            with patch.object(scheduler, "flush_inflight_aborts", return_value=0) as flush_mock:
                gi.shutdown()
                flush_mock.assert_called_once_with("gateway_shutdown")
        finally:
            if gi._bus is not None:
                gi.shutdown()

    def test_shutdown_polls_subscribers_when_aborts_emitted(self):
        """When flush_inflight_aborts emits >= 1 cron_aborted, shutdown()
        forces a synchronous registry.poll_all() so AuditLogger writes the
        events to audit.jsonl in the same shutdown cycle (rather than
        waiting for the next gateway start to drain them)."""
        from unittest.mock import patch, MagicMock
        from cron import scheduler

        gi.startup()
        try:
            assert gi._registry is not None
            poll_all_spy = MagicMock(wraps=gi._registry.poll_all)
            gi._registry.poll_all = poll_all_spy
            with patch.object(scheduler, "flush_inflight_aborts", return_value=2):
                gi.shutdown()
            poll_all_spy.assert_called_once()
        finally:
            if gi._bus is not None:
                gi.shutdown()

    def test_shutdown_skips_poll_all_when_no_aborts(self):
        """When flush_inflight_aborts returns 0, the synchronous poll
        is skipped — no point spinning subscribers if nothing was emitted."""
        from unittest.mock import patch, MagicMock
        from cron import scheduler

        gi.startup()
        try:
            assert gi._registry is not None
            poll_all_spy = MagicMock(wraps=gi._registry.poll_all)
            gi._registry.poll_all = poll_all_spy
            with patch.object(scheduler, "flush_inflight_aborts", return_value=0):
                gi.shutdown()
            poll_all_spy.assert_not_called()
        finally:
            if gi._bus is not None:
                gi.shutdown()

    def test_shutdown_survives_flush_exception(self):
        """A broken flush_inflight_aborts must not wedge the rest of
        shutdown — the global bus + thread state must still tear down."""
        from unittest.mock import patch
        from cron import scheduler

        gi.startup()
        try:
            with patch.object(
                scheduler,
                "flush_inflight_aborts",
                side_effect=RuntimeError("boom"),
            ):
                gi.shutdown()  # must NOT raise
            assert gi._bus is None
            assert gi._subscriber_thread is None
        finally:
            if gi._bus is not None:
                gi.shutdown()

    def test_shutdown_calls_flush_before_stop_event_and_bus_close(self):
        """Ordering contract: flush_inflight_aborts runs BEFORE _stop_event
        is set and BEFORE the bus is closed. Otherwise events emitted by
        the flush would have nowhere to land."""
        from unittest.mock import patch
        from cron import scheduler

        gi.startup()
        try:
            order = []

            def fake_flush(reason):
                order.append((
                    reason,
                    gi._bus is not None,
                    not gi._stop_event.is_set(),
                ))
                return 0

            with patch.object(scheduler, "flush_inflight_aborts", side_effect=fake_flush):
                gi.shutdown()

            assert len(order) == 1
            reason, bus_open, stop_unset = order[0]
            assert reason == "gateway_shutdown"
            assert bus_open, "bus must still be open when flush runs"
            assert stop_unset, "stop_event must still be unset when flush runs"
        finally:
            if gi._bus is not None:
                gi.shutdown()


# Gateway lifecycle emission tests — added 2026-04-30 (M1 in
# profiles/sentinel/workspace/gateway-restart-cluster-2026-04-30.md).
# Behavioural surface: emit_gateway_started fires once per boot with the
# operator-relevant context; emit_gateway_stopped is idempotent so the
# graceful-path call + atexit + signal-handler triple don't triple-emit.


class _FakeBus:
    """Minimal EventBus stand-in that records emit() calls."""

    def __init__(self):
        self.events: list[dict] = []
        self.last_event_id = 0

    def emit(self, *, event_type, source, payload, priority=None,
             correlation_id=None, job_id=None, tags=None):
        self.last_event_id += 1
        self.events.append({
            "event_id": str(self.last_event_id),
            "event_type": event_type,
            "source": source,
            "payload": dict(payload),
            "priority": priority,
        })
        return str(self.last_event_id)


@pytest.fixture
def isolated_bus(monkeypatch):
    """Substitute the module-level _bus with a fake; reset the dedupe flag."""
    fake = _FakeBus()
    monkeypatch.setattr(gi, "_bus", fake)
    # Each test starts with a fresh dedupe flag and a known started_at, so
    # tests that exercise stopped-without-prior-started can still be assertive
    # about the runtime_seconds key (it should be absent rather than computed
    # against another test's leftover monotonic).
    monkeypatch.setattr(gi, "_gateway_stopped_emitted", False)
    monkeypatch.setattr(gi, "_gateway_started_at_monotonic", None)
    return fake


class TestEmitGatewayStarted:
    def test_emits_with_default_pid_payload(self, isolated_bus):
        gi.emit_gateway_started()
        assert len(isolated_bus.events) == 1
        ev = isolated_bus.events[0]
        assert ev["event_type"] == EventType.GATEWAY_STARTED
        assert ev["source"] == "gateway"
        assert ev["payload"]["pid"] > 0  # os.getpid() is always positive

    def test_merges_caller_payload(self, isolated_bus):
        gi.emit_gateway_started({
            "parent_pid": 12345,
            "parent_cmdline": "hermes gateway run",
            "boot_reason": "manual",
            "argv": ["hermes", "gateway", "run"],
        })
        ev = isolated_bus.events[0]
        assert ev["payload"]["parent_pid"] == 12345
        assert ev["payload"]["boot_reason"] == "manual"
        assert ev["payload"]["argv"] == ["hermes", "gateway", "run"]

    def test_records_monotonic_start_for_runtime_calc(self, isolated_bus):
        assert gi._gateway_started_at_monotonic is None
        gi.emit_gateway_started()
        assert gi._gateway_started_at_monotonic is not None

    def test_no_op_when_bus_is_none(self, monkeypatch):
        monkeypatch.setattr(gi, "_bus", None)
        # Must not raise even when the bus is unavailable. This is the path
        # taken if startup() failed mid-init.
        gi.emit_gateway_started({"pid": 1})


class TestEmitGatewayStopped:
    def test_emits_basic_payload(self, isolated_bus):
        gi.emit_gateway_stopped({"exit_reason": "graceful"})
        assert len(isolated_bus.events) == 1
        ev = isolated_bus.events[0]
        assert ev["event_type"] == EventType.GATEWAY_STOPPED
        assert ev["source"] == "gateway"
        assert ev["payload"]["pid"] > 0
        assert ev["payload"]["exit_reason"] == "graceful"

    def test_idempotent_across_multiple_calls(self, isolated_bus):
        # Graceful-path + atexit + signal-handler triple must not triple-emit.
        # This is the core dedupe guarantee that justifies wiring all three.
        gi.emit_gateway_stopped({"exit_reason": "graceful"})
        gi.emit_gateway_stopped({"exit_reason": "atexit"})
        gi.emit_gateway_stopped({"exit_reason": "signal", "signal": "SIGTERM"})
        assert len(isolated_bus.events) == 1
        # The first call wins — that's the most-informed exit reason since
        # the graceful path runs first and knows the most context.
        assert isolated_bus.events[0]["payload"]["exit_reason"] == "graceful"

    def test_runtime_seconds_computed_when_started_recorded(
        self, isolated_bus, monkeypatch
    ):
        # Pretend we started 1.5 seconds ago.
        monkeypatch.setattr(gi.time, "monotonic", lambda: 100.0)
        gi.emit_gateway_started()
        monkeypatch.setattr(gi.time, "monotonic", lambda: 101.5)
        gi.emit_gateway_stopped({"exit_reason": "graceful"})
        # Last event is the STOPPED one (started + stopped both emitted).
        stopped_ev = isolated_bus.events[-1]
        assert stopped_ev["payload"]["runtime_seconds"] == 1.5

    def test_runtime_seconds_omitted_when_started_never_recorded(
        self, isolated_bus
    ):
        # If somehow we land in shutdown without having recorded a start,
        # don't fabricate a runtime_seconds value.
        gi.emit_gateway_stopped({"exit_reason": "atexit"})
        ev = isolated_bus.events[0]
        assert "runtime_seconds" not in ev["payload"]

    def test_no_op_when_bus_is_none(self, monkeypatch):
        monkeypatch.setattr(gi, "_bus", None)
        monkeypatch.setattr(gi, "_gateway_stopped_emitted", False)
        gi.emit_gateway_stopped({"exit_reason": "atexit"})
        # No exception, and the dedupe flag stays unflipped so a later call
        # (after the bus comes back) can still emit the canonical event.
        assert gi._gateway_stopped_emitted is False
