"""Agent-facing tools for the workflow plugin.

These wrap the in-process ``WorkflowEngine`` class so an agent (e.g. Sherlock)
can drive pipeline execution through its normal tool calls without having
to know the CLI exists. Every handler returns a JSON-serializable dict the
agent can read directly.

Tools
-----
- ``workflow_start``    — kick off a pipeline (predefined or dynamic mode);
                         creates kanban cards and monitors them layer-by-layer
- ``workflow_view``     — load a workflow template for inspection
- ``workflow_validate`` — structural check: DAG, cycles, missing nodes
- ``workflow_status``   — current state of a running (or last-run) pipeline
- ``workflow_list``     — available pipeline definitions
- ``workflow_show``     — pipeline structure: layers + nodes + dependencies

All tools are read-only except ``workflow_start``, which creates kanban
cards via the same code path the CLI uses. The engine handles revision
loops (``LOOP:<target>`` blocked-card convention) internally; the agent
sees the final summary in the returned dict.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Runtime gate
# ---------------------------------------------------------------------------

def check_workflow_requirements() -> bool:
    """Return True when the workflow engine can be invoked.

    Gates on:
      * ``docs/fleet-pipelines/`` directory existing (ships with the repo)
      * the ``WorkflowEngine`` class being importable

    No external API keys, no subprocess runner — pure-Python DAG execution
    against local kanban + filesystem state.
    """
    try:
        from plugins.workflow.engine import WorkflowEngine  # noqa: F401
    except ImportError as exc:
        logger.debug("workflow_engine import failed: %s", exc)
        return False

    # workflows_dir lives under the repo; resolve via the engine class so
    # the gate reflects whatever directory the engine itself would use.
    try:
        from pathlib import Path
        engine_mod = __import__("plugins.workflow.engine", fromlist=["WorkflowEngine"])
        workflows_dir = Path(engine_mod.__file__).resolve().parent.parent / "docs" / "fleet-pipelines"
        if not workflows_dir.is_dir():
            return False
    except Exception:
        return False
    return True


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------
def _engine():
    """Lazy import + instantiate the engine. Kept inside a function so the
    plugin still loads in test contexts where ``tools.workflow_engine`` may
    not be importable (the check_fn already gates on this, but defense in
    depth is cheap)."""
    from plugins.workflow.engine import WorkflowEngine
    return WorkflowEngine()


def _ok(payload: Any) -> str:
    """Wrap a successful result in the standard tool-output envelope."""
    return json.dumps({"ok": True, "result": payload}, indent=2, default=str)


def _err(message: str, **extra: Any) -> str:
    """Wrap an error result; ``message`` is a short agent-readable string."""
    return json.dumps({"ok": False, "error": message, **extra}, indent=2, default=str)


def handle_workflow_start(
    workflow: str,
    context: Optional[Dict[str, Any]] = None,
    mode: str = "predefined",
    node: Optional[str] = None,
    dry_run: bool = False,
    resume: bool = False,
    scope: str = "project",
    single_flight: bool = False,
    delivery_target: str = "",
    **kwargs: Any,
) -> str:
    """Start a pipeline in the given mode.

    Mode ``"predefined"`` (default):
        Reads a YAML pipeline definition from ``docs/fleet-pipelines/``,
        validates it, and dispatches via the kanban engine.  Creates
        kanban cards for layer-0 nodes, monitors them, and advances the
        DAG as cards complete.  Returns a final summary keyed by node_id.

    Mode ``"dynamic"``:
        Delegates to ``dynamic_bridge.run_dynamic_workflow()`` which
        creates an ad-hoc DAG at runtime from the objective and node
        list passed in *context*.

    See ``workflow_list`` for available pipelines, ``workflow_show`` to
    inspect structure, and ``workflow_status`` to check a running run.
    """
    if not workflow or not isinstance(workflow, str):
        return _err("workflow must be a non-empty string")

    if mode == "dynamic":
        return _handle_workflow_start_dynamic(
            workflow=workflow,
            context=context,
            scope=scope,
            single_flight=single_flight,
            delivery_target=delivery_target,
            dry_run=dry_run,
            **kwargs,
        )

    # Default: predefined mode
    return _handle_workflow_start_predefined(
        workflow=workflow,
        context=context,
        node=node,
        dry_run=dry_run,
        resume=resume,
        single_flight=single_flight,
    )


def _handle_workflow_start_predefined(
    workflow: str,
    context: Optional[Dict[str, Any]] = None,
    node: Optional[str] = None,
    dry_run: bool = False,
    resume: bool = False,
    single_flight: bool = False,
) -> str:
    """Predefined mode: look up YAML in docs/fleet-pipelines/, validate,
    dispatch via engine."""
    try:
        engine = _engine()
    except Exception as exc:
        return _err(f"engine import failed: {exc}")

    # Single-flight opt-in check: if the workflow declares
    # ``single_flight: true`` in YAML, refuse to start when another
    # run is already in progress. Prevents duplicate parallel runs
    # from webhook storms or repeated dispatch signals.
    # Skipped for dry-run and resume — those are explicitly about
    # inspecting / continuing an existing run, not starting fresh.
    if not dry_run and not resume and single_flight:
        try:
            wf_def = engine.load_workflow(workflow)
        except Exception:
            wf_def = None
        if wf_def is not None and getattr(wf_def, "single_flight", False):
            if engine._has_active_run(workflow):
                return _err(
                    f"single_flight: another run of '{workflow}' is in progress",
                    hint="wait for the current run to finish, or call workflow_status to inspect",
                )

    try:
        result = engine.execute(
            workflow_name=workflow,
            context=context or {},
            start_node=node,
            dry_run=dry_run,
            resume=resume,
        )
    except FileNotFoundError as exc:
        return _err(f"workflow not found: {workflow}", hint=str(exc))
    except Exception as exc:
        logger.exception("workflow_start failed for %s", workflow)
        return _err(f"execution failed: {exc}")

    return _ok(result)


def _handle_workflow_start_dynamic(
    workflow: str,
    context: Optional[Dict[str, Any]] = None,
    scope: str = "project",
    single_flight: bool = False,
    delivery_target: str = "",
    dry_run: bool = False,
    **kwargs: Any,
) -> str:
    """Dynamic mode: delegate to dynamic_bridge.run_dynamic_workflow().

    Unlike predefined mode which reads pre-defined YAML pipeline
    definitions, this creates an ad-hoc DAG at runtime.  The
    ``workflow`` parameter is the workflow_id to create (or reuse), and
    ``context`` carries the objective and nodes from the calling agent.

    Scope controls fleet integration:
      - ``project`` (default): creates kanban cards for worker nodes
      - ``global``: no kanban, in-memory only
      - ``durable``: persists state to disk
    """
    from plugins.workflow.dynamic_bridge import (
        run_dynamic_workflow,
    )

    # Extract objective and nodes from context or kwargs
    ctx = context or {}
    objective = ctx.get("objective", "")
    nodes = ctx.get("nodes", [])
    wf_context = ctx.get("context", "")

    # Allow overriding via kwargs (for future flexibility)
    if not objective and "objective" in kwargs:
        objective = kwargs["objective"]
    if not nodes and "nodes" in kwargs:
        nodes = kwargs["nodes"]
    if not wf_context and "wf_context" in kwargs:
        wf_context = kwargs["wf_context"]

    if not objective:
        return _err("objective is required (pass in context.objective)")
    if not isinstance(nodes, list) or not nodes:
        return _err("nodes must be a non-empty list (pass in context.nodes)")
    if scope not in ("project", "global", "durable"):
        return _err(f"invalid scope: {scope!r}; must be project, global, or durable")

    if dry_run:
        return _ok({
            "dry_run": True,
            "workflow_id": workflow,
            "objective": objective,
            "node_count": len(nodes),
            "scope": scope,
            "single_flight": single_flight,
            "delivery_target": delivery_target,
        })

    try:
        result = run_dynamic_workflow(
            workflow_id=workflow,
            objective=objective,
            nodes=nodes,
            context=wf_context,
            scope=scope,
            single_flight=single_flight,
            dispatch_ready=True,
            delivery_target=delivery_target,
        )
    except Exception as exc:
        logger.exception("dynamic_workflow_start failed for %s", workflow)
        return _err(f"dynamic workflow failed: {exc}")

    return _ok(result)


def handle_workflow_view(workflow: str = "", **kwargs: Any) -> str:
    """Load a workflow template (predefined YAML or dynamic starter) for inspection."""
    from plugins.workflow.registry import _fleet_pipelines_dirs, _user_workflows_dir

    if not workflow or not isinstance(workflow, str):
        return _err("workflow must be a non-empty string")

    # Check if it's a predefined pipeline
    for fp_dir in _fleet_pipelines_dirs():
        path = fp_dir / f"{workflow}.yaml"
        if path.is_file():
            return _ok({
                "name": workflow,
                "mode": "predefined",
                "path": str(path),
                "yaml": path.read_text(),
            })

    # Check if it's a dynamic template
    uw_dir = _user_workflows_dir()
    if uw_dir:
        path = uw_dir / f"{workflow}.yaml"
        if path.is_file():
            return _ok({
                "name": workflow,
                "mode": "dynamic",
                "path": str(path),
                "yaml": path.read_text(),
            })

    return _err(f"workflow template not found: {workflow}")


def handle_workflow_validate(workflow: str, **kwargs: Any) -> str:
    """Structural validation only. Returns nodes/layers/cycle check without
    creating kanban cards. Safe to call before committing to a start."""
    try:
        engine = _engine()
    except Exception as exc:
        return _err(f"engine import failed: {exc}")

    if not workflow or not isinstance(workflow, str):
        return _err("workflow must be a non-empty string")

    try:
        result = engine.validate(workflow)
    except FileNotFoundError as exc:
        return _err(f"workflow not found: {workflow}", hint=str(exc))
    except Exception as exc:
        logger.exception("workflow_validate failed for %s", workflow)
        return _err(f"validation failed: {exc}")

    return _ok(result)


def handle_workflow_status(workflow: Optional[str] = None, **kwargs: Any) -> str:
    """Current state of a running pipeline (or all pipelines if workflow is
    omitted). Mirrors ``hermes kanban status`` for the kanban cards the
    engine owns."""
    try:
        engine = _engine()
    except Exception as exc:
        return _err(f"engine import failed: {exc}")

    try:
        result = engine.status(workflow)
    except Exception as exc:
        logger.exception("workflow_status failed for %s", workflow)
        return _err(f"status query failed: {exc}")

    return _ok(result)


def handle_workflow_list(
    trigger: Optional[str] = None,
    **kwargs: Any,
) -> str:
    """List available workflow definitions from both the fleet pipelines
    directory (pre-defined) and ``~/.hermes/workflows/`` (dynamic).

    When *trigger* is provided the list is filtered to workflows whose
    trigger keywords appear in the given string (case-insensitive).
    Without *trigger* every registered workflow is returned.
    """
    from plugins.workflow.registry import list_workflows, match_workflow_trigger

    try:
        workflows = list_workflows()
    except Exception as exc:
        return _err(f"registry scan failed: {exc}")

    # Optional trigger-based filter
    if trigger:
        matched = match_workflow_trigger(trigger)
        if matched is not None:
            workflows = [w for w in workflows if w["name"] == matched["name"]]
        else:
            workflows = []

    # Partition for a clearer response
    predefined = [w for w in workflows if w["mode"] == "predefined"]
    dynamic = [w for w in workflows if w["mode"] == "dynamic"]

    return _ok({
        "workflows": workflows,
        "predefined_count": len(predefined),
        "dynamic_count": len(dynamic),
        "total": len(workflows),
    })


def handle_workflow_show(workflow: str, **kwargs: Any) -> str:
    """Show pipeline structure: layers, nodes, dependencies. Use before
    ``workflow_start`` to understand the DAG."""
    try:
        engine = _engine()
    except Exception as exc:
        return _err(f"engine import failed: {exc}")

    if not workflow or not isinstance(workflow, str):
        return _err("workflow must be a non-empty string")

    try:
        wf = engine.load_workflow(workflow)
        layers = engine.topological_sort(wf)
    except FileNotFoundError as exc:
        return _err(f"workflow not found: {workflow}", hint=str(exc))
    except Exception as exc:
        logger.exception("workflow_show failed for %s", workflow)
        return _err(f"show failed: {exc}")

    nodes = []
    for nid, node in wf.nodes.items():
        nodes.append({
            "id": nid,
            "agent": node.agent,
            "task": node.task,
            "deps": sorted(node.depends_on),
            "timeout_min": node.timeout_minutes,
            "layer": next((i for i, l in enumerate(layers) if nid in l), None),
        })
    return _ok({
        "name": wf.name,
        "description": wf.description,
        "layers": len(layers),
        "nodes": len(wf.nodes),
        "structure": nodes,
    })


# ---------------------------------------------------------------------------
# Deprecated alias — prefer handle_workflow_start(mode="dynamic") instead
# ---------------------------------------------------------------------------

def handle_workflow_dynamic_start(
    workflow: str = "",
    context: Optional[Dict[str, Any]] = None,
    scope: str = "project",
    single_flight: bool = False,
    delivery_target: str = "",
    dry_run: bool = False,
    **kwargs: Any,
) -> str:
    """Deprecated: use ``handle_workflow_start`` with ``mode="dynamic"`` instead.

    This thin wrapper maintains backward compatibility for callers that
    still reference the old entry point.
    """
    return handle_workflow_start(
        workflow=workflow,
        context=context,
        mode="dynamic",
        scope=scope,
        single_flight=single_flight,
        delivery_target=delivery_target,
        dry_run=dry_run,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Tool schemas — fed to PluginContext.register_tool() in __init__.py
# ---------------------------------------------------------------------------

WORKFLOW_START_SCHEMA: Dict[str, Any] = {
    "name": "workflow_start",
    "description": (
        "Start a pipeline by name in the given mode. "
        "Mode 'predefined' (default): reads a YAML pipeline from "
        "docs/fleet-pipelines/, creates kanban cards for layer-0 nodes, "
        "monitors them layer-by-layer, advances the DAG as cards complete. "
        "Supports revision loops via the LOOP:<target> convention: if a "
        "reviewer blocks a card with a reason starting with 'LOOP:<node-id> |', "
        "the engine reruns the targeted node automatically. "
        "Mode 'dynamic': creates an ad-hoc DAG at runtime from the objective "
        "and node list in context; delegates to the dynamic bridge. "
        "Returns a final summary dict. Use workflow_list to see available "
        "pipelines, workflow_show to inspect structure, and workflow_status "
        "to check a running pipeline."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "workflow": {
                "type": "string",
                "description": (
                    "Pipeline name (without .yaml). For predefined mode, "
                    "current canon: 'ideation' (spec→security→validate→decompose), "
                    "'feature-dev' (build→CI→review→merge→post-merge). "
                    "For dynamic mode, this is the workflow_id to create or reuse."
                ),
            },
            "context": {
                "type": "object",
                "description": (
                    "Optional key=value context pairs (e.g. {'project': 'foo'}). "
                    "Available as substitutions in the pipeline YAML. "
                    "For dynamic mode, must contain 'objective' (string) and "
                    "'nodes' (array of node dicts with node_id, goal, depends_on)."
                ),
            },
            "mode": {
                "type": "string",
                "enum": ["predefined", "dynamic"],
                "default": "predefined",
                "description": (
                    "Workflow mode. 'predefined' uses YAML pipeline from "
                    "docs/fleet-pipelines/. 'dynamic' creates a model-authored "
                    "DAG from a template."
                ),
            },
            "node": {
                "type": "string",
                "description": (
                    "Start from a specific node id (partial execution). "
                    "Only used in predefined mode."
                ),
            },
            "dry_run": {
                "type": "boolean",
                "description": "Print the execution plan without creating kanban cards.",
                "default": False,
            },
            "resume": {
                "type": "boolean",
                "description": "Resume from saved state if a previous run was interrupted.",
                "default": False,
            },
            "scope": {
                "type": "string",
                "enum": ["project", "global", "durable"],
                "description": (
                    "Fleet integration scope (dynamic mode only). "
                    "'project' creates kanban cards, 'durable' persists state, "
                    "'global' is in-memory only."
                ),
                "default": "project",
            },
            "single_flight": {
                "type": "boolean",
                "description": (
                    "If True, refuse to create a new workflow when a run "
                    "with the same workflow_id is already in progress. "
                    "In predefined mode, also checks the YAML single_flight flag."
                ),
                "default": False,
            },
            "delivery_target": {
                "type": "string",
                "description": (
                    "Optional delivery target (e.g. 'discord:CHANNEL_ID'). "
                    "When set, the workflow summary is posted on completion. "
                    "Dynamic mode only."
                ),
            },
        },
        "required": ["workflow"],
    },
}

WORKFLOW_VIEW_SCHEMA: Dict[str, Any] = {
    "name": "workflow_view",
    "description": (
        "Load a workflow template for inspection. Checks predefined YAML "
        "pipelines from docs/fleet-pipelines/ first, then user-saved dynamic "
        "templates from ~/.hermes/workflows/. Returns the template name, "
        "mode (predefined/dynamic), filesystem path, and raw YAML content."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "workflow": {
                "type": "string",
                "description": "Workflow name to load (without .yaml).",
            },
        },
        "required": ["workflow"],
    },
}

WORKFLOW_VALIDATE_SCHEMA: Dict[str, Any] = {
    "name": "workflow_validate",
    "description": (
        "Validate a pipeline definition without executing. Checks for cycles, "
        "missing dependencies, and unknown agent references. Returns "
        "{valid, nodes, layers, issues}. Safe to call any time."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "workflow": {
                "type": "string",
                "description": "Pipeline name to validate (without .yaml).",
            },
        },
        "required": ["workflow"],
    },
}

WORKFLOW_STATUS_SCHEMA: Dict[str, Any] = {
    "name": "workflow_status",
    "description": (
        "Query the current state of a running or last-completed pipeline. "
        "Omit the workflow argument to get status for all known pipelines. "
        "Mirrors the engine's internal state file plus live kanban card state."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "workflow": {
                "type": "string",
                "description": "Pipeline name to query (omit for all).",
            },
        },
    },
}

WORKFLOW_LIST_SCHEMA: Dict[str, Any] = {
    "name": "workflow_list",
    "description": (
        "List available workflow definitions from both fleet pipelines "
        "(pre-defined) and user-saved templates (~/.hermes/workflows/). "
        "Returns workflows with metadata: name, description, trigger "
        "keywords, mode (predefined/dynamic), category, and path. "
        "Pass trigger to filter by keyword match."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "trigger": {
                "type": "string",
                "description": (
                    "Optional trigger string to filter workflows by keyword "
                    "match (case-insensitive). When provided, only workflows "
                    "whose trigger keywords appear in this string are returned."
                ),
            },
        },
    },
}

WORKFLOW_SHOW_SCHEMA: Dict[str, Any] = {
    "name": "workflow_show",
    "description": (
        "Show the structure of a pipeline: its nodes, the agent each node "
        "targets, the dependencies between nodes, and the layer index of "
        "each node. Use before workflow_start to understand the DAG."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "workflow": {
                "type": "string",
                "description": "Pipeline name to inspect (without .yaml).",
            },
        },
        "required": ["workflow"],
    },
}

# ---------------------------------------------------------------------------
# Dynamic workflow tools — model-authored DAGs (deprecated entry point)
# ---------------------------------------------------------------------------

def check_dynamic_workflow_requirements() -> bool:
    """Return True when the dynamic workflow engine can be invoked.

    Gates on the ``dynamic`` and ``dynamic_bridge`` modules being
    importable.  No external API keys or subprocess runners needed.
    """
    try:
        from plugins.workflow.dynamic import handle_workflow_dynamic  # noqa: F401
        from plugins.workflow.dynamic_bridge import run_dynamic_workflow  # noqa: F401
    except ImportError as exc:
        logger.debug("dynamic workflow import failed: %s", exc)
        return False
    return True


DYNAMIC_WORKFLOW_SCHEMA: Dict[str, Any] = {
    "name": "workflow_dynamic_start",
    "description": (
        "Deprecated: use workflow_start with mode='dynamic' instead. "
        "Start a dynamic (model-authored) workflow — create an ad-hoc DAG at "
        "runtime instead of reading pre-defined YAML pipelines.  Pass the "
        "workflow_id, objective, and node list.  Nodes define worker goals "
        "and dependencies; the engine dispatches ready nodes as background "
        "delegations and advances the graph as results arrive.\n\n"
        "Scope controls fleet integration:\n"
        "  - project (default): creates kanban cards on the dynamic-workflows board\n"
        "  - global: in-memory only, no kanban\n"
        "  - durable: persists node state to ~/.hermes/workflow-logs/\n\n"
        "Use workflow_status to query progress, or pass delivery_target to "
        "route the final summary to Discord/Telegram."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "workflow": {
                "type": "string",
                "description": (
                    "Workflow ID (unique identifier).  If empty, the engine "
                    "generates one.  Must match ^[A-Za-z0-9_.\\-]{1,96}$."
                ),
            },
            "context": {
                "type": "object",
                "description": (
                    "Workflow configuration object with keys: "
                    "'objective' (required, string — the high-level goal), "
                    "'nodes' (required, array — each with 'node_id', 'goal', "
                    "and optional 'depends_on'), 'context' (optional string "
                    "shared context for all nodes)."
                ),
            },
            "scope": {
                "type": "string",
                "enum": ["project", "global", "durable"],
                "description": (
                    "Fleet integration scope. 'project' creates kanban cards, "
                    "'durable' persists state, 'global' is in-memory only."
                ),
                "default": "project",
            },
            "single_flight": {
                "type": "boolean",
                "description": (
                    "If True, refuse to create a new workflow when a run "
                    "with the same workflow_id is already in progress."
                ),
                "default": False,
            },
            "delivery_target": {
                "type": "string",
                "description": (
                    "Optional delivery target (e.g. 'discord:CHANNEL_ID'). "
                    "When set, the workflow summary is posted on completion."
                ),
            },
            "dry_run": {
                "type": "boolean",
                "description": (
                    "Print the execution plan without creating kanban cards "
                    "or dispatching nodes."
                ),
                "default": False,
            },
        },
        "required": ["workflow", "context"],
    },
}
