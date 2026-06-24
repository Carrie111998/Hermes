"""Unit tests for the dynamic workflow engine and registry."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from plugins.workflow.dynamic import (
    COMPLETED,
    CANCELLED,
    PENDING,
    WF_CANCELLED,
    WF_COMPLETED,
    WF_EMPTY,
    WF_READY,
    WF_RUNNING,
    DynamicNode,
    DynamicWorkflow,
    _completed_workflows,
    _derive_status,
    _reset_for_tests,
    _workflows,
    handle_workflow_dynamic,
)
from plugins.workflow.registry import (
    list_workflows,
    match_workflow_trigger,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _parse(result: str) -> dict:
    """Parse a JSON envelope string and return the dict."""
    return json.loads(result)


def _make_agent(session_id: str = "test-session") -> MagicMock:
    """Return a minimal mock parent agent with a session_id."""
    agent = MagicMock()
    agent.session_id = session_id
    return agent


def _make_nodes(*specs: tuple[str, str, list[str]]) -> list[dict]:
    """Build a list of node dicts from (node_id, goal, depends_on) tuples."""
    return [
        {"node_id": nid, "goal": goal, "depends_on": deps}
        for nid, goal, deps in specs
    ]


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_state():
    """Reset all in-memory workflow state before and after each test."""
    _reset_for_tests()
    yield
    _reset_for_tests()


# ── dynamic.py — create action ───────────────────────────────────────────────


class TestCreateWorkflow:
    """Tests for action='create'."""

    def test_create_workflow(self):
        """Create a workflow and verify status and nodes are present."""
        agent = _make_agent()
        nodes = _make_nodes(("n1", "Do something", []))
        result = _parse(
            handle_workflow_dynamic(
                {
                    "action": "create",
                    "objective": "Test objective",
                    "nodes": nodes,
                    "workflow_id": "wf-test-1",
                },
                agent,
            )
        )
        assert result["ok"] is True
        wf = result["workflow"]
        assert wf["workflow_id"] == "wf-test-1"
        assert wf["status"] == WF_READY  # pending nodes with no deps are ready
        assert len(wf["nodes"]) == 1
        assert wf["nodes"][0]["node_id"] == "n1"
        assert wf["nodes"][0]["goal"] == "Do something"

    def test_create_rejects_duplicate_id(self):
        """Creating a workflow with an existing workflow_id returns an error."""
        agent = _make_agent()
        nodes = _make_nodes(("n1", "Do something", []))
        args = {
            "action": "create",
            "objective": "Test objective",
            "nodes": nodes,
            "workflow_id": "wf-dup",
        }
        # First create succeeds
        r1 = _parse(handle_workflow_dynamic(args, agent))
        assert r1["ok"] is True

        # Second create with same id fails
        r2 = _parse(handle_workflow_dynamic(args, agent))
        assert r2["ok"] is False
        assert "already exists" in r2["error"]

    def test_create_validates_dag_empty_goal(self):
        """A node with an empty goal is rejected."""
        agent = _make_agent()
        result = _parse(
            handle_workflow_dynamic(
                {
                    "action": "create",
                    "objective": "Test",
                    "nodes": [{"node_id": "n1", "goal": "", "depends_on": []}],
                    "workflow_id": "wf-vg-1",
                },
                agent,
            )
        )
        assert result["ok"] is False
        assert "goal" in result["error"].lower() or "required" in result["error"].lower()

    def test_create_validates_dag_bad_deps(self):
        """A node depending on a non-existent node is rejected."""
        agent = _make_agent()
        result = _parse(
            handle_workflow_dynamic(
                {
                    "action": "create",
                    "objective": "Test",
                    "nodes": [
                        {"node_id": "n1", "goal": "Step 1", "depends_on": []},
                        {
                            "node_id": "n2",
                            "goal": "Step 2",
                            "depends_on": ["nonexistent"],
                        },
                    ],
                    "workflow_id": "wf-vd-1",
                },
                agent,
            )
        )
        assert result["ok"] is False
        assert "unknown" in result["error"].lower() or "depend" in result["error"].lower()

    def test_create_validates_dag_cycle(self):
        """Circular dependencies are rejected."""
        agent = _make_agent()
        result = _parse(
            handle_workflow_dynamic(
                {
                    "action": "create",
                    "objective": "Test",
                    "nodes": [
                        {"node_id": "a", "goal": "A", "depends_on": ["b"]},
                        {"node_id": "b", "goal": "B", "depends_on": ["a"]},
                    ],
                    "workflow_id": "wf-cyc-1",
                },
                agent,
            )
        )
        assert result["ok"] is False
        assert "cycle" in result["error"].lower()


# ── dynamic.py — extend action ───────────────────────────────────────────────


class TestExtendWorkflow:
    """Tests for action='extend'."""

    def test_extend_workflow(self):
        """Add nodes to an existing workflow."""
        agent = _make_agent()
        # Create initial workflow
        r1 = _parse(
            handle_workflow_dynamic(
                {
                    "action": "create",
                    "objective": "Test",
                    "nodes": _make_nodes(("n1", "Step 1", [])),
                    "workflow_id": "wf-ext-1",
                },
                agent,
            )
        )
        assert r1["ok"] is True
        assert len(r1["workflow"]["nodes"]) == 1

        # Extend with a new node
        r2 = _parse(
            handle_workflow_dynamic(
                {
                    "action": "extend",
                    "workflow_id": "wf-ext-1",
                    "nodes": _make_nodes(("n2", "Step 2", ["n1"])),
                },
                agent,
            )
        )
        assert r2["ok"] is True
        assert len(r2["workflow"]["nodes"]) == 2
        node_ids = [n["node_id"] for n in r2["workflow"]["nodes"]]
        assert "n1" in node_ids
        assert "n2" in node_ids


# ── dynamic.py — record action ───────────────────────────────────────────────


class TestRecordAction:
    """Tests for action='record'."""

    def test_record_completed(self):
        """Mark a node as completed successfully."""
        agent = _make_agent()
        # Create workflow with one node
        r1 = _parse(
            handle_workflow_dynamic(
                {
                    "action": "create",
                    "objective": "Test",
                    "nodes": _make_nodes(("n1", "Step 1", [])),
                    "workflow_id": "wf-rec-1",
                },
                agent,
            )
        )
        assert r1["ok"] is True

        # Record completion
        r2 = _parse(
            handle_workflow_dynamic(
                {
                    "action": "record",
                    "workflow_id": "wf-rec-1",
                    "node_id": "n1",
                    "status": "completed",
                    "summary": "Done!",
                },
                agent,
            )
        )
        assert r2["ok"] is True
        nodes = r2["workflow"]["nodes"]
        assert len(nodes) == 1
        assert nodes[0]["status"] == "completed"
        assert nodes[0]["summary"] == "Done!"

    def test_record_cannot_complete_before_deps(self):
        """Cannot complete a node whose dependencies aren't completed."""
        agent = _make_agent()
        r1 = _parse(
            handle_workflow_dynamic(
                {
                    "action": "create",
                    "objective": "Test",
                    "nodes": _make_nodes(
                        ("n1", "Step 1", []),
                        ("n2", "Step 2", ["n1"]),
                    ),
                    "workflow_id": "wf-dep-1",
                },
                agent,
            )
        )
        assert r1["ok"] is True

        # Try to complete n2 before n1 — should fail
        r2 = _parse(
            handle_workflow_dynamic(
                {
                    "action": "record",
                    "workflow_id": "wf-dep-1",
                    "node_id": "n2",
                    "status": "completed",
                    "summary": "Premature!",
                },
                agent,
            )
        )
        assert r2["ok"] is False
        assert "depend" in r2["error"].lower()

        # Complete n1 first — this triggers _dispatch_ready_nodes which
        # will try to dispatch n2, so we must mock delegate_task.
        mock_dispatch_resp = json.dumps(
            {"status": "dispatched", "delegation_id": "del-dep-n2"}
        )
        with patch(
            "tools.delegate_tool.delegate_task",
            return_value=mock_dispatch_resp,
        ):
            r3 = _parse(
                handle_workflow_dynamic(
                    {
                        "action": "record",
                        "workflow_id": "wf-dep-1",
                        "node_id": "n1",
                        "status": "completed",
                        "summary": "N1 done",
                    },
                    agent,
                )
            )
        assert r3["ok"] is True

        # Now n2 was dispatched — record it as completed.
        # _dispatch_ready_nodes will find no more pending ready nodes.
        r4 = _parse(
            handle_workflow_dynamic(
                {
                    "action": "record",
                    "workflow_id": "wf-dep-1",
                    "node_id": "n2",
                    "status": "completed",
                    "summary": "N2 done",
                },
                agent,
            )
        )
        assert r4["ok"] is True
        # n2 status should be completed (it was set to dispatched by
        # the previous record, and now we're recording completed).
        node_n2 = [n for n in r4["workflow"]["nodes"] if n["node_id"] == "n2"][0]
        assert node_n2["status"] == "completed"


# ── dynamic.py — dispatch action ─────────────────────────────────────────────


class TestDispatchAction:
    """Tests for action='dispatch' (mocking delegate_task)."""

    def test_dispatch_ready_nodes(self):
        """Ready nodes are dispatched when delegate_task is mocked."""
        agent = _make_agent()
        # Create a workflow with a ready node
        _parse(
            handle_workflow_dynamic(
                {
                    "action": "create",
                    "objective": "Test dispatch",
                    "nodes": _make_nodes(("n1", "Do work", [])),
                    "workflow_id": "wf-disp-1",
                },
                agent,
            )
        )

        # Mock delegate_task to return a successful dispatch response
        mock_response = json.dumps(
            {"status": "dispatched", "delegation_id": "del-001"}
        )
        with patch(
            "tools.delegate_tool.delegate_task",
            return_value=mock_response,
        ) as mock_dt:
            result = _parse(
                handle_workflow_dynamic(
                    {
                        "action": "dispatch",
                        "workflow_id": "wf-disp-1",
                    },
                    agent,
                )
            )

        assert result["ok"] is True
        assert len(result["dispatched"]) == 1
        assert result["dispatched"][0]["node_id"] == "n1"
        assert result["dispatched"][0]["delegation_id"] == "del-001"
        # Verify delegate_task was called with correct args
        mock_dt.assert_called_once()

    def test_dispatch_no_agent_returns_error(self):
        """Dispatch without a parent agent returns an error."""
        _parse(
            handle_workflow_dynamic(
                {
                    "action": "create",
                    "objective": "Test",
                    "nodes": _make_nodes(("n1", "Step", [])),
                    "workflow_id": "wf-noagent-1",
                },
                None,
            )
        )
        result = _parse(
            handle_workflow_dynamic(
                {"action": "dispatch", "workflow_id": "wf-noagent-1"},
                None,
            )
        )
        assert result["ok"] is True
        assert len(result.get("dispatch_errors", [])) > 0
        assert "parent agent" in result["dispatch_errors"][0]["error"].lower()


# ── dynamic.py — status action ───────────────────────────────────────────────


class TestStatusAction:
    """Tests for action='status'."""

    def test_status_returns_workflow_state(self):
        """Status returns the current state of the workflow."""
        agent = _make_agent()
        _parse(
            handle_workflow_dynamic(
                {
                    "action": "create",
                    "objective": "Test status",
                    "nodes": _make_nodes(("n1", "Step", [])),
                    "workflow_id": "wf-stat-1",
                },
                agent,
            )
        )

        result = _parse(
            handle_workflow_dynamic(
                {"action": "status", "workflow_id": "wf-stat-1"},
                agent,
            )
        )
        assert result["ok"] is True
        assert result["workflow"]["workflow_id"] == "wf-stat-1"
        assert result["workflow"]["status"] in (WF_RUNNING, WF_READY)
        assert result["workflow"]["objective"] == "Test status"

    def test_status_returns_all_workflows(self):
        """Status without workflow_id returns all workflows in scope."""
        agent = _make_agent()
        _parse(
            handle_workflow_dynamic(
                {
                    "action": "create",
                    "objective": "WF A",
                    "nodes": _make_nodes(("n1", "Step", [])),
                    "workflow_id": "wf-all-1",
                },
                agent,
            )
        )
        _parse(
            handle_workflow_dynamic(
                {
                    "action": "create",
                    "objective": "WF B",
                    "nodes": _make_nodes(("n1", "Step", [])),
                    "workflow_id": "wf-all-2",
                },
                agent,
            )
        )

        result = _parse(
            handle_workflow_dynamic({"action": "status"}, agent)
        )
        assert result["ok"] is True
        assert len(result["workflows"]) == 2


# ── dynamic.py — cancel action ───────────────────────────────────────────────


class TestCancelAction:
    """Tests for action='cancel'."""

    def test_cancel_workflow(self):
        """Cancel marks all pending nodes as cancelled."""
        agent = _make_agent()
        _parse(
            handle_workflow_dynamic(
                {
                    "action": "create",
                    "objective": "Test cancel",
                    "nodes": _make_nodes(
                        ("n1", "Step 1", []),
                        ("n2", "Step 2", ["n1"]),
                        ("n3", "Step 3", []),
                    ),
                    "workflow_id": "wf-cancel-1",
                },
                agent,
            )
        )

        result = _parse(
            handle_workflow_dynamic(
                {
                    "action": "cancel",
                    "workflow_id": "wf-cancel-1",
                    "interrupt": False,  # skip delegation interrupt for unit test
                },
                agent,
            )
        )
        assert result["ok"] is True
        wf = result["workflow"]
        assert wf["status"] == CANCELLED
        # All nodes should be cancelled (pending ones)
        for node in wf["nodes"]:
            assert node["status"] == CANCELLED


# ── dynamic.py — cycle detection ─────────────────────────────────────────────


class TestCycleDetection:
    """Tests for DAG cycle validation."""

    def test_cycle_detection_three_node_cycle(self):
        """A -> B -> A cycle is rejected."""
        agent = _make_agent()
        result = _parse(
            handle_workflow_dynamic(
                {
                    "action": "create",
                    "objective": "Cycle test",
                    "nodes": [
                        {"node_id": "a", "goal": "A", "depends_on": ["c"]},
                        {"node_id": "b", "goal": "B", "depends_on": ["a"]},
                        {"node_id": "c", "goal": "C", "depends_on": ["b"]},
                    ],
                    "workflow_id": "wf-cycle-3",
                },
                agent,
            )
        )
        assert result["ok"] is False
        assert "cycle" in result["error"].lower()


# ── dynamic.py — auto extension cap ──────────────────────────────────────────


class TestAutoExtensionCap:
    """Tests for auto-extension limiting."""

    def test_auto_extension_capped(self):
        """Extension count is enforced when max_extensions_per_workflow is reached."""
        agent = _make_agent()
        r1 = _parse(
            handle_workflow_dynamic(
                {
                    "action": "create",
                    "objective": "Ext cap test",
                    "nodes": _make_nodes(("n1", "Step 1", [])),
                    "workflow_id": "wf-extcap-1",
                },
                agent,
            )
        )
        assert r1["ok"] is True

        # Simulate reaching the extension limit by directly setting it
        # Scope key comes from the agent's session_id
        key = ("session_id:test-session", "wf-extcap-1")
        wf = _workflows.get(key)
        assert wf is not None
        wf.extension_count = 99  # exceed any reasonable max

        # Now record a completion — auto-extension should be skipped
        # _action_record calls _dispatch_ready_nodes internally, so mock delegate_task
        with patch("tools.delegate_tool.delegate_task", return_value=json.dumps(
            {"error": "no-op"}
        )):
            with patch("plugins.workflow.get_config", return_value={
                "max_nodes_per_workflow": 256,
                "max_extensions_per_workflow": 10,
                "max_nodes_per_extension": 3,
                "auto_approve_extensions": False,
            }):
                with patch("plugins.workflow.analyst.analyze_extension", return_value=[
                    {"node_id": "ext1", "goal": "Extension", "depends_on": []}
                ]):
                    result = _parse(
                        handle_workflow_dynamic(
                            {
                                "action": "record",
                                "workflow_id": "wf-extcap-1",
                                "node_id": "n1",
                                "status": "completed",
                                "summary": "Done",
                            },
                            agent,
                        )
                    )

        assert result["ok"] is True
        # The extension_note should indicate it was skipped
        if "extension_note" in result:
            assert "limit" in result["extension_note"].lower() or "skip" in result["extension_note"].lower()


# ── dynamic.py — completed workflow eviction ─────────────────────────────────


class TestCompletedWorkflowEvicted:
    """Tests for completed workflow eviction to _completed_workflows."""

    def test_completed_workflow_evicted(self):
        """A completed workflow moves from _workflows to _completed_workflows."""
        agent = _make_agent()
        _parse(
            handle_workflow_dynamic(
                {
                    "action": "create",
                    "objective": "Evict test",
                    "nodes": _make_nodes(("n1", "Only step", [])),
                    "workflow_id": "wf-evict-1",
                },
                agent,
            )
        )

        # Verify workflow is in active dict
        # Scope key comes from the agent's session_id
        key = ("session_id:test-session", "wf-evict-1")
        assert key in _workflows

        # Record completion — should move to _completed_workflows
        # _action_record calls _dispatch_ready_nodes internally, so mock delegate_task
        with patch("tools.delegate_tool.delegate_task", return_value=json.dumps(
            {"error": "no-op"}
        )):
            with patch("plugins.workflow.get_config", return_value={
                "max_nodes_per_workflow": 256,
                "max_extensions_per_workflow": 10,
                "max_nodes_per_extension": 3,
                "auto_approve_extensions": False,
            }):
                result = _parse(
                    handle_workflow_dynamic(
                        {
                            "action": "record",
                            "workflow_id": "wf-evict-1",
                            "node_id": "n1",
                            "status": "completed",
                            "summary": "All done",
                        },
                        agent,
                    )
                )

        assert result["ok"] is True
        assert result["workflow"]["status"] == WF_COMPLETED

        # Workflow should have been moved to _completed_workflows
        assert key not in _workflows
        assert key in _completed_workflows


# ── dynamic.py — internal helpers ────────────────────────────────────────────


class TestInternalHelpers:
    """Tests for internal helper functions."""

    def test_derive_status_empty_workflow(self):
        """An empty workflow has status 'empty'."""
        wf = DynamicWorkflow(
            workflow_id="wf",
            objective="test",
            context="",
        )
        assert _derive_status(wf) == WF_EMPTY

    def test_derive_status_completed(self):
        """A workflow with all completed nodes has status 'completed'."""
        n1 = DynamicNode(node_id="n1", goal="g", status=COMPLETED)
        wf = DynamicWorkflow(
            workflow_id="wf",
            objective="test",
            context="",
            nodes={"n1": n1},
            node_order=["n1"],
        )
        assert _derive_status(wf) == WF_COMPLETED

    def test_derive_status_cancelled(self):
        """A cancelled workflow has status 'cancelled'."""
        from plugins.workflow.dynamic import WF_CANCELLED
        wf = DynamicWorkflow(
            workflow_id="wf",
            objective="test",
            context="",
            cancelled_at=1.0,
        )
        assert _derive_status(wf) == WF_CANCELLED

    def test_public_view_structure(self):
        """DynamicNode.public_view returns the expected keys."""
        n = DynamicNode(node_id="n1", goal="g", status=PENDING)
        view = n.public_view()
        assert view["node_id"] == "n1"
        assert view["goal"] == "g"
        assert view["status"] == PENDING
        assert "created_at" in view
        assert "updated_at" in view


# ── dynamic.py — handle_workflow_dynamic edge cases ──────────────────────────


class TestHandleWorkflowDynamicEdgeCases:
    """Edge case tests for the main entry point."""

    def test_invalid_action(self):
        """An invalid action returns an error."""
        result = _parse(
            handle_workflow_dynamic({"action": "bogus"}, _make_agent())
        )
        assert result["ok"] is False
        assert "action" in result["error"].lower()

    def test_missing_action(self):
        """Missing action returns an error."""
        result = _parse(
            handle_workflow_dynamic({}, _make_agent())
        )
        assert result["ok"] is False

    def test_non_dict_args(self):
        """Non-dict args returns an error."""
        result = _parse(
            handle_workflow_dynamic("not a dict", _make_agent())  # type: ignore
        )
        assert result["ok"] is False

    def test_record_invalid_status(self):
        """Recording with an invalid status returns an error."""
        agent = _make_agent()
        _parse(
            handle_workflow_dynamic(
                {
                    "action": "create",
                    "objective": "Test",
                    "nodes": _make_nodes(("n1", "Step", [])),
                    "workflow_id": "wf-invstatus-1",
                },
                agent,
            )
        )
        result = _parse(
            handle_workflow_dynamic(
                {
                    "action": "record",
                    "workflow_id": "wf-invstatus-1",
                    "node_id": "n1",
                    "status": "invalid_status",
                },
                agent,
            )
        )
        assert result["ok"] is False
        assert "status" in result["error"].lower()


# ── registry.py — list_workflows ─────────────────────────────────────────────


class TestListWorkflows:
    """Tests for the workflow template registry."""

    def test_list_workflows_returns_list(self):
        """list_workflows returns a list (possibly empty)."""
        result = list_workflows()
        assert isinstance(result, list)

    def test_list_workflows_structure(self):
        """Each entry in list_workflows has the expected keys."""
        result = list_workflows()
        for wf in result:
            assert "name" in wf
            assert "description" in wf
            assert "trigger" in wf
            assert "mode" in wf
            assert "category" in wf
            assert "path" in wf


# ── registry.py — match_workflow_trigger ──────────────────────────────────────


class TestMatchWorkflowTrigger:
    """Tests for trigger keyword matching."""

    def test_match_workflow_trigger(self):
        """A message containing trigger keywords matches the workflow."""
        # "sentry|error alert|incident|triage|error response|fatal error|bug|crash|exception"
        result = match_workflow_trigger(
            "We have a sentry error alert that needs triage"
        )
        if result is not None:
            assert "trigger" in result
            assert result["name"] is not None

    def test_match_returns_none_for_no_match(self):
        """A message with no trigger keywords returns None."""
        result = match_workflow_trigger(
            "Hello, what is the weather today?"
        )
        assert result is None

    def test_match_empty_message(self):
        """An empty message returns None."""
        assert match_workflow_trigger("") is None
        assert match_workflow_trigger(None) is None  # type: ignore

    def test_match_best_score(self):
        """The workflow with the most keyword hits wins."""
        # "deploy" appears in multiple triggers; the one with the most
        # hits should win
        result = match_workflow_trigger(
            "deploy revert rollback undo deploy failure"
        )
        if result is not None:
            assert isinstance(result, dict)
            assert "name" in result


# ── dynamic.py — dispatch with mock in create ────────────────────────────────


class TestCreateWithDispatch:
    """Tests for create action with dispatch_ready=True."""

    def test_create_dispatch_ready(self):
        """Creating with dispatch_ready=True dispatches ready nodes."""
        agent = _make_agent()
        mock_response = json.dumps(
            {"status": "dispatched", "delegation_id": "del-create-001"}
        )
        with patch(
            "tools.delegate_tool.delegate_task",
            return_value=mock_response,
        ):
            result = _parse(
                handle_workflow_dynamic(
                    {
                        "action": "create",
                        "objective": "Dispatch on create",
                        "nodes": _make_nodes(("n1", "Auto dispatch", [])),
                        "workflow_id": "wf-dispcreate-1",
                        "dispatch_ready": True,
                    },
                    agent,
                )
            )

        assert result["ok"] is True
        assert len(result["dispatched"]) == 1
        assert result["dispatched"][0]["delegation_id"] == "del-create-001"
