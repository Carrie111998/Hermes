"""Routing rejection lifecycle tests for claim and spawn boundaries."""

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.routing_contract import RoutingContractError


@pytest.fixture
def conn(tmp_path, monkeypatch):
    """Return an initialized isolated Kanban connection."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    connection = kb.connect()
    try:
        yield connection
    finally:
        connection.close()


def _event_payloads(conn, task_id, kind):
    """Return decoded event payloads for one task and kind."""
    rows = conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id",
        (task_id, kind),
    ).fetchall()
    return [json.loads(row["payload"]) for row in rows]


def test_claim_rejection_rolls_back_and_audits_separately(conn, monkeypatch):
    """Invalid routing creates no run or state transition but audits the attempt."""
    task_id = kb.create_task(conn, title="reject", assignee="coder")
    conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (task_id,))
    monkeypatch.setattr(
        kb, "_resolve_routing_snapshot",
        lambda *args, **kwargs: (_ for _ in ()).throw(RoutingContractError("unknown role")),
    )

    with pytest.raises(RoutingContractError, match="unknown role"):
        kb.claim_task(conn, task_id)

    task = kb.get_task(conn, task_id)
    assert task.status == "ready"
    assert task.current_run_id is None
    assert conn.execute(
        "SELECT COUNT(*) FROM task_runs WHERE task_id=?", (task_id,)
    ).fetchone()[0] == 0
    payload = _event_payloads(conn, task_id, "claim_rejected")[0]
    assert payload["reason"] == "unknown role"
    assert payload["attempt_id"]


def test_claim_rejection_attempt_id_deduplicates_retry_only(conn, monkeypatch):
    """One logical attempt audits once while fresh calls remain distinct."""
    task_id = kb.create_task(conn, title="deduplicate reject", assignee="coder")
    conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (task_id,))
    monkeypatch.setattr(
        kb, "_resolve_routing_snapshot",
        lambda *args, **kwargs: (_ for _ in ()).throw(RoutingContractError("bad route")),
    )
    attempt_id = "93722498-a216-4ea7-9f14-cdb46fbc69b9"

    for _ in range(2):
        with pytest.raises(RoutingContractError, match="bad route"):
            kb.claim_task(conn, task_id, attempt_id=attempt_id)
    with pytest.raises(RoutingContractError, match="bad route"):
        kb.claim_task(conn, task_id)

    payloads = _event_payloads(conn, task_id, "claim_rejected")
    assert [payload["attempt_id"] for payload in payloads].count(attempt_id) == 1
    assert len(payloads) == 2
    assert payloads[1]["attempt_id"] != attempt_id


@pytest.mark.parametrize("claim_name,status", [("claim_task", "ready"), ("claim_review_task", "review")])
def test_claim_rejection_audit_failure_preserves_contract_error(
    conn, monkeypatch, caplog, claim_name, status
):
    """A broken secondary audit never masks the routing contract error."""
    task_id = kb.create_task(conn, title="audit failure", assignee="coder")
    conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, task_id))
    monkeypatch.setattr(
        kb, "_resolve_routing_snapshot",
        lambda *args, **kwargs: (_ for _ in ()).throw(RoutingContractError("original route")),
    )
    monkeypatch.setattr(
        kb,
        "_append_claim_rejected_once",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("audit unavailable")),
    )

    with pytest.raises(RoutingContractError, match="original route"):
        getattr(kb, claim_name)(conn, task_id)

    assert "audit unavailable" in caplog.text


def test_review_claim_rejection_rolls_back_and_audits_separately(conn, monkeypatch):
    """Review routing rejection preserves review state and creates no run."""
    task_id = kb.create_task(conn, title="reject review", assignee="coder")
    conn.execute("UPDATE tasks SET status='review' WHERE id=?", (task_id,))
    monkeypatch.setattr(
        kb, "_resolve_routing_snapshot",
        lambda *args, **kwargs: (_ for _ in ()).throw(RoutingContractError("bad reviewer")),
    )

    with pytest.raises(RoutingContractError, match="bad reviewer"):
        kb.claim_review_task(conn, task_id)

    task = kb.get_task(conn, task_id)
    assert task.status == "review"
    assert task.current_run_id is None
    assert conn.execute(
        "SELECT COUNT(*) FROM task_runs WHERE task_id=?", (task_id,)
    ).fetchone()[0] == 0
    payload = _event_payloads(conn, task_id, "claim_rejected")[0]
    assert payload["reason"] == "bad reviewer"
    assert payload["attempt_id"]


def test_corrupted_claimed_run_rejects_spawn_and_accounts_retry(conn, monkeypatch, tmp_path):
    """A malformed frozen run traverses the complete rejection lifecycle."""
    task_id = kb.create_task(conn, title="corrupt after claim", assignee="coder")
    conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (task_id,))
    monkeypatch.setattr(
        kb,
        "_resolve_routing_snapshot",
        lambda *args, **kwargs: {
            "routing_role": "executor", "routing_model": "model",
            "routing_provider": "provider", "routing_contract": 1,
            "routing_reason": "test", "roster_digest": "digest",
            "routing_policy": "{}", "ac_revision": "revision",
            "routing_source": "task_role",
        },
    )
    monkeypatch.setattr(kb, "resolve_workspace", lambda *args, **kwargs: tmp_path)
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda profile: True)

    def corrupt_then_spawn(task, workspace, **kwargs):
        conn.execute(
            "UPDATE task_runs SET routing_model=NULL WHERE id=?",
            (task.current_run_id,),
        )
        return kb._default_spawn(task, workspace, **kwargs)

    result = kb.dispatch_once(conn, spawn_fn=corrupt_then_spawn, failure_limit=3)

    task = kb.get_task(conn, task_id)
    run = conn.execute(
        "SELECT status,outcome,ended_at FROM task_runs WHERE id=?",
        (task.current_run_id or 1,),
    ).fetchone()
    event = conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='spawn_rejected'",
        (task_id,),
    ).fetchone()
    assert result.spawned == []
    assert task.status == "ready"
    assert task.current_run_id is None
    assert task.consecutive_failures == 1
    assert tuple(run)[:2] == ("failed", "spawn_failed")
    assert run["ended_at"] is not None
    assert "incomplete frozen routing snapshot" in json.loads(event["payload"])["reason"]


@pytest.mark.parametrize("kind", ["spawn_failed", "spawn_rejected"])
def test_resume_phase_dual_reads_legacy_and_canonical_spawn_events(conn, kind):
    """Resume inference accepts old and canonical spawn event kinds."""
    task_id = kb.create_task(conn, title="resume review", assignee="coder")
    kb._append_event(conn, task_id, kind, {"retry_status": "review"})

    assert kb._resume_status_from_events(conn, task_id) == "review"


@pytest.mark.parametrize("failure_limit, expected_status", [(3, "ready"), (1, "blocked")])
def test_spawn_rejection_closes_run_and_deduplicates_event(
    conn, monkeypatch, failure_limit, expected_status
):
    """Spawn failure closes its run, releases lease, accounts retry, and audits once."""
    task_id = kb.create_task(conn, title="spawn", assignee="coder")
    conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (task_id,))
    monkeypatch.setattr(
        kb, "_resolve_routing_snapshot",
        lambda *args, **kwargs: {
            "routing_role": "executor", "routing_model": "m",
            "routing_provider": "p", "routing_contract": None,
            "routing_reason": "task role executor", "roster_digest": "d",
            "routing_policy": "p1", "ac_revision": None,
            "routing_source": "task_role",
        },
    )
    kb.claim_task(conn, task_id)
    run_id = kb.get_task(conn, task_id).current_run_id

    blocked = kb._record_spawn_failure(
        conn, task_id, "launch failed", failure_limit=failure_limit
    )
    # Replay event insertion for the same run to prove run_id+kind dedup.
    kb._append_spawn_rejected_event(
        conn, task_id, run_id, "duplicate", 2, failure_limit
    )

    task = kb.get_task(conn, task_id)
    run = conn.execute("SELECT * FROM task_runs WHERE id=?", (run_id,)).fetchone()
    assert blocked is (expected_status == "blocked")
    assert task.status == expected_status
    assert task.current_run_id is None
    assert task.claim_lock is None and task.claim_expires is None
    assert run["status"] == "failed"
    assert run["outcome"] == "spawn_failed"
    payloads = _event_payloads(conn, task_id, "spawn_rejected")
    assert len(payloads) == 1
    assert payloads[0]["run_id"] == run_id
    assert payloads[0]["reason"] == "launch failed"
    assert payloads[0]["consecutive_failures"] == 1
    assert payloads[0]["max_retries"] == failure_limit
