"""Tests for the ``hermes prompt-size`` diagnostic (issue #34667)."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli.prompt_size import (
    _DEFERRED_BRIDGE_LABEL,
    _SKILLS_BLOCK_RE,
    _build_inspection_agent,
    _compute_skills_breakdown,
    _compute_toolsets_breakdown,
    _resolve_inspection_toolsets,
    _skill_md_paths_by_name,
    compute_prompt_breakdown,
    render_breakdown,
)


def _seed_memory(hermes_home, memory_text="", user_text=""):
    mem_dir = hermes_home / "memories"
    mem_dir.mkdir(parents=True, exist_ok=True)
    if memory_text:
        (mem_dir / "MEMORY.md").write_text(memory_text, encoding="utf-8")
    if user_text:
        (mem_dir / "USER.md").write_text(user_text, encoding="utf-8")


def _seed_skill(hermes_home, name, description):
    skill_dir = hermes_home / "skills" / "demo" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n# {name}\nbody\n",
        encoding="utf-8",
    )


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.chdir(tmp_path)  # avoid picking up the repo's AGENTS.md
    return hermes_home




def test_runs_offline_without_credentials(isolated_home, monkeypatch):
    """No provider credentials configured → still produces a breakdown."""
    for var in ("OPENROUTER_API_KEY", "OPENAI_API_KEY", "NOUS_API_KEY",
                "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    data = compute_prompt_breakdown("cli")
    assert data["system_prompt"]["bytes"] > 0












def test_skills_breakdown_shape_sorted_and_attributed(isolated_home):
    """Per-skill breakdown reports index-line + on-disk SKILL.md bytes.

    Seeded before the first build (skills prompt is cached per-process).
    """
    _seed_skill(isolated_home, "small-skill", "short desc")
    _seed_skill(isolated_home, "big-skill", "a much longer description " * 20)
    data = compute_prompt_breakdown("cli")
    skills = data["skills_breakdown"]
    names = {s["name"] for s in skills}
    assert {"small-skill", "big-skill"} <= names
    for s in skills:
        assert set(s) >= {"name", "index_line_bytes", "skill_md_bytes", "path"}
        assert s["index_line_bytes"] > 0
    # Sorted largest-first by on-disk SKILL.md size.
    md_sizes = [s["skill_md_bytes"] or 0 for s in skills]
    assert md_sizes == sorted(md_sizes, reverse=True)
    # On-disk bytes match the real file; big-skill's SKILL.md is the larger.
    by_name = {s["name"]: s for s in skills}
    big = by_name["big-skill"]
    assert big["path"] and Path(big["path"]).stat().st_size == big["skill_md_bytes"]
    assert big["skill_md_bytes"] > by_name["small-skill"]["skill_md_bytes"]
    # Per-skill index lines are a subset of the whole <available_skills> block,
    # so they never exceed it (on-disk SKILL.md bytes are separate and don't).
    assert sum(s["index_line_bytes"] for s in skills) <= data["skills_index"]["bytes"]


def test_skills_breakdown_attributes_demoted_category_shared_line(isolated_home):
    """A real posture-demoted category retains every skill in the breakdown."""
    from agent.prompt_builder import build_skills_system_prompt

    _seed_skill(isolated_home, "alpha-skill", "alpha description")
    _seed_skill(isolated_home, "beta-skill", "beta description")
    prompt = build_skills_system_prompt(compact_categories=frozenset({"demo"}))
    skills_match = _SKILLS_BLOCK_RE.search(prompt)
    assert skills_match is not None
    skills_block = skills_match.group(0)
    shared_line = next(
        line for line in skills_block.splitlines() if "demo [names only]" in line
    )

    entries = _compute_skills_breakdown(skills_block)
    by_name = {entry["name"]: entry for entry in entries}
    assert set(by_name) == {"alpha-skill", "beta-skill"}

    shared_line_bytes = len(shared_line.encode("utf-8"))
    assert sum(entry["index_line_bytes"] for entry in entries) == shared_line_bytes
    for entry in entries:
        assert entry["index_line_total_bytes"] == shared_line_bytes
        assert entry["index_line_shared_bytes"] > 0
        assert entry["index_line_skill_count"] == 2


# ---------------------------------------------------------------------------
# Composite-less platforms (desktop / tui / subagent)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("platform", ["desktop", "tui", "subagent"])
def test_composite_less_platforms_resolve_real_toolsets(isolated_home, platform):
    """desktop/tui/subagent own no ``hermes-<platform>`` composite toolset.

    A bare ``_get_platform_tools`` lookup returns an empty set for them, which
    made the diagnostic report a toolless, skill-less prompt no session ships.
    The resolver must route them through their real host instead.
    """
    from hermes_cli.config import load_config

    cfg = load_config()
    enabled, _disabled_extra = _resolve_inspection_toolsets(cfg, platform)
    assert enabled, f"{platform} resolved to no toolsets"
    # The composite these platforms do NOT have must never appear.
    assert f"hermes-{platform}" not in enabled


def test_desktop_folds_in_gui_surface_toolsets(isolated_home):
    """``desktop`` gets the client-surface toolsets the tui_gateway adds.

    They are deliberately off ``_HERMES_CORE_TOOLS``, so only the gateway
    resolver exposes them — mirroring it is what keeps the numbers honest.
    """
    from hermes_cli.config import load_config

    cfg = load_config()
    desktop, _ = _resolve_inspection_toolsets(cfg, "desktop")
    tui, _ = _resolve_inspection_toolsets(cfg, "tui")
    assert "desktop_ui" in desktop
    assert "project" in desktop
    # ``desktop_ui`` is desktop-only; ``project`` is on both GUI surfaces.
    assert "desktop_ui" not in tui
    assert "project" in tui


def test_subagent_carries_delegation_role_blocklist(isolated_home):
    """A leaf subagent inherits the parent's toolsets minus the role blocklist."""
    from hermes_cli.config import load_config

    cfg = load_config()
    enabled, disabled_extra = _resolve_inspection_toolsets(cfg, "subagent")
    assert enabled
    # delegate_task/clarify/memory/cronjob are blocked for a leaf child.
    assert "delegation" not in enabled
    assert "kanban" in disabled_extra


@pytest.mark.parametrize("platform", ["desktop", "tui", "subagent"])
def test_breakdown_reports_tools_and_skills_for_composite_less_platforms(
    isolated_home, platform
):
    """Regression: these three previously reported skills_index=0 and tools=0."""
    _seed_skill(isolated_home, "probe-skill", "probe description")
    data = compute_prompt_breakdown(platform)
    assert data["tools"]["count"] > 0
    # An empty tool list serialises to ``[]`` — exactly 2 bytes.
    assert data["tools"]["json_bytes"] > 2
    assert data["skills_index"]["bytes"] > 0
    assert data["skills_breakdown"]
    assert data["toolsets_breakdown"]


# ---------------------------------------------------------------------------
# Skill name/dir collisions
# ---------------------------------------------------------------------------


def _write_skill(path: Path, *, name: str, body_size: int) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: d\n---\n" + "x" * body_size,
        encoding="utf-8",
    )


def test_declared_name_beats_another_skills_dir_alias(tmp_path, monkeypatch):
    """A skill's declared ``name`` must not be shadowed by another's dir alias.

    ``local/alpha/`` declares ``name: renamed`` (a renamed skill — the common
    shape), while ``ext/other/`` genuinely declares ``name: alpha``. The old
    single-pass ``setdefault`` registered the ``alpha`` *directory* alias first,
    so both names resolved to the same file: one file's bytes were reported
    twice and the real ``alpha`` skill's SKILL.md never appeared.
    """
    import agent.skill_utils as su

    local = tmp_path / "local"
    ext = tmp_path / "ext"
    _write_skill(local / "alpha", name="renamed", body_size=50)
    _write_skill(ext / "other", name="alpha", body_size=9000)

    monkeypatch.setattr(su, "get_all_skills_dirs", lambda *a, **k: [local, ext])

    mapping = _skill_md_paths_by_name()
    assert mapping["alpha"] == ext / "other" / "SKILL.md"
    assert mapping["renamed"] == local / "alpha" / "SKILL.md"

    block = (
        "<available_skills>\n  cat:\n"
        "    - alpha: d\n"
        "    - renamed: d\n"
        "</available_skills>"
    )
    entries = {e["name"]: e for e in _compute_skills_breakdown(block)}
    # Distinct files, distinct sizes — no double-counted winner path.
    assert entries["alpha"]["path"] != entries["renamed"]["path"]
    assert entries["alpha"]["skill_md_bytes"] > entries["renamed"]["skill_md_bytes"]
    paths = [e["path"] for e in entries.values()]
    assert len(set(paths)) == len(paths)


def test_local_skill_still_wins_on_true_name_collision(tmp_path, monkeypatch):
    """Two skills declaring the SAME name: local still wins (index precedence)."""
    import agent.skill_utils as su

    local = tmp_path / "local"
    ext = tmp_path / "ext"
    _write_skill(local / "dupe", name="dupe", body_size=10)
    _write_skill(ext / "dupe", name="dupe", body_size=9000)

    monkeypatch.setattr(su, "get_all_skills_dirs", lambda *a, **k: [local, ext])

    mapping = _skill_md_paths_by_name()
    assert mapping["dupe"] == local / "dupe" / "SKILL.md"


# ---------------------------------------------------------------------------
# Deferred tool-search bridge attribution
# ---------------------------------------------------------------------------


def _fn_tool(name: str, description: str = "d") -> dict:
    return {
        "type": "function",
        "function": {"name": name, "description": description, "parameters": {}},
    }


def test_bridge_tools_get_their_own_bucket_not_unknown():
    """tool_search/tool_describe/tool_call are synthesised, not registry-owned.

    They previously landed in ``(unknown)``, hiding the deferred-tool bridge —
    often the largest single lever on the shipped payload — behind a label that
    reads like a bug.
    """
    from tools.tool_search import BRIDGE_TOOL_NAMES

    tools = [_fn_tool(name) for name in sorted(BRIDGE_TOOL_NAMES)]
    tools.append(_fn_tool("read_file"))

    groups = {g["toolset"]: g for g in _compute_toolsets_breakdown(tools)}
    assert _DEFERRED_BRIDGE_LABEL in groups
    assert groups[_DEFERRED_BRIDGE_LABEL]["tool_count"] == len(BRIDGE_TOOL_NAMES)
    assert "(unknown)" not in groups


def test_toolsets_breakdown_bytes_are_fully_attributable():
    """Per-toolset json_bytes sum to the array total minus JSON framing."""
    from tools.tool_search import BRIDGE_TOOL_NAMES

    tools = [_fn_tool(n) for n in sorted(BRIDGE_TOOL_NAMES)] + [_fn_tool("read_file")]
    total = len(json.dumps(tools, ensure_ascii=False).encode("utf-8"))
    attributed = sum(g["json_bytes"] for g in _compute_toolsets_breakdown(tools))
    # Framing: the enclosing "[]" plus ", " between each pair of elements.
    assert attributed == total - 2 - 2 * (len(tools) - 1)


def test_render_does_not_truncate_the_bridge_label():
    """The bridge label is wider than the old fixed column — must stay intact."""
    data = {
        "platform": "cli",
        "model": "m",
        "system_prompt": {"chars": 1, "bytes": 1},
        "skills_index": {"chars": 0, "bytes": 0},
        "memory": {"chars": 0, "bytes": 0},
        "user_profile": {"chars": 0, "bytes": 0},
        "tools": {"count": 3, "json_bytes": 300},
        "sections": [],
        "skills_breakdown": [],
        "toolsets_breakdown": [
            {"toolset": _DEFERRED_BRIDGE_LABEL, "tool_count": 3, "json_bytes": 300}
        ],
    }
    out = render_breakdown(data)
    assert _DEFERRED_BRIDGE_LABEL in out




