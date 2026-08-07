import pytest

from devflow_delegation.contract import parse_request
from devflow_delegation.ledger import DelegationLedger
from devflow_delegation.lifecycle import (
    STATE_EVENTS,
    TRANSITIONS,
    IllegalTransitionError,
    transition,
)
from events.bus import EventBus
from events.schema import EventType


def seed(ledger):
    payload = {
        "schema_version": "3.0",
        "type": "DEVFLOW_WORK_REQUEST",
        "idempotency_key": "test:lifecycle:v1",
        "source": {"agent": "tester", "kind": "explicit", "finding_id": "T-1"},
        "kind": "bug",
        "title": "Lifecycle fixture",
        "problem_statement": "Fixture request for lifecycle tests.",
        "evidence": [{"kind": "manual", "ref": "test", "summary": "fixture"}],
        "target": {"repo": "hermes", "subsystem": "test"},
        "severity": "medium",
        "priority": "P2",
        "confidence": 0.9,
        "acceptance_criteria": ["n/a"],
        "safety_notes": [],
    }
    req = parse_request(payload)
    ledger.insert_request(req)
    return req.request_id


@pytest.fixture
def ledger(tmp_path):
    return DelegationLedger(tmp_path / "delegation_ledger.db")


@pytest.fixture
def bus(tmp_path):
    return EventBus(db_path=tmp_path / "event_bus.db")


def test_machine_shape_matches_spec(ledger):
    assert TRANSITIONS["REQUESTED"] >= {"TRIAGED", "DECLINED", "CANCELLED"}
    assert TRANSITIONS["MERGE_PENDING"] == {"MERGED", "AUTO_MERGED", "CANCELLED", "FAILED"}
    assert "BUILDING" not in TRANSITIONS["REQUESTED"], "no stage-skipping"
    for state in ("DUPLICATE", "SUPPRESSED", "DECLINED", "CANCELLED", "DEPLOYED", "FAILED", "REVERTED"):
        assert state not in TRANSITIONS, f"{state} is terminal/side and has no forward edges"


def test_legal_forward_path_emits_events(ledger, bus):
    rid = seed(ledger)
    assert transition(ledger, bus, rid, "TRIAGED", actor="test") == "TRIAGED"
    assert transition(ledger, bus, rid, "PLANNED", actor="test") == "PLANNED"
    hist = ledger.transitions_for(rid)
    assert [t["to_state"] for t in hist] == ["TRIAGED", "PLANNED"]
    rows = bus._get_conn().execute(  # verify telemetry landed
        "SELECT event_type FROM events ORDER BY timestamp").fetchall()
    types = [r["event_type"] for r in rows]
    assert "devflow.work_triaged" in types
    assert "devflow.work_planned" in types


def test_illegal_transition_rejected_and_state_unchanged(ledger, bus):
    rid = seed(ledger)
    with pytest.raises(IllegalTransitionError):
        transition(ledger, bus, rid, "MERGED", actor="test")
    with pytest.raises(IllegalTransitionError):
        transition(ledger, bus, rid, "BUILDING", actor="test")
    assert ledger.get_request(rid)["state"] == "REQUESTED"
    assert ledger.transitions_for(rid) == []


def test_unknown_request_rejected(ledger, bus):
    with pytest.raises(IllegalTransitionError):
        transition(ledger, bus, "dwr_nonexistent", "TRIAGED", actor="test")


def test_terminal_states_record_terminal_reason(ledger, bus):
    rid = seed(ledger)
    transition(ledger, bus, rid, "DECLINED", actor="emitter", evidence_ref="target_unresolved")
    row = ledger.get_request(rid)
    assert row["state"] == "DECLINED"
    assert row["terminal_reason"] == "DECLINED"
    # evidence_ref is routed to the transition-history row, NOT terminal_reason.
    assert ledger.transitions_for(rid)[-1]["evidence_ref"] == "target_unresolved"


def test_bus_none_suppresses_emission(ledger):
    # bus=None is the documented dry-tooling path: the transition still
    # persists durable state but emits nothing (and does not crash).
    rid = seed(ledger)
    assert transition(ledger, None, rid, "TRIAGED", actor="test") == "TRIAGED"
    assert ledger.get_request(rid)["state"] == "TRIAGED"
    assert [t["to_state"] for t in ledger.transitions_for(rid)] == ["TRIAGED"]


def test_ledger_only_transition_emits_no_event(ledger, bus):
    # CANCELLED maps to None in STATE_EVENTS — the emit gate must fire zero
    # events while still persisting the durable transition.
    rid = seed(ledger)
    transition(ledger, bus, rid, "CANCELLED", actor="test")
    assert ledger.get_request(rid)["state"] == "CANCELLED"
    rows = bus._get_conn().execute("SELECT COUNT(*) AS n FROM events").fetchone()
    assert rows["n"] == 0


class _RaisingBus:
    """A bus whose emit always raises, to prove telemetry failure never rolls
    back durable ledger state."""

    def emit(self, **_kwargs):
        raise RuntimeError("telemetry sink down")


def test_failed_emit_does_not_roll_back_durable_state(ledger):
    # The module's central durability claim: a raising emit propagates, but the
    # state advance and transition row committed before it remain durable.
    rid = seed(ledger)
    with pytest.raises(RuntimeError, match="telemetry sink down"):
        transition(ledger, _RaisingBus(), rid, "TRIAGED", actor="test")
    assert ledger.get_request(rid)["state"] == "TRIAGED"
    assert [t["to_state"] for t in ledger.transitions_for(rid)] == ["TRIAGED"]


def test_stage2_states_have_event_mappings():
    assert STATE_EVENTS["MERGE_PENDING"] is EventType.DEVFLOW_MERGE_PENDING
    assert STATE_EVENTS["MERGED"] is EventType.DEVFLOW_MERGED
    assert STATE_EVENTS["AUTO_MERGED"] is EventType.DEVFLOW_AUTO_MERGED
    assert STATE_EVENTS["BUILDING"] is EventType.DEVFLOW_BUILD_STARTED  # reused
    assert STATE_EVENTS["PR_OPEN"] is EventType.DEVFLOW_PR_OPENED        # reused
    assert STATE_EVENTS["VALIDATED"] is None  # ledger-only
