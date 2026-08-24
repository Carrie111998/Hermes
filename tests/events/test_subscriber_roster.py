"""Tests for events.roster — the canonical subscriber roster.

The roster replaced three hand-maintained copies of the subscriber id list,
two of which had already drifted in production (see events/roster.py for the
dates and the operator-visible damage). These tests cover the two things that
make the replacement worth having:

  1. The shipping roster actually matches what ``startup()`` registers — the
     assertion that would have caught the 2026-08-23 drift at commit time
     instead of at 04:52 the next morning.
  2. The loader REFUSES malformed rosters rather than returning a partial one,
     because every consumer treats an unreadable roster as fail-closed and a
     half-parsed roster would defeat that.
"""

import json

import pytest

from events import gateway_integration as gi
from events.roster import (
    LIVE,
    RETIRED,
    ROSTER_PATH,
    RosterError,
    load_roster,
    parse_roster,
)


def _doc(subs):
    return {"version": 1, "subscribers": subs}


class TestShippingRoster:
    """The file that actually ships, not a fixture."""

    def test_parses(self):
        roster = load_roster()
        assert roster.live, "roster names no live subscribers"

    def test_no_id_is_both_live_and_retired(self):
        roster = load_roster()
        assert not (roster.live & set(roster.retired))

    def test_core_is_a_subset_of_live(self):
        roster = load_roster()
        assert set(roster.core) <= roster.live

    def test_every_retired_entry_records_when_and_why(self):
        roster = load_roster()
        for sid, entry in roster.retired.items():
            assert entry.since, f"retired '{sid}' has no since date"
            assert entry.note, f"retired '{sid}' has no note explaining the retirement"

    def test_known_retirements_are_still_listed(self):
        """Deleting a retired entry silently re-arms two warnings.

        Its cursor row survives retirement on purpose, so dropping the entry
        makes the nightly sweep call the cursor unknown AND makes
        --prune-cursor willing to delete it.
        """
        retired = load_roster().retired
        assert {"scribe-realtime", "devflow-bridge"} <= set(retired)


class TestRegistrationMatchesRoster:
    """The check that gateway_integration runs at every boot, at test time."""

    def test_startup_registers_exactly_the_live_set(self):
        roster = load_roster()
        gi.startup()
        try:
            registered = {
                s.subscriber_id for s in gi._registry.subscribers if s.subscriber_id
            }
        finally:
            gi.shutdown()

        missing = sorted(roster.live - registered)
        extra = sorted(registered - roster.live)
        assert not missing, (
            f"{ROSTER_PATH.name} lists these as live but startup() registered "
            f"nothing for them: {missing}"
        )
        assert not extra, (
            f"startup() registers these but {ROSTER_PATH.name} does not list them "
            f"as live: {extra}"
        )

    def test_no_registered_subscriber_is_marked_retired(self):
        roster = load_roster()
        gi.startup()
        try:
            registered = {
                s.subscriber_id for s in gi._registry.subscribers if s.subscriber_id
            }
        finally:
            gi.shutdown()
        assert not (registered & set(roster.retired))

    def test_verify_helper_is_wired_into_startup(self):
        """A roster that nothing checks is just a fourth copy of the list."""
        import inspect

        src = inspect.getsource(gi.startup)
        assert "_verify_subscriber_roster()" in src


class TestValidation:
    """Every rejection here is a drift shape that used to be representable."""

    def test_core_on_a_retired_entry_is_rejected(self):
        # The telegram-mirror false-FAIL: a required-cursor check naming a
        # subscriber that no longer runs.
        with pytest.raises(RosterError, match="core"):
            parse_roster(_doc([
                {"id": "gone", "status": RETIRED, "since": "2026-01-01", "core": True},
            ]))

    def test_duplicate_id_is_rejected(self):
        with pytest.raises(RosterError, match="duplicate"):
            parse_roster(_doc([
                {"id": "a", "status": LIVE},
                {"id": "a", "status": RETIRED, "since": "2026-01-01"},
            ]))

    def test_live_entry_may_not_declare_a_frozen_cursor(self):
        with pytest.raises(RosterError, match="cursor_frozen_at_rowid"):
            parse_roster(_doc([
                {"id": "a", "status": LIVE, "cursor_frozen_at_rowid": 5},
            ]))

    def test_retired_entry_needs_a_since_date(self):
        with pytest.raises(RosterError, match="since"):
            parse_roster(_doc([{"id": "a", "status": RETIRED}]))

    def test_unknown_status_is_rejected(self):
        with pytest.raises(RosterError, match="status"):
            parse_roster(_doc([{"id": "a", "status": "paused"}]))

    def test_missing_id_is_rejected(self):
        with pytest.raises(RosterError, match="id"):
            parse_roster(_doc([{"status": LIVE}]))

    def test_empty_subscriber_list_is_rejected(self):
        with pytest.raises(RosterError, match="subscribers"):
            parse_roster(_doc([]))

    def test_absent_file_raises_rather_than_returning_empty(self, tmp_path):
        with pytest.raises(RosterError, match="cannot read"):
            load_roster(tmp_path / "nope.json")

    def test_malformed_json_raises_rather_than_returning_empty(self, tmp_path):
        p = tmp_path / "roster.json"
        p.write_text("{not json", encoding="utf-8")
        with pytest.raises(RosterError, match="not valid JSON"):
            load_roster(p)

    def test_round_trips_a_well_formed_file(self, tmp_path):
        p = tmp_path / "roster.json"
        p.write_text(json.dumps(_doc([
            {"id": "a", "status": LIVE, "core": True},
            {"id": "b", "status": RETIRED, "since": "2026-01-01",
             "cursor_frozen_at_rowid": 42, "note": "why"},
        ])), encoding="utf-8")
        roster = load_roster(p)
        assert roster.live == {"a"}
        assert roster.core == ("a",)
        assert roster.known == {"a", "b"}
        assert roster.retired["b"].cursor_frozen_at_rowid == 42
        assert roster.get("b").since == "2026-01-01"
        assert roster.get("missing") is None


class _FakeBus:
    def __init__(self):
        self.emitted = []

    def emit(self, **kw):
        self.emitted.append(kw)
        return "evt-1"


class _FakeSub:
    def __init__(self, sid):
        self.subscriber_id = sid


class _FakeRegistry:
    def __init__(self, ids):
        self.subscribers = [_FakeSub(i) for i in ids]


@pytest.fixture
def drift_harness(monkeypatch):
    """Drive _verify_subscriber_roster with a fake bus + registry."""
    bus = _FakeBus()
    monkeypatch.setattr(gi, "_bus", bus)

    def arrange(registered, roster_doc=None, roster_exc=None):
        monkeypatch.setattr(gi, "_registry", _FakeRegistry(registered))
        if roster_exc is not None:
            def boom(*a, **kw):
                raise roster_exc
            monkeypatch.setattr(gi, "load_roster", boom)
        elif roster_doc is not None:
            monkeypatch.setattr(gi, "load_roster",
                                lambda *a, **kw: parse_roster(roster_doc))
        gi._verify_subscriber_roster()
        return bus.emitted

    return arrange


class TestVerifySubscriberRosterAnnounces:
    """The mechanism itself: drift must reach the bus, agreement must not."""

    def test_agreement_emits_nothing(self, drift_harness):
        emitted = drift_harness(
            ["a", "b"], _doc([{"id": "a", "status": LIVE}, {"id": "b", "status": LIVE}])
        )
        assert emitted == []

    def test_registered_but_absent_from_roster_emits(self, drift_harness):
        """A subscriber added to startup() without a roster entry."""
        emitted = drift_harness(["a", "newcomer"], _doc([{"id": "a", "status": LIVE}]))
        assert len(emitted) == 1
        payload = emitted[0]["payload"]
        assert payload["registered_but_not_live"] == ["newcomer"]
        assert "newcomer" in payload["detail"]

    def test_live_in_roster_but_never_registered_emits(self, drift_harness):
        """The jobflow-dispatcher shape, inverted: roster says live, nothing ran.

        Also the shape a failed conditional registration takes — the
        JobFlowDispatcher try/except only logs today.
        """
        emitted = drift_harness(
            ["a"], _doc([{"id": "a", "status": LIVE}, {"id": "b", "status": LIVE}])
        )
        assert len(emitted) == 1
        assert emitted[0]["payload"]["live_but_not_registered"] == ["b"]

    def test_registered_while_marked_retired_names_the_retirement(self, drift_harness):
        """The scribe-realtime shape: the roster says retired, the code says no."""
        emitted = drift_harness(["a", "zombie"], _doc([
            {"id": "a", "status": LIVE},
            {"id": "zombie", "status": RETIRED, "since": "2026-07-18"},
        ]))
        assert len(emitted) == 1
        assert "zombie (roster: retired 2026-07-18)" in emitted[0]["payload"]["detail"]

    def test_unreadable_roster_emits_rather_than_passing_silently(self, drift_harness):
        emitted = drift_harness(["a"], roster_exc=RosterError("file eaten by a bear"))
        assert len(emitted) == 1
        assert "bear" in emitted[0]["payload"]["detail"]

    def test_emission_is_high_priority_agent_error(self, drift_harness):
        from events.schema import EventType, Priority

        emitted = drift_harness(["a", "newcomer"], _doc([{"id": "a", "status": LIVE}]))
        assert emitted[0]["event_type"] is EventType.AGENT_ERROR
        assert emitted[0]["priority"] is Priority.HIGH
        assert emitted[0]["source"] == "event-bus"

    def test_survives_a_bus_that_cannot_emit(self, monkeypatch):
        """A drift report must never be the thing that kills the gateway."""
        class _BrokenBus:
            def emit(self, **kw):
                raise RuntimeError("bus down")

        monkeypatch.setattr(gi, "_bus", _BrokenBus())
        monkeypatch.setattr(gi, "_registry", _FakeRegistry(["surprise"]))
        monkeypatch.setattr(gi, "load_roster",
                            lambda *a, **kw: parse_roster(_doc([{"id": "a", "status": LIVE}])))
        gi._verify_subscriber_roster()  # must not raise
