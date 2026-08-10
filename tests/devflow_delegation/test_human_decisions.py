import sqlite3

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


def test_human_decision_is_unique_request_wide_and_has_request_lookup(tmp_path):
    ledger = DelegationLedger(tmp_path / "delegation_ledger.db")
    request = _request()
    ledger.insert_request(request)

    assert ledger.record_human_decision(
        request.request_id, "telegram:u1", "approve", "reviewed", "token-1"
    )
    assert not ledger.record_human_decision(
        request.request_id, "telegram:u2", "decline", "disagreed", "token-2"
    )
    decision = ledger.human_decision_for_request(request.request_id)
    assert decision is not None
    assert decision["actor"] == "telegram:u1"
    assert decision["decision"] == "approve"


@pytest.mark.parametrize(
    ("actor", "decision", "evidence_ref", "confirmation_token"),
    [
        ("", "approve", "reviewed", "token-1"),
        ("telegram:u1", "maybe", "reviewed", "token-1"),
        ("telegram:u1", "approve", "", "token-1"),
        ("telegram:u1", "approve", "reviewed", ""),
    ],
)
def test_human_decision_validates_inputs(
    tmp_path, actor, decision, evidence_ref, confirmation_token
):
    ledger = DelegationLedger(tmp_path / "delegation_ledger.db")
    request = _request()
    ledger.insert_request(request)

    with pytest.raises(ValueError):
        ledger.record_human_decision(
            request.request_id, actor, decision, evidence_ref, confirmation_token
        )


def test_existing_db_migration_adds_request_wide_unique_index(tmp_path):
    db_path = tmp_path / "delegation_ledger.db"
    ledger = DelegationLedger(db_path)
    request = _request()
    ledger.insert_request(request)
    ledger.close()
    conn = sqlite3.connect(db_path)
    conn.execute("DROP INDEX IF EXISTS uq_human_decisions_request")
    conn.commit()
    conn.close()

    migrated = DelegationLedger(db_path)
    indexes = {
        row[1]
        for row in migrated._conn().execute("PRAGMA index_list(human_decisions)")
    }
    assert "uq_human_decisions_request" in indexes


def test_existing_db_migration_fails_without_deleting_conflicting_rows(tmp_path):
    db_path = tmp_path / "delegation_ledger.db"
    ledger = DelegationLedger(db_path)
    request = _request()
    ledger.insert_request(request)
    conn = ledger._conn()
    conn.execute("DROP INDEX IF EXISTS uq_human_decisions_request")
    conn.execute(
        "INSERT INTO human_decisions "
        "(request_id, actor, decision, evidence_ref, confirmation_token, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (request.request_id, "telegram:u1", "approve", "reviewed", "token-1", "2026-01-01"),
    )
    conn.execute(
        "INSERT INTO human_decisions "
        "(request_id, actor, decision, evidence_ref, confirmation_token, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (request.request_id, "telegram:u2", "decline", "disagreed", "token-2", "2026-01-02"),
    )
    conn.commit()
    ledger.close()

    with pytest.raises(RuntimeError, match="conflicting legacy human_decisions"):
        DelegationLedger(db_path)

    check = sqlite3.connect(db_path)
    try:
        assert check.execute(
            "SELECT COUNT(*) FROM human_decisions WHERE request_id=?", (request.request_id,)
        ).fetchone()[0] == 2
    finally:
        check.close()
