
import os

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import projects_db as pdb
from hermes_cli.kanban_swarm import (
    SwarmWorkerSpec,
    create_swarm,
    latest_blackboard,
    post_blackboard_update,
)


def test_create_swarm_builds_parallel_workers_verifier_and_synthesizer(tmp_path):
    conn = kb.connect(tmp_path / "kanban.db")
    try:
        created = create_swarm(
            conn,
            goal="Map the target market and produce a decision memo.",
            workers=[
                SwarmWorkerSpec(profile="researcher-a", title="Market scan", body="Find competitors"),
                SwarmWorkerSpec(profile="researcher-b", title="Customer scan", body="Find customer pains"),
            ],
            verifier_assignee="reviewer",
            synthesizer_assignee="writer",
            tenant="intel",
            created_by="orchestrator",
        )

        root = kb.get_task(conn, created.root_id)
        workers = [kb.get_task(conn, tid) for tid in created.worker_ids]
        verifier = kb.get_task(conn, created.verifier_id)
        synthesizer = kb.get_task(conn, created.synthesizer_id)

        assert root.status == "done"
        assert root.assignee == "orchestrator"
        assert [task.status for task in workers] == ["ready", "ready"]
        assert [task.assignee for task in workers] == ["researcher-a", "researcher-b"]
        assert verifier.status == "todo"
        assert synthesizer.status == "todo"
        assert set(kb.parent_ids(conn, created.verifier_id)) == set(created.worker_ids)
        assert kb.parent_ids(conn, created.synthesizer_id) == [created.verifier_id]
        assert all(created.root_id in (task.body or "") for task in workers)
    finally:
        conn.close()


def test_swarm_blackboard_merges_structured_updates(tmp_path):
    conn = kb.connect(tmp_path / "kanban.db")
    try:
        created = create_swarm(
            conn,
            goal="Collect evidence.",
            workers=[SwarmWorkerSpec(profile="researcher", title="Evidence", body="Find proof")],
            verifier_assignee="reviewer",
            synthesizer_assignee="writer",
        )

        post_blackboard_update(
            conn,
            created.root_id,
            author="researcher",
            key="sources",
            value=["https://example.com/a"],
        )
        post_blackboard_update(
            conn,
            created.root_id,
            author="reviewer",
            key="risks",
            value={"missing_primary_source": True},
        )

        board = latest_blackboard(conn, created.root_id)
        assert board["sources"] == ["https://example.com/a"]
        assert board["risks"] == {"missing_primary_source": True}
        assert board["_authors"]["sources"] == "researcher"
    finally:
        conn.close()


def test_swarm_verifier_and_synthesis_are_dependency_gated(tmp_path):
    conn = kb.connect(tmp_path / "kanban.db")
    try:
        created = create_swarm(
            conn,
            goal="Research two branches then verify and synthesize.",
            workers=[
                SwarmWorkerSpec(profile="a", title="Branch A", body="A"),
                SwarmWorkerSpec(profile="b", title="Branch B", body="B"),
            ],
            verifier_assignee="reviewer",
            synthesizer_assignee="writer",
        )

        kb.complete_task(
            conn,
            created.worker_ids[0],
            summary="A done",
            metadata={"confidence": 0.8},
        )
        kb.recompute_ready(conn)
        assert kb.get_task(conn, created.verifier_id).status == "todo"
        assert kb.get_task(conn, created.synthesizer_id).status == "todo"

        kb.complete_task(conn, created.worker_ids[1], summary="B done")
        kb.recompute_ready(conn)
        assert kb.get_task(conn, created.verifier_id).status == "ready"
        assert kb.get_task(conn, created.synthesizer_id).status == "todo"

        kb.complete_task(
            conn,
            created.verifier_id,
            summary="Verified both branches",
            metadata={"gate": "pass"},
        )
        kb.recompute_ready(conn)
        assert kb.get_task(conn, created.synthesizer_id).status == "ready"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Worktree swarms: every card shares one checkout
# ---------------------------------------------------------------------------

def _make_project(name="Web App", repo="/tmp/webapp"):
    with pdb.connect_closing() as pc:
        pid = pdb.create_project(pc, name=name, folders=[repo])
        return pdb.get_project(pc, pid)


def test_worktree_swarm_shares_explicit_checkout_across_all_cards(tmp_path):
    """An explicit worktree path/branch reaches the verifier and synthesizer.

    Without this the verifier opens its own checkout and reviews an empty
    tree instead of what the worker committed.
    """
    conn = kb.connect(tmp_path / "kanban.db")
    try:
        created = create_swarm(
            conn,
            goal="Wire the login endpoint and review it.",
            workers=[SwarmWorkerSpec(profile="programmer", title="Wire login", body="Implement")],
            verifier_assignee="code-reviewer",
            synthesizer_assignee="writer",
            workspace_kind="worktree",
            workspace_path="/repo/checkout",
            branch_name="wt/login",
        )

        cards = [
            kb.get_task(conn, created.worker_ids[0]),
            kb.get_task(conn, created.verifier_id),
            kb.get_task(conn, created.synthesizer_id),
        ]
        assert {c.workspace_kind for c in cards} == {"worktree"}
        assert {c.workspace_path for c in cards} == {"/repo/checkout"}
        # The branch matters as much as the path: resolve_workspace only reuses
        # an existing checkout when the branch matches, and otherwise forks a
        # fresh worktree — which would defeat the shared path.
        assert {c.branch_name for c in cards} == {"wt/login"}
    finally:
        conn.close()


def test_worktree_swarm_pins_project_derived_checkout_for_downstream_cards(tmp_path):
    """With only --project, the first worker's *resolved* worktree is reused.

    ``create_task`` hands each project-linked task its own fresh
    ``<repo>/.worktrees/<task-id>``, so without pinning every card in the swarm
    would land somewhere different.
    """
    proj = _make_project()
    conn = kb.connect(tmp_path / "kanban.db")
    try:
        created = create_swarm(
            conn,
            goal="Add login and review it.",
            workers=[SwarmWorkerSpec(profile="programmer", title="Add login", body="Implement")],
            verifier_assignee="code-reviewer",
            synthesizer_assignee="writer",
            workspace_kind="worktree",
            project_id=proj.slug,
        )

        worker = kb.get_task(conn, created.worker_ids[0])
        verifier = kb.get_task(conn, created.verifier_id)
        synthesizer = kb.get_task(conn, created.synthesizer_id)

        # The worker got the deterministic project worktree...
        assert worker.workspace_path == os.path.join(
            proj.primary_path, ".worktrees", worker.id
        )
        # ...and the downstream cards were pinned to that exact path + branch,
        # not to fresh dirs keyed on their own task ids.
        assert verifier.workspace_path == worker.workspace_path
        assert synthesizer.workspace_path == worker.workspace_path
        assert verifier.branch_name == worker.branch_name
        assert synthesizer.branch_name == worker.branch_name
    finally:
        conn.close()


def test_worktree_swarm_rejects_multiple_workers(tmp_path):
    """Parallel workers on one checkout would clobber each other — fail loudly."""
    conn = kb.connect(tmp_path / "kanban.db")
    try:
        with pytest.raises(ValueError, match="exactly one"):
            create_swarm(
                conn,
                goal="Two people editing one branch.",
                workers=[
                    SwarmWorkerSpec(profile="programmer", title="A", body="a"),
                    SwarmWorkerSpec(profile="programmer", title="B", body="b"),
                ],
                verifier_assignee="code-reviewer",
                synthesizer_assignee="writer",
                workspace_kind="worktree",
                workspace_path="/repo/checkout",
            )
    finally:
        conn.close()


def test_scratch_swarm_keeps_independent_workspaces(tmp_path):
    """Regression: the default swarm is unchanged — no branch, no sharing."""
    conn = kb.connect(tmp_path / "kanban.db")
    try:
        created = create_swarm(
            conn,
            goal="Two independent research tracks.",
            workers=[
                SwarmWorkerSpec(profile="researcher-a", title="A", body="a"),
                SwarmWorkerSpec(profile="researcher-b", title="B", body="b"),
            ],
            verifier_assignee="reviewer",
            synthesizer_assignee="writer",
        )
        cards = [kb.get_task(conn, tid) for tid in created.worker_ids]
        cards += [kb.get_task(conn, created.verifier_id),
                  kb.get_task(conn, created.synthesizer_id)]
        assert {c.workspace_kind for c in cards} == {"scratch"}
        assert {c.branch_name for c in cards} == {None}
    finally:
        conn.close()
