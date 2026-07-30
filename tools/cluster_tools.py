"""Agent tools for multi-PC cluster status and activate."""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

CLUSTER_STATUS_SCHEMA = {
    "name": "cluster_status",
    "description": (
        "List Hermes agent instances across PCs: who is online, who is the "
        "active agent, who owns the messaging gateway (Telegram/etc), and "
        "handoff state. Use before switching or when the user asks which PCs "
        "are available."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}

CLUSTER_ACTIVATE_SCHEMA = {
    "name": "cluster_activate",
    "description": (
        "Switch the active Hermes agent to another PC/instance. Accepts "
        "node_id, machine_id, or hostname. Messaging gateway moves with a "
        "lease handoff (old PC releases Telegram first; if the new PC fails "
        "health-check, the switch rolls back). Tell the user the result."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "description": "node_id, machine_id, or hostname to activate",
            },
            "reason": {
                "type": "string",
                "description": "Optional reason for the switch",
            },
        },
        "required": ["target"],
    },
}


def cluster_status_tool(**kwargs) -> str:
    from hermes_storage import get_storage, is_mongo_mode
    from hermes_storage.orchestrator_client import orch_cluster_status, orchestrator_configured

    if not is_mongo_mode():
        return json.dumps({
            "success": False,
            "error": "Mongo/cluster mode is not enabled (no bootstrap.yaml / HERMES_MONGO_URI).",
        })

    # Prefer mTLS orchestrator when configured — plaintext / no-cert is refused.
    if orchestrator_configured():
        try:
            status = orch_cluster_status()
            if status.get("ok") is False:
                return json.dumps({"success": False, **status}, default=str)
            return json.dumps({"success": True, "via": "orchestrator_mtls", **status}, default=str)
        except Exception as exc:
            return json.dumps({
                "success": False,
                "error": str(exc),
                "hint": "Orchestrator requires a valid agent client certificate (mTLS).",
            })

    storage = get_storage()
    if storage is None:
        return json.dumps({"success": False, "error": "Storage unavailable"})
    status = storage.cluster_status()
    return json.dumps({"success": True, "via": "mongo", **status}, default=str)


def cluster_activate_tool(target: str = "", reason: str = "agent", **kwargs) -> str:
    from hermes_storage import get_storage, is_mongo_mode
    from hermes_storage.orchestrator_client import orch_cluster_activate, orchestrator_configured

    if not is_mongo_mode():
        return json.dumps({
            "success": False,
            "error": "Mongo/cluster mode is not enabled.",
        })

    if not target or not str(target).strip():
        return json.dumps({"success": False, "error": "target is required"})

    if orchestrator_configured():
        try:
            result = orch_cluster_activate(str(target).strip(), reason=reason or "agent")
            ok = result.get("ok", True) and not result.get("error")
            return json.dumps({
                "success": bool(ok),
                "via": "orchestrator_mtls",
                "message": f"Activation via orchestrator toward {target}.",
                **result,
            }, default=str)
        except Exception as exc:
            return json.dumps({
                "success": False,
                "error": str(exc),
                "hint": "Orchestrator requires a valid agent client certificate (mTLS).",
            })

    storage = get_storage()
    if storage is None:
        return json.dumps({"success": False, "error": "Storage unavailable"})

    # Honor agent_can_activate policy from shared settings
    settings = storage.settings.get("default") or {}
    cluster_cfg = settings.get("cluster") if isinstance(settings, dict) else None
    if isinstance(cluster_cfg, dict) and cluster_cfg.get("agent_can_activate") is False:
        return json.dumps({
            "success": False,
            "error": "cluster.agent_can_activate is disabled by policy.",
        })

    try:
        state = storage.activate(str(target).strip(), reason=reason or "agent")
        return json.dumps({
            "success": True,
            "via": "mongo",
            "message": f"Activation/handoff started toward {target}.",
            "state": state,
        }, default=str)
    except Exception as exc:
        return json.dumps({"success": False, "error": str(exc)})


def check_cluster_requirements() -> bool:
    from hermes_storage import is_mongo_mode
    return is_mongo_mode()


# --- Registry ---
from tools.registry import registry

registry.register(
    name="cluster_status",
    toolset="cluster",
    schema=CLUSTER_STATUS_SCHEMA,
    handler=lambda args, **kw: cluster_status_tool(**(args or {})),
    check_fn=check_cluster_requirements,
    emoji="🖥️",
)

registry.register(
    name="cluster_activate",
    toolset="cluster",
    schema=CLUSTER_ACTIVATE_SCHEMA,
    handler=lambda args, **kw: cluster_activate_tool(
        target=(args or {}).get("target", ""),
        reason=(args or {}).get("reason", "agent"),
    ),
    check_fn=check_cluster_requirements,
    emoji="🔀",
)
