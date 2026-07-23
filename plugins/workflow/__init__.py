"""Workflow engine plugin — registers the workflow_analyst auxiliary task and loads plugin config.

Plugin config lives at ``~/.hermes/profiles/<profile>/workflow/config.yaml``.
See that file for available settings (auto_approve_extensions, max_nodes_per_workflow, etc.).

The engine invokes the analyst via ``get_text_auxiliary_client("workflow_analyst")``
for three analysis modes: escalation, status summary, and failure diagnosis.

See ``plugins/workflow/analyst.py`` for the auxiliary module.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

# ---------------------------------------------------------------------------
# Plugin config loader
# ---------------------------------------------------------------------------

_CONFIG: Dict[str, Any] | None = None

_DEFAULTS: Dict[str, Any] = {
    "auto_discovery": True,
    "auto_approve_extensions": False,
    "auto_approve_template_saves": False,
    "auto_approve_optimizations": False,
    "max_nodes_per_workflow": 256,
    "max_dispatch_per_call": 16,
    "max_extensions_per_workflow": 10,
    "max_nodes_per_extension": 3,
    "default_scope": "project",
    "default_assignee": "",
    "persist_dir": "~/.hermes/workflow-logs",
}


def load_config() -> Dict[str, Any]:
    """Load workflow plugin config from ``~/.hermes/workflows/config.yaml``.

    Returns a dict with defaults merged under any user-set values.
    Caches the result for the lifetime of the process.
    """
    global _CONFIG
    if _CONFIG is not None:
        return _CONFIG

    hermes_home = Path(os.environ.get("HERMES_HOME", "")).expanduser()
    if not hermes_home or not hermes_home.is_dir():
        hermes_home = Path.home() / ".hermes"

    # Try profile-scoped config first, then fall back to shared
    config_paths = [
        hermes_home / "workflows" / "config.yaml",
        Path.home() / ".hermes" / "workflows" / "config.yaml",
    ]

    user_config: Dict[str, Any] = {}
    for path in config_paths:
        if path.is_file():
            try:
                import yaml
                user_config = yaml.safe_load(path.read_text()) or {}
            except Exception:
                pass
            break

    _CONFIG = {**_DEFAULTS, **user_config}
    return _CONFIG


def get_config() -> Dict[str, Any]:
    """Return the cached workflow plugin config.  Loads on first call."""
    return load_config()


def register(ctx):
    """Register workflow tools, the workflow_analyst auxiliary, kanban hooks, and the skill."""
    ctx.register_auxiliary_task(
        key="workflow_analyst",
        display_name="Workflow analyst",
        description="pipeline escalation, status, and failure analysis",
        defaults={
            "timeout": 180,
            "extra_body": {},
        },
    )

    # --- Register the workflow-engine skill ------------------------------------
    skill_path = Path(__file__).parent / "skills" / "workflow-engine" / "SKILL.md"
    if skill_path.exists():
        ctx.register_skill(
            "workflow-engine",
            skill_path,
            "Run DAG-based pipelines via workflow_start",
        )

    # --- Agent-facing workflow tools -------------------------------------------
    from plugins.workflow.tools import (
        check_workflow_requirements,
        handle_workflow_start,
        handle_workflow_view,
        handle_workflow_validate,
        handle_workflow_status,
        handle_workflow_list,
        handle_workflow_show,
        WORKFLOW_START_SCHEMA,
        WORKFLOW_VIEW_SCHEMA,
        WORKFLOW_VALIDATE_SCHEMA,
        WORKFLOW_STATUS_SCHEMA,
        WORKFLOW_LIST_SCHEMA,
        WORKFLOW_SHOW_SCHEMA,
    )

    _TOOLS = [
        (WORKFLOW_START_SCHEMA,    handle_workflow_start),
        (WORKFLOW_VIEW_SCHEMA,     handle_workflow_view),
        (WORKFLOW_VALIDATE_SCHEMA, handle_workflow_validate),
        (WORKFLOW_STATUS_SCHEMA,   handle_workflow_status),
        (WORKFLOW_LIST_SCHEMA,     handle_workflow_list),
        (WORKFLOW_SHOW_SCHEMA,     handle_workflow_show),
    ]

    for schema, handler in _TOOLS:
        ctx.register_tool(
            name=schema["name"],
            toolset="workflow",
            schema=schema,
            handler=handler,
            check_fn=check_workflow_requirements,
        )

    # Register kanban lifecycle hooks to update the job log DB
    ctx.register_hook("kanban_task_completed", _on_kanban_task_completed)
    ctx.register_hook("kanban_task_blocked", _on_kanban_task_blocked)


def _on_kanban_task_completed(*, task_id: str, **kwargs):
    """Update the job log DB when a workflow node card completes."""
    _update_node_card_db(task_id, "done")
    _handle_workflow_node_event(task_id, "done")


def _on_kanban_task_blocked(*, task_id: str, reason: str = None, **kwargs):
    """Update the job log DB when a workflow node card is blocked."""
    _update_node_card_db(task_id, "blocked")
    _handle_workflow_node_event(task_id, "blocked", reason=reason)


def _update_node_card_db(card_id: str, status: str):
    """Update a node card's status and check if the run is complete."""
    try:
        import sqlite3
        from pathlib import Path
        db_path = Path.home() / ".hermes" / "workflows" / "executions.db"
        if not db_path.exists():
            return
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute(
                "UPDATE workflow_node_cards SET status = ? WHERE card_id = ?",
                (status, card_id)
            )
            # Check if all cards for this run are terminal
            row = conn.execute(
                "SELECT run_id FROM workflow_node_cards WHERE card_id = ?",
                (card_id,)
            ).fetchone()
            if not row:
                return
            run_id = row[0]
            total = conn.execute(
                "SELECT COUNT(*) FROM workflow_node_cards WHERE run_id = ?",
                (run_id,)
            ).fetchone()[0]
            done = conn.execute(
                "SELECT COUNT(*) FROM workflow_node_cards WHERE run_id = ? AND status IN ('done','failed')",
                (run_id,)
            ).fetchone()[0]
            if done >= total:
                has_failed = conn.execute(
                    "SELECT COUNT(*) FROM workflow_node_cards WHERE run_id = ? AND status = 'failed'",
                    (run_id,)
                ).fetchone()[0]
                final = "failed" if has_failed > 0 else "completed"
                from datetime import datetime, timezone
                conn.execute(
                    "UPDATE workflow_executions SET status = ?, finished_at = ? WHERE run_id = ?",
                    (final, datetime.now(timezone.utc).isoformat(), run_id)
                )
    except Exception:
        pass  # Non-fatal — state files still work


# ---------------------------------------------------------------------------
# Workflow node event handler — loop logic and layer advancement
# ---------------------------------------------------------------------------

def _find_state_for_card(task_id: str):
    """Find the workflow state file that contains this task_id.

    Returns (state_dict, state_file_path) or None.
    """
    state_dir = Path.home() / ".hermes" / "workspace" / "docs" / "fleet-pipelines" / ".engine-state"
    if not state_dir.exists():
        return None
    for state_file in sorted(state_dir.glob("*_state.json"), reverse=True):
        try:
            import json
            state = json.loads(state_file.read_text())
            states = state.get("states", {})
            for nid, node_state in states.items():
                if node_state.get("kanban_card_id") == task_id:
                    return (state, str(state_file))
        except Exception:
            continue
    return None


def _find_verify_nodes(workflow_name: str):
    """Find verify→revision mappings for a workflow.

    Returns {verify_node_id: revision_node_id} — nodes where a
    revision node depends on the verify node (loop pattern).
    """
    wf_files = Path.home() / ".hermes" / "workspace" / "docs" / "fleet-pipelines"
    import yaml
    wf_path = wf_files / f"{workflow_name}.yaml"
    if not wf_path.exists():
        return {}
    wf = yaml.safe_load(wf_path.read_text())
    nodes = wf.get("nodes", {})
    verify_map = {}
    for name, node in nodes.items():
        name_lower = name.lower()
        if "revise" in name_lower:
            for dep in node.get("depends_on", []):
                if dep in nodes:
                    verify_map[dep] = name
    return verify_map


def _handle_workflow_node_event(task_id: str, status: str, reason: str = None):
    """Handle a workflow node card completion or block event.

    Core loop mechanism:
    - BLOCK of a verify node: enrich the implementer's card with the
      failure report and re-dispatch (loop).
    - COMPLETION: check if the layer is done and advance.
    """
    try:
        result = _find_state_for_card(task_id)
        if result is None:
            return  # Not a workflow card
        state, state_path = result
        workflow_name = state.get("workflow_name", "")
        layers = state.get("layers", [])
        states = state.get("states", {})
        loop_counts = state.get("loop_counts", {})
        max_loops = state.get("max_revision_loops", 3)

        # Find which node this card belongs to
        node_id = None
        for nid, ns in states.items():
            if ns.get("kanban_card_id") == task_id:
                node_id = nid
                break
        if not node_id:
            return

        verify_map = _find_verify_nodes(workflow_name)

        if status == "blocked" and node_id in verify_map:
            # This is a verify node that blocked — LOOP
            revision_node = verify_map[node_id]
            loop_key = f"{node_id}:{revision_node}"
            current_loop = loop_counts.get(loop_key, 0)

            if current_loop >= max_loops:
                print(f"   🚫 Workflow loop exceeded max ({max_loops}) — escalating")
                states[node_id]["status"] = "escalated"
                states[node_id]["error"] = f"Exceeded {max_loops} revision loops"
                _save_state_file(state_path, state)
                return

            # Find the implementer's card (the node this verify depends on)
            wf_files = Path.home() / ".hermes" / "workspace" / "docs" / "fleet-pipelines"
            import yaml
            wf_path = wf_files / f"{workflow_name}.yaml"
            if not wf_path.exists():
                return
            wf = yaml.safe_load(wf_path.read_text())
            verify_node_def = wf.get("nodes", {}).get(node_id, {})
            node_deps = verify_node_def.get("depends_on", [])

            # The implementer is the first dependency that isn't a revision node
            implementer_nid = None
            for dep in node_deps:
                if dep != revision_node:
                    implementer_nid = dep
                    break
            if not implementer_nid and node_deps:
                implementer_nid = node_deps[0]

            if not implementer_nid:
                return

            impl_state = states.get(implementer_nid, {})
            impl_card_id = impl_state.get("kanban_card_id")
            if not impl_card_id:
                return

            # Get the failure report from the blocked card
            from hermes_cli import kanban_db as kb
            board = state.get("kanban_board", "adventours")
            conn = kb.connect(board=board)
            try:
                blocked_card = kb.get_task(conn, task_id)
                failure_report = blocked_card.body if blocked_card else (reason or "Unknown failure")
            except Exception:
                failure_report = reason or "Unknown failure"
            finally:
                conn.close()

            # Enrich the implementer's card with the failure report
            conn = kb.connect(board=board)
            try:
                card = kb.get_task(conn, impl_card_id)
                if card:
                    original_body = card.body or ""
                    enriched_body = (
                        f"{original_body}\n\n"
                        f"## LOOP #{current_loop + 1} — Revision Required\n\n"
                        f"The review failed. Here is the failure report:\n\n"
                        f"{failure_report}\n\n"
                        f"Fix the issues above and try again."
                    )
                    conn.execute(
                        "UPDATE tasks SET body = ?, status = 'ready' WHERE id = ?",
                        (enriched_body, impl_card_id),
                    )
                    conn.commit()
            finally:
                conn.close()

            # Increment loop count
            loop_counts[loop_key] = current_loop + 1
            state["loop_counts"] = loop_counts
            states[node_id]["status"] = "looping"
            states[node_id]["loop_count"] = current_loop + 1
            _save_state_file(state_path, state)
            print(f"   ↩  LOOP #{current_loop + 1}: {implementer_nid} re-dispatched with failure report")

        elif status == "done":
            # Check if all nodes in the current layer are done
            current_layer = state.get("current_layer", 0)
            if current_layer >= len(layers):
                return
            layer_nodes = layers[current_layer]
            all_done = all(
                states.get(nid, {}).get("status") == "done"
                for nid in layer_nodes
            )
            if all_done and node_id in layer_nodes:
                state["current_layer"] = current_layer + 1
                _save_state_file(state_path, state)
                print(f"   ✓ Layer {current_layer} complete — advancing to {current_layer + 1}")

    except Exception as e:
        print(f"   ⚠  Workflow event handler error: {e}")


def _save_state_file(path, state):
    """Persist the workflow state file."""
    import json
    from datetime import datetime, timezone
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    Path(path).write_text(json.dumps(state, indent=2, default=str))
