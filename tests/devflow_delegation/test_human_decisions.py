import pytest

from devflow_delegation.contract import parse_request
from devflow_delegation.ledger import DelegationLedger
from devflow_delegation.lifecycle import transition


def _request():
    return parse_request({
        "schema_version": "3.0",
        "type": "DEVFLOW_WORK_REQUEST",
        "idempotency_key": "test:human-decision:v1",
        "source": {"agent": "tester", "kind": "explicit", "finding_id": "T-1"},
        "kind": "bug",
        "title": "Human decision fixture",
        "problem_statement": "Exercise the durable approval decision path.",
        "evidence": [{"kind": "manual", "ref": "test", "summary": "fixture"}],
        "target": {"repo": "hermes", "subsystem": "test"},
        "severity": "medium",
        "priority": "P2",
        "confidence": 0.9,
        "acceptance_criteria": ["n/a"],
        "safety_notes": [],
    })


def test_human_decision_and_lifecycle_transition_commit_together(tmp_path):
    ledger = DelegationLedger(tmp_path / "delegation_ledger.db")
    request = _request()
    ledger.insert_request(request)
    transition(ledger, None, request.request_id, "TRIAGED", actor="triage")

    with ledger.transaction():
        assert ledger.record_human_decision(
            request.request_id,
            "telegram:u1",
            "approve",
            "operator reviewed fixture",
            "token-1",
        )
        transition(
            ledger,
            None,
            request.request_id,
            "PLANNED",
            actor="telegram:u1",
            evidence_ref="operator reviewed fixture",
        )

    decision = ledger.human_decision_for(request.request_id, "telegram:u1")
    assert decision is not None
    assert decision["decision"] == "approve"
    assert decision["confirmation_token"] == "token-1"
    assert ledger.get_request(request.request_id)["state"] == "PLANNED"


def test_human_decision_is_one_time_per_actor_and_token(tmp_path):
    ledger = DelegationLedger(tmp_path / "delegation_ledger.db")
    request = _request()
    ledger.insert_request(request)

    assert ledger.record_human_decision(
        request.request_id, "telegram:u1", "decline", "not ready", "token-1"
    )
    assert not ledger.record_human_decision(
        request.request_id, "telegram:u1", "decline", "retry", "token-2"
    )
    assert not ledger.record_human_decision(
        request.request_id, "telegram:u2", "decline", "retry", "token-1"
    )


def test_human_decision_rolls_back_when_the_lifecycle_transition_fails(tmp_path):
    ledger = DelegationLedger(tmp_path / "delegation_ledger.db")
    request = _request()
    ledger.insert_request(request)

    with pytest.raises(ValueError):
        with ledger.transaction():
            assert ledger.record_human_decision(
                request.request_id,
                "telegram:u1",
                "approve",
                "operator reviewed",
                "token-rollback",
            )
            transition(
                ledger,
                None,
                request.request_id,
                "PLANNED",  # Illegal directly from REQUESTED.
                actor="telegram:u1",
                evidence_ref="operator reviewed",
            )

    assert ledger.human_decision_for(request.request_id, "telegram:u1") is None
    assert ledger.get_request(request.request_id)["state"] == "REQUESTED"
    assert ledger.transitions_for(request.request_id) == []


def test_schema_creates_human_decisions_table(tmp_path):
    ledger = DelegationLedger(tmp_path / "delegation_ledger.db")
    tables = {
        row[0]
        for row in ledger._conn().execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "human_decisions" in tables
