"""Prompt-size diagnostic: ``hermes prompt-size``.

Reports a byte/char breakdown of the system prompt the agent would build for
a fresh session — system prompt total, the ``<available_skills>`` index,
memory + user profile, and tool-schema JSON. Lets users see where their fixed
prompt budget goes (issue #34667) without parsing a saved session JSON by hand.

The diagnostic builds a real inspection agent (so the numbers match what
actually ships on the wire) but never makes a network call: it passes dummy
credentials so ``AIAgent.__init__`` takes the direct-construction path, then
calls ``build_system_prompt_parts`` / inspects ``agent.tools`` offline.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# The skills index is wrapped in this tag pair inside the stable tier.
_SKILLS_BLOCK_RE = re.compile(r"<available_skills>.*?</available_skills>", re.DOTALL)

# A rendered skill entry inside <available_skills> is ``    - name: desc`` (or
# ``    - name`` when the skill has no description). Category headers use two
# leading spaces, so the four-space + ``- `` prefix isolates skill lines.
_SKILL_LINE_PREFIX = "    - "

# Posture-demoted categories render all visible skill names on one shared line.
_NAMES_ONLY_LINE_RE = re.compile(r"^  .+ \[names only\]: (?P<names>.+)$")

# Cap the human-readable "Skills by size" table; ``--json`` always has them all.
_SKILLS_TABLE_LIMIT = 20

# Bucket label for the tool_search bridge tools. They are synthesised at
# assembly time (``tools.tool_search.assemble_tool_defs``) rather than
# registered in a toolset, so they have no registry attribution of their own.
_DEFERRED_BRIDGE_LABEL = "(deferred tool-search bridge)"


def _bytes(s: str) -> int:
    return len(s.encode("utf-8"))


def _tool_name(tool: Any) -> str:
    """Return the callable name of a tool schema (OpenAI ``function`` shape)."""
    if not isinstance(tool, dict):
        return ""
    fn = tool.get("function")
    if isinstance(fn, dict) and fn.get("name"):
        return str(fn["name"])
    return str(tool.get("name", ""))


def _resolve_gui_surface_toolsets(platform: str) -> set:
    """GUI/client-surface toolsets the tui_gateway folds in at agent creation.

    ``desktop``/``tui`` sessions are not built from a ``hermes-<platform>``
    composite — they run through ``tui_gateway.server._load_enabled_toolsets``,
    which resolves the CLI toolsets and then adds the client-surface toolsets
    that are deliberately off ``_HERMES_CORE_TOOLS``. Delegating to the real
    resolver keeps this diagnostic in lockstep with runtime instead of
    re-deriving the set here.
    """
    try:
        from tui_gateway.server import _gui_surface_toolsets

        return set(_gui_surface_toolsets(platform))
    except Exception:
        # Conservative mirror of the resolver's contract if the gateway module
        # is unavailable (e.g. a trimmed install).
        surfaces = {"project"}
        if platform == "desktop":
            surfaces.add("desktop_ui")
        return surfaces


def _resolve_subagent_toolsets(cfg: dict) -> Tuple[List[str], List[str]]:
    """Enabled + extra-disabled toolsets for a default delegated subagent.

    ``tools/delegate_tool.py`` never resolves a ``hermes-subagent`` composite.
    A child *inherits the parent's* enabled toolsets (``child_toolsets =
    _strip_blocked_tools(parent_enabled)``) and is additionally handed
    ``_blocked_toolsets_for_role(role) + ["kanban"]`` as ``disabled_toolsets``
    so the blocked tools are subtracted after composite expansion. Model a
    leaf child of a CLI-configured parent — the common case — so
    ``--platform subagent`` reports a prompt a real subagent would receive.

    Returns ``(enabled, disabled_extra)``.
    """
    from hermes_cli.tools_config import _get_platform_tools

    parent = sorted(_get_platform_tools(cfg, "cli", include_default_mcp_servers=True))
    try:
        from tools.delegate_tool import (
            _blocked_toolsets_for_role,
            _strip_blocked_tools,
        )

        enabled = _strip_blocked_tools(parent)
        disabled_extra = list(
            dict.fromkeys(_blocked_toolsets_for_role("leaf") + ["kanban"])
        )
    except Exception:
        enabled = [ts for ts in parent if ts not in {"delegation", "kanban"}]
        disabled_extra = ["kanban"]
    return sorted(enabled), disabled_extra


def _resolve_inspection_toolsets(
    cfg: dict, platform: str
) -> Tuple[List[str], List[str]]:
    """Resolve enabled + extra-disabled toolsets the way *platform*'s host does.

    ``_get_platform_tools`` only knows platforms that own a ``hermes-<name>``
    composite toolset (see ``hermes_cli/platforms.py``). ``desktop``, ``tui``
    and ``subagent`` are real ``agent.platform`` values with no such composite,
    so a bare lookup resolved to an empty set and the diagnostic reported a
    toolless, skill-less prompt that no session ever ships. Route those three
    through the resolver their actual host uses.

    Returns ``(enabled, disabled_extra)``; ``disabled_extra`` is empty for
    every platform except ``subagent``, whose host imposes a role blocklist.
    """
    from hermes_cli.tools_config import _get_platform_tools

    if platform == "subagent":
        return _resolve_subagent_toolsets(cfg)
    if platform in ("desktop", "tui"):
        # tui_gateway resolves the CLI toolsets, then folds in the surfaces
        # that only exist because of the client on the other end.
        base = _get_platform_tools(cfg, "cli", include_default_mcp_servers=True)
        return sorted(set(base) | _resolve_gui_surface_toolsets(platform)), []
    return sorted(_get_platform_tools(cfg, platform)), []


def _build_inspection_agent(platform: str) -> Any:
    """Construct an offline AIAgent for prompt inspection.

    Dummy ``api_key`` + ``base_url`` force the direct-construction path in
    ``run_agent.py`` (no provider auto-detection, no network). Toolsets and
    platform come from the caller so the breakdown matches a real session.
    """
    from run_agent import AIAgent
    from hermes_cli.config import load_config

    cfg = load_config()
    model_cfg = cfg.get("model", {}) if isinstance(cfg.get("model"), dict) else {}
    model = model_cfg.get("default") or model_cfg.get("model") or ""

    # Resolve platform-specific toolsets the same way the platform's host does.
    enabled_toolsets, disabled_extra = _resolve_inspection_toolsets(cfg, platform)
    agent_cfg = cfg.get("agent") or {}
    from agent.skill_utils import parse_config_string_list

    disabled_toolsets = parse_config_string_list(agent_cfg.get("disabled_toolsets")) or []
    # The host's own role blocklist stacks on top of the user's config.
    disabled_toolsets = list(dict.fromkeys(list(disabled_toolsets) + disabled_extra))

    return AIAgent(
        model=model,
        api_key="inspect-only",
        base_url="https://openrouter.ai/api/v1",
        quiet_mode=True,
        save_trajectories=False,
        platform=platform,
        enabled_toolsets=enabled_toolsets,
        disabled_toolsets=disabled_toolsets or None,
    )


def _skill_md_paths_by_name() -> Dict[str, Path]:
    """Map each installed skill's name to its ``SKILL.md`` path on disk.

    Keyed by both the frontmatter ``name`` (what the index renders) and the
    skill directory name, so either resolves. Local skills win over external
    dirs (``get_all_skills_dirs`` yields local first), matching the index's own
    precedence. Used to attribute the real on-disk read cost per skill.

    Resolution is two-pass: every frontmatter ``name`` is registered first, and
    directory names are only added as aliases for keys no frontmatter claimed.
    A single-pass ``setdefault`` let an earlier skill's *directory* alias shadow
    a later skill's declared ``name`` (renamed skills are exactly this shape),
    so the breakdown attributed one file's bytes to two different skills while
    the real skill's SKILL.md went unreported.
    """
    from agent.skill_utils import (
        get_all_skills_dirs,
        iter_skill_index_files,
        parse_frontmatter,
    )

    by_frontmatter: Dict[str, Path] = {}
    by_dirname: Dict[str, Path] = {}
    for skills_dir in get_all_skills_dirs():
        if not skills_dir.exists():
            continue
        for skill_file in iter_skill_index_files(skills_dir, "SKILL.md"):
            dir_name = skill_file.parent.name
            frontmatter_name = dir_name
            try:
                frontmatter, _ = parse_frontmatter(
                    skill_file.read_text(encoding="utf-8")
                )
                frontmatter_name = str(frontmatter.get("name") or dir_name)
            except Exception:
                pass
            # setdefault keeps the first (local) occurrence on name collisions.
            by_frontmatter.setdefault(frontmatter_name, skill_file)
            by_dirname.setdefault(dir_name, skill_file)

    # Declared names always win; dir names only fill keys nobody declared.
    mapping: Dict[str, Path] = dict(by_frontmatter)
    for dir_name, skill_file in by_dirname.items():
        mapping.setdefault(dir_name, skill_file)
    return mapping


def _compute_skills_breakdown(skills_block: str) -> List[Dict[str, Any]]:
    """Per-skill byte breakdown parsed from the rendered ``<available_skills>``.

    Two honest, distinct numbers per skill:

    * ``index_line_bytes`` — the skill's attributed bytes in the always-on
      index (the fixed per-call cost of *listing* the skill). For a compact
      ``[names only]`` line, each name keeps its own bytes and receives an
      even share of the category prefix and separators. The attributed bytes
      therefore sum exactly to the shared rendered line.
    * ``skill_md_bytes`` — the on-disk size of the skill's ``SKILL.md`` (the
      real token cost paid only when the model loads it via ``skill_view``).
      ``None`` when the name can't be mapped to a file (e.g. a plugin skill
      whose source lives outside the scanned skill dirs).

    Sorted largest-first by ``skill_md_bytes`` (the read cost that dominates
    pruning decisions), tie-broken by name.
    """
    name_to_path = _skill_md_paths_by_name()
    entries: List[Dict[str, Any]] = []

    def append_entry(
        name: str,
        *,
        attributed_bytes: int,
        total_bytes: int,
        shared_bytes: int,
        skill_count: int,
    ) -> None:
        path = name_to_path.get(name)
        md_bytes: Optional[int] = None
        if path is not None:
            try:
                md_bytes = path.stat().st_size
            except OSError:
                md_bytes = None
        entries.append({
            "name": name,
            "index_line_bytes": attributed_bytes,
            "index_line_total_bytes": total_bytes,
            "index_line_shared_bytes": shared_bytes,
            "index_line_skill_count": skill_count,
            "skill_md_bytes": md_bytes,
            "path": str(path) if path is not None else "",
        })

    for line in skills_block.splitlines():
        compact_match = _NAMES_ONLY_LINE_RE.match(line)
        if compact_match is not None:
            names = [
                name.strip()
                for name in compact_match.group("names").split(",")
                if name.strip()
            ]
            if not names:
                continue
            total_bytes = _bytes(line)
            name_bytes = [_bytes(name) for name in names]
            shared_total = total_bytes - sum(name_bytes)
            shared_base, shared_remainder = divmod(shared_total, len(names))
            for index, name in enumerate(names):
                shared_bytes = shared_base + (1 if index < shared_remainder else 0)
                append_entry(
                    name,
                    attributed_bytes=name_bytes[index] + shared_bytes,
                    total_bytes=total_bytes,
                    shared_bytes=shared_bytes,
                    skill_count=len(names),
                )
            continue

        if not line.startswith(_SKILL_LINE_PREFIX):
            continue
        rest = line[len(_SKILL_LINE_PREFIX):]
        # ``name: desc`` — the first ``": "`` separates name from description.
        # Namespaced names (``codex:rescue``) have no space after their colon,
        # so partitioning on ``": "`` keeps the full name intact.
        name = rest.partition(": ")[0].strip()
        if not name:
            continue
        line_bytes = _bytes(line)
        append_entry(
            name,
            attributed_bytes=line_bytes,
            total_bytes=line_bytes,
            shared_bytes=0,
            skill_count=1,
        )
    entries.sort(key=lambda e: (-(e["skill_md_bytes"] or 0), e["name"]))
    return entries


def _compute_toolsets_breakdown(tools: List[Any]) -> List[Dict[str, Any]]:
    """Per-toolset schema-byte breakdown of the resolved tool list.

    Each tool is attributed to its single canonical toolset from the registry,
    so ``json_bytes`` sums are fully attributable: the grand total equals the
    sum of the individual tool serializations (which is the array total from
    ``tools['json_bytes']`` minus JSON framing of ``2 * count`` bytes). Sorted
    largest-first by ``json_bytes``, tie-broken by toolset name.

    ``tool_search``/``tool_describe``/``tool_call`` are synthesised by
    ``tools.tool_search.assemble_tool_defs`` and are in no toolset, so the
    registry map has no entry for them. Bucketing them under ``(unknown)``
    hid the single largest lever on the shipped payload — the deferred-tool
    bridge — behind a label that reads like a bug. They get their own
    ``(deferred tool-search bridge)`` bucket instead, which also carries the
    embedded catalog listing's bytes where the reader can see them.
    """
    from tools.registry import registry

    try:
        from tools.tool_search import BRIDGE_TOOL_NAMES
    except Exception:
        BRIDGE_TOOL_NAMES = frozenset()

    tool_to_toolset = registry.get_tool_to_toolset_map()
    groups: Dict[str, Dict[str, Any]] = {}
    for tool in tools:
        name = _tool_name(tool)
        if name in BRIDGE_TOOL_NAMES:
            toolset = _DEFERRED_BRIDGE_LABEL
        else:
            toolset = tool_to_toolset.get(name) or "(unknown)"
        group = groups.setdefault(
            toolset, {"toolset": toolset, "tool_count": 0, "json_bytes": 0}
        )
        group["tool_count"] += 1
        group["json_bytes"] += _bytes(json.dumps(tool, ensure_ascii=False))
    out = list(groups.values())
    out.sort(key=lambda g: (-g["json_bytes"], g["toolset"]))
    return out


def compute_prompt_breakdown(platform: str = "cli") -> Dict[str, Any]:
    """Return a dict of prompt-size measurements for a fresh session.

    Keys: ``system_prompt`` (chars/bytes), ``skills_index``, ``memory``,
    ``user_profile``, ``tools`` (count + json bytes), ``sections`` (a list of
    (label, chars, bytes) for the three prompt tiers), ``skills_breakdown``
    (per-skill index-line + on-disk SKILL.md bytes, largest-first), and
    ``toolsets_breakdown`` (per-toolset tool count + schema json bytes,
    largest-first). The last two answer "what should I disable to cut tokens?".
    """
    from agent.system_prompt import build_system_prompt, build_system_prompt_parts

    agent = _build_inspection_agent(platform)

    parts = build_system_prompt_parts(agent)
    full = build_system_prompt(agent)

    stable = parts.get("stable", "")
    context = parts.get("context", "")
    volatile = parts.get("volatile", "")

    # Skills index — the <available_skills> block (the largest single block
    # when many skills are installed). Lives in the volatile tier (moved from
    # stable so skill edits don't invalidate the cached identity prefix).
    skills_match = _SKILLS_BLOCK_RE.search(volatile) or _SKILLS_BLOCK_RE.search(stable)
    skills_index = skills_match.group(0) if skills_match else ""

    # Memory + user profile live in the volatile tier. We re-derive their
    # blocks directly from the memory store so the numbers are attributable
    # even though they're joined into ``volatile``.
    memory_block = ""
    user_block = ""
    store = getattr(agent, "_memory_store", None)
    if store is not None:
        try:
            if getattr(agent, "_memory_enabled", True):
                memory_block = store.format_for_system_prompt("memory") or ""
            if getattr(agent, "_user_profile_enabled", True):
                user_block = store.format_for_system_prompt("user") or ""
        except Exception:
            pass

    # Tool-schema JSON — the other half of the fixed per-call payload.
    tools = getattr(agent, "tools", None) or []
    tools_json = json.dumps(tools, ensure_ascii=False)

    sections: List[Tuple[str, int, int]] = [
        ("stable (identity/guidance/skills)", len(stable), _bytes(stable)),
        ("context (AGENTS.md/cwd files)", len(context), _bytes(context)),
        ("volatile (memory/profile/timestamp)", len(volatile), _bytes(volatile)),
    ]

    return {
        "platform": platform,
        "model": getattr(agent, "model", "") or "",
        "system_prompt": {"chars": len(full), "bytes": _bytes(full)},
        "skills_index": {"chars": len(skills_index), "bytes": _bytes(skills_index)},
        "memory": {"chars": len(memory_block), "bytes": _bytes(memory_block)},
        "user_profile": {"chars": len(user_block), "bytes": _bytes(user_block)},
        "tools": {"count": len(tools), "json_bytes": _bytes(tools_json)},
        "sections": sections,
        "skills_breakdown": _compute_skills_breakdown(skills_index),
        "toolsets_breakdown": _compute_toolsets_breakdown(tools),
    }


def _fmt_kb(n: int) -> str:
    return f"{n / 1024:.1f} KB"


def render_breakdown(data: Dict[str, Any]) -> str:
    """Render the breakdown as plain text suitable for a terminal."""
    lines: List[str] = []
    sp = data["system_prompt"]
    lines.append(f"Prompt-size breakdown (platform={data['platform']}, model={data['model'] or 'unset'})")
    lines.append("")
    lines.append(f"  System prompt total : {sp['bytes']:>8,} B  ({_fmt_kb(sp['bytes'])}, {sp['chars']:,} chars)")
    lines.append("")
    lines.append("  Major blocks:")
    si = data["skills_index"]
    mem = data["memory"]
    up = data["user_profile"]
    lines.append(f"    skills index       : {si['bytes']:>8,} B  ({_fmt_kb(si['bytes'])})")
    lines.append(f"    memory             : {mem['bytes']:>8,} B  ({_fmt_kb(mem['bytes'])})")
    lines.append(f"    user profile       : {up['bytes']:>8,} B  ({_fmt_kb(up['bytes'])})")
    lines.append("")
    lines.append("  Prompt tiers:")
    for label, chars, byts in data["sections"]:
        lines.append(f"    {label:<36}: {byts:>8,} B  ({_fmt_kb(byts)})")
    lines.append("")
    tools = data["tools"]
    lines.append(f"  Tool schemas         : {tools['json_bytes']:>8,} B  ({_fmt_kb(tools['json_bytes'])}, {tools['count']} tools)")

    # Per-toolset schema cost — which toolset's tools cost the most to ship.
    toolsets = data.get("toolsets_breakdown") or []
    if toolsets:
        width = max(22, *(len(ts["toolset"]) for ts in toolsets))
        lines.append("")
        lines.append("  Toolsets by size (tool-schema JSON, largest first):")
        lines.append(f"    {'toolset':<{width}} {'tools':>5}  {'schema':>10}")
        for ts in toolsets:
            lines.append(
                f"    {ts['toolset']:<{width}} {ts['tool_count']:>5}  "
                f"{ts['json_bytes']:>8,} B  ({_fmt_kb(ts['json_bytes'])})"
            )

    # Per-skill cost — index line (always shipped) vs SKILL.md (read on load).
    skills = data.get("skills_breakdown") or []
    if skills:
        lines.append("")
        lines.append(
            "  Skills by size (SKILL.md on-disk = read cost; index cost = "
            "attributed always-on bytes, largest first):"
        )
        lines.append(f"    {'skill':<28} {'SKILL.md':>10}  {'index cost':>10}")
        shown = skills[:_SKILLS_TABLE_LIMIT]
        for sk in shown:
            md = sk["skill_md_bytes"]
            md_str = f"{md:>8,} B" if md is not None else f"{'n/a':>10}"
            name = sk["name"]
            if len(name) > 28:
                name = name[:27] + "…"
            lines.append(
                f"    {name:<28} {md_str}  {sk['index_line_bytes']:>8,} B"
            )
        remaining = len(skills) - len(shown)
        if remaining > 0:
            lines.append(f"    … and {remaining} more (use --json for the full list)")
    return "\n".join(lines)


def cmd_prompt_size(args: Any) -> None:
    """Entry point for ``hermes prompt-size``."""
    platform = getattr(args, "platform", "cli") or "cli"
    as_json = getattr(args, "json", False)
    try:
        data = compute_prompt_breakdown(platform)
    except Exception as e:
        print(f"Could not compute prompt-size breakdown: {e}")
        return
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(render_breakdown(data))
