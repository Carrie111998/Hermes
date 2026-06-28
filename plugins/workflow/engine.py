"""
Workflow Execution Engine — mechanical DAG runner for multi-agent pipelines.

Reads a workflow YAML, topologically sorts agent nodes, creates kanban cards
for ready nodes, monitors completion, and advances the graph. Supports revision
loops via the LOOP:<target> convention in block reasons.

Usage:
    python -m plugins.workflow.engine start ideation --context pr=123
    python -m plugins.workflow.engine validate ideation
    python -m plugins.workflow.engine list

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


def _hermes_binary() -> str:
    """Resolve the ``hermes`` CLI binary from the venv.

    The engine spawns ``hermes kanban create/show`` subprocesses to
    interact with the fleet-wide kanban board.  When called from inside a
    Hermes agent process (e.g. via MCP tools), ``hermes`` is not on PATH
    — it only lives in the venv's ``bin/`` directory alongside
    ``sys.executable``.

    Resolution order:
      1. ``sys.executable``'s parent — works when invoked via the venv's
         own python (agent-in-process invocation).
      2. ``sys.prefix/bin/hermes`` — works when invoked via ``python3 -m``
         with a different ``sys.executable`` (CLI invocation outside the
         venv); ``sys.prefix`` always points to the venv root.
      3. Bare ``"hermes"`` — last resort for dev environments with PATH.

    Returns the absolute path to the ``hermes`` binary, which is always
    the correct target for ``subprocess.run`` regardless of how the
    engine is invoked (CLI or in-process).
    """
    candidate = Path(sys.executable).parent / "hermes"
    if candidate.is_file():
        return str(candidate)
    # ``sys.prefix`` is set to the venv root by the activated
    # environment, regardless of which python binary executed this
    # module.  This catches ``python3 -m plugins.workflow.engine``
    # invocations where ``sys.executable`` is the system python.
    venv_candidate = Path(sys.prefix) / "bin" / "hermes"
    if venv_candidate.is_file():
        return str(venv_candidate)
    # Look for the project's .venv next to the module.  This catches
    # cases where neither sys.executable nor sys.prefix points to
    # the project venv (e.g. session agent invoking the engine via
    # ``python3 -m`` with system python outside any activation).
    project_venv = Path(__file__).resolve().parent.parent / ".venv" / "bin" / "hermes"
    if project_venv.is_file():
        return str(project_venv)
    # Fallback: bare name works when PATH is set (CLI invocations)
    return "hermes"
from datetime import datetime, timezone

# ── Data structures ──────────────────────────────────────────────

@dataclass
class WorkflowNode:
    """A single agent task in the DAG.

    A node is either a real agent task (synthetic=False, agent is the
    dispatch target) or a synthetic gate (synthetic=True, no agent).
    Synthetic gates are auto-completed once their dependencies are done —
    no kanban card is created, no agent is dispatched. They exist to
    enforce ordering in the DAG (e.g. a privacy barrier between two
    phases) without adding a no-op task to the board.
    """
    id: str
    agent: Optional[str]          # Agent name (matches kanban worker).
                                  # None for synthetic gate nodes.
    task: str                     # Task description for the kanban card
    depends_on: list[str] = field(default_factory=list)
    timeout_minutes: int = 30
    model: Optional[str] = None   # Optional model override
    channel: str = "debug"        # Where to send notifications
    synthetic: bool = False       # True for gate nodes (no agent, auto-complete)
    phase: Optional[str] = None   # Explicit phase label for {phaseN.X} template
                                  # substitution. When None, the engine auto-
                                  # derives it from the topological layer
                                  # index ("phase0", "phase1", ...) at
                                  # lookup time.
    # ── New fields ──────────────────────────────────────────────
    outputs: list[dict] = field(default_factory=list)
    """"Expected artifact outputs. Each entry:
        path: str — resolved against agent workspace + {run_id}
        required: bool — if True and file missing, validation fails
        schema: str — optional: "json", "markdown", "text"
    """
    fallback_on_timeout: str = "skip"
    """Behavior when this node times out:
        skip  — mark failed, cascade skip downstream (default)
        degraded — mark degraded, downstream proceeds with warning
        retry — re-create the card (up to 3 attempts)
    """
    privacy_gate: bool = False
    """When True, this node's output is excluded from template lookup
       for downstream nodes. Used for premortem isolation — position
       agents should not see the premortem's failure imagination.
    """
    goal_max_turns: Optional[int] = None
    """Per-node goal-mode turn limit. When None (default), the
       kanban CLI's own default (20) is used. Set in YAML to
       constrain deep-research tasks that would otherwise exhaust
       the session before calling kanban_complete, or to tighten
       limits for trivial tasks.
    """
    triage: bool = False
    """When True, the card is created in 'triage' status instead of
       'ready'. The auto-decomposer (or manual decompose) will break
       it into sub-tasks with sibling dependencies. The root card
       keeps its depends_on relationships and promotes to 'ready'
       when all children complete.
    """
    when: str = ""
    """Conditional expression controlling whether this node dispatches.

    Empty string (default) means always run — preserving the existing
    behavior for all workflows.  Non-empty strings are evaluated against
    the workflow state + context at dispatch time; a truthy result means
    the node runs, falsy means it is skipped.

    Supported references inside the expression:
      {node-id}.status, {node-id}.result, {node-id}.error,
      {node-id}.attempts, {node-id}.duration_seconds, {node-id}.error_count
      {context.key}

    Supported operators:
      ==, !=, >, <, >=, <=, contains, starts_with, in, and, or, not
    """

@dataclass
class Workflow:
    """Complete workflow definition."""
    name: str
    description: str = ""
    trigger_events: list[str] = field(default_factory=list)
    nodes: dict[str, WorkflowNode] = field(default_factory=dict)
    run_id: str = ""              # Generated at execute() time
    kanban_board: str = ""         # Per-pipeline board override (empty = engine default)
    scope: str = "project"         # "project" (default) — creates kanban cards per node.
                                    # "global" — in-process only, no cards created.
                                    # Used for maintenance / notification / heartbeat
                                    # workflows that should not pollute project boards.
    single_flight: bool = False     # When True, refuse to start a new run if any
                                    # run for this workflow is already in progress.
                                    # Used to prevent duplicate parallel runs from
                                    # webhook storms or repeated dispatch signals.
                                    # Default False preserves the existing "multiple
                                    # parallel runs allowed" behavior.

@dataclass
class NodeState:
    """Runtime state for a workflow node."""
    node_id: str
    status: str = "pending"       # pending | running | done | failed | blocked | timed_out | revision_needed | degraded
    kanban_card_id: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    attempts: int = 0
    error: Optional[str] = None
    loop_count: int = 0           # Number of revision loops for this verify node
    loop_history: list[str] = field(default_factory=list)  # Full history of LOOP rejections
    result: Optional[str] = None  # Captured card body (output text) populated
                                  # when the node transitions to "done" or
                                  # "revision_needed". Available to
                                  # downstream nodes via {phaseN.X} or
                                  # {node-id} template substitution.
    duration_seconds: Optional[float] = None
    """Wall-clock duration from started_at → completed_at.

    Populated when the node enters a terminal status (done / failed /
    timed_out / blocked / revision_needed). Computed from started_at
    and completed_at; agents don't need to set it manually. Exposed
    via workflow_status for cost / bottleneck analysis.
    """
    error_count: int = 0
    """Cumulative count of failed / timed_out transitions for this node.

    Incremented each time the node enters a failure state. Multiple
    retries on a flaky node will increment this counter — useful for
    spotting nodes that keep failing across runs.
    """
    validation_warnings: list[str] = field(default_factory=list)

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
            workflows_dir = os.environ.get(
                "HERMES_FLEET_PIPELINES",
                ""
            )
            if not workflows_dir:
                # Profile-scoped: $HERMES_HOME/workflows/
                hermes_home = os.environ.get("HERMES_HOME", "")
                if hermes_home:
                    candidate = Path(hermes_home) / "workflows"
                    if candidate.is_dir():
                        workflows_dir = str(candidate)
            if not workflows_dir:
                # Ship defaults: next to the engine module
                workflows_dir = str(
                    Path(__file__).resolve().parent.parent / "docs" / "fleet-pipelines"
                )
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

        # Read role→profile bindings from the top-level `roles:` block.
        # Nodes can use `agent: "{role_name}"` placeholders that resolve to
        # the bound profile at load time. To swap a profile, edit one line
        # in `roles:` and every node that references the role picks it up.
        roles = raw.get("roles", {}) or {}

        workflow = Workflow(
            name=raw.get("name", name),
            description=raw.get("description", ""),
            trigger_events=raw.get("trigger_events", []),
            kanban_board=raw.get("kanban_board", ""),
            scope=raw.get("scope", "project"),
            single_flight=bool(raw.get("single_flight", False)),
        )

        for node_id, node_data in raw.get("nodes", {}).items():
            # Synthetic gate nodes: no agent dispatch, auto-completed when
            # their depends_on are satisfied. Used for privacy barriers
            # and pure ordering (e.g. council-ready gate between the
            # premortem phase and the position phase).
            synthetic = bool(node_data.get("synthetic", False))
            if synthetic:
                # Warn if the author left a redundant agent field —
                # easy to forget to remove when converting an existing
                # node into a synthetic gate.
                if "agent" in node_data:
                    print(f"   ⚠  Node '{node_id}' has synthetic: true "
                          f"with an explicit agent — ignoring agent field")
                agent_value: Optional[str] = None
                # Task is optional for synthetic gates; default to the
                # node id so logs/UI still have something to display.
                task_value = node_data.get("task", f"[synthetic gate] {node_id}")
                # Default to a trivial timeout — synthetic gates
                # auto-complete in the dispatch loop, but we still want
                # a sane value if anyone reads the node later.
                timeout_value = node_data.get("timeout_minutes", 1)
            else:
                agent_value = node_data["agent"]
                # Resolve {role} placeholders against the `roles:` block.
                # Templates that don't match any role key pass through
                # unchanged (the engine surfaces the missing assignee on
                # the resulting kanban card, so the failure is visible).
                if isinstance(agent_value, str) and "{" in agent_value and roles:
                    try:
                        agent_value = agent_value.format(**{
                            k: v for k, v in roles.items()
                            if isinstance(v, (str, int, float))
                        })
                    except (KeyError, ValueError, TypeError):
                        pass
                task_value = node_data["task"]
                timeout_value = node_data.get("timeout_minutes", 30)

            # Parse new-style fields
            outputs_raw = node_data.get("outputs", [])
            if isinstance(outputs_raw, list):
                for o in outputs_raw:
                    if isinstance(o, str):
                        # Shorthand: just a path string
                        outputs_raw = [{"path": o, "required": True, "schema": "text"}]
                        break

            fallback_raw = node_data.get("fallback_on_timeout", "skip")
            if fallback_raw not in ("skip", "degraded", "retry"):
                print(f"   ⚠  Node '{node_id}' has invalid "
                      f"fallback_on_timeout='{fallback_raw}' — defaulting to 'skip'")
                fallback_raw = "skip"

            workflow.nodes[node_id] = WorkflowNode(
                id=node_id,
                agent=agent_value,
                task=task_value,
                depends_on=node_data.get("depends_on", []),
                timeout_minutes=timeout_value,
                model=node_data.get("model"),
                channel=node_data.get("channel", "debug"),
                synthetic=synthetic,
                # `phase:` is optional in YAML. When omitted, the engine
                # auto-derives a phase label from the topological layer
                # index at template-substitution time (e.g. "phase0",
                # "phase1", "phase2"). Setting an explicit phase is
                # useful when the author wants sub-phases ("phase2a",
                # "phase2b") that don't map 1:1 to a single layer.
                phase=node_data.get("phase"),
                outputs=outputs_raw,
                fallback_on_timeout=fallback_raw,
                privacy_gate=bool(node_data.get("privacy_gate", False)),
                goal_max_turns=node_data.get("goal_max_turns"),
                triage=bool(node_data.get("triage", False)),
                when=node_data.get("when", ""),
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

    def create_kanban_card(self, node: WorkflowNode, context: dict = None,
                            *, workflow: Optional["Workflow"] = None,
                            states: Optional[dict] = None,
                            layers: Optional[list] = None) -> str:
        """Create a kanban card for a workflow node. Returns card ID.

        Refuses to create a card for a synthetic gate node — those are
        auto-completed by the executor and never reach this function in
        the normal flow. The explicit guard exists so a future caller
        that forgets the check gets a clear error rather than a
        confusing subprocess failure on the None agent.

        When ``workflow``, ``states``, and ``layers`` are provided, the
        engine runs the B2 template-substitution pass on ``node.task``
        before posting. The resolved text becomes the kanban card body.
        The legacy ``\\n\\nContext: {json}`` footer is appended
        after substitution so it does not get treated as a template.

        The ``workflow``/``states``/``layers`` keyword args are
        optional for backward compatibility with direct callers (e.g.
        the synthetic-node guard test) that invoke this method without
        a running workflow. Callers that omit them get the
        pre-substitution behavior — context footer only, no
        ``{namespace.field}`` resolution.
        """
        if node.synthetic:
            raise ValueError(
                f"Refusing to create a kanban card for synthetic gate "
                f"node '{node.id}' — synthetic nodes are auto-completed "
                f"by the executor and do not dispatch."
            )

        if workflow is not None and states is not None and layers is not None:
            # Full B2 path: resolve {namespace.field} and {bare}
            # references, then append the legacy Context footer. The
            # footer goes after substitution because the JSON literal
            # contains its own braces that the resolver would otherwise
            # chew on.
            task_with_context = self._build_task_body(
                node, workflow, states, layers, context
            )
        else:
            # Backward-compat path: direct callers that pass only
            # (node, context). Preserves the pre-B2 footer-only
            # behavior exactly. Used by tests like
            # test_create_kanban_card_refuses_synthetic that never
            # reach the substitution step.
            task_with_context = node.task
            if context:
                task_with_context += f"\n\nContext: {json.dumps(context)}"

        title = f"[{node.id}] {node.agent}: {node.task[:60]}"
        cmd = [
            _hermes_binary(), "kanban", "create",
            title,
            "--tenant", self.kanban_board,
            "--body", task_with_context,
            "--assignee", node.agent,
            "--goal",
            "--priority", "2",
        ]
        if node.goal_max_turns is not None:
            cmd.extend(["--goal-max-turns", str(node.goal_max_turns)])
        # Pass --max-runtime so the heartbeat sweep uses the node's
        # timeout as the threshold, not the 30-minute default.
        if node.timeout_minutes is not None:
            cmd.extend(["--max-runtime", str(node.timeout_minutes * 60)])
        if node.model:
            cmd.extend(["--model", node.model])
        if node.triage:
            cmd.append("--triage")
        # Real agents start in their own persistent workspace so files
        # they write (e.g. council artifacts) survive card completion.
        # Synthetic/gate nodes have no agent and get the default scratch.
        if not node.synthetic and node.agent:
            # Cannot use Path.home() — it resolves to the profile's fake
            # home dir when the engine runs inside a Hermes profile.
            # HERMES_HOME always points to the real profile root
            # (e.g. /home/ubuntu/.hermes/profiles/sherlock), so
            # its parent is the profiles directory.
            hermes_home = os.environ.get("HERMES_HOME")
            if hermes_home:
                profiles_root = Path(hermes_home).parent
            else:
                profiles_root = Path.home() / ".hermes" / "profiles"
            agent_ws = profiles_root / node.agent / "workspace"
            cmd.extend(["--workspace", f"dir:{agent_ws}"])

        # When kanban_board is set (e.g. "council"), inject the env var
        # so the CLI resolves to the right board file.
        run_env = dict(os.environ)
        if self.kanban_board:
            run_env["HERMES_KANBAN_BOARD"] = self.kanban_board
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=run_env)
        if result.returncode != 0:
            raise RuntimeError(f"Kanban card creation failed: {result.stderr}")

        # Card ID can come from either a "Created card <id>" line OR
        # a JSON object (--json mode); try JSON first since it's structured.
        out = result.stdout.strip()
        try:
            card_obj = json.loads(out)
            if "id" in card_obj:
                return card_obj["id"]
        except (json.JSONDecodeError, ValueError):
            pass
        match = re.match(r'Created\s+(t_\S+)', out)
        if match:
            return match.group(1)
        # Fallback: try last token (fragile but works for legacy output)
        card_id = out.split()[-1] if out else ""
        return card_id

    def dispatch_node(self, state: NodeState, node: WorkflowNode, context: dict,
                       workflow: "Workflow", states: dict, layers: list) -> Optional[str]:
        """Dispatch a node to kanban, or mark it done in-process.

        For ``scope: global`` workflows (maintenance, notifications, heartbeat)
        no kanban card is created — the node is marked ``done`` with a
        sentinel ``result`` and ``None`` is returned. Callers should treat
        ``None`` as "in-process, no card to monitor" and skip heartbeat /
        monitoring for this node.

        For ``scope: project`` (default), this delegates straight to
        :meth:`create_kanban_card` and returns the card ID.
        """
        if workflow is not None and getattr(workflow, "scope", "project") == "global":
            state.status = "done"
            state.completed_at = datetime.now(timezone.utc).isoformat()
            state.result = "[in-process, scope: global]"
            return None
        return self.create_kanban_card(
            node, context,
            workflow=workflow, states=states, layers=layers,
        )



    # Terminal statuses for which we record telemetry. Anything else
    # (running, pending, blocked, revision_needed, degraded) is mid-flight.
    _TELEMETRY_TERMINAL_STATUSES = frozenset({
        "done", "failed", "timed_out",
    })
    """Statuses that mark a node as finished for telemetry purposes.

    "blocked" and "revision_needed" are mid-flight (the engine may
    rerun the node), so we don't count them as terminal for duration
    tracking. "degraded" is also mid-flight (downstream proceeds).
    """

    # Tracks which (state-id, status) pairs have already had telemetry
    # recorded, so repeated _save_state calls don't double-count
    # error_count or recompute duration. Reset per engine instance.
    _telemetry_recorded: "set[tuple[int, str]]" = None  # lazy-init in __init__

    def _record_node_completion(self, state: NodeState) -> None:
        """Capture telemetry when a node enters a terminal status.

        Computes ``duration_seconds`` from ``started_at`` and
        ``completed_at``, and increments ``error_count`` for failure
        outcomes. Idempotent — safe to call multiple times via
        ``_save_state`` without double-counting, using a per-node-id
        dedup set.
        """
        if state.status not in self._TELEMETRY_TERMINAL_STATUSES:
            return
        if self._telemetry_recorded is None:
            self._telemetry_recorded = set()
        dedup_key = (id(state), state.status)
        if dedup_key in self._telemetry_recorded:
            return
        self._telemetry_recorded.add(dedup_key)
        if state.duration_seconds is None and state.started_at and state.completed_at:
            try:
                start = datetime.fromisoformat(state.started_at)
                end = datetime.fromisoformat(state.completed_at)
                state.duration_seconds = (end - start).total_seconds()
            except (ValueError, TypeError):
                pass
        if state.status in ("failed", "timed_out"):
            state.error_count += 1

    # State files older than this are considered stale — the engine
    # crashed or was killed mid-run and the state is no longer accurate.
    # Single-flight checks ignore stale state files so a single bad
    # crash doesn't permanently block a workflow from running again.
    ACTIVE_RUN_STALE_SECONDS = 3600

    # How many historical state files to retain per workflow. Older
    # state files are pruned at the end of each save so disk usage
    # doesn't grow unbounded with long-running fleet usage. Set to
    # a low default to keep telemetry disk-cheap; raise via
    # ``STATE_RETENTION_PER_WORKFLOW`` env var if more history needed.
    STATE_RETENTION_PER_WORKFLOW = 20

    def _prune_old_runs(self, keep: int = None) -> int:
        """Delete oldest state files beyond ``keep`` per workflow.

        Walks the state directory, groups files by workflow name,
        sorts each group by mtime, and unlinks everything past the
        ``keep`` threshold. Returns the number of files pruned.

        Called automatically at the end of ``_save_state`` so retention
        is enforced without callers needing to remember. Safe to call
        when STATE_DIR doesn't exist yet (returns 0).
        """
        if self.STATE_DIR is None or not self.STATE_DIR.exists():
            return 0
        if keep is None:
            keep = self.STATE_RETENTION_PER_WORKFLOW
        # Group state files by workflow name (strip "_<run_id>_state.json"
        # or "_state.json" suffix).
        groups: dict[str, list[Path]] = defaultdict(list)
        for path in self.STATE_DIR.glob("*_state.json"):
            stem = path.stem  # e.g. "council_20260101T120000_state"
            # Strip "_state" suffix and split off the trailing timestamp.
            if stem.endswith("_state"):
                stem = stem[:-len("_state")]
            # If the stem still has an underscore-separated timestamp suffix
            # (looks like YYYYMMDDTHHMMSS), strip it for grouping.
            parts = stem.rsplit("_", 1)
            workflow_name = parts[0] if len(parts) > 1 else stem
            groups[workflow_name].append(path)
        pruned = 0
        for wf_name, paths in groups.items():
            # Sort by mtime ascending; prune the oldest.
            paths.sort(key=lambda p: p.stat().st_mtime)
            for old in paths[:-keep] if keep > 0 else paths:
                try:
                    old.unlink()
                    pruned += 1
                except OSError:
                    pass
        return pruned

    def _has_active_run(self, workflow_name: str) -> bool:
        """Return True if any in-progress run exists for ``workflow_name``.

        Used to enforce single-flight semantics: workflows with
        ``single_flight: true`` refuse to start a new run when another
        run is already in progress. A run is considered active when its
        state file was updated within ``ACTIVE_RUN_STALE_SECONDS`` and
        contains at least one node in ``running``, ``pending``, or
        ``blocked`` status.

        Returns False if no state file exists, all state files are stale,
        or all nodes in the active state file are in terminal status.
        """
        for path in sorted(self.STATE_DIR.glob(f"{workflow_name}_*_state.json")):
            try:
                with open(path) as f:
                    state = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue
            updated_at = state.get("updated_at")
            if updated_at:
                try:
                    age = (
                        datetime.now(timezone.utc)
                        - datetime.fromisoformat(updated_at)
                    ).total_seconds()
                    if age > self.ACTIVE_RUN_STALE_SECONDS:
                        continue
                except (ValueError, TypeError):
                    pass
            states = state.get("states", {})
            if any(
                s.get("status") in ("running", "pending", "blocked")
                for s in states.values()
            ):
                return True
        return False

    def get_card_status(self, card_id: str) -> dict:
        """Query a kanban card's current state.

        The CLI returns ``{task: {..., status, body, ...}, latest_summary: ...}``.
        Unwrap the ``task`` key so callers see ``status``, ``body``,
        etc. at the top level — matching what ``get_card_body`` and
        the monitor loop expect.  Also merge ``latest_summary`` (the
        agent's completion summary) into the returned dict so
        ``get_card_body`` can prefer it over the raw task body.
        """
        import os as _os
        _env = dict(_os.environ)
        if self.kanban_board:
            _env["HERMES_KANBAN_BOARD"] = self.kanban_board
        result = subprocess.run(
            [_hermes_binary(), "kanban", "show", card_id, "--json"],
            capture_output=True, text=True, timeout=15,
            env=_env,
        )
        if result.returncode != 0:
            return {"status": "unknown", "error": result.stderr}
        try:
            raw = json.loads(result.stdout)
            # Unwrap the task envelope so status/body are at top level.
            if "task" in raw and isinstance(raw["task"], dict):
                card = dict(raw["task"])
                # Merge the agent's completion summary so
                # get_card_body can prefer it over the input prompt.
                if "latest_summary" in raw and raw["latest_summary"]:
                    card["latest_summary"] = raw["latest_summary"]
                return card
            return raw
        except (json.JSONDecodeError, ValueError):
            return {"status": "unknown"}

    def get_card_body(self, card_id: str) -> str:
        """Get the agent's output from a completed kanban card.

        Resolution order:
          1. ``latest_summary`` — the agent's completion summary
             (set via ``kanban_complete(summary=...)``).  This is the
             agent's actual output, not the input prompt.
          2. ``result`` — legacy field (set via
             ``kanban_complete(result=...)``).
          3. ``body`` — the task description / input prompt.  This is
             a fallback; in normal operation it should NOT be the
             agent's output.
        """
        card = self.get_card_status(card_id)
        return card.get("latest_summary",
                        card.get("result",
                                 card.get("body",
                                          card.get("reason",
                                                   card.get("description", "")))))

    # ── Template substitution ───────────────────────────────────
    #
    # When a workflow node is dispatched, its `task` text may contain
    # template references like:
    #
    #     {context.question}            <- value from the -c flag set
    #     {phase1.position-edison}      <- specific upstream node's output
    #     {phase1.all}                  <- concatenation of all phase-1 outputs
    #     {question}                    <- legacy bare form: tries context
    #                                    first, then top-level node ids
    #
    # The engine resolves these before posting the body to kanban. The
    # resolution is a *single substitution pass* applied to the task
    # text — no other transformation is layered on top, so YAML authors
    # can predict the output.
    #
    # Unresolved references are left as literal text in the body. The
    # engine prints a one-line warning to stdout for each so operators
    # notice missing upstream data instead of silently shipping empty
    # fields to agents.

    # Pattern matches {namespace.field} OR {field} (legacy bare form).
    # Namespace allows letters/digits/underscore; field allows the same
    # plus hyphens (so node ids like "position-edison" match as a field
    # under the "phase1" namespace). Bare form is a single token.
    _TEMPLATE_RE = re.compile(
        r"\{(?P<ns>[A-Za-z_][A-Za-z0-9_]*)\.(?P<field>[A-Za-z0-9_\-]+)\}"
        r"|\{(?P<bare>[A-Za-z_][A-Za-z0-9_\-]*)\}"
    )

    def _build_template_lookup(self, workflow: "Workflow",
                                states: dict[str, "NodeState"],
                                layers: list[list[str]],
                                context: Optional[dict] = None) -> dict:
        """Build the substitution lookup dict for downstream nodes.

        Returns a dict with three top-level flavors:

        1. ``"context"`` — the start-time context dict (from ``-c`` flags).
           This is what ``{context.X}`` and legacy ``{X}`` look up first.

        2. ``"phaseN"`` (or the explicit ``phase:`` label) — a sub-dict
           containing:
             - one key per completed node in that phase, mapped to the
               captured card body
             - an ``"all"`` key whose value is the concatenation of every
               completed node's result in that phase, in a stable order

        3. Each completed node id is also exposed at the top level. This
           is purely for legacy support — the original council.yaml uses
           un-prefixed names like ``{position-edison-output}``. New
           pipelines should prefer ``{phase1.position-edison}``.

        The lookup is keyed by *node name + phase label*, not by Kanban
        card id. The card body is captured into ``state.result`` when a
        node completes (see ``_monitor_layer``).
        """
        lookup: dict = {"context": dict(context or {})}
        run_id = workflow.run_id or "no-run-id"

        # Add {run_id} and {date} so YAML authors can reference them in
        # output paths and task prompts (e.g. "council/{date}/{run_id}/premortem.json").
        if workflow.run_id:
            lookup["run_id"] = workflow.run_id
            lookup["context"]["run_id"] = workflow.run_id
            # date = YYYY-MM-DD derived from the run_id timestamp
            if "-" in workflow.run_id:
                ts_part = workflow.run_id.split("-", 1)[1]  # "20260610-214500"
                date_part = ts_part.split("-")[0]            # "20260610"
                lookup["date"] = date_part
                lookup["context"]["date"] = date_part

        # Pre-compute the phase label for each node. Authors can set
        # `phase:` explicitly in YAML; otherwise we default to the
        # layer index ("phase0", "phase1", ...). We do the
        # default-derivation here, at lookup time, so the loader stays
        # simple and there's no chance of the derived phase drifting
        # from the actual topological layout.
        node_phase: dict[str, str] = {}
        for layer_idx, layer in enumerate(layers):
            for nid in layer:
                node = workflow.nodes[nid]
                node_phase[nid] = node.phase or f"phase{layer_idx}"

        # Collect completed nodes' outputs, grouped by phase.
        # "Completed" means the node has a captured result — i.e. it
        # transitioned to done / revision_needed and we successfully
        # pulled its body off the kanban card. Failed/timed-out nodes
        # are intentionally excluded so the agent prompt doesn't
        # silently embed a half-finished output; the author should
        # gate the pipeline on success via depends_on.
        #
        # Privacy gates: nodes with privacy_gate=True are excluded from
        # the template lookup entirely. Their output is never visible to
        # downstream agents — not even as "{phase0.premortem-nikola}".
        # This prevents e.g. the premortem's failure imagination from
        # biasing position agents. The privacy is enforced here, at the
        # substitution layer, not by the prompt.
        by_phase: dict[str, dict[str, str]] = defaultdict(dict)
        states_with_result = 0
        states_without_result = 0
        privacy_dropped = 0
        orphan_dropped = 0
        for nid, st in states.items():
            if st.result is None:
                states_without_result += 1
                continue
            states_with_result += 1
            node = workflow.nodes[nid]
            # Privacy gate: skip nodes whose output should not leak
            # to downstream agents (e.g. premortem → position barrier).
            if node.privacy_gate:
                privacy_dropped += 1
                continue
            phase_label = node_phase.get(nid)
            if phase_label is None:
                orphan_dropped += 1
                # Node isn't in the topological layout (shouldn't happen
                # in practice). Skip rather than crash — the engine
                # tolerates orphan state.
                continue
            by_phase[phase_label][nid] = st.result
            # Also expose at top level for legacy {node-id} lookups.
            # Namespace collisions (e.g. a node literally named
            # "context" or "phase1") would clobber the namespace keys
            # here. We don't try to defend against that — the YAML
            # author is responsible for choosing unambiguous ids, and
            # the docstring above calls out the convention.
            lookup[nid] = st.result

        for phase_label, members in by_phase.items():
            # Stable concatenation order: follow the topological layer
            # order, not the dict iteration order. Within a single
            # layer, fall back to the workflow.nodes dict order which
            # matches YAML declaration order.
            ordered = []
            for layer in layers:
                for nid in layer:
                    if nid in members:
                        ordered.append((nid, members[nid]))
            members_ordered = dict(ordered)
            members_ordered["all"] = "\n\n---\n\n".join(
                f"[{nid}]\n{body}" for nid, body in ordered
            )
            lookup[phase_label] = members_ordered

        # ── Diagnostics snapshot 📋 ──
        diag = {
            "run_id": run_id,
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": "lookup_built",
            "total_states": len(states),
            "states_with_result": states_with_result,
            "states_without_result": states_without_result,
            "privacy_dropped": privacy_dropped,
            "orphan_dropped": orphan_dropped,
            "namespace_labels": list(by_phase.keys()),
            "namespace_node_counts": {k: [pk for pk in v.keys() if pk != "all"] for k, v in by_phase.items()},
            "top_level_lookup_keys": [k for k in lookup.keys() if k not in ("context",)],
        }
        import sys as _sys
        # Use a well-known prefix so log grepping is reliable
        print(f"\n📋 WFE:DIAG {json.dumps(diag, default=str)}", file=_sys.stderr)

        return lookup

    def _resolve_template(self, text: str, lookup: dict) -> str:
        """Apply a single substitution pass over ``text``.

        Resolution rules, in order:
          1. ``{namespace.field}`` — ``lookup[namespace][field]`` if both
             keys exist. The namespace must be a string of letters /
             digits / underscores; the field is the same plus hyphens
             (so node ids like ``position-edison`` are reachable as a
             field under the ``phase1`` namespace).
          2. ``{field}`` (bare, no dot) — try ``lookup["context"][field]``
             first, then ``lookup[field]``. This is the legacy form
             (e.g. ``{question}`` in the original council.yaml) and
             makes the existing pipelines work without touching them.

        Unresolved references are left in the output unchanged. A one-
        line warning is printed to stdout per unresolved reference so
        operators notice missing upstream data instead of silently
        shipping a card with literal ``{...}`` braces.
        """

        def _replace(match: re.Match) -> str:
            if match.group("ns") is not None:
                ns, field = match.group("ns"), match.group("field")
                ns_val = lookup.get(ns)
                if isinstance(ns_val, dict) and field in ns_val:
                    return str(ns_val[field])
                # ── Resolution failure diagnostics ──
                ns_type = type(ns_val).__name__ if ns_val is not None else "MISSING"
                ns_keys = sorted(ns_val.keys())[:20] if isinstance(ns_val, dict) else "N/A"
                ctx_keys = sorted(lookup.get("context", {}).keys())[:10]
                top_keys = sorted(k for k in lookup.keys() if k not in ("context",))[:10]
                diag = {
                    "run_id": lookup.get("run_id", lookup.get("context", {}).get("run_id", "unknown")),
                    "event": "unresolved_namespace",
                    "template_ref": f"{{{ns}.{field}}}",
                    "ns_key": ns,
                    "field_key": field,
                    "lookup_has_ns": ns in lookup,
                    "ns_type": ns_type,
                    "ns_keys": ns_keys,
                    "ns_is_dict_and_has_field": isinstance(ns_val, dict) and field in ns_val,
                    "context_keys": ctx_keys,
                    "top_level_keys": top_keys,
                    "known_namespaces": sorted(k for k in lookup.keys() if isinstance(lookup.get(k), dict) and k != "context"),
                }
                import sys as _sys
                print(f"   📋 WFE:DIAG {json.dumps(diag, default=str)}", file=_sys.stderr)
                print(
                    f"   ⚠  Unresolved template {{{ns}.{field}}} "
                    f"— leaving literal"
                )
                return match.group(0)
            # Bare form — legacy compat
            bare = match.group("bare")
            ctx = lookup.get("context")
            if isinstance(ctx, dict) and bare in ctx:
                return str(ctx[bare])
            if bare in lookup:
                return str(lookup[bare])
            print(
                f"   ⚠  Unresolved template {{{bare}}} "
                f"— leaving literal"
            )
            return match.group(0)

        return self._TEMPLATE_RE.sub(_replace, text)

    def _build_task_body(self, node: WorkflowNode, workflow: "Workflow",
                          states: dict[str, "NodeState"],
                          layers: list[list[str]],
                          context: Optional[dict] = None) -> str:
        """Compose the final card body for a workflow node.

        Steps:
          1. Resolve ``{namespace.field}`` and ``{bare}`` template
             references in ``node.task`` against upstream nodes' results
             and the start-time context.
          2. Append the legacy ``\n\nContext: {json}`` footer. This is
             preserved verbatim from the pre-substitution behavior so
             the kanban card still carries the raw context dict for
             agents that prefer to read it explicitly.

        The Context footer is intentionally appended *after* template
        resolution so it doesn't get accidentally treated as a template
        (the value is JSON, which contains its own braces).
        """
        lookup = self._build_template_lookup(
            workflow, states, layers, context
        )
        resolved_task = self._resolve_template(node.task, lookup)

        # ── Post-resolution diagnostics ──
        # Count how many template variables survived unresolved
        unresolved = self._TEMPLATE_RE.findall(resolved_task)
        unresolved_formatted = []
        for ns, field, bare in unresolved:
            if ns:
                unresolved_formatted.append(f"{{{ns}.{field}}}")
            else:
                unresolved_formatted.append(f"{{{bare}}}")
        if unresolved_formatted:
            import sys as _sys
            diag = {
                "run_id": lookup.get("run_id", "unknown"),
                "event": "post_resolution",
                "node_id": node.id,
                "agent": node.agent,
                "unresolved_count": len(unresolved_formatted),
                "unresolved_refs": unresolved_formatted,
            }
            print(f"   📋 WFE:DIAG {json.dumps(diag, default=str)}", file=_sys.stderr)

        if context:
            # Preserve the pre-B2 footer. It contains braces (JSON
            # dict literal) that would otherwise be eaten by the
            # template resolver if we appended it first.
            return resolved_task + f"\n\nContext: {json.dumps(context)}"
        return resolved_task

    # ── State persistence ──────────────────────────────────────

    def _state_path(self, workflow_name: str, run_id: str = None) -> Path:
        if run_id:
            return self.STATE_DIR / f"{workflow_name}_{run_id}_state.json"
        return self.STATE_DIR / f"{workflow_name}_state.json"

    def _save_state(self, workflow_name: str, states: dict, results: dict,
                    current_layer: int, layers: list[list[str]],
                    run_id: str = None):
        """Persist engine state for crash recovery."""
        # Telemetry: capture duration_seconds + error_count for any node
        # that has reached a terminal status but hasn't been recorded yet.
        # Idempotent — running _record_node_completion on already-recorded
        # states is a no-op (duration_seconds check guards).
        for node_state in states.values():
            self._record_node_completion(node_state)
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
                # Round-trip the captured output so a resume can still
                # substitute {phaseN.X} / {node-id} references for
                # downstream nodes that haven't been dispatched yet.
                "result": s.result,
                # Telemetry: populated by _record_node_completion before
                # this save runs. Surface to workflow_status for cost /
                # bottleneck analysis.
                "duration_seconds": s.duration_seconds,
                "error_count": s.error_count,
            } for nid, s in states.items()},
            "results": results,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        with open(self._state_path(workflow_name, run_id), "w") as f:
            json.dump(state, f, indent=2)
        # Retention: prune state files beyond STATE_RETENTION_PER_WORKFLOW
        # so disk usage stays bounded. No-op if nothing to prune.
        self._prune_old_runs()

    def _load_state(self, workflow_name: str, run_id: str = None) -> Optional[dict]:
        """Load persisted state if it exists."""
        path = self._state_path(workflow_name, run_id)
        if not path.exists():
            return None
        with open(path) as f:
            return json.load(f)

    def _clear_state(self, workflow_name: str, run_id: str = None):
        """Remove state file after successful completion."""
        path = self._state_path(workflow_name, run_id)
        if path.exists():
            path.unlink()

    def _find_latest_state(self, workflow_name: str) -> Optional[dict]:
        """Find the most recent state file for a workflow (supports parallel runs)."""
        # Prefer run_id-tagged files
        tagged = sorted(self.STATE_DIR.glob(f"{workflow_name}_*_state.json"))
        if tagged:
            with open(tagged[-1]) as f:
                return json.load(f)
        # Fall back to legacy untagged file
        legacy = self.STATE_DIR / f"{workflow_name}_state.json"
        if legacy.exists():
            with open(legacy) as f:
                return json.load(f)
        return None

    # ── Output validation ──────────────────────────────────────

    def _validate_outputs(self, node: WorkflowNode,
                          state: NodeState) -> list[str]:
        """Check expected artifact outputs for a completed node.

        Returns a list of warning strings. Empty list = all checks pass.
        Checks are best-effort — a missing file is a warning, not a
        failure. The engine logs the warning and continues.

        For each output entry:
          1. Resolve {run_id} in the path if the node belongs to a
             workflow with a run_id.
          2. Stat the resolved path in the agent's workspace (same
             dir: logic as create_kanban_card).
          3. If required=True and file doesn't exist, add a warning.
          4. If schema is set, do a basic content check.
        """
        warnings = []
        if not node.outputs:
            return warnings

        # Resolve agent workspace path (same logic as create_kanban_card)
        if node.agent:
            hermes_home = os.environ.get("HERMES_HOME")
            if hermes_home:
                profiles_root = Path(hermes_home).parent
            else:
                profiles_root = Path.home() / ".hermes" / "profiles"
            agent_ws = profiles_root / node.agent / "workspace"
        else:
            return warnings  # No agent = no workspace to check

        for entry in node.outputs:
            if isinstance(entry, str):
                raw_path = entry
                required = True
                schema = None
            else:
                raw_path = entry.get("path", "")
                required = entry.get("required", False)
                schema = entry.get("schema")

            if not raw_path:
                continue

            # Resolve {run_id} and {date} in the path using the template lookup
            resolved = raw_path
            if lookup:
                resolved = resolved.replace("{run_id}", lookup.get("run_id", "run"))
                resolved = resolved.replace("{date}", lookup.get("date", "unknown"))
            else:
                resolved = resolved.replace("{run_id}", state.node_id.split("_")[0]
                                            if "_" in state.node_id else "run")

            full_path = agent_ws / resolved
            if not full_path.exists():
                if required:
                    msg = f"Expected output not found: {resolved}"
                    print(f"   ⚠  [{node.id}] {msg}")
                    warnings.append(msg)
                continue

            # Basic schema check for JSON files
            if schema == "json":
                try:
                    data = json.loads(full_path.read_text())
                    if not data:
                        warnings.append(f"Output {resolved} is empty JSON")
                        print(f"   ⚠  [{node.id}] Output {resolved} is empty JSON")
                except (json.JSONDecodeError, ValueError) as e:
                    msg = f"Output {resolved} failed JSON validation: {e}"
                    warnings.append(msg)
                    print(f"   ⚠  [{node.id}] {msg}")

        return warnings

    # ── Auxiliary analyst (LLM-backed, best-effort) ──────────────

    def _try_escalation_analysis(self, workflow: Workflow,
                                  verify_nid: str, verify_state: NodeState,
                                  context: dict = None):
        """Try LLM analysis of a deadlocked revision loop. Best-effort —
        failure is silent; the engine continues with mechanical escalation."""
        try:
            from plugins.workflow.analyst import analyze_escalation
        except Exception:
            return  # Auxiliary module not available

        # Build loop history from the full list of LOOP rejections
        if verify_state.loop_history:
            loop_history = "\n".join(verify_state.loop_history)
        else:
            loop_history = verify_state.error or "No loop history available"
        project = (context or {}).get("project", "unknown")

        outcome = analyze_escalation(
            project=project,
            gate=verify_nid,
            verify_node=verify_nid,
            loop_history=loop_history,
        )

        if outcome.success and outcome.result:
            summary = outcome.result.get("summary", "")
            sticking = outcome.result.get("sticking_point", "")
            actions = outcome.result.get("suggested_actions", [])
            escalation = outcome.result.get("recommended_escalation", "sherlock_can_resolve")

            print(f"   🧠 Escalation analysis: {summary}")
            if sticking:
                print(f"      Sticking point: {sticking}")
            for i, action in enumerate(actions[:3], 1):
                print(f"      Option {i}: {action}")
            if escalation == "needs_randy":
                print(f"   ⚠  Analyst recommends Randy involvement")
        else:
            print(f"   ⚠  Escalation analysis unavailable — "
                  f"Sherlock must review manually")

    def _try_failure_analysis(self, node: WorkflowNode, state: NodeState,
                               elapsed_sec: float):
        """Try LLM diagnosis of a node failure. Best-effort — silent on failure."""
        # Synthetic gates don't fail (they auto-complete), so the analyst
        # has nothing useful to say about them. Returning early also
        # avoids a type error passing None to analyze_failure's `agent`
        # argument.
        if node.synthetic:
            return
        try:
            from plugins.workflow.analyst import analyze_failure
        except Exception:
            return

        outcome = analyze_failure(
            node_id=node.id,
            agent=node.agent,
            task=node.task[:500],
            timeout_minutes=node.timeout_minutes,
            elapsed=f"{elapsed_sec:.0f}s",
            error=state.error or "No error details",
        )

        if outcome.success and outcome.result:
            cause = outcome.result.get("likely_cause", "unknown")
            category = outcome.result.get("cause_category", "unknown")
            fix = outcome.result.get("suggested_fix", "")
            retry = outcome.result.get("should_retry", False)

            print(f"   🧠 Failure diagnosis [{category}]: {cause}")
            if fix:
                print(f"      Fix: {fix}")
            if retry:
                print(f"      Analyst suggests retry")
        # Silent on failure — mechanical handling continues

    def _try_status_summary(self, workflow_name: str,
                             saved_state: dict) -> Optional[str]:
        """Try LLM summary of pipeline state. Returns summary text or None."""
        try:
            from plugins.workflow.analyst import analyze_status
        except Exception:
            return None

        outcome = analyze_status(
            pipeline_name=workflow_name,
            state_json=json.dumps(saved_state, indent=2)[:8000],
        )

        if outcome.success and outcome.result:
            status = outcome.result.get("overall_status", "unknown")
            alerts = outcome.result.get("attention_needed", [])
            eta = outcome.result.get("estimated_completion", "")

            lines = [f"Pipeline: {workflow_name} | Status: {status}"]
            if eta:
                lines.append(f"Estimated: {eta}")
            for alert in alerts:
                lines.append(f"⚠ {alert}")
            return "\n".join(lines)
        return None

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
            # Synthetic gate nodes have no agent — skip the profile
            # existence check entirely. This is what makes the loader
            # able to accept synthetic nodes without a "real" agent
            # name to validate against.
            if node.synthetic:
                continue
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

        # incomplete_branch rule (adapted from itechmeat/hermes-workflows).
        # Catches non-terminal nodes that rely on the implicit default
        # ``fallback_on_timeout="skip"`` — which silently cascades skip to
        # all downstream nodes. Authors should be intentional about how a
        # node handles timeout / failure when other nodes depend on it.
        # Non-fatal: surfaces as an issue for the caller to act on, but
        # doesn't flip ``valid`` (existing fleet workflows commonly omit
        # the explicit declaration — warn, don't break).
        try:
            yaml_path = self.workflows_dir / f"{workflow_name}.yaml"
            raw_yaml = yaml.safe_load(yaml_path.read_text()) if yaml_path.exists() else None
            nodes_raw = (raw_yaml or {}).get("nodes", {}) if raw_yaml else {}
        except Exception:
            nodes_raw = {}
        for nid, node in workflow.nodes.items():
            if node.synthetic:
                continue
            # Terminal = no downstream consumers; skip the check.
            has_downstream = any(
                nid in other.depends_on
                for other_id, other in workflow.nodes.items()
                if other_id != nid
            )
            if not has_downstream:
                continue
            raw_node = nodes_raw.get(nid, {})
            if "fallback_on_timeout" not in raw_node:
                result["issues"].append(
                    f"Node '{nid}' has downstream consumers but no explicit "
                    f"fallback_on_timeout in YAML. Add one of: skip | degraded "
                    f"| retry to make failure routing intentional, not implicit."
                )

        # ── when: dependency validation (non-fatal) ──
        # Warn if a when: expression references a node that is not in
        # the current node's depends_on list.  This catches missing
        # dependency declarations — the engine would still skip nodes
        # with failed deps, but the when: condition might silently
        # evaluate to a stale value instead of being properly gated.
        for nid, node in workflow.nodes.items():
            if not node.when:
                continue
            # Extract all {node-id.field} references from the when expr
            refs = re.findall(
                r"\{([A-Za-z_][A-Za-z0-9_]*)\.[A-Za-z0-9_\-]+\}",
                node.when,
            )
            for ref_nid in set(refs):
                if ref_nid == "context":
                    continue  # context is always available
                if ref_nid not in workflow.nodes:
                    continue  # unknown node — separate issue
                if ref_nid not in node.depends_on and ref_nid != nid:
                    result["issues"].append(
                        f"Node '{nid}' has when: referencing '{ref_nid}' "
                        f"but does not declare it in depends_on. Add "
                        f"'{ref_nid}' to depends_on or use a context "
                        f"variable instead."
                    )

        return result

    # ── When-condition evaluation ──────────────────────────────

    # Operator tokens recognised in when: expressions
    _WHEN_OPS = frozenset({"==", "!=", ">", "<", ">=", "<=", "contains", "starts_with"})
    _WHEN_KEYWORDS = frozenset({"and", "or", "not", "in", "True", "False", "None"})

    def _resolve_when_references(self, when_expr: str, states: dict,
                                  context: Optional[dict] = None) -> str:
        """Resolve ``{node-id.field}`` and ``{context.key}`` in a
        ``when:`` expression, replacing them with their literal values
        (quoted strings, raw numbers).

        Unresolved references are left as literal text — the evaluator
        will treat unknown identifiers as string values.
        """
        # Build a lookup of node state fields
        when_lookup: dict = {}
        for nid, st in states.items():
            when_lookup[nid] = {
                "status": st.status,
                "result": st.result or "",
                "error": st.error or "",
                "attempts": st.attempts,
                "duration_seconds": st.duration_seconds,
                "error_count": st.error_count,
            }
        when_lookup["context"] = dict(context or {})

        def _replace(match: re.Match) -> str:
            if match.group("ns") is not None:
                ns, field = match.group("ns"), match.group("field")
                ns_val = when_lookup.get(ns)
                if isinstance(ns_val, dict) and field in ns_val:
                    val = ns_val[field]
                    if isinstance(val, str):
                        return f'"{val}"'
                    if val is None:
                        return "None"
                    return str(val)
                return match.group(0)  # Leave unresolved
            bare = match.group("bare")
            ctx = when_lookup.get("context")
            if isinstance(ctx, dict) and bare in ctx:
                val = ctx[bare]
                if isinstance(val, str):
                    return f'"{val}"'
                if val is None:
                    return "None"
                return str(val)
            if bare in when_lookup:
                val = when_lookup[bare]
                if isinstance(val, str):
                    return f'"{val}"'
                if val is None:
                    return "None"
                return str(val)
            return match.group(0)

        return self._TEMPLATE_RE.sub(_replace, when_expr)

    def _tokenize_when(self, expr: str) -> list:
        """Tokenize a resolved when: expression into a flat list of
        (type, value) tuples.  Unquoted identifiers that are not
        reserved keywords are treated as string literals.
        """
        tokens = []
        i = 0
        n = len(expr)
        while i < n:
            # Whitespace — skip
            if expr[i].isspace():
                i += 1
                continue
            # Quoted string
            if expr[i] == '"':
                j = i + 1
                while j < n and expr[j] != '"':
                    if expr[j] == '\\':
                        j += 1
                    j += 1
                tokens.append(("STRING", expr[i + 1:j]))
                i = j + 1
                continue
            # Number (possibly negative)
            if expr[i].isdigit() or (
                expr[i] == '-' and i + 1 < n and expr[i + 1].isdigit()
                and (not tokens or tokens[-1][0] in ("OP", "KEYWORD", "BRACKET", "COMMA"))
            ):
                j = i
                if expr[j] == '-':
                    j += 1
                while j < n and (expr[j].isdigit() or expr[j] == '.'):
                    j += 1
                num_str = expr[i:j]
                tokens.append(
                    ("NUMBER", float(num_str) if '.' in num_str else int(num_str))
                )
                i = j
                continue
            # Brackets
            if expr[i] in '[]()':
                tokens.append(("BRACKET", expr[i]))
                i += 1
                continue
            # Comma
            if expr[i] == ',':
                tokens.append(("COMMA", ","))
                i += 1
                continue
            # Multi-char operators (>=, <=, !=, ==) — check before single-char
            if i + 1 < n and expr[i:i+2] in ("==", "!=", ">=", "<="):
                tokens.append(("OP", expr[i:i+2]))
                i += 2
                continue
            # Single-char operators
            if expr[i] in "><":
                tokens.append(("OP", expr[i]))
                i += 1
                continue
            # Word (identifier / keyword / operator-name)
            j = i
            while j < n and not expr[j].isspace() and expr[j] not in '[],();':
                j += 1
            word = expr[i:j]
            if word in self._WHEN_OPS:
                tokens.append(("OP", word))
            elif word in self._WHEN_KEYWORDS:
                tokens.append(("KEYWORD", word))
            else:
                # Unknown identifier → string literal
                tokens.append(("STRING", word))
            i = j
        return tokens

    def _eval_when_tokens(self, tokens: list) -> bool:
        """Evaluate a tokenized when: expression via recursive descent.

        Grammar (precedence low → high):
            or_expr  → and_expr ('or' and_expr)*
            and_expr → not_expr ('and' not_expr)*
            not_expr → 'not' not_expr | in_expr
            in_expr  → comparison ('in' '[' list ']')?
            comparison → atom (op atom)?
            atom      → STRING | NUMBER | 'True' | 'False' | 'None'
                        | '(' or_expr ')'
        """
        pos = [0]

        def _peek():
            return tokens[pos[0]] if pos[0] < len(tokens) else None

        def _consume():
            t = tokens[pos[0]]
            pos[0] += 1
            return t

        def _to_num(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return 0.0

        def _parse_or():
            left = _parse_and()
            while _peek() == ("KEYWORD", "or"):
                _consume()
                right = _parse_and()
                left = left or right
            return left

        def _parse_and():
            left = _parse_not()
            while _peek() == ("KEYWORD", "and"):
                _consume()
                right = _parse_not()
                left = left and right
            return left

        def _parse_not():
            if _peek() == ("KEYWORD", "not"):
                _consume()
                return not _parse_not()
            return _parse_in()

        def _parse_in():
            left = _parse_comparison()
            if _peek() == ("KEYWORD", "in"):
                _consume()
                # Expect '['
                assert _peek() == ("BRACKET", "["), "Expected '[' after 'in'"
                _consume()
                items = []
                while _peek() and _peek() != ("BRACKET", "]"):
                    items.append(_parse_atom())
                    if _peek() == ("COMMA", ","):
                        _consume()
                assert _peek() == ("BRACKET", "]"), "Expected ']' to close list"
                _consume()
                return left in items
            return left

        def _parse_comparison():
            left = _parse_atom()
            pk = _peek()
            if pk and pk[0] == "OP" and pk[1] in self._WHEN_OPS:
                op = _consume()[1]
                right = _parse_atom()
                if op == "==":
                    return left == right
                elif op == "!=":
                    return left != right
                elif op == ">":
                    return _to_num(left) > _to_num(right)
                elif op == "<":
                    return _to_num(left) < _to_num(right)
                elif op == ">=":
                    return _to_num(left) >= _to_num(right)
                elif op == "<=":
                    return _to_num(left) <= _to_num(right)
                elif op == "contains":
                    return str(right) in str(left)
                elif op == "starts_with":
                    return str(left).startswith(str(right))
            return left

        def _parse_atom():
            pk = _peek()
            if pk is None:
                raise ValueError("Unexpected end of when: expression")
            if pk[0] == "NUMBER":
                return _consume()[1]
            if pk[0] == "STRING":
                return _consume()[1]
            if pk[0] == "KEYWORD" and pk[1] in ("True", "False", "None"):
                val = _consume()[1]
                return {"True": True, "False": False, "None": None}[val]
            if pk[0] == "BRACKET" and pk[1] == "(":
                _consume()
                val = _parse_or()
                assert _peek() == ("BRACKET", ")"), "Expected ')' in when: expression"
                _consume()
                return val
            raise ValueError(f"Unexpected token in when: expression: {pk}")

        return bool(_parse_or())

    def evaluate_when(self, when_expr: str, node: "WorkflowNode",
                      states: dict, context: Optional[dict] = None,
                      layers: list = None,
                      workflow: "Workflow" = None) -> bool:
        """Evaluate a node's ``when:`` condition against the current
        workflow state.

        Returns True when the node should dispatch (truthy expression
        or empty ``when``), False when it should be skipped.
        """
        if not when_expr or not when_expr.strip():
            return True  # Empty = always run

        try:
            resolved = self._resolve_when_references(
                when_expr, states, context
            )
            tokens = self._tokenize_when(resolved)
            if not tokens:
                return True  # Empty after resolution = always run
            return self._eval_when_tokens(tokens)
        except Exception as exc:
            # Fail open — evaluation error defaults to skip to avoid
            # dispatching a node whose condition couldn't be checked.
            import sys as _sys
            print(
                f"   ⚠  when: evaluation error for "
                f"'{when_expr}': {exc} — skipping node",
                file=_sys.stderr,
            )
            return False

    # ── Execution ──────────────────────────────────────────────

    def execute(self, workflow_name: str, context: dict = None,
                start_node: str = None, dry_run: bool = False,
                resume: bool = False, board: str = None,
                inputs: dict = None, delivery: str = None) -> dict:
        """
        Run a workflow to completion. Supports revision loops via
        the LOOP:<target> convention in block reasons.

        Board resolution priority:
          1. Workflow YAML ``kanban_board`` field
          2. ``board`` parameter passed at invocation
          3. Auto-create ``wf_<workflow_name>``

        ``inputs`` are merged into context and available as
        ``{inputs.<key>}`` template substitutions across all nodes.

        ``delivery`` is an optional delivery target (e.g.
        ``"discord:CHANNEL_ID"``). The delivery router ALWAYS activates
        — it writes to a local log file unconditionally. If ``delivery``
        is set to a platform target, it ALSO posts to that platform.
        The engine still returns results to the caller (the router is
        additive, not replacing the chat flow).

        Returns execution summary: {node_id: final_status, ...}
        """
        workflow = self.load_workflow(workflow_name)

        # Three-tier board resolution
        if workflow.kanban_board:
            # Tier 1: YAML declares a board
            self.kanban_board = workflow.kanban_board
        elif board:
            # Tier 2: Caller passes a board at invocation
            from hermes_cli.kanban_db import _normalize_board_slug
            self.kanban_board = _normalize_board_slug(board)
        else:
            # Tier 3: Auto-create wf_<workflow_name>
            auto_slug = f"wf_{workflow_name}"
            from hermes_cli.kanban_db import _normalize_board_slug
            self.kanban_board = _normalize_board_slug(auto_slug)

        # Merge inputs into context — inputs are available as
        # {inputs.<key>} in template substitution. Input keys are also
        # promoted to top-level context for backward compatibility with
        # YAML templates that use bare {key} references (e.g. {question}).
        # If context already has a key, the explicit context value wins.
        if inputs:
            if context is None:
                context = {}
            context["inputs"] = inputs
            for k, v in inputs.items():
                if k not in context:
                    context[k] = v

        # Generate a run ID for this invocation. Available as {run_id}
        # in template substitution so YAML authors can create unique
        # artifact filenames per run.
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        workflow.run_id = f"{workflow_name}-{ts}"

        layers = self.topological_sort(workflow)

        # Try resume from saved state
        states = None
        results = {}
        layer_idx = 0

        if resume:
            saved = self._find_latest_state(workflow_name)
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
                        # Restore the captured output so resume still has
                        # the upstream nodes' bodies available for
                        # {phaseN.X} template substitution.
                        result=s.get("result"),
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
                    deps_failed = []
                    for d in node.depends_on:
                        ds = states[d].status
                        if ds in ("failed", "timed_out", "blocked", "skipped"):
                            cause = states[d].error or ds
                            deps_failed.append(f"{d}={cause}")
                    if deps_failed:
                        print(f"   [SKIP] {nid} — {'; '.join(deps_failed)}")
                        continue
                    # dry-run when: check
                    if node.when:
                        if not self.evaluate_when(
                            node.when, node, states, context,
                            layers=layers, workflow=workflow,
                        ):
                            print(f"   [SKIP] {nid} — when: {node.when}")
                            continue
                    if node.synthetic:
                        # Synthetic gates auto-complete — there is no
                        # card to create, so dry-run should still
                        # reflect that rather than printing a fake one.
                        print(f"   [DRY RUN] {nid} — synthetic gate (auto-complete)")
                    else:
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
                # NOTE: "blocked" is excluded — it's transient (e.g.,
                # heartbeat sweep, human gate). Downstream nodes wait
                # instead of skipping.
                deps_failed = []
                deps_blocked = []
                for d in node.depends_on:
                    ds = states[d].status
                    if ds in ("failed", "timed_out", "skipped"):
                        # Capture WHY the dependency was skipped/degraded —
                        # the error carries the upstream cause chain so
                        # downstream nodes see the root failure, not just
                        # the immediate skip.
                        cause = states[d].error or ds
                        deps_failed.append(f"{d}={cause}")
                    elif ds == "blocked":
                        deps_blocked.append(d)
                if deps_failed:
                    state.status = "skipped"
                    causes = "; ".join(deps_failed)
                    state.error = f"Skipped: dependency {' '.join(d.split('=')[0] for d in deps_failed)} failed — {causes}"
                    results[nid] = "skipped"
                    print(f"   ⏭ {nid} — SKIPPED ({causes})")
                    continue
                if deps_blocked:
                    state.status = "blocked"
                    state.error = f"Waiting: dependency {', '.join(deps_blocked)} is blocked"
                    results[nid] = "blocked"
                    print(f"   🚧 {nid} — WAITING ({', '.join(deps_blocked)} blocked)")
                    continue

                # ── when: conditional dispatch ──
                # Evaluate the node's when: expression against the
                # current workflow state.  Empty when: = always run
                # (preserves existing behavior for all workflows).
                if node.when:
                    if not self.evaluate_when(
                        node.when, node, states, context,
                        layers=layers, workflow=workflow,
                    ):
                        state.status = "skipped"
                        state.error = f"Skipped: when condition not met ({node.when})"
                        results[nid] = "skipped"
                        print(f"   ⏭ {nid} — SKIPPED (when: {node.when})")
                        continue

                # Synthetic gate nodes: auto-complete here. By the time
                # we reach this layer, all depends_on are done (the
                # topological sort guarantees this), so the gate is
                # satisfied. No kanban card is created — the gate is
                # purely an ordering primitive.
                if node.synthetic:
                    state.status = "done"
                    state.completed_at = datetime.now(timezone.utc).isoformat()
                    results[nid] = "done"
                    print(f"   🔓 {nid} — SYNTHETIC (auto-complete)")
                    continue

                # Create the card
                state.status = "running"
                state.started_at = datetime.now(timezone.utc).isoformat()
                state.attempts += 1

                try:
                    card_id = self.dispatch_node(
                        state, node, context,
                        workflow=workflow, states=states, layers=layers,
                    )
                    if card_id is None:
                        # scope: global — in-process, no card to monitor
                        results[nid] = "done"
                        print(f"   ⊙ {nid} → in-process (scope: global)")
                        continue
                    state.kanban_card_id = card_id
                    # Initialize heartbeat so the sweep doesn't auto-block
                    # the card before the worker picks it up.
                    try:
                        from hermes_cli import kanban_db as _kb
                        with _kb.connect(board=self.kanban_board) as _conn:
                            _kb.heartbeat_worker(_conn, card_id)
                    except Exception:
                        pass  # Non-fatal: heartbeat sweep has created_at fallback
                    print(f"   ✓ {nid} → card {card_id}")
                except Exception as e:
                    state.status = "failed"
                    state.error = str(e)
                    results[nid] = "failed"
                    print(f"   ✗ {nid} → failed: {e}")

            # Save state after dispatching layer
            self._save_state(workflow_name, states, results, layer_idx, layers,
                            run_id=workflow.run_id)

            # Monitor completion for this layer. Synthetic nodes were
            # auto-completed in the dispatch loop above (state.status
            # == "done"), so they have no work to do here. Filtering
            # them out avoids an unnecessary 15s sleep when a layer
            # contains only synthetic gates.
            running_nodes = [
                nid for nid in layer
                if states[nid].status == "running"
            ]
            if running_nodes:
                revision_result = self._monitor_layer(
                    workflow, running_nodes, states, results, context
                )
            else:
                revision_result = None

            # Re-check blocked nodes — if their dependencies unblocked,
            # dispatch them now instead of waiting for the next layer.
            blocked_nodes = [
                nid for nid in layer
                if states[nid].status == "blocked"
            ]
            for nid in blocked_nodes:
                node = workflow.nodes[nid]
                deps_still_blocked = any(
                    states[d].status == "blocked"
                    for d in node.depends_on
                    if d in states
                )
                if not deps_still_blocked:
                    # Dependencies cleared — dispatch this node
                    state = states[nid]
                    state.status = "running"
                    state.started_at = datetime.now(timezone.utc).isoformat()
                    state.attempts += 1
                    try:
                        card_id = self.dispatch_node(
                            state, node, context,
                            workflow=workflow, states=states, layers=layers,
                        )
                        if card_id is None:
                            results[nid] = "done"
                            print(f"   ⊙ {nid} → in-process (scope: global)")
                            continue
                        state.kanban_card_id = card_id
                        print(f"   ✓ {nid} → card {card_id} (unblocked)")
                        running_nodes.append(nid)
                    except Exception as e:
                        state.status = "failed"
                        state.error = str(e)
                        results[nid] = "failed"
                        print(f"   ✗ {nid} → failed: {e}")

            # Re-monitor if we dispatched new nodes
            if len(running_nodes) > 0 and not revision_result:
                revision_result = self._monitor_layer(
                    workflow, running_nodes, states, results, context
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
                    # Try LLM analysis of the deadlock
                    self._try_escalation_analysis(
                        workflow, verify_nid, verify_state, context
                    )
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
                        card_id = self.dispatch_node(
                            rev_state, rev_node, context,
                            workflow=workflow, states=states, layers=layers,
                        )
                        if card_id is None:
                            results[revision_nid] = "done"
                            print(f"   ⊙ {revision_nid} → in-process (scope: global)")
                            continue
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
                        # Reset verify node for re-run. B2: clear the
                        # stale result so a re-run that fails won't
                        # leave the previous output in the lookup
                        # and mislead downstream nodes. The re-run
                        # will populate state.result fresh on its
                        # next "done" transition.
                        verify_state.status = "pending"
                        verify_state.kanban_card_id = None
                        verify_state.started_at = None
                        verify_state.completed_at = None
                        verify_state.result = None
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

        self._clear_state(workflow_name, run_id=workflow.run_id)

        # ── Delivery routing ──
        # The delivery router ALWAYS activates — it persists output to a
        # local log file unconditionally.  If a delivery target is set
        # (e.g. "discord:123456789"), it ALSO posts to that platform.
        # The engine still returns results to the caller — the router
        # is additive, not replacing the chat flow.
        from plugins.workflow.delivery_router import deliver
        delivery_result = deliver(
            results, delivery or "local", workflow.run_id, workflow.name,
        )
        results["delivery"] = delivery_result

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

            # Sweep stale heartbeats once per poll tick.  Idempotent
            # and cheap (single SELECT + conditional UPDATEs).
            try:
                from hermes_cli import kanban_db as _kb
                with _kb.connect(board=self.kanban_board) as _conn:
                    _kb.sweep_stale_heartbeats(_conn)
            except Exception:
                pass  # Non-fatal: heartbeat sweep is a safety net

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
                    # Check whether this node has a degraded fallback
                    if node.fallback_on_timeout == "degraded":
                        state.status = "degraded"
                        state.error = (f"Node timed out but has "
                                       f"fallback_on_timeout=degraded — "
                                       f"proceeding with partial data")
                        results[nid] = "degraded"
                        print(f"   ⚡ {nid} degraded (timeout {elapsed:.0f}s) "
                              f"— proceeding with partial data")
                        pending.discard(nid)
                        continue
                    elif node.fallback_on_timeout == "retry" and state.attempts < 3:
                        state.status = "running"
                        state.attempts += 1
                        state.started_at = datetime.now(timezone.utc).isoformat()
                        print(f"   🔄 {nid} timeout — retrying "
                              f"(attempt {state.attempts}/3)")
                        # Re-create the card
                        try:
                            card_id = self.dispatch_node(
                                state, node, context,
                                workflow=workflow, states=states, layers=layers,
                            )
                            if card_id is None:
                                results[nid] = "done"
                                print(f"   ⊙ {nid} → in-process (scope: global)")
                                continue
                            state.kanban_card_id = card_id
                        except Exception as e:
                            state.status = "failed"
                            state.error = f"Retry card creation failed: {e}"
                            results[nid] = "failed"
                            print(f"   ✗ {nid} retry failed: {e}")
                            pending.discard(nid)
                        continue
                    # Default: skip
                    state.status = "timed_out"
                    state.error = f"Exceeded {node.timeout_minutes}min timeout"
                    results[nid] = "timed_out"
                    print(f"   ⏰ {nid} timed out after {elapsed:.0f}s")
                    self._try_failure_analysis(node, state, elapsed)
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
                        # B2: capture the card body so downstream nodes
                        # can reference this output via
                        # {phaseN.node-id} or {node-id} in their
                        # task templates. We pull the body AFTER
                        # marking done so the kanban tool sees the
                        # latest state. A failure here is non-fatal —
                        # the node is still considered done, just
                        # without a captured result (so downstream
                        # templates would see "Unresolved" and leave
                        # the literal in place).
                        try:
                            state.result = self.get_card_body(
                                state.kanban_card_id
                            )
                        except Exception as e:
                            print(f"   ⚠  {nid} done but result "
                                  f"capture failed: {e}")
                        # Validate expected output artifacts
                        validation = self._validate_outputs(node, state)
                        state.validation_warnings = validation
                        print(f"   ✓ {nid} completed ({elapsed:.0f}s)"
                              + (f" [{len(validation)} validation warnings]"
                                 if validation else ""))
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
                                state.loop_history.append(
                                    f"Round {state.loop_count}: {body[:200]}"
                                )
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
            self._try_failure_analysis(node, state, max_polls * self.POLL_INTERVAL)

        return None  # No loop detected

    # ── Status query ────────────────────────────────────────────

    def status(self, workflow_name: str = None) -> dict:
        """Query current state of running or saved workflows."""
        if workflow_name:
            saved = self._find_latest_state(workflow_name)
            if saved:
                result = {
                    "workflow": workflow_name,
                    "current_layer": saved["current_layer"],
                    "total_layers": len(saved["layers"]),
                    "states": saved["states"],
                    "results": saved["results"],
                    "updated_at": saved.get("updated_at"),
                }
                # Try LLM summary
                summary = self._try_status_summary(workflow_name, saved)
                if summary:
                    result["summary"] = summary
                return result
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
    start.add_argument("--context", "-c", action="append", help="Key=value context pairs (repeatable)")
    start.add_argument("--board", "-b", help="Board slug to use (overrides YAML and auto-create)")
    start.add_argument("--inputs", "-i", action="append", help="Input key=value pairs (repeatable, available as {inputs.<key>})")
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
        inputs = {}
        if args.inputs:
            for pair in args.inputs:
                k, v = pair.split("=", 1)
                inputs[k] = v
        engine.execute(args.workflow, context=context, start_node=args.node,
                      dry_run=args.dry_run, resume=args.resume,
                      board=args.board, inputs=inputs or None)

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
                # Synthetic gates have no agent — show [synthetic]
                # so operators can distinguish gate nodes from real
                # dispatch targets at a glance.
                agent_label = "synthetic" if node.synthetic else node.agent
                print(f"  [{agent_label}] {nid}{deps}")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
