"""
Workflow Execution Engine — mechanical DAG runner for multi-agent pipelines.

Reads a workflow YAML, topologically sorts agent nodes, creates kanban cards
for ready nodes, monitors completion, and advances the graph. Designed to be
triggered by Sherlock or a channel watcher, not by webhooks directly.

Usage:
    python -m tools.workflow_engine start feature-dev --context pr=123

Architecture:
    Trigger (Discord/webhook) → Classify (Sherlock) → Engine (this) → Kanban → Agents
"""

import yaml
import json
import time
import subprocess
import sys
import os
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
    trigger_events: list[str] = field(default_factory=list)  # e.g. ["pr_opened", "merge"]
    nodes: dict[str, WorkflowNode] = field(default_factory=dict)

@dataclass
class NodeState:
    """Runtime state for a workflow node."""
    node_id: str
    status: str = "pending"       # pending | running | done | failed | blocked | timed_out
    kanban_card_id: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    attempts: int = 0
    error: Optional[str] = None

# ── Engine core ──────────────────────────────────────────────────

class CycleDetectedError(Exception):
    """Raised when the workflow graph contains a cycle."""
    pass

class WorkflowEngine:
    """
    Mechanical DAG runner. No HTTP, no webhooks — consumes a workflow file
    and drives kanban cards. Triggered externally (Sherlock, cron, watcher).
    """

    def __init__(self, workflows_dir: str = None):
        if workflows_dir is None:
            # Find the hermes-agent codebase (not the profile)
            # HERMES_HOME may point to a profile dir; use the repo checkout
            repo = Path(__file__).resolve().parent.parent  # tools/.. → repo root
            workflows_dir = repo / "docs" / "fleet-pipelines"
        self.workflows_dir = Path(workflows_dir)
        self.kanban_board = "fleet-workflow"  # Single board for all engine runs

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

        # Kahn's algorithm
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
            "--goal",  # goal_mode for autonomous multi-turn execution
            "--goal-max-turns", "10",
            "--priority", "2",
        ]
        if node.model:
            cmd.extend(["--model", node.model])

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            raise RuntimeError(f"Kanban card creation failed: {result.stderr}")

        # Parse card ID from output (format: "Created card <id>")
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

    # ── Execution ──────────────────────────────────────────────

    def execute(self, workflow_name: str, context: dict = None, 
                start_node: str = None, dry_run: bool = False) -> dict:
        """
        Run a workflow to completion. Blocking call — use background=true
        in Hermes terminal for long workflows.

        Returns execution summary: {node_id: final_status, ...}
        """
        workflow = self.load_workflow(workflow_name)
        layers = self.topological_sort(workflow)

        # Handle partial start (resume from a specific node)
        if start_node and start_node in workflow.nodes:
            # Only execute start_node and its descendants
            pass  # TODO: layer slicing for partial execution

        # Initialize state
        states: dict[str, NodeState] = {
            nid: NodeState(node_id=nid) for nid in workflow.nodes
        }
        results = {}

        print(f"Starting workflow: {workflow.name}")
        print(f"  Layers: {len(layers)} | Nodes: {len(workflow.nodes)}")
        if dry_run:
            print("  DRY RUN — no cards will be created")
        print()

        for layer_idx, layer in enumerate(layers):
            print(f"── Layer {layer_idx + 1}/{len(layers)} ──")
            print(f"   Nodes: {', '.join(layer)}")

            if dry_run:
                for nid in layer:
                    node = workflow.nodes[nid]
                    print(f"   [DRY RUN] Would create card for {node.agent}: {node.task[:60]}")
                continue

            # Create cards for all nodes in this layer (parallel)
            for nid in layer:
                node = workflow.nodes[nid]
                state = states[nid]
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
                    print(f"   ✗ {nid} → failed: {e}")
                    results[nid] = "failed"
                    # Don't block the layer — continue with other nodes

            # Monitor completion for this layer
            if not dry_run:
                self._monitor_layer(workflow, layer, states, results)

            print()

        # Summary
        completed = sum(1 for s in states.values() if s.status == "done")
        failed = sum(1 for s in states.values() if s.status in ("failed", "timed_out"))
        print(f"Workflow complete: {completed} done, {failed} failed")

        return results

    def _monitor_layer(self, workflow: Workflow, layer: list[str],
                       states: dict[str, NodeState], results: dict,
                       poll_interval: int = 15, max_polls: int = 120):
        """Poll kanban until all nodes in a layer complete or time out."""
        pending = set(layer)
        polls = 0

        while pending and polls < max_polls:
            time.sleep(poll_interval)
            polls += 1

            for nid in list(pending):
                state = states[nid]
                if state.status != "running":
                    pending.discard(nid)
                    continue

                node = workflow.nodes[nid]
                elapsed = (datetime.now(timezone.utc) - 
                          datetime.fromisoformat(state.started_at)).total_seconds()

                # Timeout check
                if elapsed > node.timeout_minutes * 60:
                    state.status = "timed_out"
                    state.error = f"Exceeded {node.timeout_minutes}min timeout"
                    results[nid] = "timed_out"
                    print(f"   ⏰ {nid} timed out after {elapsed:.0f}s")
                    pending.discard(nid)
                    continue

                # Check kanban card status
                try:
                    card = self.get_card_status(state.kanban_card_id)
                    card_status = card.get("status", card.get("column", "unknown"))

                    if card_status in ("done", "completed", "Done"):
                        state.status = "done"
                        state.completed_at = datetime.now(timezone.utc).isoformat()
                        results[nid] = "done"
                        print(f"   ✓ {nid} completed ({elapsed:.0f}s)")
                        pending.discard(nid)

                    elif card_status in ("blocked", "Blocked"):
                        state.status = "blocked"
                        state.error = "Card blocked — requires Sherlock intervention"
                        results[nid] = "blocked"
                        print(f"   🚫 {nid} BLOCKED — escalate to Sherlock")
                        pending.discard(nid)

                except Exception as e:
                    # Card query failed — don't fail the node, just keep polling
                    pass

        # Anything still pending after max_polls
        for nid in list(pending):
            state = states[nid]
            state.status = "timed_out"
            state.error = f"Still running after {max_polls * poll_interval}s"
            results[nid] = "timed_out"
            pending.discard(nid)

    # ── Status query ────────────────────────────────────────────

    def status(self, workflow_name: str = None) -> dict:
        """Query current state of running workflows."""
        # TODO: track active runs in a state file
        return {"status": "No active runs (state tracking not yet implemented)"}


# ── CLI ─────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Workflow Execution Engine")
    sub = parser.add_subparsers(dest="command")

    start = sub.add_parser("start", help="Start a workflow")
    start.add_argument("workflow", help="Workflow name (YAML file in docs/fleet-pipelines/)")
    start.add_argument("--context", "-c", nargs="*", help="Key=value context pairs")
    start.add_argument("--dry-run", action="store_true", help="Print plan without executing")
    start.add_argument("--node", help="Start from a specific node (partial execution)")

    sub.add_parser("status", help="Query running workflow state")

    sub.add_parser("list", help="List available workflow definitions")

    args = parser.parse_args()
    engine = WorkflowEngine()

    if args.command == "start":
        context = {}
        if args.context:
            for pair in args.context:
                k, v = pair.split("=", 1)
                context[k] = v
        engine.execute(args.workflow, context=context, start_node=args.node, 
                      dry_run=args.dry_run)

    elif args.command == "status":
        print(json.dumps(engine.status(), indent=2))

    elif args.command == "list":
        for f in sorted(engine.workflows_dir.glob("*.yaml")):
            print(f"  {f.stem}")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
