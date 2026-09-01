"""Built-in gateway hooks that are always registered."""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Patterns that indicate admin/internal messages that should not leak to groups.
_ADMIN_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in [
        r"\badmin\b.*\b(panel|interface|dashboard)\b",
        r"\b(internal|debug|diagnostic)\b.*\b(message|log|output)\b",
        r"\brecovery\b.*\b(job|task|process)\b",
        r"\bcold.?start\b.*\b(cursor|replay|restore)\b",
        r"\bsystem\b.*\b(notification|alert|status)\b",
    ]
]


def _is_admin_content(text: str) -> bool:
    """Fast-path keyword check for admin/internal content."""
    return any(p.search(text) for p in _ADMIN_PATTERNS)


def outbound_gate(
    content: str,
    chat_type: str = "dm",
    gateway: Any = None,
    **kwargs: Any,
) -> Optional[Dict[str, Any]]:
    """Reference pre_gateway_send gate: block admin content from group chats.

    Reads config from gateway.config.gate (if present):
      enabled: bool (default False)
      group_chat_types: list[str] (default ["group", "supergroup"])
      redirect_target: str (default "" — no redirect, just block)

    Returns None (allow) or an action dict (block/redirect).
    """
    # Only gate group/supergroup messages
    gated_types = {"group", "supergroup"}
    if chat_type not in gated_types:
        return None

    # Check gateway config for gate settings
    gate_config = None
    if gateway is not None:
        try:
            gw_config = getattr(gateway, "config", None)
            if gw_config is not None:
                gate_config = getattr(gw_config, "gate", None)
        except Exception:
            pass

    # If no config or not enabled, allow everything
    if gate_config is None:
        return None
    if not getattr(gate_config, "enabled", False):
        return None

    # Check gated chat types from config
    config_gated_types = getattr(gate_config, "group_chat_types", None)
    if config_gated_types and chat_type not in config_gated_types:
        return None

    # Fast-path: if content doesn't match admin patterns, allow
    if not _is_admin_content(content):
        return None

    # Admin content detected in a group chat — block or redirect
    redirect_target = getattr(gate_config, "redirect_target", "") or ""
    if redirect_target:
        return {"action": "redirect", "target": redirect_target}
    return {"action": "block", "reason": "admin-content-gate"}


def register_builtin_hooks(ctx: Any) -> None:
    """Register all built-in hooks with the plugin context."""
    ctx.register_hook("pre_gateway_send", outbound_gate)
