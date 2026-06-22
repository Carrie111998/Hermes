"""Agent-facing tools for the workflow plugin.

These wrap the in-process ``WorkflowEngine`` class so an agent (e.g. Sherlock)
can drive pipeline execution through its normal tool calls without having
to know the CLI exists. Every handler returns a JSON-serializable dict the
agent can read directly.

Tools
-----
- ``workflow_start``    — kick off a pipeline; creates kanban cards and
                         monitors them layer-by-layer
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
        from tools.workflow_engine import WorkflowEngine  # noqa: F401
    except ImportError as exc:
        logger.debug("workflow_engine import failed: %s", exc)
        return False

    # workflows_dir lives under the repo; resolve via the engine class so
    # the gate reflects whatever directory the engine itself would use.
    try:
        from pathlib import Path
        engine_mod = __import__("tools.workflow_engine", fromlist=["WorkflowEngine"])
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
    from tools.workflow_engine import WorkflowEngine
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
    node: Optional[str] = None,
    dry_run: bool = False,
    resume: bool = False,
    **kwargs: Any,
) -> str:
    """Start a pipeline. Creates kanban cards for layer-0 nodes, monitors
    them, and advances the DAG as cards complete. Returns a final summary
    keyed by node_id.

    See ``docs/fleet-pipelines/`` for available pipelines. The current
    canon: ``ideation`` (13 nodes, spec→security→validate→decompose) and
    ``feature-dev`` (9 nodes, build→CI→review→merge→post-merge).
    """
    try:
        engine = _engine()
    except Exception as exc:
        return _err(f"engine import failed: {exc}")

    if not workflow or not isinstance(workflow, str):
        return _err("workflow must be a non-empty string")

    # Single-flight opt-in check: if the workflow declares
    # ``single_flight: true`` in YAML, refuse to start when another
    # run is already in progress. Prevents duplicate parallel runs
    # from webhook storms or repeated dispatch signals.
    # Skipped for dry-run and resume — those are explicitly about
    # inspecting / continuing an existing run, not starting fresh.
    if not dry_run and not resume:
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


def handle_workflow_list(**kwargs: Any) -> str:
    """List available pipeline definitions in ``docs/fleet-pipelines/``."""
    try:
        engine = _engine()
    except Exception as exc:
        return _err(f"engine import failed: {exc}")

    files = sorted(engine.workflows_dir.glob("*.yaml"))
    return _ok({
        "pipelines": [f.stem for f in files],
        "dir": str(engine.workflows_dir),
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
# Tool schemas — fed to PluginContext.register_tool() in __init__.py
# ---------------------------------------------------------------------------

WORKFLOW_START_SCHEMA: Dict[str, Any] = {
    "name": "workflow_start",
    "description": (
        "Start a pipeline by name. Creates kanban cards for layer-0 nodes, "
        "monitors them layer-by-layer, advances the DAG as cards complete. "
        "Supports revision loops via the LOOP:<target> convention: if a "
        "reviewer blocks a card with a reason starting with 'LOOP:<node-id> |', "
        "the engine reruns the targeted node automatically. Returns a final "
        "summary dict keyed by node_id with the terminal status of each node. "
        "Use workflow_list to see available pipelines, workflow_show to "
        "inspect structure, and workflow_status to check a running pipeline."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "workflow": {
                "type": "string",
                "description": (
                    "Pipeline name (without .yaml). Current canon: "
                    "'ideation' (spec→security→validate→decompose), "
                    "'feature-dev' (build→CI→review→merge→post-merge)."
                ),
            },
            "context": {
                "type": "object",
                "description": (
                    "Optional key=value context pairs (e.g. {'project': 'foo'}). "
                    "Available as substitutions in the pipeline YAML."
                ),
            },
            "node": {
                "type": "string",
                "description": "Start from a specific node id (partial execution).",
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
        "List available pipeline definitions in docs/fleet-pipelines/. "
        "Returns the pipeline names that can be passed to workflow_start, "
        "workflow_validate, workflow_status, or workflow_show."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
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
