"""Agent-facing tool for reading & editing per-chat tool presets.

Lets Hermes itself inspect the available tool/MCP/skill surface and
create / update / delete the reusable presets a chat can adopt (the same
presets the desktop "Tool Presets" settings panel manages). Thin wrapper over
the standalone ``tool_presets`` (CRUD) and ``tool_catalog`` (selectable surface
+ token estimates) modules.

A preset is a named selection persisted to ``config.yaml``:
  - ``enabled_toolsets``: list of toolset names to turn on. ``[]`` = chat-only
    (zero non-core tools); omit / null = profile default (all tools, "Full").
  - ``disabled_tools``: individual tool names to drop (even default/core ones).
  - ``allowed_tools``: individual tool names to add even if their toolset is off.
  - ``disabled_skills``: skill names to hide from the skills index.

Two built-ins always exist and can be customized (delete resets them):
``Chat-only`` and ``Full``.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional


def _catalog_valid_names() -> Dict[str, set]:
    """Return the currently-selectable toolset / tool / skill names.

    Used to warn the model when a save references something that doesn't exist,
    so it can self-correct rather than silently persisting a typo'd name.
    """
    try:
        from tool_catalog import build_catalog

        cat = build_catalog()
    except Exception:
        return {"toolsets": set(), "tools": set(), "skills": set()}

    toolsets = set()
    tools = set()
    for ts in cat.get("toolsets", []):
        toolsets.add(ts.get("name", ""))
        for t in ts.get("tools", []):
            tools.add(t.get("name", ""))
    for srv in cat.get("mcp_servers", []):
        toolsets.add(srv.get("toolset", ""))
        for t in srv.get("tools", []):
            tools.add(t.get("name", ""))
    skills = {s.get("name", "") for s in cat.get("skills", [])}
    return {"toolsets": toolsets, "tools": tools, "skills": skills}


def _unknown(names: Optional[List[str]], known: set) -> List[str]:
    if not names:
        return []
    return [n for n in names if n and n not in known]


def _slim_catalog() -> Dict[str, Any]:
    """A compact catalog for the model: names + token estimates, no schemas."""
    from tool_catalog import build_catalog

    cat = build_catalog()
    return {
        "note": (
            "enabled_toolsets=[] means chat-only (no tools); omit/null means "
            "profile default (all tools). Token estimates are approximate "
            "(chars/4). MCP servers are enabled by their toolset name "
            "(e.g. 'mcp-<server>')."
        ),
        "toolsets": [
            {
                "name": ts.get("name"),
                "est_tokens": ts.get("est_tokens"),
                "tools": [t.get("name") for t in ts.get("tools", [])],
            }
            for ts in cat.get("toolsets", [])
        ],
        "mcp_servers": [
            {
                "name": s.get("name"),
                "toolset": s.get("toolset"),
                "est_tokens": s.get("est_tokens"),
                "tools": [t.get("name") for t in s.get("tools", [])],
            }
            for s in cat.get("mcp_servers", [])
        ],
        "skills": [
            {"name": s.get("name"), "category": s.get("category"), "est_tokens": s.get("est_tokens")}
            for s in cat.get("skills", [])
        ],
    }


def manage_presets_tool(args: Dict[str, Any]) -> str:
    """Single entry point for the ``manage_presets`` tool.

    ``args["action"]`` selects the operation: ``list`` / ``catalog`` /
    ``save`` / ``delete``. Returns a JSON string.
    """
    from tools.registry import tool_error

    import tool_presets as tp

    action = str((args or {}).get("action") or "").strip().lower()

    if action == "list":
        # Surface the configured default so the model understands which preset a
        # NEW chat starts with (null = no default → platform/coding posture).
        return json.dumps(
            {"presets": tp.list_presets(), "default": tp.get_default_preset()},
            ensure_ascii=False,
        )

    if action == "catalog":
        try:
            return json.dumps(_slim_catalog(), ensure_ascii=False)
        except Exception as e:  # pragma: no cover — defensive
            return tool_error(f"could not build catalog: {e}")

    if action == "save":
        name = str(args.get("name") or "").strip()
        if not name:
            return tool_error("'name' is required to save a preset")

        # Only forward keys the caller actually provided so an omitted
        # enabled_toolsets persists as "profile default" (null) rather than [].
        preset: Dict[str, Any] = {"name": name}
        for field in ("enabled_toolsets", "disabled_tools", "allowed_tools", "disabled_skills"):
            if field in args:
                preset[field] = args[field]

        # Warn (don't fail) on names that aren't in the current catalog — the
        # user may be authoring for tools that need credentials to appear.
        known = _catalog_valid_names()
        warnings: List[str] = []
        for ts in _unknown(preset.get("enabled_toolsets"), known["toolsets"]):
            warnings.append(f"toolset '{ts}' is not currently available")
        for t in _unknown(preset.get("disabled_tools"), known["tools"]):
            warnings.append(f"tool '{t}' (disabled_tools) is not currently available")
        for t in _unknown(preset.get("allowed_tools"), known["tools"]):
            warnings.append(f"tool '{t}' (allowed_tools) is not currently available")
        for s in _unknown(preset.get("disabled_skills"), known["skills"]):
            warnings.append(f"skill '{s}' is not currently available")

        try:
            presets = tp.save_preset(preset)
        except ValueError as e:
            return tool_error(str(e))

        saved = next((p for p in presets if p.get("name") == name), None)
        return json.dumps(
            {"ok": True, "saved": saved, "presets": presets, "warnings": warnings},
            ensure_ascii=False,
        )

    if action == "set_default":
        # Empty/omitted name clears the default (new chats fall through to the
        # platform/coding posture). A provided name must be a real preset so the
        # model can't silently persist a typo.
        name = str(args.get("name") or "").strip()
        if name:
            known = {p.get("name") for p in tp.list_presets()}
            if name not in known:
                return tool_error(
                    f"'{name}' is not a known preset. Use action='list' to see "
                    "available presets, or omit 'name' to clear the default."
                )
        default = tp.set_default_preset(name or None)
        return json.dumps(
            {"ok": True, "default": default, "presets": tp.list_presets()},
            ensure_ascii=False,
        )

    if action == "delete":
        name = str(args.get("name") or "").strip()
        if not name:
            return tool_error("'name' is required to delete a preset")
        presets = tp.delete_preset(name)
        # Built-ins are reset (still present) rather than removed.
        was_reset = name in getattr(tp, "RESERVED_NAMES", set())
        return json.dumps(
            {"ok": True, "reset" if was_reset else "deleted": name, "presets": presets},
            ensure_ascii=False,
        )

    return tool_error(
        f"unknown action '{action}'. Use one of: list, catalog, save, "
        "set_default, delete"
    )


def check_manage_presets_requirements() -> bool:
    """Preset management works everywhere (reads/writes config.yaml)."""
    return True


MANAGE_PRESETS_SCHEMA = {
    "name": "manage_presets",
    "description": (
        "Read and edit the user's reusable per-chat tool presets (the same "
        "presets shown in the desktop Tool Presets settings and the chat "
        "tool-posture picker). Use this to create or adjust a preset on the "
        "user's behalf.\n\n"
        "Actions:\n"
        "- action='catalog': list every selectable toolset, MCP server, tool, "
        "and skill with approximate token costs. Call this FIRST when creating "
        "a preset so you use real names and understand the token trade-offs.\n"
        "- action='list': show existing presets and their current selections, "
        "plus 'default' — the preset every NEW chat starts with (null = no "
        "default, new chats use the profile/platform posture).\n"
        "- action='save': create or update a preset by name.\n"
        "- action='set_default': set which preset new chats start with. Pass "
        "'name' to make it the default; omit 'name' to clear it. This does NOT "
        "change existing chats — only ones created afterward.\n"
        "- action='delete': delete a user preset (or reset a built-in to default).\n\n"
        "Preset fields (for save):\n"
        "- enabled_toolsets: toolset names to enable. Omit or null = profile "
        "default (all tools, 'Full'). [] (empty) = chat-only, zero non-core "
        "tools. Enable an MCP server via its 'mcp-<server>' toolset name.\n"
        "- disabled_tools: individual tool names to remove, even default ones "
        "(e.g. drop 'terminal' or 'browser_navigate').\n"
        "- allowed_tools: individual tool names to include even if their "
        "toolset is off.\n"
        "- disabled_skills: skill names to hide from the skills index.\n\n"
        "'Chat-only' and 'Full' are reserved built-ins; you may customize them "
        "(delete resets them). Saving a preset does not change the current "
        "chat — the user applies it from the tool-posture picker. To control "
        "what NEW chats start with, use action='set_default'."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "catalog", "save", "set_default", "delete"],
                "description": "The operation to perform.",
            },
            "name": {
                "type": "string",
                "description": (
                    "Preset name. Required for save/delete. For set_default, the "
                    "preset to make the default for new chats; omit to clear it."
                ),
            },
            "enabled_toolsets": {
                "type": ["array", "null"],
                "items": {"type": "string"},
                "description": (
                    "Toolset names to enable. Omit/null = profile default (all "
                    "tools). [] = chat-only (no tools)."
                ),
            },
            "disabled_tools": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Individual tool names to remove (even default/core tools).",
            },
            "allowed_tools": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Individual tool names to add even if their toolset is off.",
            },
            "disabled_skills": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Skill names to hide from the skills index.",
            },
        },
        "required": ["action"],
    },
}


# --- Registry ---
from tools.registry import registry

registry.register(
    name="manage_presets",
    toolset="tool_presets",
    schema=MANAGE_PRESETS_SCHEMA,
    handler=lambda args, **kw: manage_presets_tool(args or {}),
    check_fn=check_manage_presets_requirements,
    emoji="🎛️",
)
