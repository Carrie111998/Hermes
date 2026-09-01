"""Pure logic of the durable P6 silence watchdog: staleness + cooldown edges."""

import importlib.util
from pathlib import Path

# Load the script module by path (scripts/ is not a package).
_SPEC = importlib.util.spec_from_file_location(
    "p6_silence_watchdog",
    Path(__file__).resolve().parents[2] / "scripts" / "p6_silence_watchdog.py",
)
wd = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(wd)

NOW = 1_800_000_000.0


def test_evaluate_no_events_is_silent():
    silent, age = wd.evaluate(None, NOW, 1200.0)
    assert silent is True and age is None


def test_evaluate_fresh_is_healthy():
    silent, age = wd.evaluate(NOW - 300.0, NOW, 1200.0)
    assert silent is False and age == 300.0


def test_evaluate_boundary():
    # exactly at the threshold is NOT silent (strictly greater trips it)
    assert wd.evaluate(NOW - 1200.0, NOW, 1200.0)[0] is False
    assert wd.evaluate(NOW - 1200.1, NOW, 1200.0)[0] is True


def test_rising_edge_alerts_once_then_cooldown():
    state = {}
    action, state = wd.decide_emit(True, NOW, state, 3600.0)
    assert action == "alert" and state["silent"] is True
    # still silent, within cooldown -> no repeat
    action, state = wd.decide_emit(True, NOW + 600.0, state, 3600.0)
    assert action == "none"
    # cooldown elapsed -> re-alert
    action, state = wd.decide_emit(True, NOW + 3601.0, state, 3600.0)
    assert action == "alert"


def test_recovery_edge_emits_once_and_resets():
    state = {"silent": True, "last_alert_at": NOW}
    action, state = wd.decide_emit(False, NOW + 100.0, state, 3600.0)
    assert action == "recovered" and state["silent"] is False
    # healthy again -> nothing
    action, state = wd.decide_emit(False, NOW + 200.0, state, 3600.0)
    assert action == "none"


def test_healthy_from_empty_state_is_silent_noop():
    action, state = wd.decide_emit(False, NOW, {}, 3600.0)
    assert action == "none" and state["silent"] is False


def test_newest_result_epoch_takes_max():
    class R:
        def __init__(self, ts):
            self.timestamp = ts
    rows = [R("2026-08-31T15:00:00+00:00"), R("2026-08-31T15:10:00+00:00"),
            R("bad-timestamp"), R("2026-08-31T15:05:00+00:00")]
    epoch = wd.newest_result_epoch(rows)
    from datetime import datetime, timezone
    assert epoch == datetime(2026, 8, 31, 15, 10, tzinfo=timezone.utc).timestamp()
    assert wd.newest_result_epoch([]) is None
    assert wd.newest_result_epoch([R("nope")]) is None
