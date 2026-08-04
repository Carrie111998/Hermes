"""Tests for template synthesis — dynamic DAG → static pipeline YAML.

Run: python3 -m pytest tests/test_workflow_synthesize.py -v
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from plugins.workflow.synthesize import (
    build_static_yaml,
    synthesize_template,
    _slugify,
    _sanitize_task,
    _generalize_feedback_goal,
)


def _node(nid, goal, deps=None, pattern=None, review_target=None, max_review_retries=None):
    d = {
        "node_id": nid,
        "goal": goal,
        "depends_on": deps or [],
        "pattern": pattern,
        "review_target": review_target,
        "max_review_retries": max_review_retries,
    }
    return d


# ════════════════════════════════════════════════════════════════
# Unit: YAML builder
# ════════════════════════════════════════════════════════════════

def test_build_sequential_yaml():
    """A plain sequential DAG becomes a static pipeline with roles."""
    yaml = build_static_yaml(
        "wf-abc-1",
        "research the market",
        [
            _node("discover", "Research the market for opportunities"),
            _node("synthesize", "Merge findings about the market into a report",
                  deps=["discover"]),
        ],
    )
    assert "name: wf-abc-1" in yaml
    # Objective literal must not leak into node tasks (header docs are fine)
    assert "Research the market for opportunities" in yaml  # goal preserved
    assert 'agent: "{worker-1}"' in yaml
    assert 'agent: "{worker-2}"' in yaml
    assert "roles:" in yaml
    assert "worker-1: <agent-name>  # TODO: map to a real profile" in yaml
    assert "depends_on: [discover]" in yaml
    # No reviews section for plain nodes
    assert "reviews:" not in yaml


def test_build_review_loop_yaml():
    """A review-loop dynamic DAG becomes producer.reviews: + reviewer node."""
    yaml = build_static_yaml(
        "wf-review-9",
        "ship a feature",
        [
            _node("build", "Implement the feature", pattern=None),
            _node("qa", "Review the build", deps=["build"],
                  pattern="review-loop", review_target="build"),
        ],
        max_review_retries=2,
    )
    # Producer carries the reviews: list
    assert "reviews: [qa]" in yaml
    # Reviewer declared without depends_on
    assert "  qa:" in yaml
    assert "agent: \"{reviewer-1}\"" in yaml
    assert "do not add depends_on" in yaml
    # Workflow-level retry budget preserved
    assert "max_retries: 2" in yaml
    # Reviewer must NOT appear in any depends_on
    assert "depends_on: [qa]" not in yaml


def test_objective_generalized_to_context():
    """The original objective is replaced with {context.objective} in tasks."""
    yaml = build_static_yaml(
        "wf-gen-1",
        "make viral videos",
        [_node("plan", "Plan a strategy to make viral videos and win")],
    )
    assert "{context.objective}" in yaml
    assert "make viral videos" not in yaml


def test_rework_feedback_stripped_from_goal():
    """Per-run review feedback blocks are removed from synthesized tasks."""
    goal = ("Build the thing.\n\n"
            "[Review feedback — rework round 2]:\nFAIL: missing tests")
    cleaned = _generalize_feedback_goal(goal)
    assert cleaned == "Build the thing."
    assert "Review feedback" not in cleaned


def test_slugify():
    assert _slugify("My Viral Video Pipeline!") == "my-viral-video-pipeline"
    assert _slugify("") == "node"
    assert _slugify("wf_abc-1.X") == "wf_abc-1.x"


# ════════════════════════════════════════════════════════════════
# Integration: synthesize_template
# ════════════════════════════════════════════════════════════════

def _make_completed_dynamic_workflow(workflow_id, objective, nodes):
    """Create + fully complete a dynamic workflow so it's findable."""
    from plugins.workflow.dynamic import (
        handle_workflow_dynamic,
        _reset_for_tests,
    )

    _reset_for_tests()
    agent = MagicMock()
    agent.session_id = "test-session"
    r1 = handle_workflow_dynamic({
        "action": "create",
        "objective": objective,
        "workflow_id": workflow_id,
        "nodes": nodes,
    }, agent)
    assert json.loads(r1)["ok"], r1
    with patch("tools.delegate_tool.delegate_task", return_value=json.dumps(
        {"status": "dispatched", "delegation_id": "dlg-synth"}
    )):
        with patch("plugins.workflow.analyst.analyze_extension", return_value=[]):
            for nid in [n["node_id"] for n in nodes]:
                if nid == "qa":
                    continue  # reviewer completes after producer
                r = handle_workflow_dynamic({
                    "action": "record", "workflow_id": workflow_id,
                    "node_id": nid, "status": "completed",
                    "summary": f"done {nid}",
                }, agent)
                assert json.loads(r)["ok"], r


def test_synthesize_writes_startable_template(tmp_path, monkeypatch):
    """End-to-end: complete dynamic run → YAML on disk → static engine loads it."""
    from plugins.workflow.dynamic import handle_workflow_dynamic

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _make_completed_dynamic_workflow(
        "wf-synth-1",
        "build a widget",
        [
            {"node_id": "design", "goal": "Design the widget", "depends_on": []},
            {"node_id": "build", "goal": "Build the widget per the design",
             "depends_on": ["design"]},
        ],
    )

    result = synthesize_template("wf-synth-1", name="widget-pipeline")
    assert result["ok"], result
    assert result["node_count"] == 2
    assert result["reviewer_count"] == 0
    out = Path(result["path"])
    assert out.exists()
    assert out.name == "widget-pipeline.yaml"

    # The synthesized template must load through the STATIC engine.
    from plugins.workflow.engine import WorkflowEngine
    engine = WorkflowEngine(workflows_dir=str(out.parent))
    wf = engine.load_workflow("widget-pipeline")
    layers = engine.topological_sort(wf)
    assert [n.id for n in wf.nodes.values()]  # nodes parse
    assert len(layers) >= 2


def test_synthesize_review_loop_round_trip(tmp_path, monkeypatch):
    """A review-loop dynamic DAG synthesizes to a static YAML the engine accepts."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _make_completed_dynamic_workflow(
        "wf-synth-review",
        "ship quality",
        [
            {"node_id": "build", "goal": "Implement the feature", "depends_on": []},
            {"node_id": "qa", "goal": "Review the feature", "depends_on": ["build"],
             "pattern": "review-loop", "review_target": "build"},
        ],
    )
    result = synthesize_template("wf-synth-review", name="quality-pipeline")
    assert result["ok"], result
    assert result["reviewer_count"] == 1

    from plugins.workflow.engine import WorkflowEngine
    engine = WorkflowEngine(workflows_dir=str(Path(result["path"]).parent))
    wf = engine.load_workflow("quality-pipeline")
    build_node = wf.nodes["build"]
    assert build_node.reviews == ["qa"]
    assert "qa" in wf.nodes


def test_synthesize_unknown_workflow():
    result = synthesize_template("wf-does-not-exist")
    assert not result["ok"]
    assert "unknown workflow_id" in result["error"]


def test_synthesize_overwrite_protection(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _make_completed_dynamic_workflow(
        "wf-synth-2",
        "test objective",
        [{"node_id": "a", "goal": "do the thing", "depends_on": []}],
    )
    r1 = synthesize_template("wf-synth-2", name="same-name")
    assert r1["ok"]
    # Second save without overwrite → refused
    r2 = synthesize_template("wf-synth-2", name="same-name")
    assert not r2["ok"]
    assert "already exists" in r2["error"]
    # With overwrite → succeeds
    r3 = synthesize_template("wf-synth-2", name="same-name", overwrite=True)
    assert r3["ok"]


def test_synthesize_role_map(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _make_completed_dynamic_workflow(
        "wf-synth-3",
        "map roles",
        [{"node_id": "n1", "goal": "work", "depends_on": []}],
    )
    result = synthesize_template(
        "wf-synth-3", name="role-mapped", role_map={"worker-1": "newton"}
    )
    assert result["ok"], result
    text = Path(result["path"]).read_text()
    assert "worker-1: newton" in text
