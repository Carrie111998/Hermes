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


def test_stage2_states_have_event_mappings():
    assert STATE_EVENTS["MERGE_PENDING"] is EventType.DEVFLOW_MERGE_PENDING
    assert STATE_EVENTS["MERGED"] is EventType.DEVFLOW_MERGED
    assert STATE_EVENTS["AUTO_MERGED"] is EventType.DEVFLOW_AUTO_MERGED
    assert STATE_EVENTS["BUILDING"] is EventType.DEVFLOW_BUILD_STARTED  # reused
    assert STATE_EVENTS["PR_OPEN"] is EventType.DEVFLOW_PR_OPENED        # reused
    assert STATE_EVENTS["VALIDATED"] is None  # ledger-only
