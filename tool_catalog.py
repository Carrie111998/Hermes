"""Selectable tool/MCP/skill catalog with per-item token estimates.

Powers the ``tools.catalog`` JSON-RPC method (the preset editor's per-item
token badges + running total). Standalone (imported by ``tui_gateway/server.py``
as a thin wrapper) so the estimation logic stays unit-testable and off the hot
file.

Token estimate rule mirrors Tool Search: ``len(json.dumps(schema)) / 4`` (the
``CHARS_PER_TOKEN`` rule in ``tools/tool_search.py``). Skill estimate is its
``<available_skills>`` index-entry char count / 4, reusing the same approach as
``hermes_cli/prompt_size.py``.
"""

from __future__ import annotations

import json
import logging
import math
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

try:
    from tools.tool_search import CHARS_PER_TOKEN
except Exception:  # pragma: no cover — defensive fallback
    CHARS_PER_TOKEN = 4.0


def _est_tokens_from_chars(char_count: int) -> int:
    if char_count <= 0:
        return 0
    return int(math.ceil(char_count / CHARS_PER_TOKEN))


def _schema_tokens(schema: Dict[str, Any]) -> int:
    """Estimate a single tool schema's token cost (chars/4)."""
    try:
        text = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        text = str(schema)
    return _est_tokens_from_chars(len(text))


def _skill_index_tokens(name: str, description: str) -> int:
    """Estimate a skill's ``<available_skills>`` index-entry token cost.

    Matches the per-skill line rendered by
    ``agent/prompt_builder.build_skills_system_prompt`` (``    - name: desc``).
    """
    if description:
        line = f"    - {name}: {description}\n"
    else:
        line = f"    - {name}\n"
    return _est_tokens_from_chars(len(line))


def build_catalog(profile: str | None = None) -> Dict[str, Any]:
    """Build the full selectable catalog with per-item token estimates.

    Returns the ``tools.catalog`` payload::

        {
          "core_tokens": int,
          "toolsets":    [ {name, description, est_tokens, tools:[{name, est_tokens}]} ],
          "mcp_servers": [ {name, toolset, est_tokens, tools:[{name, est_tokens}]} ],
          "skills":      [ {name, category, est_tokens} ],
        }
    """
    from model_tools import get_tool_definitions, get_toolset_for_tool

    # Full available surface (no tool-search collapse, no per-tool filter) so
    # the editor can show every selectable tool with a real schema estimate.
    all_defs = get_tool_definitions(
        enabled_toolsets=None,
        quiet_mode=True,
        skip_tool_search_assembly=True,
    ) or []

    # Toolset descriptions for the grouped output.
    try:
        from toolsets import get_all_toolsets
        toolset_defs = get_all_toolsets() or {}
    except Exception:
        toolset_defs = {}

    # No tool is unconditionally included — an empty toolset selection yields
    # zero tools. So every tool (including the ``_HERMES_CORE_TOOLS`` default
    # set) is surfaced under its toolset and is individually selectable; there
    # is no hidden "always on" baseline to lock away.
    core_tokens = 0
    # toolset_name -> {"tools": [...], "est_tokens": int}
    toolset_groups: Dict[str, Dict[str, Any]] = {}
    # server_name -> {"toolset": str, "tools": [...], "est_tokens": int}
    mcp_groups: Dict[str, Dict[str, Any]] = {}

    for td in all_defs:
        fn = td.get("function", {}) if isinstance(td, dict) else {}
        name = fn.get("name", "")
        if not name:
            continue
        est = _schema_tokens(td)

        toolset = get_toolset_for_tool(name) or "other"
        item = {"name": name, "est_tokens": est}

        if str(toolset).startswith("mcp-"):
            server = str(toolset)[len("mcp-"):]
            grp = mcp_groups.setdefault(
                server, {"toolset": toolset, "tools": [], "est_tokens": 0}
            )
            grp["tools"].append(item)
            grp["est_tokens"] += est
        else:
            grp = toolset_groups.setdefault(
                toolset, {"tools": [], "est_tokens": 0}
            )
            grp["tools"].append(item)
            grp["est_tokens"] += est

    toolsets_out: List[Dict[str, Any]] = []
    for name in sorted(toolset_groups.keys()):
        grp = toolset_groups[name]
        desc = ""
        ts_def = toolset_defs.get(name)
        if isinstance(ts_def, dict):
            desc = str(ts_def.get("description") or "")
        toolsets_out.append(
            {
                "name": name,
                "description": desc,
                "est_tokens": grp["est_tokens"],
                "tools": sorted(grp["tools"], key=lambda tool_item: tool_item["name"]),
            }
        )

    mcp_out: List[Dict[str, Any]] = []
    for server in sorted(mcp_groups.keys()):
        grp = mcp_groups[server]
        mcp_out.append(
            {
                "name": server,
                "toolset": grp["toolset"],
                "est_tokens": grp["est_tokens"],
                "tools": sorted(grp["tools"], key=lambda tool_item: tool_item["name"]),
            }
        )

    skills_out: List[Dict[str, Any]] = []
    try:
        from tools.skills_tool import _find_all_skills

        for skill in _find_all_skills():
            sname = skill.get("name", "")
            if not sname:
                continue
            category = skill.get("category") or "general"
            description = str(skill.get("description") or "")
            skills_out.append(
                {
                    "name": sname,
                    "category": category,
                    "est_tokens": _skill_index_tokens(sname, description),
                }
            )
        skills_out.sort(key=lambda s: (s["category"], s["name"]))
    except Exception:
        logger.warning("tool_catalog: failed to enumerate skills", exc_info=True)
        skills_out = []

    return {
        "core_tokens": core_tokens,
        "toolsets": toolsets_out,
        "mcp_servers": mcp_out,
        "skills": skills_out,
    }
