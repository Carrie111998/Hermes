"""kanban-enforcement plugin — dispatch-routing enforcement gate.

Registers pre_tool_call / post_tool_call / post_llm_call /
on_session_start / on_session_end hooks with the PluginManager.

The enforcement logic lives in ``hermes_cli.kanban_enforcement`` so it is
directly testable without going through the plugin infrastructure.  This
module is a thin registration shim.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def register(ctx):
    """Register enforcement hooks via the supported PluginContext path.

    Called by PluginManager._load_plugin when the bundled kanban-enforcement
    plugin is discovered during startup.  Idempotent — the enforcement module
    guards against double registration.
    """
    from hermes_cli import kanban_enforcement

    kanban_enforcement.register_enforcement_hooks(ctx)
    logger.debug("kanban-enforcement plugin registered")
