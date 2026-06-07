"""
Workflow Execution Engine — mechanical DAG runner for multi-agent pipelines.

Reads a workflow YAML, topologically sorts agent nodes, creates kanban cards
for ready nodes, monitors completion, and advances the graph. Supports revision
loops via the LOOP:<target> convention in block reasons.

Usage:
    python -m tools.workflow_engine start ideation --context pr=123
    python -m tools.workflow_engine validate ideation
    python -m tools.workflow_engine list

Architecture:
    Trigger (Discord/webhook) → Classify (Sherlock) → Engine (this) → Kanban → Agents

Revision Loops:
    When an agent rejects work, they block the card with reason
    "LOOP:<verify-node> | <human-readable details>". The engine:
    1. Finds the revision node that depends on the verify node
    2. Creates + monitors the revision card
    3. Re-creates the verify card (loop back)
    4. Repeats up to 3 times, then escalates to Sherlock

    Example:
      Nikola blocks nikola-verify-spec with:
        "LOOP:nikola-verify-spec | Missing billing edge case, auth rate limiting"
      Engine: runs edison-revise-spec → re-runs nikola-verify-spec
"""

import yaml
import json
import time
import subprocess
import sys
import os
import re
from pathlib import Path
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime, timezone

# ── Data structures ──────────────────────────────────────────────

@dataclass
class WorkflowNode:
    """A single agent task in the DAG."""
    id: str
    agent: str                    # Agent name (matches kanban worker)
    task: str                     # Task description for the kanban card
    depends_on: list[str] = field(default_factory=list)
    timeout_minutes: int = 30
    model: Optional[str] = None   # Optional model override
    channel: str = "debug"        # Where to send notifications

@dataclass
class Workflow:
    """Complete workflow definition."""
    name: str
    description: str = ""
    trigger_events: list[str] = field(default_factory=list)
    nodes: dict[str, WorkflowNode] = field(default_factory=dict)

@dataclass
class NodeState:
    """Runtime state for a workflow node."""
    node_id: str
    status: str = "pending"       # pending | running | done | failed | blocked | timed_out | revision_needed
    kanban_card_id: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    attempts: int = 0
    error: Optional[str] = None
    loop_count: int = 0           # Number of revision loops for this verify node

# ── Engine core ──────────────────────────────────────────────────

class CycleDetectedError(Exception):
    """Raised when the workflow graph contains a cycle."""
    pass

class WorkflowEngine:
    """
    Mechanical DAG runner. No HTTP, no webhooks — consumes a workflow file
    and drives kanban cards. Triggered externally (Sherlock, cron, watcher).

    Supports revision loops via the LOOP:<target> convention in block reasons.
    """

    MAX_REVISION_LOOPS = 3
    POLL_INTERVAL = 15  # seconds between kanban status checks
    STATE_DIR = None     # Set after init for state persistence

    def __init__(self, workflows_dir: str = None):
        if workflows_dir is None:
            repo = Path(__file__).resolve().parent.parent
            workflows_dir = repo / "docs" / "fleet-pipelines"
        self.workflows_dir = Path(workflows_dir)
        self.kanban_board = "fleet-workflow"
        WorkflowEngine.STATE_DIR = self.workflows_dir / ".engine-state"
        self.STATE_DIR.mkdir(parents=True, exist_ok=True)

    # ── Loading ───────────────────────────────────────────────

    def load_workflow(self, name: str) -> Workflow:
        """Load a workflow definition from YAML."""
        path = self.workflows_dir / f"{name}.yaml"
        if not path.exists():
            raise FileNotFoundError(f"Workflow not found: {path}")

        with open(path) as f:
            raw = yaml.safe_load(f)

        workflow = Workflow(
            name=raw.get("name", name),
            description=raw.get("description", ""),
            trigger_events=raw.get("trigger_events", []),
        )

        for node_id, node_data in raw.get("nodes", {}).items():
            workflow.nodes[node_id] = WorkflowNode(
                id=node_id,
                agent=node_data["agent"],
                task=node_data["task"],
                depends_on=node_data.get("depends_on", []),
                timeout_minutes=node_data.get("timeout_minutes", 30),
                model=node_data.get("model"),
                channel=node_data.get("channel", "debug"),
            )

        return workflow

    # ── Topological sort ──────────────────────────────────────

    def topological_sort(self, workflow: Workflow) -> list[list[str]]:
        """
        Returns layers of nodes that can run in parallel.
        Layer 0 has no dependencies. Layer N depends only on layers < N.
        Also detects cycles.
        """
        in_degree = {nid: len(node.depends_on) for nid, node in workflow.nodes.items()}
        dependents = defaultdict(list)

        for nid, node in workflow.nodes.items():
            for dep in node.depends_on:
                if dep not in workflow.nodes:
                    raise ValueError(f"Node '{nid}' depends on unknown node '{dep}'")
                dependents[dep].append(nid)

        queue = deque([nid for nid, deg in in_degree.items() if deg == 0])
        layers = []
        processed = 0

        while queue:
            layer = list(queue)
            layers.append(layer)
            queue.clear()
            processed += len(layer)

            for nid in layer:
                for dependent in dependents[nid]:
                    in_degree[dependent] -= 1
                    if in_degree[dependent] == 0:
                        queue.append(dependent)

        if processed != len(workflow.nodes):
            remaining = [nid for nid, deg in in_degree.items() if deg > 0]
            raise CycleDetectedError(
                f"Cycle detected involving nodes: {', '.join(remaining)}"
            )

        return layers

    # ── Dependency lookup ─────────────────────────────────────

    def _find_revision_node(self, workflow: Workflow, verify_node_id: str) -> Optional[str]:
        """
        Find the revision node that depends on a verify node.

        Assumes a single-revision pattern: each verify node has exactly
        one revision node that depends on it. If future pipelines have
        multiple revision nodes for one verify node, this returns the
        first one encountered — upgrade to return a list when needed.
        """
        for nid, node in workflow.nodes.items():
            if verify_node_id in node.depends_on:
                return nid
        return None

    def _find_layer_for_node(self, layers: list[list[str]], node_id: str) -> int:
        """Find which layer a node belongs to."""
        for i, layer in enumerate(layers):
            if node_id in layer:
                return i
        return -1

    # ── Kanban dispatch ────────────────────────────────────────

    def create_kanban_card(self, node: WorkflowNode, context: dict = None) -> str:
        """Create a kanban card for a workflow node. Returns card ID."""
        context_str = json.dumps(context or {})
        task_with_context = node.task
        if context:
            task_with_context += f"\n\nContext: {context_str}"

        cmd = [
            "hermes", "kanban", "create",
            "--board", self.kanban_board,
            "--title", f"[{node.id}] {node.agent}: {node.task[:60]}",
            "--body", task_with_context,
            "--assignee", node.agent,
            "--goal",
            "--goal-max-turns", "10",
            "--priority", "2",
        ]
        if node.model:
            cmd.extend(["--model", node.model])

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            raise RuntimeError(f"Kanban card creation failed: {result.stderr}")

        # Parse card ID from output (format: "Created card <id>")
        # Use regex for robustness — preserves correctness if CLI format changes
        match = re.match(r'Created\s+card\s+(\S+)', result.stdout.strip())
        if match:
            return match.group(1)
        # Fallback: try last token (fragile but works for legacy output)
        card_id = result.stdout.strip().split()[-1]
        return card_id

    def get_card_status(self, card_id: str) -> dict:
        """Query a kanban card's current state."""
        result = subprocess.run(
            ["hermes", "kanban", "show", card_id, "--json"],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode != 0:
            return {"status": "unknown", "error": result.stderr}
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return {"status": "unknown"}

    def get_card_body(self, card_id: str) -> str:
        """Get the body/reason text of a kanban card."""
        card = self.get_card_status(card_id)
        return card.get("body", card.get("reason", card.get("description", "")))

    # ── State persistence ──────────────────────────────────────

    def _state_path(self, workflow_name: str) -> Path:
        return self.STATE_DIR / f"{workflow_name}_state.json"

    def _save_state(self, workflow_name: str, states: dict, results: dict,
                    current_layer: int, layers: list[list[str]]):
        """Persist engine state for crash recovery."""
        state = {
            "workflow_name": workflow_name,
            "current_layer": current_layer,
            "layers": layers,
            "states": {nid: {
                "node_id": s.node_id,
                "status": s.status,
                "kanban_card_id": s.kanban_card_id,
                "started_at": s.started_at,
                "completed_at": s.completed_at,
                "attempts": s.attempts,
                "error": s.error,
                "loop_count": s.loop_count,
            } for nid, s in states.items()},
            "results": results,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        with open(self._state_path(workflow_name), "w") as f:
            json.dump(state, f, indent=2)

    def _load_state(self, workflow_name: str) -> Optional[dict]:
        """Load persisted state if it exists."""
        path = self._state_path(workflow_name)
        if not path.exists():
            return None
        with open(path) as f:
            return json.load(f)

    def _clear_state(self, workflow_name: str):
        """Remove state file after successful completion."""
        path = self._state_path(workflow_name)
        if path.exists():
            path.unlink()

    # ── Validation ─────────────────────────────────────────────

    def validate(self, workflow_name: str) -> dict:
        """
        Validate a workflow without executing. Checks:
        - YAML loads cleanly
        - All dependency references resolve
        - No cycles in DAG
        - All agents referenced exist (best-effort)
        """
        result = {"valid": True, "issues": [], "layers": 0, "nodes": 0}

        try:
            workflow = self.load_workflow(workflow_name)
        except Exception as e:
            result["valid"] = False
            result["issues"].append(f"YAML load failed: {e}")
            return result

        result["nodes"] = len(workflow.nodes)

        # Check dependency references
        for nid, node in workflow.nodes.items():
            for dep in node.depends_on:
                if dep not in workflow.nodes:
                    result["valid"] = False
                    result["issues"].append(
                        f"Node '{nid}' depends on unknown node '{dep}'"
                    )

        # Check for cycles
        try:
            layers = self.topological_sort(workflow)
            result["layers"] = len(layers)
        except CycleDetectedError as e:
            result["valid"] = False
            result["issues"].append(str(e))
            return result
        except ValueError as e:
            result["valid"] = False
            result["issues"].append(str(e))
            return result

        # Check agents exist (best-effort — checks profiles dir)
        profiles_dir = Path.home() / ".hermes" / "profiles"
        for nid, node in workflow.nodes.items():
            agent_profile = profiles_dir / node.agent
            if not agent_profile.exists():
                result["issues"].append(
                    f"Node '{nid}': agent '{node.agent}' profile not found at {agent_profile}"
                )

        # Check revision loop pairs
        gate_patterns = ("verify", "security", "review")
        for nid, node in workflow.nodes.items():
            if "revise" in nid.lower():
                revise_node = nid
                # Find the gate node this revise node depends on
                gate_nodes = [d for d in node.depends_on
                             if any(p in d.lower() for p in gate_patterns)]
                if not gate_nodes:
                    result["issues"].append(
                        f"Revision node '{nid}' should depend on a gate node "
                        f"(verify/security/review), got: {node.depends_on}"
                    )

        # Check gate→revision pairs: each gate node that IS referenced by
        # a revision node's depends_on should have a dependent. This catches
        # misconfigured LOOP pairs without flagging post-merge tasks.
        verify_patterns = ("verify", "security", "review")
        revision_nodes = [nid for nid in workflow.nodes if "revise" in nid.lower()]
        referenced_gates = set()
        for rnid in revision_nodes:
            for dep in workflow.nodes[rnid].depends_on:
                if any(p in dep.lower() for p in verify_patterns):
                    referenced_gates.add(dep)

        for gate_id in referenced_gates:
            has_dependent = any(
                gate_id in other.depends_on
                for other_id, other in workflow.nodes.items()
                if other_id != gate_id
            )
            if not has_dependent:
                result["issues"].append(
                    f"Gate node '{gate_id}' is referenced by a revision node "
                    f"but has no dependents — LOOP detection will find no revision node"
                )

        return result

    # ── Execution ──────────────────────────────────────────────

    def execute(self, workflow_name: str, context: dict = None,
                start_node: str = None, dry_run: bool = False,
                resume: bool = False) -> dict:
        """
        Run a workflow to completion. Supports revision loops via
        the LOOP:<target> convention in block reasons.

        Returns execution summary: {node_id: final_status, ...}
        """
        workflow = self.load_workflow(workflow_name)
        layers = self.topological_sort(workflow)

        # Try resume from saved state
        states = None
        results = {}
        layer_idx = 0

        if resume:
            saved = self._load_state(workflow_name)
            if saved:
                print(f"Resuming {workflow_name} from layer {saved['current_layer']}")
                layer_idx = saved["current_layer"]
                results = saved["results"]
                states = {
                    nid: NodeState(
                        node_id=s["node_id"],
                        status=s["status"],
                        kanban_card_id=s.get("kanban_card_id"),
                        started_at=s.get("started_at"),
                        completed_at=s.get("completed_at"),
                        attempts=s.get("attempts", 0),
                        error=s.get("error"),
                        loop_count=s.get("loop_count", 0),
                    )
                    for nid, s in saved["states"].items()
                }

        # Initialize fresh state
        if states is None:
            states = {nid: NodeState(node_id=nid) for nid in workflow.nodes}
            results = {}

        # Handle partial start
        if start_node and start_node in workflow.nodes:
            layer_idx = self._find_layer_for_node(layers, start_node)
            if layer_idx < 0:
                raise ValueError(f"Node '{start_node}' not found in any layer")
            # Mark all nodes before this layer as done
            for i in range(layer_idx):
                for nid in layers[i]:
                    states[nid].status = "done"
                    results[nid] = "done"

        print(f"Starting workflow: {workflow.name}")
        print(f"  Layers: {len(layers)} | Nodes: {len(workflow.nodes)}")
        if dry_run:
            print("  DRY RUN — no cards will be created")
        if resume:
            print("  RESUME — skipping already-completed nodes")
        print()

        # ── Main execution loop (layer-based with loop support) ──
        while layer_idx < len(layers):
            layer = layers[layer_idx]
            print(f"── Layer {layer_idx + 1}/{len(layers)} ──")
            print(f"   Nodes: {', '.join(layer)}")

            if dry_run:
                for nid in layer:
                    node = workflow.nodes[nid]
                    if states[nid].status in ("done", "skipped"):
                        print(f"   [SKIP] {nid} — already {states[nid].status}")
                        continue
                    deps_failed = any(
                        states[d].status in ("failed", "timed_out", "blocked")
                        for d in node.depends_on
                    )
                    if deps_failed:
                        print(f"   [SKIP] {nid} — dependency failed, would skip")
                        continue
                    print(f"   [DRY RUN] Would create card for {node.agent}: {node.task[:60]}")
                layer_idx += 1
                continue

            # Create cards for this layer
            for nid in layer:
                state = states[nid]
                node = workflow.nodes[nid]

                # Skip already-completed nodes (resume)
                if state.status in ("done", "skipped"):
                    print(f"   ⏭ {nid} — {state.status}")
                    continue

                # Skip nodes with failed dependencies
                deps_failed = any(
                    states[d].status in ("failed", "timed_out", "blocked")
                    for d in node.depends_on
                )
                if deps_failed:
                    state.status = "skipped"
                    state.error = "Dependency failed"
                    results[nid] = "skipped"
                    print(f"   ⏭ {nid} — SKIPPED (dependency failed)")
                    continue

                # Create the card
                state.status = "running"
                state.started_at = datetime.now(timezone.utc).isoformat()
                state.attempts += 1

                try:
                    card_id = self.create_kanban_card(node, context)
                    state.kanban_card_id = card_id
                    print(f"   ✓ {nid} → card {card_id}")
                except Exception as e:
                    state.status = "failed"
                    state.error = str(e)
                    results[nid] = "failed"
                    print(f"   ✗ {nid} → failed: {e}")

            # Save state after dispatching layer
            self._save_state(workflow_name, states, results, layer_idx, layers)

            # Monitor completion for this layer
            revision_result = self._monitor_layer(
                workflow, layer, states, results, context
            )

            # Check for revision loops
            if revision_result:
                # A node in this layer triggered a LOOP
                verify_nid = revision_result["verify_node"]
                revision_nid = revision_result["revision_node"]
                verify_state = states[verify_nid]

                if verify_state.loop_count >= self.MAX_REVISION_LOOPS:
                    # Escalate
                    verify_state.status = "blocked"
                    verify_state.error = (
                        f"Exceeded {self.MAX_REVISION_LOOPS} revision loops — "
                        f"escalating to Sherlock"
                    )
                    results[verify_nid] = "blocked"
                    print(f"   🚫 {verify_nid} exceeded {self.MAX_REVISION_LOOPS} "
                          f"revision loops — escalating to Sherlock")
                    layer_idx += 1  # Advance past this layer
                else:
                    # Run the revision node
                    print(f"\n   ↩  LOOP #{verify_state.loop_count}: "
                          f"{verify_nid} → {revision_nid} → {verify_nid}")
                    rev_state = states[revision_nid]
                    rev_node = workflow.nodes[revision_nid]

                    # Run revision node
                    rev_state.status = "running"
                    rev_state.started_at = datetime.now(timezone.utc).isoformat()
                    rev_state.attempts += 1
                    try:
                        card_id = self.create_kanban_card(rev_node, context)
                        rev_state.kanban_card_id = card_id
                        print(f"   ✓ {revision_nid} → card {card_id}")
                    except Exception as e:
                        rev_state.status = "failed"
                        rev_state.error = str(e)
                        results[revision_nid] = "failed"
                        print(f"   ✗ {revision_nid} → failed: {e}")
                        layer_idx += 1
                        continue

                    # Monitor revision node
                    rev_layer = [revision_nid]
                    rev_states = {revision_nid: rev_state}
                    rev_results = {}
                    self._monitor_layer(
                        workflow, rev_layer, rev_states, rev_results, context,
                        skip_loop_detection=True  # Don't recurse
                    )

                    if rev_results.get(revision_nid) == "done":
                        print(f"   ✓ {revision_nid} complete — re-triggering {verify_nid}")
                        # Reset verify node for re-run
                        verify_state.status = "pending"
                        verify_state.kanban_card_id = None
                        verify_state.started_at = None
                        verify_state.completed_at = None
                        # Do NOT advance layer_idx — re-run same layer
                    else:
                        print(f"   ✗ {revision_nid} failed — cannot continue loop")
                        layer_idx += 1
            else:
                # No loops — advance to next layer
                layer_idx += 1

            print()

        # ── Summary ──
        completed = sum(1 for s in states.values() if s.status == "done")
        failed = sum(1 for s in states.values()
                    if s.status in ("failed", "timed_out"))
        skipped = sum(1 for s in states.values() if s.status == "skipped")
        blocked = sum(1 for s in states.values() if s.status == "blocked")
        print(f"Workflow complete: {completed} done, {failed} failed, "
              f"{skipped} skipped, {blocked} blocked")

        self._clear_state(workflow_name)
        return results

    def _monitor_layer(self, workflow: Workflow, layer: list[str],
                       states: dict[str, NodeState], results: dict,
                       context: dict = None,
                       skip_loop_detection: bool = False
                       ) -> Optional[dict]:
        """
        Poll kanban until all nodes in a layer complete or time out.

        Returns dict with 'verify_node' and 'revision_node' if a LOOP is
        detected, or None if the layer completed normally.
        """
        pending = set(layer)

        # Calculate dynamic max_polls from the longest node timeout in this layer
        max_node_timeout = max(
            (workflow.nodes[nid].timeout_minutes for nid in layer
             if states[nid].status == "running"),
            default=30
        )
        max_polls = int((max_node_timeout * 60) / self.POLL_INTERVAL)
        polls = 0

        while pending and polls < max_polls:
            time.sleep(self.POLL_INTERVAL)
            polls += 1

            for nid in list(pending):
                state = states[nid]
                if state.status != "running":
                    pending.discard(nid)
                    continue

                node = workflow.nodes[nid]
                elapsed = (datetime.now(timezone.utc) -
                          datetime.fromisoformat(state.started_at)).total_seconds()

                # Timeout check — uses node's own timeout
                if elapsed > node.timeout_minutes * 60:
                    state.status = "timed_out"
                    state.error = f"Exceeded {node.timeout_minutes}min timeout"
                    results[nid] = "timed_out"
                    print(f"   ⏰ {nid} timed out after {elapsed:.0f}s")
                    pending.discard(nid)
                    continue

                # Check kanban card status
                if not state.kanban_card_id:
                    continue

                try:
                    card = self.get_card_status(state.kanban_card_id)
                    card_status = card.get("status", card.get("column", "unknown"))
                    card_status_lower = card_status.lower()

                    # ── Done states ──
                    if card_status_lower in ("done", "completed", "complete"):
                        state.status = "done"
                        state.completed_at = datetime.now(timezone.utc).isoformat()
                        results[nid] = "done"
                        print(f"   ✓ {nid} completed ({elapsed:.0f}s)")
                        pending.discard(nid)

                    # ── Review states (kanban has 'review' status) ──
                    elif card_status_lower == "review":
                        # Agent marked card as review — check body for LOOP signal
                        body = self.get_card_body(state.kanban_card_id)
                        state.status = "revision_needed"
                        state.error = f"Review returned: {body[:100]}"
                        results[nid] = "revision_needed"
                        print(f"   🔄 {nid} returned REVIEW — checking for loop")
                        pending.discard(nid)

                    # ── Blocked states ──
                    elif card_status_lower in ("blocked",):
                        body = self.get_card_body(state.kanban_card_id)

                        # Check for LOOP convention: "LOOP:<target> | ..."
                        loop_match = re.match(r'^LOOP:(\S+)', body)
                        if loop_match and not skip_loop_detection:
                            target = loop_match.group(1)
                            revision_node = self._find_revision_node(workflow, nid)
                            if revision_node:
                                state.status = "revision_needed"
                                state.loop_count += 1
                                state.error = f"LOOP #{state.loop_count}: {body[:100]}"
                                results[nid] = "revision_needed"
                                print(f"   ↩  {nid} → LOOP:{target} "
                                      f"(#{state.loop_count}, revision: {revision_node})")
                                pending.discard(nid)

                                # Warn if LOOP target doesn't match blocked node
                                if target not in workflow.nodes:
                                    print(f"   ⚠  LOOP target '{target}' not a valid node — "
                                          f"falling back to blocked node '{nid}'")

                                # Return immediately — caller handles loop
                                return {
                                    "verify_node": target if target in workflow.nodes else nid,
                                    "revision_node": revision_node,
                                }
                            else:
                                # LOOP prefix but no revision node found
                                state.status = "blocked"
                                state.error = f"LOOP target but no revision node depends on {nid}"
                                results[nid] = "blocked"
                                print(f"   🚫 {nid} — LOOP prefix but no revision node")
                                pending.discard(nid)
                        else:
                            # Genuine blocker — not a LOOP
                            state.status = "blocked"
                            state.error = f"Blocked: {body[:100]}"
                            results[nid] = "blocked"
                            print(f"   🚫 {nid} BLOCKED — escalate to Sherlock")
                            pending.discard(nid)

                except Exception as e:
                    # Card query failed — keep polling
                    pass

        # Anything still pending after max_polls
        for nid in list(pending):
            state = states[nid]
            node = workflow.nodes[nid]
            state.status = "timed_out"
            state.error = f"Still running after {max_polls * self.POLL_INTERVAL}s (node timeout: {node.timeout_minutes}min)"
            results[nid] = "timed_out"
            pending.discard(nid)
            print(f"   ⏰ {nid} timed out (layer poll exhausted)")

        return None  # No loop detected

    # ── Status query ────────────────────────────────────────────

    def status(self, workflow_name: str = None) -> dict:
        """Query current state of running or saved workflows."""
        if workflow_name:
            saved = self._load_state(workflow_name)
            if saved:
                return {
                    "workflow": workflow_name,
                    "current_layer": saved["current_layer"],
                    "total_layers": len(saved["layers"]),
                    "states": saved["states"],
                    "results": saved["results"],
                    "updated_at": saved.get("updated_at"),
                }
            return {"workflow": workflow_name, "status": "no saved state"}

        # List all saved states
        runs = []
        for state_file in sorted(self.STATE_DIR.glob("*_state.json")):
            runs.append(state_file.stem.replace("_state", ""))
        return {"active_runs": runs}


# ── CLI ─────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Workflow Execution Engine")
    sub = parser.add_subparsers(dest="command")

    # start
    start = sub.add_parser("start", help="Start a workflow")
    start.add_argument("workflow", help="Workflow name (YAML file in docs/fleet-pipelines/)")
    start.add_argument("--context", "-c", nargs="*", help="Key=value context pairs")
    start.add_argument("--dry-run", action="store_true", help="Print plan without executing")
    start.add_argument("--node", help="Start from a specific node (partial execution)")
    start.add_argument("--resume", action="store_true", help="Resume from saved state")

    # validate
    validate = sub.add_parser("validate", help="Validate a workflow without executing")
    validate.add_argument("workflow", help="Workflow name to validate")

    # status
    status = sub.add_parser("status", help="Query workflow state")
    status.add_argument("workflow", nargs="?", help="Workflow name (omit for all)")

    # list
    sub.add_parser("list", help="List available workflow definitions")

    # show
    show = sub.add_parser("show", help="Show pipeline structure (layers + nodes)")
    show.add_argument("workflow", help="Workflow name to display")

    args = parser.parse_args()
    engine = WorkflowEngine()

    if args.command == "start":
        context = {}
        if args.context:
            for pair in args.context:
                k, v = pair.split("=", 1)
                context[k] = v
        engine.execute(args.workflow, context=context, start_node=args.node,
                      dry_run=args.dry_run, resume=args.resume)

    elif args.command == "validate":
        result = engine.validate(args.workflow)
        if result["valid"]:
            print(f"✓ {args.workflow} — {result['nodes']} nodes, "
                  f"{result['layers']} layers, valid DAG")
        else:
            print(f"✗ {args.workflow} — INVALID")
        if result["issues"]:
            for issue in result["issues"]:
                print(f"  • {issue}")
        sys.exit(0 if result["valid"] else 1)

    elif args.command == "status":
        state = engine.status(args.workflow)
        print(json.dumps(state, indent=2))

    elif args.command == "list":
        for f in sorted(engine.workflows_dir.glob("*.yaml")):
            print(f"  {f.stem}")

    elif args.command == "show":
        workflow = engine.load_workflow(args.workflow)
        layers = engine.topological_sort(workflow)
        print(f"Pipeline: {workflow.name}")
        print(f"Description: {workflow.description[:80]}...")
        print(f"Layers: {len(layers)} | Nodes: {len(workflow.nodes)}")
        print()
        for i, layer in enumerate(layers):
            print(f"Layer {i}:")
            for nid in layer:
                node = workflow.nodes[nid]
                deps = f" ← {', '.join(node.depends_on)}" if node.depends_on else ""
                print(f"  [{node.agent}] {nid}{deps}")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
