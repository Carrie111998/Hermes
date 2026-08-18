
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_swarm import (
    MULTI_AGENT_LANE_IDS,
    SwarmWorkerSpec,
    create_swarm,
    extract_contract,
    latest_blackboard,
    parse_worker_arg,
    post_blackboard_update,
)
import pytest


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


def test_lane_bound_swarm_persists_contracts_goal_budget_and_runtime(tmp_path):
    conn = kb.connect(tmp_path / "kanban.db")
    try:
        created = create_swarm(
            conn,
            goal="Ask four independent agents for one joke and synthesize it.",
            workers=[
                SwarmWorkerSpec(
                    profile=lane, title=f"{lane} joke", body="Return one joke.",
                    skills=[] if lane == "native_hermes" else ["kanban-worker"], lane_id=lane,
                )
                for lane in MULTI_AGENT_LANE_IDS
            ],
            verifier_assignee="verifier",
            synthesizer_assignee="synthesizer",
            tenant="delivery-test",
        )
        root = kb.get_task(conn, created.root_id)
        workers = [kb.get_task(conn, tid) for tid in created.worker_ids]
        verifier = kb.get_task(conn, created.verifier_id)
        synthesizer = kb.get_task(conn, created.synthesizer_id)

        assert root.goal_mode is True
        assert root.goal_max_turns == 5
        assert all(task.goal_mode is True for task in workers)
        assert all(task.goal_max_turns == 5 for task in workers)
        assert all(task.max_runtime_seconds == 120 for task in workers)
        assert [extract_contract(task.body)["expected_lane_id"] for task in workers] == list(MULTI_AGENT_LANE_IDS)
        assert extract_contract(verifier.body)["expected_lane_count"] == 4
        assert extract_contract(synthesizer.body)["verifier_id"] == created.verifier_id
    finally:
        conn.close()


def test_lane_bound_completion_is_fail_closed_and_synth_requires_verifier_gate(tmp_path):
    conn = kb.connect(tmp_path / "kanban.db")
    try:
        created = create_swarm(
            conn,
            goal="Run the four-lane joke test.",
            workers=[
                SwarmWorkerSpec(
                    profile=lane, title=lane, body="Work.",
                    skills=[] if lane == "native_hermes" else ["kanban-worker"], lane_id=lane,
                )
                for lane in MULTI_AGENT_LANE_IDS
            ],
            verifier_assignee="verifier",
            synthesizer_assignee="synthesizer",
        )
        for worker_id, lane in zip(created.worker_ids, MULTI_AGENT_LANE_IDS):
            with pytest.raises(ValueError, match="lane_id"):
                kb.complete_task(
                    conn, worker_id, summary="done",
                    metadata={
                        "role": "worker", "root_id": created.root_id,
                        "lane_id": "wrong", "preflight_skill_id": "" if lane == "native_hermes" else "kanban-worker",
                        "outcome": "completed", "verified_clean": True,
                    },
                )
            assert kb.complete_task(
                conn, worker_id, summary="done",
                metadata={
                    "role": "worker", "root_id": created.root_id,
                    "lane_id": lane, "preflight_skill_id": "" if lane == "native_hermes" else "kanban-worker",
                    "outcome": "completed", "verified_clean": True,
                },
            )
        assert kb.get_task(conn, created.verifier_id).status == "ready"
        assert kb.complete_task(
            conn, created.verifier_id, summary="verified",
            metadata={
                "role": "verifier", "root_id": created.root_id,
                "gate": "pass", "expected_lane_count": 4,
                "verified_lane_count": 4,
            },
        )
        assert kb.get_task(conn, created.synthesizer_id).status == "ready"
        assert kb.complete_task(
            conn, created.synthesizer_id, result="The synthesized joke.",
            metadata={
                "role": "synthesizer", "root_id": created.root_id,
                "outcome": "completed", "result_present": True,
            },
        )
        assert kb.get_task(conn, created.synthesizer_id).status == "done"
    finally:
        conn.close()


def test_swarm_worker_parser_keeps_third_segment_as_skill_only():
    spec = parse_worker_arg("claude:Return one bounded joke:kanban-worker")
    assert spec.profile == "claude"
    assert spec.body == "Return one bounded joke"
    assert spec.skills == ["kanban-worker"]
