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


def test_close_releases_connection_for_file_deletion(ledger):
    # A WAL ledger holds a persistent thread-local handle; on Windows that
    # handle blocks unlink (WinError 32). close() must release it so the db
    # file can be deleted or handed to a fresh emitter (the reconcile path).
    ledger.insert_request(make_request())
    ledger.close()
    ledger.db_path.unlink()  # would raise WinError 32 if the handle were open
    assert not ledger.db_path.exists()
    ledger.close()  # idempotent: no error when already closed


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


def test_oldest_requested_returns_earliest_open_row(ledger):
    # status CLI needs the OLDEST open request (the most-aged stuck one). Insert
    # two REQUESTED rows with distinct, explicit created_at (adopt_envelope
    # preserves the envelope's timestamp) and assert ascending selection — a
    # DESC LIMIT 1 (the list_requests default) would return the NEWER row, so
    # this is a positive control for the ascending query.
    a = make_request(key="old")
    ea = a.to_envelope()
    ea["created_at"] = "2020-01-01T00:00:00+00:00"
    ledger.adopt_envelope(ea)
    b = make_request(key="new", title="A different newer problem")
    eb = b.to_envelope()
    eb["created_at"] = "2021-01-01T00:00:00+00:00"
    ledger.adopt_envelope(eb)

    row = ledger.oldest_requested()
    assert row["request_id"] == ea["request_id"]
    assert row["created_at"] == "2020-01-01T00:00:00+00:00"

    # excludes non-REQUESTED: once the 2020 row leaves REQUESTED, the 2021 row
    # becomes the oldest OPEN request.
    ledger.set_state(ea["request_id"], "TRIAGED")
    assert ledger.oldest_requested()["request_id"] == eb["request_id"]


def test_oldest_requested_none_when_empty(ledger):
    assert ledger.oldest_requested() is None


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


# Stage 2: transaction(), leases, and executor artifacts.
def test_transaction_rolls_back_state_and_history_together(ledger):
    req = make_request()
    ledger.insert_request(req)

    with pytest.raises(RuntimeError):
        with ledger.transaction():
            ledger.set_state(req.request_id, "PLANNED")
            ledger.record_transition(req.request_id, "REQUESTED", "PLANNED", "test", "p1")
            raise RuntimeError("simulated crash")

    assert ledger.get_request(req.request_id)["state"] == "REQUESTED"
    assert ledger.transitions_for(req.request_id) == []


def test_nested_transaction_uses_savepoint(ledger):
    req = make_request()
    ledger.insert_request(req)

    with ledger.transaction():
        ledger.set_state(req.request_id, "TRIAGED")
        with pytest.raises(RuntimeError):
            with ledger.transaction():
                ledger.set_state(req.request_id, "PLANNED")
                raise RuntimeError("inner rollback")

    assert ledger.get_request(req.request_id)["state"] == "TRIAGED"


def test_lease_persists_attempt_and_worktree_identity(ledger):
    req = make_request()
    ledger.insert_request(req)

    first = ledger.acquire_lease(req.request_id, "executor", lease_id="lse-1")
    assert first["attempt_count"] == 1
    assert ledger.set_lease_worktree(req.request_id, "lse-1", "/tmp/worktree", "ddp-branch")
    assert ledger.lease_for_request(req.request_id)["branch"] == "ddp-branch"
    assert ledger.release_lease(req.request_id, "lse-1")

    second = ledger.acquire_lease(req.request_id, "executor", lease_id="lse-2")
    assert second["attempt_count"] == 2


def test_renew_heartbeat_renews_expiry(ledger):
    req = make_request()
    ledger.insert_request(req)
    lease = ledger.acquire_lease(req.request_id, "executor", lease_id="lse-1", expires_in_seconds=60)

    assert ledger.renew_heartbeat(req.request_id, lease["lease_id"], expires_in_seconds=600)
    renewed = ledger.lease_for_request(req.request_id)
    assert renewed["heartbeat_at"] >= lease["heartbeat_at"]
    assert renewed["expires_at"] > lease["expires_at"]


def test_artifacts_are_idempotent(ledger):
    req = make_request()
    ledger.insert_request(req)

    ledger.add_artifact(req.request_id, "branch", "ddp-branch")
    ledger.add_artifact(req.request_id, "branch", "ddp-branch")

    assert ledger.artifacts_for(req.request_id) == [
        {"id": 1, "request_id": req.request_id, "kind": "branch", "ref": "ddp-branch", "created_at": ledger.artifacts_for(req.request_id)[0]["created_at"]}
    ]


def test_stage2_schema_migrates_existing_ledger(tmp_path):
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE requests (
            request_id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE NOT NULL,
            fingerprint TEXT NOT NULL, envelope_json TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'REQUESTED', terminal_reason TEXT,
            source_agent TEXT NOT NULL, source_kind TEXT NOT NULL,
            target_repo TEXT NOT NULL, target_subsystem TEXT NOT NULL,
            kind TEXT NOT NULL, severity TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE leases (
            request_id TEXT PRIMARY KEY, lease_id TEXT NOT NULL, holder TEXT NOT NULL,
            acquired_at TEXT NOT NULL, expires_at TEXT NOT NULL, heartbeat_at TEXT
        );
    """)
    conn.commit()
    conn.close()

    migrated = DelegationLedger(db)
    schema = sqlite3.connect(db)
    request_columns = {row[1] for row in schema.execute("PRAGMA table_info(requests)")}
    lease_columns = {row[1] for row in schema.execute("PRAGMA table_info(leases)")}
    tables = {row[0] for row in schema.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    schema.close()

    assert "lease_attempt_count" in request_columns
    assert {"worktree_path", "branch", "attempt_count"} <= lease_columns
    assert "artifacts" in tables
    migrated.close()


def test_count_prs_for_target_since_counts_only_pr_artifacts_in_window(tmp_path):
    ledger = DelegationLedger(tmp_path / "ledger.db")

    def _seed(idem, repo):
        req = parse_request({
            "schema_version": "3.0", "type": "DEVFLOW_WORK_REQUEST",
            "idempotency_key": idem,
            "source": {"agent": "operator", "kind": "explicit", "finding_id": "f"},
            "kind": "task", "title": "t", "problem_statement": "p",
            "evidence": [{"kind": "test", "summary": "s"}],
            "target": {"repo": repo, "subsystem": "src"},
            "severity": "low", "priority": "P3", "confidence": 1.0,
            "acceptance_criteria": ["a"], "safety_notes": [],
        })
        ledger.insert_request(req)
        return req.request_id

    a = _seed("k-a", "sandbox")
    b = _seed("k-b", "sandbox")
    c = _seed("k-c", "other")
    ledger.add_artifact(a, "pr", "https://example.test/pr/1")
    ledger.add_artifact(b, "branch", "ddp-b-a1")          # not a pr -> not counted
    ledger.add_artifact(c, "pr", "https://example.test/pr/2")  # other target -> not counted

    assert ledger.count_prs_for_target_since("sandbox", "1970-01-01T00:00:00+00:00") == 1
    assert ledger.count_prs_for_target_since("other", "1970-01-01T00:00:00+00:00") == 1
    # A window that starts in the far future excludes everything.
    assert ledger.count_prs_for_target_since("sandbox", "2999-01-01T00:00:00+00:00") == 0


def test_count_prs_for_target_since_counts_pr_attempt_alone(tmp_path):
    # A pr_attempt artifact is written durably BEFORE the PR client is
    # invoked (see executor._stage_commit_push callers). If gh pr create
    # succeeds but a later step (gh pr view) raises, no "pr" artifact is
    # ever written -- but the budget must still see the attempt, or a
    # second canary tick could open a second real PR against the same
    # already-exhausted budget.
    ledger = DelegationLedger(tmp_path / "ledger.db")
    req = parse_request({
        "schema_version": "3.0", "type": "DEVFLOW_WORK_REQUEST",
        "idempotency_key": "k-attempt-only",
        "source": {"agent": "operator", "kind": "explicit", "finding_id": "f"},
        "kind": "task", "title": "t", "problem_statement": "p",
        "evidence": [{"kind": "test", "summary": "s"}],
        "target": {"repo": "sandbox", "subsystem": "src"},
        "severity": "low", "priority": "P3", "confidence": 1.0,
        "acceptance_criteria": ["a"], "safety_notes": [],
    })
    ledger.insert_request(req)
    ledger.add_artifact(req.request_id, "pr_attempt", "ddp-branch-a1")

    assert ledger.count_prs_for_target_since("sandbox", "1970-01-01T00:00:00+00:00") == 1


def test_count_prs_for_target_since_sums_multiple_rows_of_both_kinds(tmp_path):
    # H5: this is the durable backstop for "exactly one real PR" -- prove it
    # actually SUMS across multiple requests, and that a "pr" artifact on one
    # request and a "pr_attempt" artifact on another BOTH count toward the
    # same target's total (not just each kind counted in isolation, which is
    # all the pre-existing tests covered).
    ledger = DelegationLedger(tmp_path / "ledger.db")

    def _seed(idem, repo):
        req = parse_request({
            "schema_version": "3.0", "type": "DEVFLOW_WORK_REQUEST",
            "idempotency_key": idem,
            "source": {"agent": "operator", "kind": "explicit", "finding_id": "f"},
            "kind": "task", "title": "t", "problem_statement": "p",
            "evidence": [{"kind": "test", "summary": "s"}],
            "target": {"repo": repo, "subsystem": "src"},
            "severity": "low", "priority": "P3", "confidence": 1.0,
            "acceptance_criteria": ["a"], "safety_notes": [],
        })
        ledger.insert_request(req)
        return req.request_id

    a = _seed("k-sum-a", "sandbox")
    b = _seed("k-sum-b", "sandbox")
    c = _seed("k-sum-c", "other")

    ledger.add_artifact(a, "pr", "https://example.test/pr/10")
    ledger.add_artifact(b, "pr_attempt", "ddp-branch-a1")
    ledger.add_artifact(c, "pr", "https://example.test/pr/99")  # other target -> not summed in

    assert ledger.count_prs_for_target_since("sandbox", "1970-01-01T00:00:00+00:00") == 2


def test_count_prs_for_target_since_boundary_is_inclusive(tmp_path):
    # H5: an artifact whose created_at is EXACTLY equal to since_iso must be
    # counted (the query uses >=). Insert an artifact row with an explicit
    # created_at via a raw connection, since add_artifact always stamps
    # "now".
    ledger = DelegationLedger(tmp_path / "ledger.db")
    req = parse_request({
        "schema_version": "3.0", "type": "DEVFLOW_WORK_REQUEST",
        "idempotency_key": "k-boundary",
        "source": {"agent": "operator", "kind": "explicit", "finding_id": "f"},
        "kind": "task", "title": "t", "problem_statement": "p",
        "evidence": [{"kind": "test", "summary": "s"}],
        "target": {"repo": "sandbox", "subsystem": "src"},
        "severity": "low", "priority": "P3", "confidence": 1.0,
        "acceptance_criteria": ["a"], "safety_notes": [],
    })
    ledger.insert_request(req)

    boundary = "2026-01-01T00:00:00+00:00"
    conn = sqlite3.connect(str(ledger.db_path))
    try:
        conn.execute(
            "INSERT INTO artifacts (request_id, kind, ref, created_at) VALUES (?,?,?,?)",
            (req.request_id, "pr", "https://example.test/pr/boundary", boundary),
        )
        conn.commit()
    finally:
        conn.close()

    assert ledger.count_prs_for_target_since("sandbox", boundary) == 1
    # One microsecond later, the same row is excluded -- pins the boundary is
    # ">=", not "==" (a coarser fix could pass the equality case alone).
    assert ledger.count_prs_for_target_since("sandbox", "2026-01-01T00:00:00.000001+00:00") == 0
