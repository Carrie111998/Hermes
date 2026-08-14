"""End-to-end replay of the REAL 2026-08-14 disk episode.

Producer -> bus -> TelegramNotifier, driven by the actual C:-free readings and
inter-sample gaps recorded in ``~/.hermes/events/event_bus.db`` that morning
(03:50:27Z -> 11:08:52Z, 27 samples): the disk fell 13.57 GB -> 0.0 GB and then
sat at ~zero for seven hours.

What that episode delivered under the pre-band code, measured from the bus: an
undifferentiated message roughly once an hour, saying nothing at 0.0 GB that it
had not already said at 13 GB -- because ``normalize_for_fingerprint`` collapses
digit runs, so every one of those samples shared a single fingerprint.

Both tests pin the notifier's RepeatGuard window to 0 so the guard can never
suppress anything. Whatever these tests observe is therefore attributable to
the producer's ``change`` stamp alone -- otherwise the guard's own 30-min
collapse would make them pass for the wrong reason.
"""

import json

import pytest

from events.bus import EventBus
from events.noise_guards import RepeatGuard
from events.schema import EventType
from events.producers.resource_monitor import ResourcePressureMonitor
from events.subscribers.telegram_notifier import TelegramNotifier

from tests.events.producers.test_resource_monitor import make_sample

# (seconds since 03:50:27Z, C: free GB) -- verbatim from the bus.
REAL_DESCENT = [
    (0, 13.57), (908, 11.94), (1817, 11.46), (2728, 11.04), (3635, 5.44),
    (4544, 4.98), (5453, 4.47), (6360, 0.0), (7268, 0.0), (9079, 0.2),
    (9985, 0.03), (10891, 0.05), (11799, 0.0), (12705, 0.0), (13611, 0.0),
    (16333, 0.1), (17239, 0.21), (18147, 0.18), (19053, 0.14), (19961, 0.12),
    (20867, 0.0), (21772, 0.11), (22678, 0.13), (23586, 0.05), (24493, 0.03),
    (25400, 0.06), (26305, 0.0),
]


@pytest.fixture
def bus(tmp_path):
    return EventBus(db_path=tmp_path / "events" / "event_bus.db")


@pytest.fixture
def topics_config(tmp_path):
    path = tmp_path / "telegram" / "topics.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "group_chat_id": "-1001234567890",
        "topics": {
            "security_and_system": {"thread_id": 106, "name": "Security & System"},
            "action_required": {"thread_id": 107, "name": "Action Required"},
        },
    }))
    return path


@pytest.fixture
def verbosity_config(tmp_path):
    path = tmp_path / "telegram" / "verbosity.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "security_and_system": {"mode": "all"},
        "action_required": {"mode": "all"},
    }))
    return path


def _replay(bus, topics_config, verbosity_config, readings):
    """Drive the real producer into the real notifier; return sent messages."""
    sent = []
    monitor = ResourcePressureMonitor(bus)
    notifier = TelegramNotifier(
        bus, topics_path=topics_config, verbosity_path=verbosity_config,
        send_fn=lambda chat_id, thread_id, msg: sent.append(msg),
    )
    # Disarm the repeat guard so ONLY the change stamp can suppress.
    notifier._repeat_guard = RepeatGuard(window_seconds=0.0)

    seen = 0
    for offset, free_gb in readings:
        monitor.evaluate(make_sample(disk_free_gb=free_gb), now=float(offset))
        events = bus.query(event_type=EventType.RESOURCE_PRESSURE)
        for event in events[seen:]:
            notifier.handle(event)
        seen = len(events)
    return sent


def test_the_real_descent_pages_once_per_severity_step(
    bus, topics_config, verbosity_config,
):
    """13.57 GB -> 0.0 GB, then seven hours parked at zero.

    One message per band actually crossed, and NOTHING for the seven-hour
    tail -- the disk cannot get worse than 'full', so it stops talking.
    """
    sent = _replay(bus, topics_config, verbosity_config, REAL_DESCENT)

    # Anchor on the band marker, not a bare substring: the reasons line
    # carries "disk_critical", so a plain "CRITICAL" in msg matches every
    # message and the assertion would pass on garbage.
    def band_of(msg):
        for label in ("LOW", "CRITICAL", "SEVERE", "EMERGENCY", "IMMINENT", "FULL"):
            if f"— {label}" in msg.upper():
                return label
        return None

    assert [band_of(m) for m in sent] == [
        "CRITICAL", "SEVERE", "EMERGENCY", "FULL"]

    # The tail: 19 real samples over ~5.5h, every one at <= 0.21 GB free,
    # after 'full' was announced at t=6360. Not one of them is a message --
    # the disk cannot get worse than 'full', so it stops talking.
    tail = [r for r in REAL_DESCENT if r[0] > 6360]
    assert len(tail) == 19
    assert max(free for _, free in tail) <= 0.21


def test_a_stable_episode_survives_the_missed_tick_that_used_to_leak(
    bus, topics_config, verbosity_config,
):
    """The measured noise source: 11 of 15 deliveries that day came from the
    1800s guard window expiring, because the ~906s re-ping cadence means TWO
    intervals (~1812s) always exceed it. Here the disk hovers inside one band
    for four hours, including a skipped sample that opens a 1812s gap and a
    second that opens 2722s -- the exact shape that leaked before.
    """
    hovering = [
        (0, 9.4), (906, 9.1), (1812, 8.8), (3624, 9.2),   # 1812s gap
        (4530, 8.9), (5436, 9.3), (8158, 8.7),            # 2722s gap
        (9064, 9.0), (9970, 8.6), (10876, 9.1),
    ]
    sent = _replay(bus, topics_config, verbosity_config, hovering)

    assert len(sent) == 1
    assert "SEVERE" in sent[0].upper()
