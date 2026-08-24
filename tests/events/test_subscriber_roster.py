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
        # A loaded roster records the file it came from, so an operator-facing
        # message can name what was READ rather than what was assumed.
        assert roster.source == p


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


class TestDriftReachesARealBusEndToEnd:
    """End-to-end proof against a SCRATCH bus: real startup(), real EventBus,
    real SQLite row read back off disk.

    The ``TestVerifySubscriberRosterAnnounces`` cases above use a fake bus, so
    they prove the decision logic but not that an ``AGENT_ERROR`` actually
    survives ``EventBus.emit()`` and lands in ``events``. These do: they boot
    the real ``gi.startup()`` against the per-test ``HERMES_HOME`` the suite's
    ``_hermetic_environment`` fixture provides, then reopen the resulting
    database with plain ``sqlite3`` and read the row back.

    The drifted roster is DERIVED from the shipping one and mutated, so the
    live registrations stay real and only the roster lies -- which is exactly
    the shape both 2026-08-23 drifts took.
    """

    @staticmethod
    def _assert_scratch_bus(tmp_path):
        """Refuse to run against the real bus. Non-negotiable gate."""
        from events.paths import events_db_path

        db = events_db_path()
        assert tmp_path in db.parents, (
            f"REFUSING: events_db_path() is {db}, which is not under the per-test "
            f"tmp_path {tmp_path}. The hermetic HERMES_HOME redirect is not in "
            "effect and this test would write to the production event bus."
        )
        assert ".hermes" not in db.parts or tmp_path in db.parents
        return db

    @staticmethod
    def _agent_errors(db):
        """Read agent_error rows straight off disk, not through the bus."""
        import sqlite3

        if not db.exists():
            return []
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        try:
            return con.execute(
                "SELECT event_type, source, priority, payload FROM events "
                "WHERE event_type = 'agent_error' ORDER BY rowid"
            ).fetchall()
        finally:
            con.close()

    @staticmethod
    def _write_roster(path, subs):
        path.write_text(json.dumps({"version": 1, "subscribers": subs}), encoding="utf-8")

    def _shipping_entries(self):
        raw = json.loads(ROSTER_PATH.read_text(encoding="utf-8"))
        return raw["subscribers"]

    def test_drifted_roster_emits_agent_error_into_the_bus(self, tmp_path, monkeypatch):
        from events import roster as roster_mod

        db = self._assert_scratch_bus(tmp_path)
        assert self._agent_errors(db) == [], "scratch bus should start empty"

        # Derive a drifted roster from the real one: drop a subscriber that IS
        # registered, mark another retired, and invent one that is not.
        entries = [dict(e) for e in self._shipping_entries()]
        dropped = "cron-stale-monitor"
        retired_live = "critic-trigger"
        entries = [e for e in entries if e["id"] != dropped]
        for e in entries:
            if e["id"] == retired_live:
                e["status"] = RETIRED
                e["since"] = "2026-08-23"
                e.pop("core", None)
        entries.append({"id": "phantom-subscriber", "status": LIVE})

        drifted = tmp_path / "drifted_roster.json"
        self._write_roster(drifted, entries)
        monkeypatch.setattr(roster_mod, "ROSTER_PATH", drifted)

        gi.startup()
        try:
            registered = {s.subscriber_id for s in gi._registry.subscribers}
        finally:
            gi.shutdown()

        # Sanity: the drift is real, not an artefact of a broken startup().
        assert dropped in registered and retired_live in registered
        assert "phantom-subscriber" not in registered

        rows = self._agent_errors(db)
        assert len(rows) == 1, f"expected exactly one AGENT_ERROR, got {rows}"
        event_type, source, priority, payload_json = rows[0]
        payload = json.loads(payload_json)

        assert event_type == "agent_error"
        assert source == "event-bus"
        assert priority == "high"
        assert payload["subscriber_id"] == "roster-check"
        assert payload["error"] == "subscriber roster drift"
        # roster says live, nothing registered
        assert payload["live_but_not_registered"] == ["phantom-subscriber"]
        # registered, but the roster does not list them live
        assert sorted(payload["registered_but_not_live"]) == sorted([dropped, retired_live])
        # the retirement date is surfaced, so an operator can tell the two
        # failure shapes apart without opening the roster
        assert f"{retired_live} (roster: retired 2026-08-23)" in payload["detail"]
        assert f"{dropped} (absent from roster)" in payload["detail"]
        assert payload["roster_path"] == str(drifted)

    def test_accurate_roster_emits_nothing_into_the_bus(self, tmp_path, monkeypatch):
        """Control arm. Without this, the test above only proves 'emits'."""
        from events import roster as roster_mod

        db = self._assert_scratch_bus(tmp_path)
        accurate = tmp_path / "accurate_roster.json"
        self._write_roster(accurate, self._shipping_entries())
        monkeypatch.setattr(roster_mod, "ROSTER_PATH", accurate)

        gi.startup()
        gi.shutdown()

        assert self._agent_errors(db) == [], (
            "an accurate roster must not page; a check that always fires is noise"
        )

    def test_unreadable_roster_emits_rather_than_passing_silently(self, tmp_path, monkeypatch):
        """A roster that cannot be read must never read as 'no drift'."""
        from events import roster as roster_mod

        db = self._assert_scratch_bus(tmp_path)
        monkeypatch.setattr(roster_mod, "ROSTER_PATH", tmp_path / "does_not_exist.json")

        gi.startup()
        gi.shutdown()

        rows = self._agent_errors(db)
        assert len(rows) == 1
        payload = json.loads(rows[0][3])
        assert "could not be loaded" in payload["detail"]
        # Nothing was read, so the alert names the path it TRIED -- the one in
        # effect, not the import-time default.
        assert payload["roster_path"] == str(tmp_path / "does_not_exist.json")
