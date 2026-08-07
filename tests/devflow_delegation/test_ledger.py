import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from devflow_delegation.contract import parse_request
from devflow_delegation.ledger import (
    DelegationLedger,
    SUCCESS_TERMINAL_STATES,
    TERMINAL_STATES,
)


def make_request(key="critic:gw-timeout:v1", title="Restore bounded gateway health query",
                 agent="critic"):
    payload = {
        "schema_version": "3.0",
        "type": "DEVFLOW_WORK_REQUEST",
        "idempotency_key": key,
        "source": {"agent": agent, "kind": agent, "finding_id": "F-1"},
        "kind": "bug",
        "title": title,
        "problem_statement": "Health query scans all sessions without a LIMIT.",
        "evidence": [{"kind": "test_failure", "ref": "r", "summary": "timeout at 30s"}],
        "target": {"repo": "hermes", "subsystem": "gateway-health"},
        "severity": "high",
        "priority": "P1",
        "confidence": 0.94,
        "acceptance_criteria": ["Health query bounded to 3s"],
        "safety_notes": [],
    }
    return parse_request(payload)


@pytest.fixture
def ledger(tmp_path):
    return DelegationLedger(tmp_path / "devflow" / "delegation_ledger.db")


def test_schema_created_with_wal(ledger):
    conn = sqlite3.connect(str(ledger.db_path))
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"requests", "transitions", "evidence_log", "leases"} <= tables
    conn.close()


def test_insert_and_get_roundtrip(ledger):
    req = make_request()
    ledger.insert_request(req)
    row = ledger.get_request(req.request_id)
    assert row["state"] == "REQUESTED"
    assert row["idempotency_key"] == "critic:gw-timeout:v1"
    assert row["fingerprint"] == req.dedup_fingerprint
    env = json.loads(row["envelope_json"])
    assert env["request_id"] == req.request_id


def test_duplicate_idempotency_key_rejected(ledger):
    ledger.insert_request(make_request())
    with pytest.raises(sqlite3.IntegrityError):
        ledger.insert_request(make_request())


def test_find_active_by_fingerprint_excludes_terminal(ledger):
    req = make_request()
    ledger.insert_request(req)
    assert ledger.find_active_by_fingerprint(req.dedup_fingerprint)["request_id"] == req.request_id
    ledger.set_state(req.request_id, "DEPLOYED")
    assert ledger.find_active_by_fingerprint(req.dedup_fingerprint) is None


def test_latest_terminal_for_fingerprint(ledger):
    req = make_request()
    ledger.insert_request(req)
    assert ledger.latest_terminal_for_fingerprint(req.dedup_fingerprint) is None
    ledger.set_state(req.request_id, "DECLINED", terminal_reason="target_unresolved")
    row = ledger.latest_terminal_for_fingerprint(req.dedup_fingerprint)
    assert row["state"] == "DECLINED"
    assert "DECLINED" in TERMINAL_STATES
    assert "DEPLOYED" in SUCCESS_TERMINAL_STATES


def test_append_evidence_and_count(ledger):
    req = make_request()
    ledger.insert_request(req)
    ledger.append_evidence(req.request_id, {"kind": "test_failure", "ref": "r2", "summary": "again"})
    assert ledger.evidence_count(req.request_id) == 1


def test_set_state_and_transition_history(ledger):
    req = make_request()
    ledger.insert_request(req)
    ledger.set_state(req.request_id, "TRIAGED")
    ledger.record_transition(req.request_id, "REQUESTED", "TRIAGED", "test-actor", "policy-v1", "evidence-ref")
    hist = ledger.transitions_for(req.request_id)
    assert hist[0]["from_state"] == "REQUESTED"
    assert hist[0]["to_state"] == "TRIAGED"
    assert hist[0]["actor"] == "test-actor"
    assert ledger.get_request(req.request_id)["state"] == "TRIAGED"


def test_count_since_scopes_by_source(ledger):
    ledger.insert_request(make_request(key="a", agent="critic"))
    ledger.insert_request(make_request(key="b", agent="watchdog", title="Different problem entirely"))
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    assert ledger.count_since("critic", past) == 1
    assert ledger.count_since(None, past) == 2
    assert ledger.count_critical_since(past) == 0


def test_summary_counts(ledger):
    ledger.insert_request(make_request(key="a"))
    s = ledger.summary_counts()
    assert s["total"] == 1
    assert s["by_state"]["REQUESTED"] == 1
    assert s["by_source"]["critic"] == 1


def test_list_requests_filters_state(ledger):
    a = make_request(key="a")
    b = make_request(key="b", title="Another problem")
    ledger.insert_request(a)
    ledger.insert_request(b)
    ledger.set_state(a.request_id, "TRIAGED")
    assert [r["request_id"] for r in ledger.list_requests(state="TRIAGED")] == [a.request_id]
    assert len(ledger.list_requests()) == 2


def test_adopt_envelope_preserves_request_id(ledger):
    req = make_request()
    env = req.to_envelope()
    ledger.adopt_envelope(env)
    row = ledger.get_request(req.request_id)
    assert row is not None
    assert row["state"] == "REQUESTED"


def test_adopt_envelope_preserves_created_at(ledger):
    # Reconciler path: adopting an aged envelope (e.g. crash recovery from the
    # mailbox hours later) must NOT re-date the request to "now" — count_since /
    # count_critical_since rate windows and created_at ordering depend on the
    # original creation time. Use an explicit past timestamp so the assertion is
    # deterministic (not dependent on two now() calls landing in different ticks).
    req = make_request()
    env = req.to_envelope()
    env["created_at"] = "2020-01-01T00:00:00+00:00"
    ledger.adopt_envelope(env)
    row = ledger.get_request(env["request_id"])
    assert row["created_at"] == "2020-01-01T00:00:00+00:00"
    assert json.loads(row["envelope_json"])["created_at"] == "2020-01-01T00:00:00+00:00"


def test_find_by_idempotency_key(ledger):
    req = make_request()
    ledger.insert_request(req)
    found = ledger.find_by_idempotency_key("critic:gw-timeout:v1")
    assert found["request_id"] == req.request_id
    assert ledger.find_by_idempotency_key("nonexistent:key") is None
