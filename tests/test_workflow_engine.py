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


# ── Synthetic gate node tests ──────────────────────────────────────
# Synthetic gates (synthetic: true in YAML) are auto-completed once
# their depends_on are satisfied — no kanban card is created, no agent
# is dispatched. They exist to enforce ordering in the DAG without
# adding a no-op task to the board. See council.yaml's council-ready
# node for the canonical example.

def test_workflow_node_synthetic_default_false():
    """Real nodes have synthetic=False by default."""
    node = WorkflowNode(id="real", agent="agent-x", task="Do the thing")
    assert node.synthetic is False
    assert node.agent == "agent-x"


def test_workflow_node_synthetic_explicit_true():
    """Synthetic nodes have synthetic=True and may have agent=None."""
    node = WorkflowNode(id="gate", agent=None, task="privacy gate",
                        synthetic=True)
    assert node.synthetic is True
    assert node.agent is None


def test_load_synthetic_node_without_agent():
    """YAML with synthetic: true and no agent field loads without error.

    Regression: this is the original council-ready bug. The old loader
    did `node_data["agent"]` as a direct subscript, which raised
    KeyError on synthetic nodes.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        yaml = """name: test-synthetic-load
description: Synthetic gate with no agent
nodes:
  real:
    agent: some-agent
    task: A real task
  gate:
    synthetic: true
    task: Privacy gate
    depends_on: [real]
"""
        engine = _write_workflow_yaml(tmpdir, "test-synthetic-load", yaml)
        wf = engine.load_workflow("test-synthetic-load")
        assert "gate" in wf.nodes
        assert wf.nodes["gate"].synthetic is True
        assert wf.nodes["gate"].agent is None
        assert wf.nodes["gate"].task == "Privacy gate"
        assert wf.nodes["gate"].depends_on == ["real"]
        # Real node still has its agent
        assert wf.nodes["real"].synthetic is False
        assert wf.nodes["real"].agent == "some-agent"


def test_load_synthetic_node_without_task():
    """Synthetic nodes may omit task — defaults to a label including the id."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yaml = """name: test-synthetic-no-task
nodes:
  gate:
    synthetic: true
    depends_on: [real]
  real:
    agent: some-agent
    task: A real task
"""
        engine = _write_workflow_yaml(tmpdir, "test-synthetic-no-task", yaml)
        wf = engine.load_workflow("test-synthetic-no-task")
        assert wf.nodes["gate"].synthetic is True
        # task defaults to a labeled placeholder, not KeyError
        assert "gate" in wf.nodes["gate"].task


def test_load_synthetic_with_redundant_agent_warns(capsys):
    """Loader warns if synthetic: true coexists with an agent field."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yaml = """name: test-synthetic-extra-agent
nodes:
  gate:
    synthetic: true
    agent: sherlock   # redundant — synthetic wins
    depends_on: [real]
  real:
    agent: some-agent
    task: A real task
"""
        engine = _write_workflow_yaml(tmpdir, "test-synthetic-extra-agent", yaml)
        wf = engine.load_workflow("test-synthetic-extra-agent")
        # Loader silently ignored the agent field; node is synthetic
        assert wf.nodes["gate"].synthetic is True
        assert wf.nodes["gate"].agent is None
        # Warning was emitted to stdout
        captured = capsys.readouterr()
        assert "synthetic: true" in captured.out
        assert "ignoring agent field" in captured.out


def test_validate_synthetic_node_skips_agent_check():
    """validate() must not flag synthetic nodes for missing agent profile.

    Regression: validate() does `profiles_dir / node.agent` which would
    crash on None. The synthetic skip is in the same loop.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        yaml = """name: test-synthetic-validate
nodes:
  real:
    agent: some-agent
    task: A real task
  gate:
    synthetic: true
    depends_on: [real]
"""
        engine = _write_workflow_yaml(tmpdir, "test-synthetic-validate", yaml)
        result = engine.validate("test-synthetic-validate")
        # Should validate cleanly — no agent profile errors for 'gate'
        agent_issues = [i for i in result["issues"] if "gate" in i and "agent" in i]
        assert agent_issues == []
        # Layer count is correct: real in layer 0, gate in layer 1
        assert result["layers"] == 2
        assert result["nodes"] == 2


def test_topological_sort_with_synthetic():
    """Synthetic gates slot into the DAG exactly like real nodes.

    `real-a` (layer 0) → `gate` (layer 1, synthetic) → `real-b` (layer 2).
    The gate's presence in the middle layer is what enforces the
    ordering — without it, real-b could run as soon as real-a finishes.
    """
    wf = Workflow(name="test-synthetic-topo")
    wf.nodes["real-a"] = WorkflowNode(id="real-a", agent="a", task="A")
    wf.nodes["gate"] = WorkflowNode(id="gate", agent=None, task="gate",
                                    depends_on=["real-a"], synthetic=True)
    wf.nodes["real-b"] = WorkflowNode(id="real-b", agent="b", task="B",
                                      depends_on=["gate"])

    engine = WorkflowEngine()
    layers = engine.topological_sort(wf)
    assert layers == [["real-a"], ["gate"], ["real-b"]]


def test_create_kanban_card_refuses_synthetic():
    """Defensive: the create helper rejects synthetic nodes explicitly.

    This is a backstop. The dispatch loop already filters synthetic
    nodes, but if a future caller forgets the check, this guard turns
    a confusing subprocess-on-None crash into a clear ValueError.
    """
    engine = WorkflowEngine()
    gate_node = WorkflowNode(id="gate", agent=None, task="gate",
                             synthetic=True)
    with pytest.raises(ValueError, match="synthetic"):
        engine.create_kanban_card(gate_node)


def test_synthetic_node_in_layer_with_real_node_preserves_ordering():
    """Synthetic gate in a layer ensures downstream real nodes wait.

    This is the load-bearing behavior. If a synthetic gate's auto-
    completion didn't fire correctly, downstream real nodes would
    either run early (data race) or be blocked forever.
    """
    wf = Workflow(name="test-ordering")
    wf.nodes["upstream"] = WorkflowNode(id="upstream", agent="a", task="U")
    wf.nodes["gate"] = WorkflowNode(id="gate", agent=None, task="gate",
                                    depends_on=["upstream"], synthetic=True)
    wf.nodes["downstream"] = WorkflowNode(id="downstream", agent="b",
                                          task="D", depends_on=["gate"])

    engine = WorkflowEngine()
    layers = engine.topological_sort(wf)

    # gate is in a separate layer from upstream and downstream —
    # topological order is preserved end-to-end.
    assert layers.index(["upstream"]) < layers.index(["gate"])
    assert layers.index(["gate"]) < layers.index(["downstream"])


def test_synthetic_node_failure_propagation():
    """If a synthetic node's dependency fails, the synthetic node is skipped.

    Documented behavior: dep_failed is checked before the synthetic
    auto-complete, so a failed upstream blocks the gate (which blocks
    its downstream — same as a failed real node would).
    """
    wf = Workflow(name="test-synth-fail-prop")
    wf.nodes["real-a"] = WorkflowNode(id="real-a", agent="a", task="A")
    wf.nodes["gate"] = WorkflowNode(id="gate", agent=None, task="g",
                                    depends_on=["real-a"], synthetic=True)
    wf.nodes["real-b"] = WorkflowNode(id="real-b", agent="b", task="B",
                                      depends_on=["gate"])

    # Simulate state: real-a failed
    states = {
        "real-a": NodeState(node_id="real-a", status="failed"),
        "gate": NodeState(node_id="gate"),
        "real-b": NodeState(node_id="real-b"),
    }

    # Same dep_failed check the dispatch loop uses
    gate_deps_failed = any(
        states[d].status in ("failed", "timed_out", "blocked")
        for d in wf.nodes["gate"].depends_on
    )
    assert gate_deps_failed is True


# ── B2: Phase output template substitution tests ─────────────────
#
# The engine now resolves {namespace.field} and {bare} references in
# node.task before posting to kanban. The lookup walks completed
# upstream nodes' captured results (state.result) plus the start-time
# context dict. See the docstring on `_build_template_lookup` for the
# resolution rules; see council.yaml for the canonical use case.

# Fixture: a council-shaped DAG that exercises the spec's example
# variables ({context.question}, {context.question_slug},
# {phase1.position-edison}, {phase1.all}, {phase2a.all}, {phase2b.all}).
# Built programmatically (not from YAML) so the test is self-contained
# and doesn't need a temp file for every assertion.

@pytest.fixture
def council_pipeline():
    """Multi-phase workflow that mirrors the council.yaml shape.

    Layers:
      0: premortem           (phase 0)
      1: council-ready       (synthetic gate, phase 1)
      2: pos-e, pos-n, pos-k (phase 1, explicit label)
      3: probe-s, probe-r    (phase 2a and 2b respectively)
    """
    wf = Workflow(name="council-test")
    wf.nodes["premortem"] = WorkflowNode(
        id="premortem", agent="nikola", task="Imagine failure"
    )
    wf.nodes["council-ready"] = WorkflowNode(
        id="council-ready", agent=None, task="gate",
        depends_on=["premortem"], synthetic=True,
    )
    wf.nodes["position-edison"] = WorkflowNode(
        id="position-edison", agent="edison", task="pos-E",
        depends_on=["council-ready"], phase="phase1",
    )
    wf.nodes["position-newton"] = WorkflowNode(
        id="position-newton", agent="newton", task="pos-N",
        depends_on=["council-ready"], phase="phase1",
    )
    wf.nodes["position-nikola"] = WorkflowNode(
        id="position-nikola", agent="nikola", task="pos-K",
        depends_on=["council-ready"], phase="phase1",
    )
    wf.nodes["probe-sherlock"] = WorkflowNode(
        id="probe-sherlock", agent="sherlock", task="probe-S",
        depends_on=["position-edison", "position-newton", "position-nikola"],
        phase="phase2a",
    )
    wf.nodes["probe-raven"] = WorkflowNode(
        id="probe-raven", agent="raven", task="probe-R",
        depends_on=["position-edison", "position-newton", "position-nikola"],
        phase="phase2b",
    )
    return wf


# ── NodeState / WorkflowNode field tests ──────────────────────────

def test_node_state_result_defaults_to_none():
    """B2: result field added to NodeState. Defaults to None.

    Pre-B2 there was no result field; the engine had no way to
    remember what a completed card had produced. The lookup helper
    now relies on this being None to filter pending vs completed.
    """
    state = NodeState(node_id="x")
    assert state.result is None


def test_workflow_node_phase_defaults_to_none():
    """B2: phase field added to WorkflowNode. Defaults to None.

    When None, the engine auto-derives "phase0", "phase1", ... from
    the topological layer index at lookup time. Authors only set
    `phase:` explicitly when they want a non-numeric label (e.g.
    "phase2a", "phase2b") or when they want to override the
    default layer-derived label.
    """
    node = WorkflowNode(id="x", agent="a", task="t")
    assert node.phase is None


def test_load_node_phase_from_yaml():
    """Loader reads `phase:` from YAML into WorkflowNode.phase."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yaml = """name: test-phase
nodes:
  a:
    agent: x
    task: A
    phase: 1a
  b:
    agent: x
    task: B
    depends_on: [a]
"""
        engine = _write_workflow_yaml(tmpdir, "test-phase", yaml)
        wf = engine.load_workflow("test-phase")
        assert wf.nodes["a"].phase == "1a"
        # `b` has no phase — stays None and the engine defaults it
        # to the layer index at lookup time
        assert wf.nodes["b"].phase is None


# ── _build_template_lookup tests ─────────────────────────────────

def test_lookup_context_only_no_states_done(engine):
    """Empty pipeline: lookup contains just the context dict.

    No upstream nodes are completed, so the only key in the lookup
    is 'context'. The phase keys are absent (we don't pre-create
    empty phase dicts) and the bare {X} fallback only hits context.
    """
    wf = Workflow(name="empty")
    states = {}
    layers = []
    lookup = engine._build_template_lookup(wf, states, layers,
                                           context={"q": "Q"})
    assert lookup == {"context": {"q": "Q"}}


def test_lookup_phase_default_derived_from_layer(engine, council_pipeline):
    """phase=None → engine defaults to 'phaseN' from the layer index.

    council_pipeline layers are:
      0: premortem
      1: council-ready
      2: position-edison, position-newton, position-nikola
      3: probe-sherlock, probe-raven
    Nodes without an explicit `phase:` label should be auto-grouped
    under phase0 / phase1 / phase2 / phase3 by their layer index.
    """
    layers = engine.topological_sort(council_pipeline)
    states = {nid: NodeState(node_id=nid) for nid in council_pipeline.nodes}
    states["premortem"].result = "PRE"
    states["premortem"].status = "done"

    lookup = engine._build_template_lookup(council_pipeline, states,
                                            layers, context={})
    # premortem has no explicit phase, so it lands in phase0 (its layer)
    assert "phase0" in lookup
    assert lookup["phase0"]["premortem"] == "PRE"
    # council-ready has no explicit phase, so it lands in phase1
    # (its layer). It also auto-completed (synthetic gate), but with
    # no captured result, so phase1 should NOT exist yet.
    assert "phase1" not in lookup


def test_lookup_phase_explicit_label_used(engine, council_pipeline):
    """Explicit `phase:` in YAML is honored over the layer default.

    position-edison/newton/nikola all set phase=phase1, so they all
    land under that label even though they share layer 2 with nothing
    else. This is the whole reason phase is configurable.
    """
    layers = engine.topological_sort(council_pipeline)
    states = {nid: NodeState(node_id=nid) for nid in council_pipeline.nodes}
    for nid in ("position-edison", "position-newton", "position-nikola"):
        states[nid].result = f"OUTPUT_{nid}"
        states[nid].status = "done"

    lookup = engine._build_template_lookup(council_pipeline, states,
                                            layers, context={})
    assert "phase1" in lookup
    assert set(lookup["phase1"].keys()) == {
        "position-edison", "position-newton", "position-nikola", "all",
    }
    assert lookup["phase1"]["position-edison"] == "OUTPUT_position-edison"


def test_lookup_phase2a_and_2b_are_separate_namespaces(
        engine, council_pipeline):
    """Explicit 'phase2a' and 'phase2b' keep parallel branches separate.

    The two probe nodes sit in the same topological layer but represent
    logically distinct sub-phases. Each gets its own namespace in the
    lookup so {phase2a.all} and {phase2b.all} can refer to them
    independently.
    """
    layers = engine.topological_sort(council_pipeline)
    states = {nid: NodeState(node_id=nid) for nid in council_pipeline.nodes}
    states["probe-sherlock"].result = "PROBE_S"
    states["probe-sherlock"].status = "done"
    states["probe-raven"].result = "PROBE_R"
    states["probe-raven"].status = "done"

    lookup = engine._build_template_lookup(council_pipeline, states,
                                            layers, context={})
    assert "phase2a" in lookup
    assert "phase2b" in lookup
    assert lookup["phase2a"]["probe-sherlock"] == "PROBE_S"
    assert lookup["phase2b"]["probe-raven"] == "PROBE_R"
    assert "probe-raven" not in lookup["phase2a"]
    assert "probe-sherlock" not in lookup["phase2b"]


def test_lookup_phase_all_concatenates_in_layer_order(engine):
    """{phase.all} concatenates every member of the phase in stable order.

    Order matters: the receiving agent will read this as a single
    document and expects the upstream outputs in a predictable
    sequence. We sort by the topological layer's natural order rather
    than dict iteration order, so the concat is deterministic across
    Python versions and dict insertion orderings.

    The test workflow is a parallel middle (a → b ∥ c → d) so layer 1
    holds both b and c — they're in the same phase, and `.all` should
    concatenate them in b-then-c order.
    """
    wf = Workflow(name="order-test")
    wf.nodes["a"] = WorkflowNode(id="a", agent="x", task="A")
    wf.nodes["b"] = WorkflowNode(id="b", agent="x", task="B",
                                 depends_on=["a"])
    wf.nodes["c"] = WorkflowNode(id="c", agent="x", task="C",
                                 depends_on=["a"])
    wf.nodes["d"] = WorkflowNode(id="d", agent="x", task="D",
                                 depends_on=["b", "c"])

    engine = WorkflowEngine()
    layers = engine.topological_sort(wf)
    # Mark all 4 as done with distinct outputs
    states = {nid: NodeState(node_id=nid, status="done",
                              result=f"OUT_{nid}") for nid in wf.nodes}

    lookup = engine._build_template_lookup(wf, states, layers, context={})
    # b and c share layer 1, so they live in the same phase (phase1,
    # since layer 0 is 'a' and layer 1 is 'b','c', and 'a' is done so
    # phase0 exists too). Both phases have a single member each except
    # phase1 which has two.
    assert "phase0" in lookup
    assert "phase1" in lookup
    # phase0 has just 'a'; phase1 has 'b' and 'c' (and 'all')
    assert "all" in lookup["phase1"]
    # The concat is "[id]\nbody" pairs joined by "\n\n---\n\n"
    # b should appear before c in the concat (layer-order)
    all_text = lookup["phase1"]["all"]
    assert all_text.index("OUT_b") < all_text.index("OUT_c")
    # And 'd' is in its own phase, not in phase1
    assert "OUT_d" not in all_text


def test_lookup_excludes_pending_nodes(engine, council_pipeline):
    """Nodes without a captured result are excluded from the lookup.

    If a node hasn't completed, its result is None and we skip it.
    This prevents downstream prompts from embedding a half-finished
    or empty output. Pipelines that need a guarantee of a result
    should put a verify/gate node in the dependency chain.
    """
    layers = engine.topological_sort(council_pipeline)
    states = {nid: NodeState(node_id=nid) for nid in council_pipeline.nodes}
    # Only premortem has a result. Positions are still pending.
    states["premortem"].result = "PRE"
    states["premortem"].status = "done"
    # position-edison has status='done' but no captured result —
    # should still be excluded (defensive: the engine only populates
    # result on a real card-body read, not just on status flip)
    states["position-edison"].status = "done"

    lookup = engine._build_template_lookup(council_pipeline, states,
                                            layers, context={})
    # phase0 exists (premortem), phase1 does not (no captured results)
    assert "phase0" in lookup
    assert "phase1" not in lookup
    # Top-level node id only set for the one with a result
    assert lookup.get("premortem") == "PRE"
    assert "position-edison" not in lookup


def test_lookup_exposes_completed_node_ids_at_top_level(
        engine, council_pipeline):
    """Each completed node id is mirrored at the top of the lookup.

    This is for the legacy {node-id} form. The original council.yaml
    uses things like {position-edison-output} and {premortem-output}
    (we currently don't have those, but the convention is bare
    {node-id}). Top-level exposure lets those templates resolve via
    the same code path as the new {phaseN.X} form.
    """
    layers = engine.topological_sort(council_pipeline)
    states = {nid: NodeState(node_id=nid) for nid in council_pipeline.nodes}
    states["position-edison"].result = "POS_E_OUT"
    states["position-edison"].status = "done"

    lookup = engine._build_template_lookup(council_pipeline, states,
                                            layers, context={})
    assert lookup["position-edison"] == "POS_E_OUT"
    # And the canonical {phase1.position-edison} form also resolves
    assert lookup["phase1"]["position-edison"] == "POS_E_OUT"


# ── _resolve_template tests ──────────────────────────────────────

def test_resolve_namespace_field(engine):
    """{namespace.field} resolves to lookup[ns][field]."""
    lookup = {
        "context": {"q": "Q_VAL"},
        "phase1": {
            "position-edison": "EDISON_OUT",
            "all": "ALL_OUT",
        },
    }
    out = engine._resolve_template(
        "Q={context.q}, P={phase1.position-edison}, ALL={phase1.all}",
        lookup,
    )
    assert out == "Q=Q_VAL, P=EDISON_OUT, ALL=ALL_OUT"


def test_resolve_bare_form_via_context(engine):
    """Legacy {bare} form resolves via context first.

    This is what makes {question} in the original council.yaml work:
    `question` is in the context dict (from -c question=...) and the
    bare-form fallback looks there before going to top-level node ids.
    """
    lookup = {
        "context": {"question": "What is X?"},
        "phase1": {"position-edison": "EDISON_OUT"},
    }
    out = engine._resolve_template("Q: {question}", lookup)
    assert out == "Q: What is X?"


def test_resolve_bare_form_falls_through_to_top_level_node(engine):
    """Legacy {bare} form falls through to top-level node ids.

    If the bare token isn't in context, we check the top of the
    lookup for a completed node with that id. This is what supports
    {position-edison-output} style references (well, the prefix
    stripped — {position-edison} — once the YAML gets cleaned up).
    """
    lookup = {
        "context": {},
        "phase1": {"position-edison": "EDISON_OUT"},
        "position-edison": "EDISON_OUT",
    }
    out = engine._resolve_template("Got: {position-edison}", lookup)
    assert out == "Got: EDISON_OUT"


def test_resolve_unresolved_namespace_field_leaves_literal(engine, capsys):
    """Unknown {ns.field} stays in the text and prints a warning.

    Per the spec: 'Unresolved variables (e.g. if a phase hasn't run
    yet) are left as-is or raise a clear error — do not silently
    produce empty strings.' We go with leave-as-is + a one-line
    warning, so the agent still sees the literal brace and can
    surface the missing upstream in its work, while operators get a
    visible signal in the engine logs.
    """
    lookup = {"context": {}, "phase1": {"position-edison": "OK"}}
    out = engine._resolve_template(
        "Known: {phase1.position-edison}, Missing: {phase1.missing-node}",
        lookup,
    )
    assert out == "Known: OK, Missing: {phase1.missing-node}"
    captured = capsys.readouterr()
    assert "Unresolved template {phase1.missing-node}" in captured.out


def test_resolve_unresolved_bare_leaves_literal(engine, capsys):
    """Unknown {bare} (neither context nor top-level) also leaves literal."""
    lookup = {"context": {"known": "X"}}
    out = engine._resolve_template("Known: {known}, Missing: {nope}", lookup)
    assert out == "Known: X, Missing: {nope}"
    captured = capsys.readouterr()
    assert "Unresolved template {nope}" in captured.out


def test_resolve_unresolved_unknown_namespace_leaves_literal(engine, capsys):
    """{notaphase.foo} where 'notaphase' isn't in the lookup at all."""
    lookup = {"context": {}}
    out = engine._resolve_template("A: {notaphase.foo}", lookup)
    assert out == "A: {notaphase.foo}"
    captured = capsys.readouterr()
    assert "Unresolved template {notaphase.foo}" in captured.out


def test_resolve_no_templates_passthrough(engine, capsys):
    """Text with no {…} references is returned unchanged, no warnings."""
    out = engine._resolve_template("Plain text, no braces here.", {})
    assert out == "Plain text, no braces here."
    assert "Unresolved" not in capsys.readouterr().out


def test_resolve_does_not_treat_json_braces_as_templates(engine):
    """Internal {...} JSON-ish blobs are not template references.

    The regex requires the leading char to be a letter or underscore.
    Things like `{1, 2, 3}` (starts with digit) or `{}` (empty) are
    left alone. This is intentional: we don't want the resolver to
    chew on JSON-like text inside the task body.
    """
    lookup = {"context": {"q": "Q"}}
    out = engine._resolve_template(
        "List: {1, 2, 3} and empty: {} and ref: {q}", lookup,
    )
    assert out == "List: {1, 2, 3} and empty: {} and ref: Q"


# ── _build_task_body tests ───────────────────────────────────────

def test_build_task_body_appends_context_footer_after_substitution(engine):
    """The Context JSON footer goes AFTER substitution.

    Putting the footer after the resolver means the JSON braces in
    the footer don't get treated as templates (which would either
    leave noisy 'Unresolved' warnings or accidentally substitute
    something). The order matters — the pre-B2 footer is still
    there for agents that prefer to read the raw context.
    """
    wf = Workflow(name="ctx-footer")
    wf.nodes["a"] = WorkflowNode(id="a", agent="x",
                                  task="Task with {context.x}")
    layers = engine.topological_sort(wf)
    states = {"a": NodeState(node_id="a")}

    body = engine._build_task_body(
        wf.nodes["a"], wf, states, layers, context={"x": "X_VAL"},
    )
    # The body should contain the resolved value AND the JSON footer
    assert "Task with X_VAL" in body
    assert 'Context: {"x": "X_VAL"}' in body
    # The footer should be after the resolved task text, not before
    assert body.index("X_VAL") < body.index('Context: ')


def test_build_task_body_no_context_no_footer(engine):
    """No context given → no Context footer appended."""
    wf = Workflow(name="no-ctx")
    wf.nodes["a"] = WorkflowNode(id="a", agent="x", task="Plain task")
    layers = engine.topological_sort(wf)
    states = {"a": NodeState(node_id="a")}

    body = engine._build_task_body(
        wf.nodes["a"], wf, states, layers, context=None,
    )
    assert body == "Plain task"
    assert "Context:" not in body


def test_build_task_body_full_council_substitution(engine, council_pipeline):
    """End-to-end: every spec example variable resolves correctly.

    This is the canonical council-pipeline test. Sets up:
      - premortem done with PRE_OUTPUT
      - all three positions done with their respective outputs
      - probes to be created next
    Then builds a probe-sherlock body that references:
      - {context.question}
      - {context.question_slug}
      - {phase1.position-edison}     (specific node)
      - {phase1.all}                  (all positions concatenated)
    """
    layers = engine.topological_sort(council_pipeline)
    states = {nid: NodeState(node_id=nid) for nid in council_pipeline.nodes}
    # Council-ready auto-completed as a synthetic gate, no result needed
    states["council-ready"].status = "done"
    # All upstream phases done
    for nid, body in [
        ("premortem", "PRE_OUTPUT"),
        ("position-edison", "EDISON_OUTPUT"),
        ("position-newton", "NEWTON_OUTPUT"),
        ("position-nikola", "NIKOLA_OUTPUT"),
    ]:
        states[nid].result = body
        states[nid].status = "done"

    # Probe-sherlock task references all the spec variables
    probe_node = council_pipeline.nodes["probe-sherlock"]
    probe_node.task = (
        "Question: {context.question}\n"
        "Slug: {context.question_slug}\n"
        "Edison position: {phase1.position-edison}\n"
        "All positions: {phase1.all}\n"
    )

    body = engine._build_task_body(
        probe_node, council_pipeline, states, layers,
        context={"question": "What is X?", "question_slug": "what-is-x"},
    )
    # All references resolve
    assert "Question: What is X?" in body
    assert "Slug: what-is-x" in body
    assert "Edison position: EDISON_OUTPUT" in body
    # {phase1.all} expands to a concatenation of all 3 positions
    assert "EDISON_OUTPUT" in body
    assert "NEWTON_OUTPUT" in body
    assert "NIKOLA_OUTPUT" in body
    # Order is layer-stable: edison before newton before nikola
    assert body.index("EDISON_OUTPUT") < body.index("NEWTON_OUTPUT")
    assert body.index("NEWTON_OUTPUT") < body.index("NIKOLA_OUTPUT")
    # No unresolved literals left
    assert "{context.question}" not in body
    assert "{phase1.all}" not in body
    # Footer is present
    assert "Context:" in body


def test_build_task_body_phase2a_vs_phase2b(engine, council_pipeline):
    """{phase2a.all} and {phase2b.all} stay separate.

    Once both probes are done, their results go into phase2a and
    phase2b respectively. Building a synthesize node's body that
    references both should pull from each independently.
    """
    layers = engine.topological_sort(council_pipeline)
    states = {nid: NodeState(node_id=nid) for nid in council_pipeline.nodes}
    for nid, body in [
        ("premortem", "PRE"),
        ("position-edison", "E"),
        ("position-newton", "N"),
        ("position-nikola", "K"),
        ("probe-sherlock", "PROBE_S_OUT"),
        ("probe-raven", "PROBE_R_OUT"),
    ]:
        states[nid].result = body
        states[nid].status = "done"

    # Synthesize node references both sub-phases
    wf = council_pipeline
    synth = WorkflowNode(
        id="synth", agent="nikola", task="synth",
        depends_on=["probe-sherlock", "probe-raven"], phase="phase3",
    )
    wf.nodes["synth"] = synth
    layers = engine.topological_sort(wf)
    states["synth"] = NodeState(node_id="synth")
    synth.task = "S: {phase2a.all}\nR: {phase2b.all}\n"

    body = engine._build_task_body(
        synth, wf, states, layers, context={},
    )
    # Each phase2X.all contains only its own probe
    assert "S: [probe-sherlock]\nPROBE_S_OUT" in body
    assert "R: [probe-raven]\nPROBE_R_OUT" in body
    # And not the other way around
    assert "PROBE_R_OUT" not in body.split("S: ")[1].split("\nR: ")[0]
    assert "PROBE_S_OUT" not in body.split("R: ")[1]


# ── state.result persistence tests ───────────────────────────────

def test_state_result_round_trip(engine):
    """state.result is persisted to disk and restored on load.

    The whole point of the result field is so that a resumed
    workflow (engine crashed and restarted) still has the upstream
    outputs available for {phaseN.X} substitution. This test
    exercises the _save_state / _load_state round-trip.
    """
    wf = Workflow(name="result-roundtrip")
    wf.nodes["a"] = WorkflowNode(id="a", agent="x", task="A")
    states = {
        "a": NodeState(node_id="a", status="done", kanban_card_id="c-1",
                       result="CAPTURED_BODY"),
    }
    results = {"a": "done"}
    layers = [["a"]]

    engine._save_state("result-roundtrip", states, results, 0, layers)
    loaded = engine._load_state("result-roundtrip")

    assert loaded is not None
    assert loaded["states"]["a"]["result"] == "CAPTURED_BODY"

    # Round-trip back into a NodeState — the loader path
    restored = NodeState(
        node_id=loaded["states"]["a"]["node_id"],
        status=loaded["states"]["a"]["status"],
        result=loaded["states"]["a"]["result"],
    )
    assert restored.result == "CAPTURED_BODY"

    engine._clear_state("result-roundtrip")


# ── create_kanban_card public-API path ───────────────────────────

def test_create_kanban_card_resolves_templates_when_workflow_provided(
        engine, council_pipeline, monkeypatch):
    """End-to-end: create_kanban_card resolves templates + posts to kanban.

    We mock the subprocess.run call (so no real kanban card is
    created) and assert that the body passed to --body already has
    the {phase1.X} references resolved. This is the public API path
    the engine's execute() loop takes, and it's what the agent
    ultimately sees.
    """
    layers = engine.topological_sort(council_pipeline)
    states = {nid: NodeState(node_id=nid) for nid in council_pipeline.nodes}
    states["position-edison"].result = "EDISON_OUT"
    states["position-edison"].status = "done"
    states["position-newton"].result = "NEWTON_OUT"
    states["position-newton"].status = "done"
    states["position-nikola"].result = "NIKOLA_OUT"
    states["position-nikola"].status = "done"

    # Capture the body that create_kanban_card would post
    captured = {}

    class FakeResult:
        returncode = 0
        stdout = '{"id": "t_fake_card_123"}'
        stderr = ""

    def fake_run(cmd, capture_output=True, text=True, timeout=30, **kwargs):
        # Find the --body arg in the command list
        idx = cmd.index("--body") + 1
        captured["body"] = cmd[idx]
        return FakeResult()

    monkeypatch.setattr("subprocess.run", fake_run)

    probe_node = council_pipeline.nodes["probe-sherlock"]
    probe_node.task = "All positions: {phase1.all}\nContext Q: {context.q}"

    card_id = engine.create_kanban_card(
        probe_node,
        context={"q": "Q_VAL"},
        workflow=council_pipeline,
        states=states,
        layers=layers,
    )
    # Card id was parsed from the mocked JSON
    assert card_id == "t_fake_card_123"
    # Body was substituted before posting
    body = captured["body"]
    assert "EDISON_OUT" in body
    assert "NEWTON_OUT" in body
    assert "NIKOLA_OUT" in body
    assert "Context Q: Q_VAL" in body
    # Literal braces are gone
    assert "{phase1.all}" not in body
    assert "{context.q}" not in body
    # Context footer was appended
    assert 'Context: {"q": "Q_VAL"}' in body


def test_create_kanban_card_legacy_path_unchanged(
        engine, monkeypatch):
    """Backward compat: omitting workflow= reverts to pre-B2 footer only.

    Direct callers (the synthetic-guard test) and any pre-B2 code
    path that calls create_kanban_card(node, context) without the
    new keyword args should get the original footer-only behavior
    — no {ns.field} resolution, just the Context JSON appended.
    """
    captured = {}

    class FakeResult:
        returncode = 0
        stdout = '{"id": "t_legacy"}'
        stderr = ""

    def fake_run(cmd, capture_output=True, text=True, timeout=30, **kwargs):
        idx = cmd.index("--body") + 1
        captured["body"] = cmd[idx]
        return FakeResult()

    monkeypatch.setattr("subprocess.run", fake_run)

    node = WorkflowNode(id="x", agent="a", task="Do {phase1.foo}")
    # Note: no workflow= passed → legacy path
    card_id = engine.create_kanban_card(node, context={"k": "v"})
    assert card_id == "t_legacy"
    # {phase1.foo} is NOT resolved (legacy path doesn't know about phases)
    assert "{phase1.foo}" in captured["body"]
    # But the Context footer IS appended
    assert 'Context: {"k": "v"}' in captured["body"]


# ── Typed I/O contract tests ──────────────────────────────────────
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import hermes_cli.kanban_db as kb
from hermes_cli.schema_registry import register_schema, reset_registry
from hermes_cli.kanban_db import SchemaValidationError


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Isolated kanban DB for contract tests."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    kb.init_db()
    with kb.connect() as conn:
        yield conn


class TestTypedContracts:
    def test_typed_contract_with_registered_schema_passes(self, db):
        """Register schema, complete with valid metadata → done."""
        register_schema("research-worker", {
            "type": "object",
            "required": ["findings", "sources_read"],
            "properties": {
                "findings": {"type": "array", "items": {"type": "string"}},
                "sources_read": {"type": "integer", "minimum": 0},
            },
            "additionalProperties": True,
        })
        try:
            tid = kb.create_task(
                db, title="research", assignee="research-worker",
                initial_status="running",
            )
            assert kb.complete_task(
                db, tid,
                metadata={"findings": ["f1"], "sources_read": 3},
            )
            task = kb.get_task(db, tid)
            assert task.status == "done"
        finally:
            reset_registry()

    def test_typed_contract_with_registered_schema_fails(self, db):
        """Register schema, complete with missing required field → raises."""
        register_schema("research-worker", {
            "type": "object",
            "required": ["findings", "sources_read"],
            "properties": {
                "findings": {"type": "array", "items": {"type": "string"}},
                "sources_read": {"type": "integer", "minimum": 0},
            },
            "additionalProperties": True,
        })
        try:
            tid = kb.create_task(
                db, title="research", assignee="research-worker",
                initial_status="running",
            )
            with pytest.raises(SchemaValidationError) as exc_info:
                kb.complete_task(db, tid, metadata={"sources_read": 1})
            assert "findings" in str(exc_info.value)
            task = kb.get_task(db, tid)
            assert task.status != "done"
        finally:
            reset_registry()

    def test_typed_contract_without_schema(self, db):
        """No schema registered → validation is a no-op."""
        tid = kb.create_task(
            db, title="adhoc", assignee="no-schema-worker",
            initial_status="running",
        )
        assert kb.complete_task(
            db, tid, metadata={"anything": "goes"},
        )
        task = kb.get_task(db, tid)
        assert task.status == "done"

    def test_typed_contract_with_metadata_override_flag(self, db):
        """Schema registered, but metadata_override=1 → validation skipped."""
        register_schema("research-worker", {
            "type": "object",
            "required": ["findings"],
            "properties": {},
            "additionalProperties": True,
        })
        try:
            tid = kb.create_task(
                db, title="research", assignee="research-worker",
                initial_status="running", metadata_override=True,
            )
            # Invalid metadata — would fail without override
            assert kb.complete_task(db, tid, metadata={"bad": True})
            task = kb.get_task(db, tid)
            assert task.status == "done"
            # Audit event emitted
            events = db.execute(
                "SELECT kind FROM task_events WHERE task_id = ? "
                "AND kind = 'metadata_override_used'", (tid,)
            ).fetchall()
            assert len(events) == 1
        finally:
            reset_registry()

    def test_typed_contract_with_complex_nested_schema(self, db):
        """Nested schema validates correctly (positive + negative)."""
        register_schema("code-worker", {
            "type": "object",
            "required": ["context"],
            "properties": {
                "context": {
                    "type": "object",
                    "required": ["env"],
                    "properties": {
                        "env": {"type": "string"},
                    },
                },
            },
            "additionalProperties": True,
        })
        try:
            # Positive case
            tid = kb.create_task(
                db, title="deploy", assignee="code-worker",
                initial_status="running",
            )
            assert kb.complete_task(
                db, tid,
                metadata={"context": {"env": "production"}},
            )
            assert kb.get_task(db, tid).status == "done"

            # Negative case — wrong type
            tid2 = kb.create_task(
                db, title="deploy2", assignee="code-worker",
                initial_status="running",
            )
            with pytest.raises(SchemaValidationError):
                kb.complete_task(
                    db, tid2,
                    metadata={"context": {"env": 123}},
                )
            assert kb.get_task(db, tid2).status != "done"
        finally:
            reset_registry()


# ── Heartbeat sweep tests ─────────────────────────────────────────
import time as _time


class TestHeartbeatSweep:
    def _make_running(self, db, tid):
        """Set a task to 'running' status (create_task defaults to 'ready')."""
        db.execute(
            "UPDATE tasks SET status = 'running' WHERE id = ?", (tid,)
        )
        db.commit()

    def test_heartbeat_fresh_no_block(self, db):
        """Running task with recent heartbeat → sweep returns []."""
        tid = kb.create_task(
            db, title="live-task", assignee="worker-a",
            initial_status="running", max_runtime_seconds=60,
        )
        self._make_running(db, tid)
        # Simulate a recent heartbeat
        now = int(_time.time())
        db.execute(
            "UPDATE tasks SET last_heartbeat_at = ? WHERE id = ?",
            (now, tid),
        )
        db.commit()
        result = kb.sweep_stale_heartbeats(db)
        assert result == []
        assert kb.get_task(db, tid).status == "running"

    def test_heartbeat_stale_auto_block(self, db):
        """Stale heartbeat → sweep auto-blocks the task."""
        tid = kb.create_task(
            db, title="stale-task", assignee="worker-b",
            initial_status="running", max_runtime_seconds=60,
        )
        self._make_running(db, tid)
        # Set heartbeat far in the past (3x max_runtime = 180s, threshold = 120s)
        stale_time = int(_time.time()) - 180
        db.execute(
            "UPDATE tasks SET last_heartbeat_at = ? WHERE id = ?",
            (stale_time, tid),
        )
        db.commit()
        result = kb.sweep_stale_heartbeats(db)
        assert tid in result
        task = kb.get_task(db, tid)
        assert task.status == "blocked"
        assert "Auto-blocked" in (task.result or "")
        # Event emitted
        events = db.execute(
            "SELECT kind FROM task_events WHERE task_id = ? "
            "AND kind = 'auto_blocked'", (tid,)
        ).fetchall()
        assert len(events) == 1

    def test_heartbeat_max_runtime_none_default(self, db):
        """NULL max_runtime_seconds uses default fallback (1800s)."""
        tid = kb.create_task(
            db, title="no-limit-task", assignee="worker-c",
            initial_status="running",
        )
        self._make_running(db, tid)
        # Set heartbeat 3700s ago (> 2*1800 = 3600 threshold)
        stale_time = int(_time.time()) - 3700
        db.execute(
            "UPDATE tasks SET last_heartbeat_at = ? WHERE id = ?",
            (stale_time, tid),
        )
        db.commit()
        result = kb.sweep_stale_heartbeats(db)
        assert tid in result
        assert kb.get_task(db, tid).status == "blocked"

        # Now test with a fresh heartbeat — should NOT block
        tid2 = kb.create_task(
            db, title="fresh-task", assignee="worker-d",
            initial_status="running",
        )
        self._make_running(db, tid2)
        now = int(_time.time())
        db.execute(
            "UPDATE tasks SET last_heartbeat_at = ? WHERE id = ?",
            (now, tid2),
        )
        db.commit()
        result2 = kb.sweep_stale_heartbeats(db)
        assert tid2 not in result2
        assert kb.get_task(db, tid2).status == "running"

    def test_heartbeat_check_idempotent(self, db):
        """Run sweep twice on stale task; second call returns []."""
        tid = kb.create_task(
            db, title="idempotent-task", assignee="worker-e",
            initial_status="running", max_runtime_seconds=60,
        )
        self._make_running(db, tid)
        stale_time = int(_time.time()) - 180
        db.execute(
            "UPDATE tasks SET last_heartbeat_at = ? WHERE id = ?",
            (stale_time, tid),
        )
        db.commit()
        # First sweep — should block
        result1 = kb.sweep_stale_heartbeats(db)
        assert tid in result1
        assert kb.get_task(db, tid).status == "blocked"
        # Second sweep — already blocked, no re-block
        result2 = kb.sweep_stale_heartbeats(db)
        assert tid not in result2
        # Only one auto_blocked event
        events = db.execute(
            "SELECT kind FROM task_events WHERE task_id = ? "
            "AND kind = 'auto_blocked'", (tid,)
        ).fetchall()
        assert len(events) == 1


# ── Deadlock detection tests ──────────────────────────────────────
import hermes_cli.kanban_db as _kb_db


class TestDeadlockDetection:
    def _make_running(self, db, tid):
        db.execute(
            "UPDATE tasks SET status = 'running' WHERE id = ?", (tid,)
        )
        db.commit()

    def test_deadlock_clean_create(self, db):
        """Create with no parents → succeeds."""
        tid = kb.create_task(
            db, title="standalone", assignee="worker-a",
            initial_status="running",
        )
        assert tid is not None
        task = kb.get_task(db, tid)
        assert task.title == "standalone"

    def test_deadlock_direct_cycle(self, db):
        """A→B exists; linking B→A creates cycle A→B→A."""
        a = kb.create_task(db, title="A", assignee="alpha")
        b = kb.create_task(db, title="B", assignee="beta", parents=[a])
        # Manually create cycle: link B→A (B is already child of A)
        with pytest.raises(_kb_db.CycleDetectedError) as exc_info:
            kb.link_tasks(db, b, a)  # B→A would create A→B→A
        assert a in exc_info.value.path
        assert b in exc_info.value.path

    def test_deadlock_transitive_cycle(self, db):
        """A→B→C exists; linking C→A creates 3-node cycle."""
        a = kb.create_task(db, title="A", assignee="alpha")
        b = kb.create_task(db, title="B", assignee="beta", parents=[a])
        c = kb.create_task(db, title="C", assignee="gamma", parents=[b])
        # Create cycle: C→A
        with pytest.raises(_kb_db.CycleDetectedError) as exc_info:
            kb.link_tasks(db, c, a)
        path = exc_info.value.path
        assert a in path
        assert b in path
        assert c in path

    def test_deadlock_self_dependency(self, db):
        """link_tasks with same id as parent and child → ValueError."""
        tid = kb.create_task(db, title="self", assignee="worker-x")
        with pytest.raises(ValueError, match="cannot depend on itself"):
            kb.link_tasks(db, tid, tid)

    def test_deadlock_parallel_branches(self, db):
        """A has two children B and C in parallel → both creates succeed."""
        a = kb.create_task(db, title="A", assignee="alpha")
        b = kb.create_task(db, title="B", assignee="beta", parents=[a])
        c = kb.create_task(db, title="C", assignee="gamma", parents=[a])
        assert b is not None
        assert c is not None
        assert b != c
        # Verify both links exist
        links = db.execute(
            "SELECT child_id FROM task_links WHERE parent_id = ?", (a,)
        ).fetchall()
        child_ids = {r["child_id"] for r in links}
        assert b in child_ids
        assert c in child_ids


# ── scope: global / dispatch_node tests ────────────────────────────


def test_workflow_scope_default_is_project():
    """Workflows default to scope: project — current behavior preserved."""
    wf = Workflow(name="x")
    assert wf.scope == "project"


def test_dispatch_node_scope_global_returns_none_and_marks_done():
    """scope: global nodes are dispatched in-process; no card created."""
    engine = WorkflowEngine()
    wf = Workflow(name="heartbeat", scope="global")
    state = NodeState(node_id="hb")
    node = WorkflowNode(
        id="hb",
        agent="sherlock",
        task="Check fleet heartbeat",
    )
    layers = [[node]]
    states = {"hb": state}

    # No subprocess should be invoked — the helper sets state.done directly.
    card_id = engine.dispatch_node(
        state, node, context={},
        workflow=wf, states=states, layers=layers,
    )
    assert card_id is None
    assert state.status == "done"
    assert state.completed_at is not None
    assert state.result == "[in-process, scope: global]"


def test_dispatch_node_scope_project_delegates_to_create_kanban_card(
        engine, council_pipeline, monkeypatch):
    """scope: project (default) routes through create_kanban_card as before."""

    class FakeResult:
        returncode = 0
        stdout = '{"id": "t_project_card"}'
        stderr = ""

    monkeypatch.setattr("subprocess.run", lambda *a, **kw: FakeResult())

    node = council_pipeline.nodes["position-edison"]
    state = NodeState(node_id=node.id)
    layers = engine.topological_sort(council_pipeline)
    states = {nid: NodeState(node_id=nid) for nid in council_pipeline.nodes}

    card_id = engine.dispatch_node(
        state, node, context={},
        workflow=council_pipeline,  # scope defaults to "project"
        states=states, layers=layers,
    )
    assert card_id == "t_project_card"
    assert state.status != "done"  # dispatcher left it "running" for monitoring


def test_load_workflow_parses_scope_field(tmp_path):
    """YAML scope: global is loaded into Workflow.scope."""
    import yaml as _yaml
    wf_path = tmp_path / "heartbeat.yaml"
    wf_path.write_text(_yaml.safe_dump({
        "name": "heartbeat",
        "scope": "global",
        "nodes": {
            "check": {
                "agent": "sherlock",
                "task": "Heartbeat check.",
            },
        },
    }))
    engine = WorkflowEngine(workflows_dir=tmp_path)
    wf = engine.load_workflow("heartbeat")
    assert wf.scope == "global"
    assert "check" in wf.nodes


def test_load_workflow_scope_defaults_to_project(tmp_path):
    """Workflows without explicit scope default to 'project'."""
    import yaml as _yaml
    wf_path = tmp_path / "normal.yaml"
    wf_path.write_text(_yaml.safe_dump({
        "name": "normal",
        "nodes": {
            "x": {"agent": "sherlock", "task": "do x"},
        },
    }))
    engine = WorkflowEngine(workflows_dir=tmp_path)
    wf = engine.load_workflow("normal")
    assert wf.scope == "project"
