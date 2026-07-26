"""Implementer-owned positive and schema tests for the Task 2 verifier gate.

All databases are disposable ``tmp_path`` fixtures.  The independently frozen
negative contract remains in ``test_kanban_verifier_gate.py`` and is not edited
here.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


WORKFLOW = "task2-positive-v1"
STEP_ATTEMPT = "task2-positive-v1/implement/1"
REVISION = "4b399c477c8b78a34b08456618a8580653c41003"
BASELINE = "sha256:" + "b" * 64
CONTRACT = "sha256:" + "c" * 64
MANIFEST = [
    {
        "path": "src/example.py",
        "type": "file",
        "size": 12,
        "sha256": "a" * 64,
    }
]


@pytest.fixture
def conn(tmp_path: Path):
    connection = kb.connect(tmp_path / "task2-positive.db")
    try:
        yield connection
    finally:
        connection.close()


def _positive_graph(conn):
    implementation = kb.create_task(conn, title="implementation", assignee="circuit")
    claimed_impl = kb.claim_task(conn, implementation, claimer="implementation-claim")
    assert claimed_impl is not None
    implementation_run_id = kb.get_task(conn, implementation).current_run_id
    assert implementation_run_id is not None
    assert kb.complete_task(
        conn,
        implementation,
        result="implementation complete",
        expected_run_id=implementation_run_id,
        expected_claim_token="implementation-claim",
        authority_mode=kb.CompletionAuthority.WORKER,
    )
    implementation_event_id = next(
        event.id
        for event in reversed(kb.list_events(conn, implementation))
        if event.kind == "completed"
    )

    verifier = kb.create_task(
        conn,
        title="independent verifier",
        assignee="gauge",
        parents=[implementation],
    )
    successor = kb.create_task(
        conn,
        title="integration successor",
        assignee="circuit",
        parents=[verifier],
    )
    conn.execute(
        "UPDATE tasks SET workflow_template_id = ?, current_step_key = ? WHERE id = ?",
        (WORKFLOW, "implement", implementation),
    )
    conn.execute(
        "UPDATE tasks SET workflow_template_id = ?, current_step_key = ? WHERE id = ?",
        (WORKFLOW, "verify", verifier),
    )
    conn.execute(
        "UPDATE tasks SET workflow_template_id = ?, current_step_key = ? WHERE id = ?",
        (WORKFLOW, "integrate", successor),
    )
    conn.commit()

    handoff_id = kb.record_accepted_handoff(
        conn,
        workflow_step_attempt_id=STEP_ATTEMPT,
        implementation_task_id=implementation,
        implementation_run_id=implementation_run_id,
        implementation_event_id=implementation_event_id,
        verifier_task_id=verifier,
        verifier_role="gauge",
        artifact_manifest=MANIFEST,
        source_revision=REVISION,
        baseline_manifest_digest=BASELINE,
        acceptance_contract_hash=CONTRACT,
    )
    claimed_verifier = kb.claim_task(conn, verifier, claimer="verifier-claim")
    assert claimed_verifier is not None
    verifier_run_id = kb.get_task(conn, verifier).current_run_id
    assert verifier_run_id is not None
    return verifier, successor, verifier_run_id, handoff_id


def _record_pass(conn, verifier, verifier_run_id, handoff_id, **overrides):
    payload = {
        "verifier_role": "gauge",
        "accepted_handoff_id": handoff_id,
        "artifact_manifest": MANIFEST,
        "source_revision": REVISION,
        "baseline_manifest_digest": BASELINE,
        "acceptance_contract_hash": CONTRACT,
    }
    payload.update(overrides)
    return kb.record_verifier_pass(
        conn,
        verifier,
        expected_run_id=verifier_run_id,
        expected_claim_token="verifier-claim",
        **payload,
    )


def test_additive_schema_has_immutable_receipts_and_prerequisite_snapshots(conn):
    tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert {
        "accepted_handoffs",
        "verifier_pass_receipts",
        "task_run_prerequisite_sets",
    } <= tables

    triggers = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")
    }
    assert {
        "accepted_handoffs_immutable_update",
        "accepted_handoffs_immutable_delete",
        "verifier_pass_receipts_immutable_update",
        "verifier_pass_receipts_immutable_delete",
        "task_run_prerequisite_sets_immutable_update",
        "task_run_prerequisite_sets_immutable_delete",
    } <= triggers


def test_task2_schema_migration_is_idempotent_and_preserves_existing_rows(tmp_path: Path):
    db_path = tmp_path / "pre-task2.db"
    connection = kb.connect(db_path)
    legacy_task = kb.create_task(connection, title="preserve me", assignee="circuit")
    connection.executescript(
        """
        DROP TRIGGER task_run_prerequisite_sets_immutable_delete;
        DROP TRIGGER task_run_prerequisite_sets_immutable_update;
        DROP TRIGGER verifier_pass_receipts_immutable_delete;
        DROP TRIGGER verifier_pass_receipts_immutable_update;
        DROP TRIGGER accepted_handoffs_immutable_delete;
        DROP TRIGGER accepted_handoffs_immutable_update;
        DROP TABLE task_run_prerequisite_sets;
        DROP TABLE verifier_pass_receipts;
        DROP TABLE accepted_handoffs;
        """
    )
    connection.close()
    with kb._INIT_LOCK:
        kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))

    kb.init_db(db_path)
    kb.init_db(db_path)

    migrated = kb.connect(db_path)
    try:
        preserved = kb.get_task(migrated, legacy_task)
        assert preserved is not None
        assert preserved.title == "preserve me"
        tables = {
            row["name"]
            for row in migrated.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {
            "accepted_handoffs",
            "verifier_pass_receipts",
            "task_run_prerequisite_sets",
        } <= tables
        assert migrated.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert migrated.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        migrated.close()


def test_accepted_handoff_is_unique_per_step_attempt_and_database_immutable(conn):
    verifier, _, _, handoff_id = _positive_graph(conn)
    row = conn.execute(
        "SELECT * FROM accepted_handoffs WHERE id = ?", (handoff_id,)
    ).fetchone()
    assert row["workflow_step_attempt_id"] == STEP_ATTEMPT
    assert json.loads(row["artifact_manifest_json"]) == MANIFEST

    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute(
            "UPDATE accepted_handoffs SET verifier_task_id = ? WHERE id = ?",
            ("different", handoff_id),
        )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("DELETE FROM accepted_handoffs WHERE id = ?", (handoff_id,))

    with pytest.raises(sqlite3.IntegrityError):
        kb.record_accepted_handoff(
            conn,
            workflow_step_attempt_id=STEP_ATTEMPT,
            implementation_task_id=verifier,
            implementation_run_id=999,
            implementation_event_id=999,
            verifier_task_id=verifier,
            verifier_role="gauge",
            artifact_manifest=MANIFEST,
            source_revision=REVISION,
            baseline_manifest_digest=BASELINE,
            acceptance_contract_hash=CONTRACT,
        )


def test_kernel_created_exact_verifier_pass_unlocks_only_predeclared_successor(conn):
    verifier, successor, verifier_run_id, handoff_id = _positive_graph(conn)

    receipt_id = _record_pass(conn, verifier, verifier_run_id, handoff_id)

    assert receipt_id.startswith("vpr_")
    assert kb.get_task(conn, verifier).status == "done"
    assert kb.get_task(conn, successor).status == "ready"
    receipt = conn.execute(
        "SELECT * FROM verifier_pass_receipts WHERE id = ?", (receipt_id,)
    ).fetchone()
    assert receipt["accepted_handoff_id"] == handoff_id
    assert receipt["outcome"] == "PASS"
    assert receipt["receipt_version"] == 1
    assert kb.claim_task(conn, successor, claimer="successor-claim") is not None


@pytest.mark.parametrize(
    "field,value",
    [
        ("verifier_role", "circuit"),
        ("source_revision", "wrong-revision"),
        ("baseline_manifest_digest", "sha256:" + "0" * 64),
        ("acceptance_contract_hash", "sha256:" + "1" * 64),
        (
            "artifact_manifest",
            [{"path": "src/example.py", "type": "file", "size": 12, "sha256": "d" * 64}],
        ),
    ],
)
def test_kernel_pass_mismatch_records_rework_and_no_receipt(conn, field, value):
    verifier, successor, verifier_run_id, handoff_id = _positive_graph(conn)

    receipt_id = _record_pass(
        conn, verifier, verifier_run_id, handoff_id, **{field: value}
    )

    assert receipt_id is None
    assert kb.get_task(conn, verifier).status == "blocked"
    assert kb.get_task(conn, successor).status == "todo"
    assert conn.execute("SELECT count(*) FROM verifier_pass_receipts").fetchone()[0] == 0
    assert any(event.kind == "verifier_rework" for event in kb.list_events(conn, verifier))


def test_route_gated_handoff_requires_exact_runtime_telemetry(conn):
    implementation = kb.create_task(conn, title="route implementation", assignee="circuit")
    run = kb.claim_task(conn, implementation, claimer="route-impl")
    assert run is not None
    implementation_run_id = kb.get_task(conn, implementation).current_run_id
    assert kb.complete_task(
        conn,
        implementation,
        result="done",
        expected_run_id=implementation_run_id,
        expected_claim_token="route-impl",
        authority_mode=kb.CompletionAuthority.WORKER,
    )
    implementation_event_id = next(
        event.id
        for event in reversed(kb.list_events(conn, implementation))
        if event.kind == "completed"
    )
    verifier = kb.create_task(conn, title="route verifier", assignee="gauge", parents=[implementation])
    successor = kb.create_task(conn, title="route successor", parents=[verifier])
    for task, step in ((implementation, "implement"), (verifier, "verify"), (successor, "integrate")):
        conn.execute(
            "UPDATE tasks SET workflow_template_id = ?, current_step_key = ? WHERE id = ?",
            (WORKFLOW + "-route", step, task),
        )
    conn.commit()
    handoff_id = kb.record_accepted_handoff(
        conn,
        workflow_step_attempt_id=STEP_ATTEMPT + "-route",
        implementation_task_id=implementation,
        implementation_run_id=implementation_run_id,
        implementation_event_id=implementation_event_id,
        verifier_task_id=verifier,
        verifier_role="gauge",
        artifact_manifest=MANIFEST,
        source_revision=REVISION,
        baseline_manifest_digest=BASELINE,
        acceptance_contract_hash=CONTRACT,
        route_gate={"requested_provider": "openai-codex", "requested_model": "gpt-5.6-sol"},
    )
    assert kb.claim_task(conn, verifier, claimer="route-verifier") is not None
    verifier_run_id = kb.get_task(conn, verifier).current_run_id

    receipt = kb.record_verifier_pass(
        conn,
        verifier,
        expected_run_id=verifier_run_id,
        expected_claim_token="route-verifier",
        verifier_role="gauge",
        accepted_handoff_id=handoff_id,
        artifact_manifest=MANIFEST,
        source_revision=REVISION,
        baseline_manifest_digest=BASELINE,
        acceptance_contract_hash=CONTRACT,
        route_telemetry={
            "requested_provider": "openai-codex",
            "requested_model": "gpt-5.6-sol",
            "actual_provider": "other",
            "actual_model": "gpt-5.6-sol",
            "fallback_index": 0,
        },
    )

    assert receipt is None
    assert kb.get_task(conn, successor).status == "todo"


def test_direct_sql_done_verifier_without_receipt_stays_unclaimable(conn):
    verifier = kb.create_task(conn, title="sql verifier", assignee="gauge")
    successor = kb.create_task(conn, title="sql successor", parents=[verifier])
    for task, step in ((verifier, "verify"), (successor, "integrate")):
        conn.execute(
            "UPDATE tasks SET workflow_template_id = ?, current_step_key = ? WHERE id = ?",
            (WORKFLOW + "-sql", step, task),
        )
    conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (verifier,))
    conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (successor,))
    conn.commit()

    assert kb.claim_task(conn, successor, claimer="sql-bypass") is None
    assert kb.get_task(conn, successor).status == "todo"


def test_successor_completion_revalidates_captured_verifier_receipt_version(conn):
    verifier, successor, verifier_run_id, handoff_id = _positive_graph(conn)
    assert _record_pass(conn, verifier, verifier_run_id, handoff_id)
    assert kb.claim_task(conn, successor, claimer="successor") is not None
    successor_run_id = kb.get_task(conn, successor).current_run_id

    # Direct status repair cannot delete the immutable receipt, but reopening the
    # verifier makes the captured prerequisite set stale and must fail closed.
    conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (verifier,))
    conn.commit()

    assert not kb.complete_task(
        conn,
        successor,
        result="must not complete",
        expected_run_id=successor_run_id,
        expected_claim_token="successor",
        authority_mode=kb.CompletionAuthority.WORKER,
    )
    final_successor = kb.get_task(conn, successor)
    assert final_successor is not None
    assert final_successor.status != "done"


def test_predeployment_workflow_run_without_snapshot_fails_closed(conn):
    verifier = kb.create_task(conn, title="legacy verifier", assignee="gauge")
    conn.execute("UPDATE tasks SET status = 'done', result = 'PASS' WHERE id = ?", (verifier,))
    successor = kb.create_task(conn, title="legacy successor", parents=[verifier])
    for task, step in ((verifier, "verify"), (successor, "integrate")):
        conn.execute(
            "UPDATE tasks SET workflow_template_id = ?, current_step_key = ? WHERE id = ?",
            (WORKFLOW + "-legacy", step, task),
        )
    # Simulate a run that was already active when the additive table appeared.
    conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (successor,))
    conn.commit()
    assert kb.claim_task(conn, successor, claimer="legacy-successor") is None

    # A pre-deployment run can exist with no snapshot row. Construct that exact
    # legacy state and prove terminal completion does not receive compatibility
    # authority merely because the additive row is absent.
    now = 1_700_000_000
    conn.execute(
        """
        INSERT INTO task_runs (task_id, profile, status, claim_lock, started_at)
        VALUES (?, 'circuit', 'running', 'legacy-claim', ?)
        """,
        (successor, now),
    )
    run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        """
        UPDATE tasks SET status = 'running', current_run_id = ?, claim_lock = 'legacy-claim'
        WHERE id = ?
        """,
        (run_id, successor),
    )
    conn.commit()

    assert not kb.complete_task(
        conn,
        successor,
        result="must fail closed",
        expected_run_id=run_id,
        expected_claim_token="legacy-claim",
        authority_mode=kb.CompletionAuthority.WORKER,
    )
    final_successor = kb.get_task(conn, successor)
    assert final_successor is not None
    assert final_successor.status != "done"
