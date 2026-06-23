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
    "auto_deliver": True,
    "max_nodes_per_workflow": 256,
    "max_dispatch_per_call": 16,
    "default_scope": "project",
    "default_delivery_target": "",
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
    """Register the workflow_analyst auxiliary with the Hermes plugin system."""
    ctx.register_auxiliary_task(
        key="workflow_analyst",
        display_name="Workflow analyst",
        description="pipeline escalation, status, and failure analysis",
        defaults={
            "timeout": 180,
            "extra_body": {},
        },
    )
