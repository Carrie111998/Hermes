"""Bounded OSINT façade over the world-intel-mcp package.

The native MCP server exposes the complete low-level tool catalog.  This
plugin adds a small, stable surface for the unified OSINT agent so routine
briefs do not need to select among 113 individual tools.
"""

from __future__ import annotations

import asyncio
import importlib
import json
from typing import Any


_ALLOWED_TOOLS = {
    "intel_alert_digest",
    "intel_cyber_threats",
    "intel_country_dossier",
    "intel_earthquakes",
    "intel_gdelt_search",
    "intel_news_feed",
    "intel_space_weather",
    "intel_status",
    "intel_strategic_posture",
    "intel_world_brief",
}


def _server_module():
    return importlib.import_module("world_intel_mcp.server")


def check_available() -> bool:
    try:
        _server_module()
        return True
    except Exception:
        return False


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _dispatch(name: str, arguments: dict[str, Any]) -> Any:
    if name not in _ALLOWED_TOOLS:
        return {
            "success": False,
            "error": f"Tool is not allowed by world-intel-osint façade: {name}",
            "allowed_tools": sorted(_ALLOWED_TOOLS),
        }
    try:
        return asyncio.run(_server_module()._dispatch(name, arguments))
    except Exception as exc:
        return {"success": False, "error": str(exc)[:500], "tool": name}


STATUS_SCHEMA = {
    "name": "world_intel_status",
    "description": "Check local World Intel MCP package readiness and exposed tool count.",
    "parameters": {"type": "object", "properties": {}, "required": []},
}

QUERY_SCHEMA = {
    "name": "world_intel_query",
    "description": (
        "Run a bounded World Intel OSINT query. Use this façade for routine collection; "
        "the native world-intel MCP server still exposes the complete 113-tool catalog."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "tool": {"type": "string", "enum": sorted(_ALLOWED_TOOLS)},
            "arguments": {"type": "object", "description": "Arguments for the selected intel tool."},
        },
        "required": ["tool"],
    },
}

BRIEF_SCHEMA = {
    "name": "world_intel_brief",
    "description": (
        "Collect a compact multi-source OSINT brief using World Intel: strategic posture, "
        "alerts, news, cyber threats, and space weather."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "news_category": {"type": "string", "description": "Optional RSS category filter."},
            "news_limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 10},
            "cyber_limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 10},
        },
        "required": [],
    },
}


def handle_status(_args: dict[str, Any], **_: Any) -> str:
    try:
        server = _server_module()
        tools = getattr(server, "TOOLS", [])
        return _json({
            "success": True,
            "package": "world-intel-mcp",
            "server_module": server.__name__,
            "tool_count": len(tools),
            "expected_tool_count": 113,
            "mcp_server_command": "world-intel-mcp",
            "native_mcp": "configured separately as server 'world-intel'",
            "façade_tools": sorted(_ALLOWED_TOOLS),
        })
    except Exception as exc:
        return _json({"success": False, "error": str(exc)[:500]})


def handle_query(args: dict[str, Any], **_: Any) -> str:
    name = str(args.get("tool") or "").strip()
    arguments = args.get("arguments")
    if not isinstance(arguments, dict):
        arguments = {}
    return _json(_dispatch(name, arguments))


async def _brief(args: dict[str, Any]) -> dict[str, Any]:
    news_limit = max(1, min(int(args.get("news_limit", 10)), 20))
    cyber_limit = max(1, min(int(args.get("cyber_limit", 10)), 20))
    news_args: dict[str, Any] = {"limit": news_limit}
    if args.get("news_category"):
        news_args["category"] = str(args["news_category"])
    calls = {
        "strategic_posture": ("intel_strategic_posture", {}),
        "alert_digest": ("intel_alert_digest", {}),
        "news": ("intel_news_feed", news_args),
        "cyber": ("intel_cyber_threats", {"limit": cyber_limit}),
        "space_weather": ("intel_space_weather", {}),
    }
    results = await asyncio.gather(
        *(_server_module()._dispatch(tool, args) for tool, args in calls.values()),
        return_exceptions=True,
    )
    payload = {}
    for key, result in zip(calls, results):
        payload[key] = {"success": False, "error": str(result)[:500]} if isinstance(result, Exception) else result
    return {"success": True, "sources": payload}


def handle_brief(args: dict[str, Any], **_: Any) -> str:
    try:
        return _json(asyncio.run(_brief(args)))
    except Exception as exc:
        return _json({"success": False, "error": str(exc)[:500]})


def handle_slash(args: str) -> str:
    sub = (args or "").strip().split()
    if sub and sub[0].lower() in {"brief", "run"}:
        return handle_brief({})
    if sub and sub[0].lower() == "status":
        return handle_status({})
    return "Usage: /world-intel-osint [status|brief]"


def register(ctx) -> None:
    ctx.register_tool(
        name="world_intel_status",
        toolset="world_intel_osint",
        schema=STATUS_SCHEMA,
        handler=handle_status,
        check_fn=check_available,
        emoji="WI",
    )
    ctx.register_tool(
        name="world_intel_query",
        toolset="world_intel_osint",
        schema=QUERY_SCHEMA,
        handler=handle_query,
        check_fn=check_available,
        emoji="WI",
    )
    ctx.register_tool(
        name="world_intel_brief",
        toolset="world_intel_osint",
        schema=BRIEF_SCHEMA,
        handler=handle_brief,
        check_fn=check_available,
        emoji="WI",
    )
    ctx.register_command(
        "world-intel-osint",
        handler=handle_slash,
        description="Bounded World Intel OSINT brief and status.",
        args_hint="[status|brief]",
    )
