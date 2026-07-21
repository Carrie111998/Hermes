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
    """Load workflow plugin config from ``~/.hermes/profiles/<profile>/workflow/config.yaml``.

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
        hermes_home / "workflow" / "config.yaml",
        Path.home() / ".hermes" / "workflow" / "config.yaml",
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
    """Register workflow tools and the workflow_analyst auxiliary."""
    ctx.register_auxiliary_task(
        key="workflow_analyst",
        display_name="Workflow analyst",
        description="pipeline escalation, status, and failure analysis",
        defaults={
            "timeout": 180,
            "extra_body": {},
        },
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
