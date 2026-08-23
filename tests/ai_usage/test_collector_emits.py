"""Task 2: wiring ai_usage.quota_signal.evaluate() into collector.collect() so
findings become MODEL_RATE_LIMITED alerts via events.rate_limit_signal.record.

Phase 3 is REPORT-ONLY: Claude Code and the Codex CLI are separate processes
Hermes cannot reroute, so no button may ever appear on these alerts. Every
test here uses a fake record()/clear() -- no real event bus, no real state
file -- so these tests never depend on or mutate ~/.hermes state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from ai_usage.collector import collect

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)


@dataclass
class FakeWin:
    label: str
    used_percent: Optional[float]
    reset_at: Optional[datetime]


@dataclass
class FakeSnap:
    available: bool
    windows: tuple
    fetched_at: datetime = NOW
    unavailable_reason: Optional[str] = None
    balance_usd: Optional[float] = None


def _unconfigured(_provider):
    return FakeSnap(False, (), unavailable_reason="no token")


def _fetch_with(overrides):
    """Build a fetch_usage(provider) callable; unlisted providers -> unconfigured."""

    def fetch(provider):
        if provider in overrides:
            return overrides[provider]
        return _unconfigured(provider)

    return fetch


def _fake_record(calls):
    def record(**kwargs):
        calls.append(kwargs)
        return True

    return record


def test_full_window_emits_once_with_chain_exhausted(tmp_path, monkeypatch):
    import events.rate_limit_signal as rate_limit_signal

    calls: list[dict] = []
    monkeypatch.setattr(rate_limit_signal, "record", _fake_record(calls))

    fetch = _fetch_with(
        {"anthropic": FakeSnap(True, (FakeWin("Current session", 100.0, None),))}
    )
    db = tmp_path / "state.db"
    data = collect(db_path=str(db), prev=None, fetch_usage=fetch, now=NOW)

    # The snapshot itself is unaffected by the emit path.
    assert any(p["key"] == "anthropic" for p in data["providers"])

    assert len(calls) == 1
    call = calls[0]
    assert call["provider"] == "anthropic"
    assert call["model"] == "5h-window"
    assert call["reason"] == "quota_window"
    assert call["detector"] == "usage_poller"
    assert call["outcome"] == "chain_exhausted"


def test_healthy_snapshot_emits_nothing(tmp_path, monkeypatch):
    import events.rate_limit_signal as rate_limit_signal

    calls: list[dict] = []
    monkeypatch.setattr(rate_limit_signal, "record", _fake_record(calls))

    fetch = _fetch_with(
        {"anthropic": FakeSnap(True, (FakeWin("Current session", 12.0, None),))}
    )
    db = tmp_path / "state.db"
    collect(db_path=str(db), prev=None, fetch_usage=fetch, now=NOW)

    assert calls == []


def test_record_raising_does_not_break_collect(tmp_path, monkeypatch):
    import events.rate_limit_signal as rate_limit_signal

    def raising_record(**kwargs):
        raise RuntimeError("boom: simulated record() failure")

    monkeypatch.setattr(rate_limit_signal, "record", raising_record)

    fetch = _fetch_with(
        {"anthropic": FakeSnap(True, (FakeWin("Current session", 100.0, None),))}
    )
    db = tmp_path / "state.db"

    # Must not raise -- a detector failure can never break usage collection.
    data = collect(db_path=str(db), prev=None, fetch_usage=fetch, now=NOW)

    # The snapshot returned must still be intact and correct.
    assert data["generated_at"] == "2026-08-18T12:00:00Z"
    by = {p["key"]: p for p in data["providers"]}
    assert by["anthropic"]["state"] == "ok"
    assert by["anthropic"]["windows"][0]["used_pct"] == 100.0
    assert len(data["providers"]) == 8
    assert "diagnostics" in data


def test_buttons_for_returns_none_for_usage_poller_detector():
    """PROVE THE NO-BUTTON GUARANTEE (defining constraint of Phase 3).

    Phase 2's buttons_for() gates on detector == "runtime"; usage_poller
    inherits the block, but that inheritance must be pinned with a test
    rather than trusted on faith.
    """
    from events.override_buttons import buttons_for
    from events.schema import Event, EventType

    for outcome in ("diverted", "chain_exhausted"):
        event = Event.create(
            event_type=EventType.MODEL_RATE_LIMITED,
            source="usage_poller",
            payload={
                "provider": "anthropic",
                "model": "5h-window",
                "reason": "quota_window",
                "detector": "usage_poller",
                "outcome": outcome,
                "fallback_provider": "",
                "fallback_model": "",
                "resets_at": "",
                "diverted_calls": 1,
                "episode_opened_at": "x",
            },
        )
        assert buttons_for(event) is None


def test_absent_window_never_recovers_an_open_episode(tmp_path, monkeypatch):
    """An episode open for a window must never be read as RECOVERED just
    because the next snapshot omits that window entirely.

    Codex nulls its 5h window precisely when the weekly is capped -- if a
    disappearing window caused a "recovery", the operator would get a false
    all-clear at the worst possible moment.

    This test does not merely check that recovery doesn't happen: it patches
    clear() itself, so it would FAIL if a future change added a naive "no
    finding this round -> the open episode must have recovered" clear.
    """
    import events.rate_limit_signal as rate_limit_signal

    record_calls: list[dict] = []
    clear_calls: list[dict] = []

    monkeypatch.setattr(rate_limit_signal, "record", _fake_record(record_calls))

    def fake_clear(**kwargs):
        clear_calls.append(kwargs)
        return True

    monkeypatch.setattr(rate_limit_signal, "clear", fake_clear)

    db = tmp_path / "state.db"

    # Round 1: the 5h window is fully exhausted -- an episode opens.
    fetch_capped = _fetch_with(
        {"anthropic": FakeSnap(True, (FakeWin("Current session", 100.0, None),))}
    )
    first = collect(db_path=str(db), prev=None, fetch_usage=fetch_capped, now=NOW)
    assert len(record_calls) == 1
    assert record_calls[0]["outcome"] == "chain_exhausted"

    # Round 2: the window is gone entirely from the snapshot (Codex-style
    # nulling), not present-and-healthy. evaluate() correctly yields no
    # finding for it either way -- the property under test is that our emit
    # layer never turns "no finding" into a clear() call.
    fetch_absent = _fetch_with({"anthropic": FakeSnap(True, ())})
    collect(db_path=str(db), prev=first, fetch_usage=fetch_absent, now=NOW)

    assert clear_calls == []


class TestStaleResetsAtIsNotForwarded:
    """Regression for a defect caught IN PRODUCTION within two poll cycles.

    Phase 1's episode reaper forgets any episode whose `resets_at` has passed.
    That is right for a real rate limit. It is wrong for this detector: on
    2026-08-18 anthropic reported used_pct 100.0 with resets_at 2026-08-17 -- a
    day in the past on a still-capped window. Forwarding it got the episode
    reaped on the next read, so every 5-minute poll re-alerted (observed at
    00:20:13 and again at 00:25:14 for the same window).

    The design rule "resets_at is display-only" was already in place, but was
    enforced one layer too shallow -- in evaluate(), not at the boundary where
    the value reaches a consumer that branches on it.
    """

    def test_past_resets_at_is_dropped(self):
        from ai_usage.collector import _future_resets_at
        assert _future_resets_at("2026-08-17T08:00:00Z") == ""

    def test_future_resets_at_is_preserved(self):
        from ai_usage.collector import _future_resets_at
        from datetime import datetime, timedelta, timezone
        soon = (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat()
        assert _future_resets_at(soon) == soon

    def test_missing_or_garbage_resets_at_is_dropped_not_raised(self):
        from ai_usage.collector import _future_resets_at
        assert _future_resets_at(None) == ""
        assert _future_resets_at("") == ""
        assert _future_resets_at("not-a-timestamp") == ""

    def test_a_still_capped_window_with_a_past_reset_emits_without_resets_at(self, monkeypatch):
        """The end-to-end shape of the production defect."""
        calls = []
        import events.rate_limit_signal as rls
        monkeypatch.setattr(rls, "record", lambda **kw: calls.append(kw) or True)
        from ai_usage.collector import _emit_quota_findings
        _emit_quota_findings({
            "providers": [{
                "key": "anthropic", "mode": "budget",
                "windows": [{"id": "wk", "label": "Weekly", "used_pct": 100.0,
                             "resets_at": "2026-08-17T08:00:00Z"}],
            }]
        })
        assert len(calls) == 1
        assert calls[0]["outcome"] == "chain_exhausted"
        assert calls[0]["resets_at"] == "", (
            "a stale reset time must not reach record() -- Phase 1's reaper "
            "branches on it and would forget the episode every poll"
        )
