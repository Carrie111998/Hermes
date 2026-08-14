"""Tests for Discord local reliability telemetry (feature R4)."""

import threading

import pytest

from plugins.platforms.discord.reliability_telemetry import (
    CONNECT,
    DELIVERY_FAILURE,
    DROPPED_ATTACHMENT,
    RATE_LIMIT_429,
    READY,
    RECONNECT,
    SYNC,
    UNAUTHORIZED,
    VOICE_ATTACH,
    VOICE_DETACH,
    CallbackSink,
    ReliabilityTelemetry,
    ReliabilityTelemetryError,
    TelemetrySink,
)


def test_increment_and_get():
    telemetry = ReliabilityTelemetry()
    assert telemetry.get_count(CONNECT) == 0

    telemetry.record_event(CONNECT)
    telemetry.record_event(CONNECT)
    telemetry.record_event(READY)

    assert telemetry.get_count(CONNECT) == 2
    assert telemetry.get_count(READY) == 1
    assert telemetry.get_count(RECONNECT) == 0


def test_record_event_with_count_argument():
    telemetry = ReliabilityTelemetry()
    telemetry.record_event(RATE_LIMIT_429, count=5)
    assert telemetry.get_count(RATE_LIMIT_429) == 5


def test_snapshot_returns_all_recorded_events():
    telemetry = ReliabilityTelemetry()
    telemetry.record_event(SYNC)
    telemetry.record_event(SYNC)
    telemetry.record_event(DELIVERY_FAILURE)

    snap = telemetry.snapshot()
    assert snap == {SYNC: 2, DELIVERY_FAILURE: 1}

    # Snapshot is a copy; mutating it must not affect the telemetry.
    snap[SYNC] = 999
    assert telemetry.get_count(SYNC) == 2


def test_snapshot_empty_when_nothing_recorded():
    telemetry = ReliabilityTelemetry()
    assert telemetry.snapshot() == {}


def test_unknown_event_name_rejected():
    telemetry = ReliabilityTelemetry()
    with pytest.raises(ReliabilityTelemetryError) as excinfo:
        telemetry.record_event("bogus_event")
    assert isinstance(excinfo.value, ValueError)
    assert telemetry.snapshot() == {}


def test_empty_event_name_rejected():
    telemetry = ReliabilityTelemetry()
    with pytest.raises(ReliabilityTelemetryError):
        telemetry.record_event("")
    assert telemetry.snapshot() == {}


def test_non_string_event_name_rejected():
    telemetry = ReliabilityTelemetry()
    with pytest.raises(ReliabilityTelemetryError):
        telemetry.record_event(12345)
    assert telemetry.snapshot() == {}


def test_all_predefined_event_names_are_accepted():
    telemetry = ReliabilityTelemetry()
    for name in (
        CONNECT,
        READY,
        RECONNECT,
        SYNC,
        RATE_LIMIT_429,
        DROPPED_ATTACHMENT,
        DELIVERY_FAILURE,
        VOICE_ATTACH,
        VOICE_DETACH,
        UNAUTHORIZED,
    ):
        telemetry.record_event(name)
    snap = telemetry.snapshot()
    assert len(snap) == 10
    assert all(value == 1 for value in snap.values())


def test_callback_sink_receives_snapshot():
    received = []
    telemetry = ReliabilityTelemetry(sink=CallbackSink(received.append))

    telemetry.record_event(CONNECT)
    telemetry.record_event(CONNECT)
    telemetry.record_event(UNAUTHORIZED)

    snap = telemetry.flush()
    assert snap == {CONNECT: 2, UNAUTHORIZED: 1}
    assert received == [{CONNECT: 2, UNAUTHORIZED: 1}]


def test_default_sink_is_noop():
    # Default sink must be a no-op: flush with no sink must not raise.
    telemetry = ReliabilityTelemetry()
    telemetry.record_event(CONNECT)
    assert telemetry.flush() == {CONNECT: 1}
    assert isinstance(telemetry._sink, TelemetrySink)


def test_thread_safety_concurrent_recording():
    telemetry = ReliabilityTelemetry()
    threads = 8
    per_thread = 250

    def worker():
        for _ in range(per_thread):
            telemetry.record_event(CONNECT)
            telemetry.record_event(DELIVERY_FAILURE)

    workers = [threading.Thread(target=worker) for _ in range(threads)]
    for t in workers:
        t.start()
    for t in workers:
        t.join()

    assert telemetry.get_count(CONNECT) == threads * per_thread
    assert telemetry.get_count(DELIVERY_FAILURE) == threads * per_thread
