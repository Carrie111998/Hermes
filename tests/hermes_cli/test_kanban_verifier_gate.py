"""Task 2 RED contract: kernel verifier PASS and prerequisite versions.

These tests deliberately exercise only disposable SQLite databases.  They
freeze the security boundary before production code exists: a verification
edge is not satisfied by a task status, completion metadata, or another
mutation surface.  Every failing assertion on the Task-1 baseline identifies
an unsafe eligibility/completion path, rather than an import or fixture error.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


WORKFLOW = "task2-verifier-gate-v1"
ACCEPTANCE_CONTRACT_HASH = "sha256:acceptance-v1"
SOURCE_REVISION = "4b399c477c8b78a34b08456618a8580653c41003"
MANIFEST_HASH = "sha256:canonical-manifest-v1"
HANDOFF_ID = "handoff-immutable-1"
PREDECESSOR_RECEIPT_VERSION = 7


@pytest.fixture
def conn(tmp_path: Path):
    db_path = tmp_path / "task2-red.db"
    connection = kb.connect(db_path)
    try:
        yield connection
    finally:
        connection.close()


def _verification_graph(conn):
    """Create a v2-tagged verifier -> successor edge using existing kernel fields."""
    verifier = kb.create_task(conn, title="independent verifier", assignee="gauge")
    successor = kb.create_task(
        conn,
        title="integration successor",
        assignee="circuit",
        parents=[verifier],
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
    assert kb.get_task(conn, verifier).status == "ready"
    assert kb.get_task(conn, successor).status == "todo"
    return verifier, successor


def _generic_complete(conn, verifier):
    assert kb.complete_task(conn, verifier, result="PASS")


def _manual_complete(conn, verifier):
    assert kb.complete_task(
        conn, verifier, result="PASS", authority_mode=kb.CompletionAuthority.LEGACY
    )


def _orchestrator_complete(conn, verifier):
    assert kb.complete_task(
        conn,
        verifier,
        result="PASS",
        authority_mode=kb.CompletionAuthority.ORCHESTRATOR,
    )


def _metadata_pass(conn, verifier):
    """Models API/dashboard/AgentOS/bulk callers forwarding claimed PASS metadata."""
    assert kb.complete_task(
        conn,
        verifier,
        result="PASS",
        metadata={
            "verifier_outcome": "PASS",
            "verifier_role": "gauge",
            "handoff_id": HANDOFF_ID,
            "manifest_hash": MANIFEST_HASH,
            "source_revision": SOURCE_REVISION,
            "acceptance_contract_hash": ACCEPTANCE_CONTRACT_HASH,
            "predecessor_receipt_versions": [PREDECESSOR_RECEIPT_VERSION],
        },
        authority_mode=kb.CompletionAuthority.ORCHESTRATOR,
    )


def _archive(conn, verifier):
    assert kb.archive_task(conn, verifier)


def _direct_sql(conn, verifier):
    conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (verifier,))
    conn.commit()
    kb.recompute_ready(conn)


BYPASS_ACTIONS: dict[str, Callable] = {
    "generic-complete": _generic_complete,
    "manual-cli-complete": _manual_complete,
    "api-complete": _metadata_pass,
    "dashboard-agentos-complete": _metadata_pass,
    "bulk-complete": _orchestrator_complete,
    "direct-orchestrator-complete": _orchestrator_complete,
    "archive": _archive,
    "direct-sql-status-repair": _direct_sql,
}


@pytest.mark.parametrize("surface", BYPASS_ACTIONS, ids=BYPASS_ACTIONS)
def test_generic_mutation_surface_cannot_unlock_verification_dependency(conn, surface):
    verifier, successor = _verification_graph(conn)

    BYPASS_ACTIONS[surface](conn, verifier)

    assert kb.get_task(conn, successor).status == "todo", (
        f"{surface} bypassed the kernel verifier-PASS receipt predicate"
    )
    assert kb.claim_task(conn, successor, claimer="must-not-claim") is None


@pytest.mark.parametrize(
    "mismatch,wrong_value",
    [
        ("source_revision", "wrong-revision"),
        ("manifest_hash", "sha256:wrong-manifest"),
        ("acceptance_contract_hash", "sha256:wrong-contract"),
        ("predecessor_receipt_versions", [PREDECESSOR_RECEIPT_VERSION - 1]),
    ],
)
def test_purported_pass_mismatch_deterministically_enters_rework(
    conn, mismatch, wrong_value
):
    verifier, successor = _verification_graph(conn)
    metadata = {
        "verifier_outcome": "PASS",
        "verifier_role": "gauge",
        "verifier_run_id": "verifier-run-1",
        "handoff_id": HANDOFF_ID,
        "manifest_hash": MANIFEST_HASH,
        "source_revision": SOURCE_REVISION,
        "acceptance_contract_hash": ACCEPTANCE_CONTRACT_HASH,
        "predecessor_receipt_versions": [PREDECESSOR_RECEIPT_VERSION],
    }
    metadata[mismatch] = wrong_value

    kb.complete_task(
        conn,
        verifier,
        result="PASS",
        metadata=metadata,
        authority_mode=kb.CompletionAuthority.ORCHESTRATOR,
    )

    assert kb.get_task(conn, verifier).status != "done", (
        f"{mismatch} mismatch was accepted instead of entering REWORK"
    )
    assert kb.get_task(conn, successor).status == "todo"
    assert any(event.kind == "verifier_rework" for event in kb.list_events(conn, verifier))


def _claimed_running_descendant(conn):
    """Claim under ordinary dependency rules, then opt into verifier identity.

    The setup must remain reachable after the generic verifier-bypass defect is
    fixed.  Therefore the predecessor is completed while it is still an
    ordinary legacy dependency; only after its descendant is running do we tag
    the graph as the workflow verifier edge whose stability is under test.
    """
    predecessor = kb.create_task(conn, title="ordinary predecessor")
    descendant = kb.create_task(
        conn,
        title="running integration descendant",
        assignee="circuit",
        parents=[predecessor],
    )
    assert kb.get_task(conn, predecessor).status == "ready"
    assert kb.get_task(conn, descendant).status == "todo"
    assert kb.complete_task(
        conn,
        predecessor,
        result="ordinary legacy completion",
        authority_mode=kb.CompletionAuthority.LEGACY,
    )
    running = kb.claim_task(conn, descendant, claimer="descendant-claim")
    assert running is not None
    run_id = kb.get_task(conn, descendant).current_run_id
    assert run_id is not None

    conn.execute(
        "UPDATE tasks SET workflow_template_id = ?, current_step_key = ? WHERE id = ?",
        (WORKFLOW, "verify", predecessor),
    )
    conn.execute(
        "UPDATE tasks SET workflow_template_id = ?, current_step_key = ? WHERE id = ?",
        (WORKFLOW, "integrate", descendant),
    )
    conn.commit()
    return predecessor, descendant, run_id


@pytest.mark.parametrize("mutation", ["reopened", "relinked", "receipt-version-changed"])
def test_running_descendant_cannot_complete_after_predecessor_mutation(conn, mutation):
    predecessor, descendant, run_id = _claimed_running_descendant(conn)

    if mutation == "reopened":
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (predecessor,))
    elif mutation == "relinked":
        replacement = kb.create_task(conn, title="replacement predecessor")
        conn.execute(
            "DELETE FROM task_links WHERE parent_id = ? AND child_id = ?",
            (predecessor, descendant),
        )
        conn.execute(
            "INSERT INTO task_links(parent_id, child_id) VALUES (?, ?)",
            (replacement, descendant),
        )
    else:
        conn.execute(
            "UPDATE tasks SET result = ? WHERE id = ?",
            ("receipt-version-8", predecessor),
        )
    conn.commit()

    completed = kb.complete_task(
        conn,
        descendant,
        result="must be invalidated",
        expected_run_id=run_id,
        expected_claim_token="descendant-claim",
        authority_mode=kb.CompletionAuthority.WORKER,
    )

    assert completed is False, (
        f"running descendant completed after predecessor was {mutation}"
    )
    assert kb.get_task(conn, descendant).status != "done"
