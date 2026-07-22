"""Tests for events.gateway_integration — startup/shutdown wiring."""

import inspect
import json
import threading
import time

import pytest

from events import gateway_integration as gi
from events.producers.resource_monitor import ResourcePressureMonitor
from events.paths import gateway_heartbeat_path
from events.schema import EventType
from events.subscribers.mailbox_translator import MailboxTranslator
from events.subscribers.tracker_intent_applier import TrackerIntentApplierSubscriber


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
        # so the flush fires on the first ~1s iteration. Wait-until rather
        # than a fixed 2s sleep: the first tick also runs every subscriber
        # poll and the tick-zero health probes, which on a loaded host can
        # take far longer than 2s (observed >20s under the 2026-06-11
        # commit-charge incident). The regression being guarded is "the
        # flush is driven by the poll timer at all", not "within 2s".
        deadline = time.monotonic() + 20.0
        while not flush_mock.called and time.monotonic() < deadline:
            time.sleep(0.1)

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
        # the first iteration (within ~1s).  Wait-until rather than a fixed
        # 2.5s sleep: the first tick also runs every subscriber poll and the
        # tick-zero health probes, which on a loaded host can take far longer
        # than the window (same failure mode as the flush test above, observed
        # >20s under the 2026-06-11 commit-charge incident).  The guarded
        # behaviour is "the poll timer writes the heartbeat at all", not
        # "within 2.5s".
        deadline = time.monotonic() + 20.0
        while not (
            heartbeat.exists() and heartbeat.stat().st_mtime > prev_mtime
        ) and time.monotonic() < deadline:
            time.sleep(0.1)

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


class TestResourceMonitorWiring:
    """The ResourcePressureMonitor (2026-06-11 pagefile-burst remediation) must
    be constructed at startup and sampled by the poll loop. Verified via the
    accessor + source introspection rather than running the real startup():
    that boots the full subscriber registry + poll thread, which is heavy and
    timing-sensitive on loaded hosts — same reason the parallel
    _health_monitor poll-loop hook is asserted statically here. (Under pytest
    the bus is hermetic anyway: conftest points HERMES_HOME at a tempdir and
    ``get_default_hermes_root()`` returns a HERMES_HOME that lies outside
    ``~/.hermes`` as-is, so events.paths resolves into the tempdir.)
    """

    def test_getter_returns_module_global(self):
        assert gi.get_resource_monitor() is gi._resource_monitor

    def test_startup_constructs_resource_monitor(self):
        src = inspect.getsource(gi.startup)
        assert "_resource_monitor = ResourcePressureMonitor(_bus)" in src

    def test_poll_loop_samples_resource_pressure(self):
        # Guards against the per-tick sampling hook being dropped from the
        # subscriber poll loop.
        src = inspect.getsource(gi._subscriber_poll_loop)
        assert "_resource_monitor.check()" in src

    def test_default_sampler_is_real_resource_reader(self, tmp_path):
        # startup() constructs ResourcePressureMonitor(_bus) with no sampler
        # argument, so the gateway must fall back to the real in-process
        # sampler (GlobalMemoryStatusEx + shutil), not a test stub.
        from events.bus import EventBus
        from events.producers.resource_monitor import sample_resources
        bus = EventBus(db_path=tmp_path / "events" / "event_bus.db")
        assert ResourcePressureMonitor(bus)._sampler is sample_resources


class TestCodeDriftMonitorWiring:
    """CodeDriftMonitor (2026-07-21 stale-checkout remediation) must be
    constructed at startup and probed by the poll loop. Asserted statically
    for the same reasons as TestResourceMonitorWiring above."""

    def test_getter_returns_module_global(self):
        assert gi.get_code_drift_monitor() is gi._code_drift_monitor

    def test_startup_constructs_code_drift_monitor(self):
        src = inspect.getsource(gi.startup)
        assert "_code_drift_monitor = CodeDriftMonitor(_bus)" in src

    def test_poll_loop_probes_code_drift(self):
        src = inspect.getsource(gi._subscriber_poll_loop)
        assert "_code_drift_monitor.check()" in src


class TestDedicatedApplierThread:
    """The tracker-intent-applier must run on its OWN daemon thread, decoupled
    from the shared serial subscriber poll loop.

    Motivation (2026-07-13 starvation, proven live): all 13 subscribers poll
    serially in one thread. During a gateway restart the WhatsApp connect
    blocks ~30s and Telegram reconnects with ConnectionReset storms — both
    registered AHEAD of the 8th-registered applier — so the applier's ~1s
    filesystem scan was starved for MINUTES (intents posted 10:19-10:20 didn't
    apply until 10:25). A mutation stranded in that window can exceed even the
    90s dashboard fast-revert window, reviving the old-stage/revert bug.

    IntentApplier is single-threaded by design (is_applied/mark_applied and
    _move_to are not race-free), so it must be driven by EXACTLY ONE caller:
    the dedicated thread, never also the serial loop.
    """

    def test_startup_starts_dedicated_applier_thread(self):
        gi.startup()
        try:
            assert gi._applier_thread is not None, (
                "startup() must start a dedicated applier poll thread"
            )
            assert gi._applier_thread.is_alive(), (
                "the dedicated applier thread must be running after startup"
            )
            assert gi._applier_thread is not gi._subscriber_thread, (
                "the applier thread must be distinct from the shared serial "
                "subscriber thread — that separation is the whole fix"
            )
            # The applier subscriber must remain REGISTERED (startup() builds
            # its IntentApplier via startup_all); it is only skipped in the
            # serial *iteration*, not unregistered.
            assert any(
                isinstance(s, TrackerIntentApplierSubscriber)
                for s in gi._registry.subscribers
            ), "the applier subscriber must stay registered"
        finally:
            gi.shutdown()

    def test_shutdown_stops_dedicated_applier_thread(self):
        # A gateway restart calls startup() after shutdown(); if shutdown()
        # doesn't join+clear the applier thread, each restart leaks one.
        gi.startup()
        applier_thread = gi._applier_thread
        gi.shutdown()
        assert applier_thread is not None
        assert not applier_thread.is_alive(), (
            "shutdown() must stop the dedicated applier thread"
        )
        assert gi._applier_thread is None, (
            "shutdown() must clear the _applier_thread global so the next "
            "startup() doesn't join a dead thread"
        )

    def test_serial_loop_does_not_poll_the_applier(self):
        # Double-driving the single-threaded IntentApplier from both the
        # serial loop AND the dedicated thread would race is_applied/
        # mark_applied and _move_to. Attribute every poll() call to its
        # thread and assert none came from the serial subscriber thread.
        gi.startup()
        try:
            applier_sub = next(
                s for s in gi._registry.subscribers
                if isinstance(s, TrackerIntentApplierSubscriber)
            )
            callers = []
            lock = threading.Lock()

            def _recording_poll():
                with lock:
                    callers.append(threading.current_thread())
                return 0

            applier_sub.poll = _recording_poll

            # Let several ~1s ticks of both loops elapse.
            time.sleep(2.5)

            with lock:
                snapshot = list(callers)
            assert snapshot, (
                "applier was never polled — the dedicated thread is not "
                "driving it"
            )
            serial_calls = [t for t in snapshot if t is gi._subscriber_thread]
            assert not serial_calls, (
                f"the serial poll loop drove the applier {len(serial_calls)} "
                "time(s); it must be skipped in the serial iteration so the "
                "single-threaded IntentApplier has exactly one caller"
            )
        finally:
            gi.shutdown()

    def test_applier_not_starved_when_serial_loop_blocks(self):
        # Headline regression: poison the serial loop's subscriber iteration
        # so its whole tick blocks — the moral equivalent of a 30s WhatsApp
        # connect ahead of the applier. The dedicated thread must keep the
        # applier ticking because it holds a DIRECT reference to the
        # subscriber, not the registry list.
        gi.startup()
        release = threading.Event()
        entered_block = threading.Event()
        try:
            applier_sub = next(
                s for s in gi._registry.subscribers
                if isinstance(s, TrackerIntentApplierSubscriber)
            )
            count = {"n": 0}
            lock = threading.Lock()

            def _counting_poll():
                with lock:
                    count["n"] += 1
                return 0

            applier_sub.poll = _counting_poll

            class _BlockingIterable:
                def __iter__(self):
                    entered_block.set()
                    release.wait(timeout=30)
                    return iter([])

            gi._registry.subscribers = _BlockingIterable()

            # Only start counting once the serial loop is provably wedged, so
            # a pre-block serial poll can't be mistaken for an independent tick.
            assert entered_block.wait(timeout=5), (
                "serial loop never reached the blocking iterable"
            )
            with lock:
                count["n"] = 0
            time.sleep(3.5)
            with lock:
                observed = count["n"]

            assert observed >= 2, (
                f"applier polled only {observed} time(s) while the serial loop "
                "was blocked — it is still starved by the shared loop"
            )
        finally:
            release.set()
            if gi._registry is not None:
                gi._registry.subscribers = []
            gi.shutdown()


class TestPartialBacklogWiring:
    """The PartialBacklogMonitor (2026-07-14 partial-pileup remediation) must be
    constructed at startup and sampled by the SHARED subscriber poll loop; the
    auto-re-drive must be driven from the DEDICATED applier thread (single-writer
    invariant), gated at 60s. Verified via source introspection, like the
    ResourcePressureMonitor wiring above (real startup() is heavy/timing-sensitive).
    """

    def test_getter_returns_module_global(self):
        assert gi.get_partial_backlog_monitor() is gi._partial_backlog_monitor

    def test_startup_constructs_partial_backlog_monitor(self):
        src = inspect.getsource(gi.startup)
        assert "PartialBacklogMonitor(" in src

    def test_shared_loop_checks_partial_backlog(self):
        src = inspect.getsource(gi._subscriber_poll_loop)
        assert "_partial_backlog_monitor.check()" in src

    def test_applier_loop_redrives_on_its_own_thread(self):
        # Re-drive MUST be on the single-writer applier thread, gated to 60s.
        src = inspect.getsource(gi._applier_poll_loop)
        assert "redrive_partials()" in src
        assert "REDRIVE_INTERVAL_SECONDS" in src

    def test_shared_loop_does_not_redrive(self):
        # Guard the single-writer invariant: the shared loop must NOT re-drive
        # (that would race scan_inbox on the applier thread).
        src = inspect.getsource(gi._subscriber_poll_loop)
        assert "redrive_partials()" not in src
