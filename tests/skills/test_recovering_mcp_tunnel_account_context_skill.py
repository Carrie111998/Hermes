"""Contract tests for the MCP tunnel account-context recovery skill."""

import re
from pathlib import Path

import yaml


SKILL_PATH = (
    Path(__file__).resolve().parents[2]
    / "optional-skills"
    / "autonomous-ai-agents"
    / "recovering-mcp-tunnel-account-context"
    / "SKILL.md"
)


def _frontmatter_and_body():
    content = SKILL_PATH.read_text(encoding="utf-8")
    assert content.startswith("---\n")
    match = re.search(r"\n---\s*\n", content[3:])
    assert match, "frontmatter must close with ---"
    frontmatter = yaml.safe_load(content[3 : match.start() + 3])
    body = content[match.end() + 3 :]
    return frontmatter, body


def test_frontmatter_and_portability_contract():
    frontmatter, _ = _frontmatter_and_body()
    assert frontmatter["name"] == "recovering-mcp-tunnel-account-context"
    assert len(frontmatter["description"]) <= 60
    assert frontmatter["description"].endswith(".")
    assert frontmatter["author"].startswith(
        "Wade Packard (brandonwadepackard-cell),"
    )
    assert frontmatter["platforms"] == ["linux", "macos", "windows"]
    assert frontmatter["metadata"]["hermes"]["category"] == (
        "autonomous-ai-agents"
    )
    content = SKILL_PATH.read_text(encoding="utf-8")
    assert "/Users/" not in content
    assert "/home/" not in content
    assert not re.search(r"sk-[A-Za-z0-9_-]{10,}", content)


def test_related_skills_resolve_in_repo():
    frontmatter, _ = _frontmatter_and_body()
    repo_root = SKILL_PATH.parents[3]
    for name in frontmatter["metadata"]["hermes"]["related_skills"]:
        matches = list(repo_root.glob(f"skills/*/{name}/SKILL.md"))
        matches += list(repo_root.glob(f"optional-skills/*/{name}/SKILL.md"))
        assert matches, f"related skill does not resolve: {name}"


def test_modern_sections_are_ordered():
    _, body = _frontmatter_and_body()
    sections = (
        "## When to Use",
        "## Prerequisites",
        "## How to Run",
        "## Quick Reference",
        "## Procedure",
        "## Pitfalls",
        "## Verification",
    )
    positions = [body.index(section) for section in sections]
    assert positions == sorted(positions)


def test_every_procedure_step_has_a_completion_gate():
    _, body = _frontmatter_and_body()
    steps = re.findall(
        r"^### \d+\..*?(?=^### \d+\.|^## )",
        body,
        re.MULTILINE | re.DOTALL,
    )
    assert steps, "procedure must contain numbered ### steps"
    assert all("**Done when:**" in step for step in steps)


def test_recovery_and_read_only_invariants_are_explicit():
    _, body = _frontmatter_and_body()
    assert "zero daemon traffic" in body.lower()
    assert "hermes mcp serve --read-only" in body
    assert "link status is `ACTIVE`" in body
    assert re.search(r"Do not\s+invoke a mutating tool", body)
    assert "Keep the old runtime running" in body
    expected_tools = {
        "attachments_fetch",
        "channels_list",
        "conversation_get",
        "conversations_list",
        "events_poll",
        "events_wait",
        "messages_read",
        "permissions_list_open",
    }
    for tool in expected_tools | {"messages_send", "permissions_respond"}:
        assert f"`{tool}`" in body


def test_recovery_keeps_rollback_live_and_separates_maintenance():
    _, body = _frontmatter_and_body()
    assert "Account-context recovery" in body
    assert "same-alias maintenance" in body
    assert "Do not unload the healthy rollback runtime" in body
    assert "distinct replacement" in body
    assert "Local runtime health is not end-to-end proof" in body
    assert body.index("Keep the old runtime running") < body.index(
        "Stop only the precisely identified old runtime"
    )
