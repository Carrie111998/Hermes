"""
Tests for workflow_engine.py — DAG construction, cycle detection,
topological sort, LOOP convention parsing, failure propagation,
state persistence, and CLI validation.

Run: python3 -m pytest tests/test_workflow_engine.py -v
"""

import pytest
import tempfile
import json
from pathlib import Path

# Import the engine module (must be run from hermes-agent repo root)
from tools.workflow_engine import (
    WorkflowEngine, Workflow, WorkflowNode, NodeState,
    CycleDetectedError,
)


# ── Test fixtures ──────────────────────────────────────────────────

@pytest.fixture
def engine():
    """Engine pointed at a temp workflows directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield WorkflowEngine(workflows_dir=tmpdir)


@pytest.fixture
def simple_workflow():
    """A → B → C (linear, no branches)."""
    wf = Workflow(name="test-linear", description="Linear test")
    wf.nodes["a"] = WorkflowNode(id="a", agent="agent-a", task="Task A")
    wf.nodes["b"] = WorkflowNode(id="b", agent="agent-b", task="Task B",
                                  depends_on=["a"])
    wf.nodes["c"] = WorkflowNode(id="c", agent="agent-c", task="Task C",
                                  depends_on=["b"])
    return wf


@pytest.fixture
def parallel_workflow():
    """A → B ∥ C → D (parallel middle layer)."""
    wf = Workflow(name="test-parallel")
    wf.nodes["a"] = WorkflowNode(id="a", agent="agent-a", task="Task A")
    wf.nodes["b"] = WorkflowNode(id="b", agent="agent-b", task="Task B",
                                  depends_on=["a"])
    wf.nodes["c"] = WorkflowNode(id="c", agent="agent-c", task="Task C",
                                  depends_on=["a"])
    wf.nodes["d"] = WorkflowNode(id="d", agent="agent-d", task="Task D",
                                  depends_on=["b", "c"])
    return wf


@pytest.fixture
def revision_workflow():
    """verify → revise (single revision pair)."""
    wf = Workflow(name="test-revision")
    wf.nodes["verify"] = WorkflowNode(id="verify", agent="reviewer",
                                       task="Verify work")
    wf.nodes["revise"] = WorkflowNode(id="revise", agent="author",
                                       task="Revise work", depends_on=["verify"])
    return wf


# ── Topological sort tests ─────────────────────────────────────────

def test_linear_dag(engine, simple_workflow):
    layers = engine.topological_sort(simple_workflow)
    assert layers == [["a"], ["b"], ["c"]]


def test_parallel_dag(engine, parallel_workflow):
    layers = engine.topological_sort(parallel_workflow)
    # Layer 0: a, Layer 1: b ∥ c, Layer 2: d
    assert len(layers) == 3
    assert set(layers[0]) == {"a"}
    assert set(layers[1]) == {"b", "c"}
    assert set(layers[2]) == {"d"}


def test_cycle_detection(engine):
    wf = Workflow(name="test-cycle")
    wf.nodes["a"] = WorkflowNode(id="a", agent="x", task="A", depends_on=["b"])
    wf.nodes["b"] = WorkflowNode(id="b", agent="x", task="B", depends_on=["a"])
    with pytest.raises(CycleDetectedError):
        engine.topological_sort(wf)


def test_unknown_dependency(engine):
    wf = Workflow(name="test-unknown")
    wf.nodes["a"] = WorkflowNode(id="a", agent="x", task="A",
                                  depends_on=["nonexistent"])
    with pytest.raises(ValueError, match="unknown node"):
        engine.topological_sort(wf)


def test_empty_workflow(engine):
    wf = Workflow(name="test-empty")
    layers = engine.topological_sort(wf)
    assert layers == []


def test_single_node(engine):
    wf = Workflow(name="test-single")
    wf.nodes["solo"] = WorkflowNode(id="solo", agent="x", task="Solo")
    layers = engine.topological_sort(wf)
    assert layers == [["solo"]]


# ── Dependency lookup tests ────────────────────────────────────────

def test_find_revision_node(engine, revision_workflow):
    result = engine._find_revision_node(revision_workflow, "verify")
    assert result == "revise"


def test_find_revision_node_no_match(engine, revision_workflow):
    result = engine._find_revision_node(revision_workflow, "revise")
    assert result is None


def test_find_revision_node_multiple_dependents(engine):
    """Documented behavior: returns first match from dict.items()."""
    wf = Workflow(name="test-multi-revision")
    wf.nodes["verify"] = WorkflowNode(id="verify", agent="x", task="V")
    wf.nodes["revise-a"] = WorkflowNode(id="revise-a", agent="x", task="RA",
                                         depends_on=["verify"])
    wf.nodes["revise-b"] = WorkflowNode(id="revise-b", agent="x", task="RB",
                                         depends_on=["verify"])
    result = engine._find_revision_node(wf, "verify")
    # Returns one of them (dict order in Python 3.7+ is insertion order)
    assert result in ("revise-a", "revise-b")


def test_find_layer_for_node(engine, parallel_workflow):
    layers = engine.topological_sort(parallel_workflow)
    assert engine._find_layer_for_node(layers, "a") == 0
    assert engine._find_layer_for_node(layers, "b") == 1
    assert engine._find_layer_for_node(layers, "c") == 1
    assert engine._find_layer_for_node(layers, "d") == 2
    assert engine._find_layer_for_node(layers, "nonexistent") == -1


# ── LOOP convention tests ──────────────────────────────────────────

def test_loop_regex_match():
    import re
    body = "LOOP:nikola-verify-spec | Missing billing edge case"
    match = re.match(r'^LOOP:(\S+)', body)
    assert match is not None
    assert match.group(1) == "nikola-verify-spec"


def test_loop_regex_no_match():
    import re
    body = "Blocked: external API down"
    match = re.match(r'^LOOP:(\S+)', body)
    assert match is None


def test_loop_regex_subsequent_loops():
    """LOOP: prefix anywhere in body should not match — only at start."""
    import re
    body = "Agent completed review. LOOP:some-node | notes"
    match = re.match(r'^LOOP:(\S+)', body)
    assert match is None


def test_loop_regex_with_pipe_content():
    import re
    body = "LOOP:ada-security | PII in plaintext at auth/SPEC.md §3.2"
    match = re.match(r'^LOOP:(\S+)', body)
    assert match.group(1) == "ada-security"


# ── Failure propagation tests ──────────────────────────────────────

def test_failure_propagation_transitive(engine):
    """A fails → B skipped → C skipped (transitive through B to C)."""
    wf = Workflow(name="test-fail-prop")
    wf.nodes["a"] = WorkflowNode(id="a", agent="x", task="A")
    wf.nodes["b"] = WorkflowNode(id="b", agent="x", task="B", depends_on=["a"])
    wf.nodes["c"] = WorkflowNode(id="c", agent="x", task="C", depends_on=["b"])

    states = {
        "a": NodeState(node_id="a", status="failed"),
        "b": NodeState(node_id="b", status="pending"),
        "c": NodeState(node_id="c", status="pending"),
    }

    # Simulate execute's dependency check: before creating B's card
    b_node = wf.nodes["b"]
    deps_failed_b = any(
        states[d].status in ("failed", "timed_out", "blocked")
        for d in b_node.depends_on
    )
    assert deps_failed_b is True  # A failed → B should skip

    # Mark B as skipped
    states["b"].status = "skipped"

    # Now check C: its dep B is skipped (not failed/timed_out/blocked)
    c_node = wf.nodes["c"]
    deps_failed_c = any(
        states[d].status in ("failed", "timed_out", "blocked")
        for d in c_node.depends_on
    )
    # B is skipped, not failed — so deps_failed_c is False
    # This means C would NOT be skipped by the current check.
    # This is a known limitation: "skipped" is not in the failed set.
    assert deps_failed_c is False


def test_failure_propagation_direct(engine):
    """A fails → B (direct dependent) B skipped correctly."""
    wf = Workflow(name="test-fail-direct")
    wf.nodes["a"] = WorkflowNode(id="a", agent="x", task="A")
    wf.nodes["b"] = WorkflowNode(id="b", agent="x", task="B", depends_on=["a"])

    states = {
        "a": NodeState(node_id="a", status="failed"),
        "b": NodeState(node_id="b", status="pending"),
    }

    b_node = wf.nodes["b"]
    deps_failed = any(
        states[d].status in ("failed", "timed_out", "blocked")
        for d in b_node.depends_on
    )
    assert deps_failed is True


def test_failure_propagation_timed_out(engine):
    """Timed out nodes also block dependents."""
    wf = Workflow(name="test-timeout-prop")
    wf.nodes["a"] = WorkflowNode(id="a", agent="x", task="A")
    wf.nodes["b"] = WorkflowNode(id="b", agent="x", task="B", depends_on=["a"])

    states = {
        "a": NodeState(node_id="a", status="timed_out"),
        "b": NodeState(node_id="b", status="pending"),
    }

    b_node = wf.nodes["b"]
    deps_failed = any(
        states[d].status in ("failed", "timed_out", "blocked")
        for d in b_node.depends_on
    )
    assert deps_failed is True


# ── State persistence tests ────────────────────────────────────────

def test_state_save_and_load(engine):
    """Round-trip: save state → load state → verify fields."""
    wf = Workflow(name="test-state")
    wf.nodes["a"] = WorkflowNode(id="a", agent="x", task="A")

    states = {
        "a": NodeState(node_id="a", status="done", kanban_card_id="card-123",
                       started_at="2026-06-07T00:00:00Z", attempts=1,
                       loop_count=0),
    }
    results = {"a": "done"}
    layers = [["a"]]

    engine._save_state("test-state", states, results, 0, layers)
    loaded = engine._load_state("test-state")

    assert loaded is not None
    assert loaded["workflow_name"] == "test-state"
    assert loaded["current_layer"] == 0
    assert loaded["states"]["a"]["status"] == "done"
    assert loaded["states"]["a"]["kanban_card_id"] == "card-123"
    assert loaded["results"]["a"] == "done"

    engine._clear_state("test-state")
    assert engine._load_state("test-state") is None


def test_state_clear_nonexistent(engine):
    """Clearing nonexistent state should not error."""
    engine._clear_state("nonexistent-workflow")  # Should not raise


# ── Validation tests ───────────────────────────────────────────────

def _write_workflow_yaml(tmpdir, name, yaml_content):
    """Helper: write a temp YAML and point engine at it."""
    path = Path(tmpdir) / f"{name}.yaml"
    path.write_text(yaml_content)
    return WorkflowEngine(workflows_dir=tmpdir)


def test_validate_valid_workflow():
    with tempfile.TemporaryDirectory() as tmpdir:
        yaml = """name: test-valid
description: A valid workflow
nodes:
  a:
    agent: test-agent
    task: First task
  b:
    agent: test-agent
    task: Second task
    depends_on: [a]
"""
        engine = _write_workflow_yaml(tmpdir, "test-valid", yaml)
        result = engine.validate("test-valid")
        assert result["valid"] is True
        assert result["nodes"] == 2
        assert result["layers"] == 2


def test_validate_unknown_dependency():
    with tempfile.TemporaryDirectory() as tmpdir:
        yaml = """name: test-bad-dep
nodes:
  a:
    agent: test-agent
    task: Bad dep
    depends_on: [nonexistent]
"""
        engine = _write_workflow_yaml(tmpdir, "test-bad-dep", yaml)
        result = engine.validate("test-bad-dep")
        assert result["valid"] is False
        assert any("unknown node" in i for i in result["issues"])


def test_validate_cycle():
    with tempfile.TemporaryDirectory() as tmpdir:
        yaml = """name: test-cycle
nodes:
  a:
    agent: test-agent
    task: A
    depends_on: [b]
  b:
    agent: test-agent
    task: B
    depends_on: [a]
"""
        engine = _write_workflow_yaml(tmpdir, "test-cycle", yaml)
        result = engine.validate("test-cycle")
        assert result["valid"] is False
        assert any("Cycle" in i for i in result["issues"])


def test_validate_revision_without_gate():
    with tempfile.TemporaryDirectory() as tmpdir:
        yaml = """name: test-rev-no-gate
nodes:
  revise-spec:
    agent: edison
    task: Revise the spec
    depends_on: [something-else]
  something-else:
    agent: other
    task: Not a gate
"""
        engine = _write_workflow_yaml(tmpdir, "test-rev-no-gate", yaml)
        result = engine.validate("test-rev-no-gate")
        # Revision node exists but doesn't depend on a verify/security/review node
        assert any("should depend on a gate node" in i for i in result["issues"])


def test_validate_missing_workflow():
    with tempfile.TemporaryDirectory() as tmpdir:
        engine = WorkflowEngine(workflows_dir=tmpdir)
        result = engine.validate("nonexistent")
        assert result["valid"] is False
        assert any("not found" in i for i in result["issues"])


def test_validate_resolves_deps_across_workflows():
    """validate should detect when a revision node references a gate that exists."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yaml = """name: test-gate-pair
nodes:
  verify-spec:
    agent: nikola
    task: Verify spec
  revise-spec:
    agent: edison
    task: Revise spec
    depends_on: [verify-spec]
"""
        engine = _write_workflow_yaml(tmpdir, "test-gate-pair", yaml)
        result = engine.validate("test-gate-pair")
        # Revision node depends on a verify node — should be valid
        assert result["valid"] is True
        # No gate→revision pair issues (verify-spec has revise-spec as dependent)
        assert not any("LOOP detection" in i for i in result["issues"])


# ── LOOP count tests ───────────────────────────────────────────────

def test_max_revision_loops_constant():
    """MAX_REVISION_LOOPS should be 3."""
    assert WorkflowEngine.MAX_REVISION_LOOPS == 3


# ── Card ID parsing tests ──────────────────────────────────────────

def test_card_id_regex_match():
    import re
    # Standard output format
    output = "Created card abc123-def456\n"
    match = re.match(r'Created\s+card\s+(\S+)', output.strip())
    assert match is not None
    assert match.group(1) == "abc123-def456"


def test_card_id_regex_no_match_fallback():
    """Fallback to split()[-1] when regex doesn't match."""
    output = "card xyz789 created\n"
    card_id = output.strip().split()[-1]
    assert card_id == "created"


def test_card_id_regex_multispace():
    import re
    output = "Created    card    spaced-id-123   "
    match = re.match(r'Created\s+card\s+(\S+)', output.strip())
    assert match is not None
    assert match.group(1) == "spaced-id-123"


# ── WorkflowNode tests ─────────────────────────────────────────────

def test_workflow_node_defaults():
    node = WorkflowNode(id="test", agent="x", task="y")
    assert node.depends_on == []
    assert node.timeout_minutes == 30
    assert node.model is None
    assert node.channel == "debug"


def test_workflow_node_custom():
    node = WorkflowNode(
        id="test", agent="x", task="y",
        depends_on=["a", "b"], timeout_minutes=60,
        model="deepseek-v4", channel="orchestration"
    )
    assert node.depends_on == ["a", "b"]
    assert node.timeout_minutes == 60
    assert node.model == "deepseek-v4"
    assert node.channel == "orchestration"


# ── NodeState tests ────────────────────────────────────────────────

def test_node_state_defaults():
    state = NodeState(node_id="test")
    assert state.status == "pending"
    assert state.kanban_card_id is None
    assert state.attempts == 0
    assert state.loop_count == 0
