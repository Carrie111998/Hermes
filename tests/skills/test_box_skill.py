"""Durable contracts for the bundled Box productivity skill."""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import unquote

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = REPO_ROOT / "skills" / "productivity" / "box"
SKILL_MD = SKILL_DIR / "SKILL.md"
TEMPLATES_DIR = SKILL_DIR / "templates"


def _parse_frontmatter(content: str) -> dict:
    from agent.skill_utils import parse_frontmatter

    frontmatter, _ = parse_frontmatter(content)
    return frontmatter


def _local_markdown_targets(path: Path) -> set[Path]:
    targets: set[Path] = set()
    for raw_target in re.findall(r"\[[^]]+\]\(([^)]+)\)", path.read_text(encoding="utf-8")):
        target = raw_target.split("#", maxsplit=1)[0].strip("<>")
        if not target or "://" in target or target.startswith("mailto:"):
            continue
        targets.add((path.parent / unquote(target)).resolve())
    return targets


@pytest.fixture(scope="module")
def skill_text() -> str:
    return SKILL_MD.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def frontmatter(skill_text: str) -> dict:
    return _parse_frontmatter(skill_text)


def test_skill_frontmatter_is_valid_and_discoverable(frontmatter: dict):
    assert frontmatter.get("name") == "box"
    description = frontmatter.get("description")
    assert isinstance(description, str) and description.strip()
    assert len(description) <= 60
    assert description.endswith(".")
    assert frontmatter.get("license") == "MIT"
    assert "Chris Kim" in str(frontmatter.get("author"))
    assert "@iskysun96" in str(frontmatter.get("author"))

    platforms = frontmatter.get("platforms")
    assert isinstance(platforms, list)
    assert {"linux", "macos", "windows"}.issubset(platforms)


def test_box_command_is_declared_without_universal_ccg_secret_gate(frontmatter: dict):
    prerequisites = frontmatter.get("prerequisites") or {}
    assert "box" in prerequisites.get("commands", [])
    assert not prerequisites.get("env_vars")


def test_ccg_credentials_remain_optional_setup_entries():
    from hermes_cli.config import OPTIONAL_ENV_VARS

    expected = {
        "BOX_CLIENT_ID": False,
        "BOX_CLIENT_SECRET": True,
        "BOX_ENTERPRISE_ID": False,
    }
    for name, is_secret in expected.items():
        entry = OPTIONAL_ENV_VARS[name]
        assert entry["category"] == "skill"
        assert entry["password"] is is_secret


def test_all_local_links_resolve_inside_the_skill():
    markdown_files = list(SKILL_DIR.rglob("*.md"))
    for source in markdown_files:
        for target in _local_markdown_targets(source):
            assert target.is_file(), f"broken link in {source.relative_to(SKILL_DIR)}: {target}"
            assert target.is_relative_to(SKILL_DIR.resolve()), (
                f"local link in {source.relative_to(SKILL_DIR)} escapes the skill: {target}"
            )


def test_every_reference_is_reachable_from_skill_entrypoint():
    entrypoint_targets = _local_markdown_targets(SKILL_MD)
    reference_files = set((SKILL_DIR / "references").glob("*.md"))
    assert reference_files <= entrypoint_targets


def test_ccg_template_is_valid_and_matches_registered_credentials():
    template = TEMPLATES_DIR / "ccg-config.json.example"
    data = json.loads(template.read_text(encoding="utf-8"))
    settings = data.get("boxAppSettings") or {}
    assert settings.get("clientID") == "YOUR_BOX_CLIENT_ID"
    assert settings.get("clientSecret") == "YOUR_BOX_CLIENT_SECRET"
    assert data.get("enterpriseID") == "YOUR_BOX_ENTERPRISE_ID"


def test_metadata_extraction_requires_complete_schema_and_readback():
    """Protect structured metadata from falling back to a truncated description."""
    search_and_ai = (SKILL_DIR / "references" / "search-and-ai.md").read_text(
        encoding="utf-8"
    )
    content_workflows = (SKILL_DIR / "references" / "content-workflows.md").read_text(
        encoding="utf-8"
    )

    assert "every requested field" in search_and_ai
    assert "require explicit user approval" in search_and_ai
    assert "metadata instance ID" in search_and_ai
    assert "missing, normalized, or rejected" in search_and_ai
    assert "Never use a file description as an automatic substitute" in search_and_ai
    assert "limited to 256 characters" in content_workflows


def test_hubs_route_large_reusable_qa_with_governance_and_safe_mutations():
    """Keep Hubs focused on curated semantic Q&A rather than a generic AI fallback."""
    skill = SKILL_MD.read_text(encoding="utf-8")
    search_and_ai = (SKILL_DIR / "references" / "search-and-ai.md").read_text(
        encoding="utf-8"
    )
    hubs = (SKILL_DIR / "references" / "hubs.md").read_text(encoding="utf-8")

    assert "more than 25 files" in skill
    assert "recurring Q&A" in skill
    assert "preserves Box permissions" in skill
    assert "governed AI integration" in skill
    assert "consumes AI units" in skill
    assert "Do not use a Hub for metadata extraction or text generation" in search_and_ai
    assert "single_item_qa" in hubs
    assert '"type":"hubs"' in hubs
    assert '"include_citations":true' in hubs
    assert "Do not create a Hub automatically" in hubs
    assert "Confirm before bulk additions or removals" in hubs
    assert "up to an hour" in hubs
    assert "https://app.box.com/hubs/<HUB_ID>" in hubs


def test_box_skill_never_mentions_box_drive():
    for markdown_file in SKILL_DIR.rglob("*.md"):
        assert "box drive" not in markdown_file.read_text(encoding="utf-8").lower()


def test_local_oauth_keeps_cli_in_control_and_uses_user_local_npm_install():
    """Keep local OAuth browser handling deterministic on locked-down machines."""
    skill = SKILL_MD.read_text(encoding="utf-8")
    oauth = (SKILL_DIR / "references" / "oauth-setup.md").read_text(
        encoding="utf-8"
    )
    cli = (SKILL_DIR / "references" / "cli-guide.md").read_text(encoding="utf-8")

    local_runner = 'npm exec --prefix "$BOX_CLI_HOME" -- box'
    assert 'BOX_CLI_HOME="${HERMES_HOME:-$HOME/.hermes}/tools/box-cli"' in oauth
    assert 'npm install --prefix "$BOX_CLI_HOME" @box/cli' in oauth
    assert local_runner in oauth
    assert local_runner in cli
    assert "$boxCliHome = Join-Path" in cli
    assert "tools\\box-cli" in cli
    assert "global npm install" in skill
    assert "Do not attempt a global npm install" in cli
    assert "Do not use browser tools" in oauth
    assert "leave its terminal process running until it exits" in oauth
    assert "after the local callback path fails" not in oauth

    for markdown_file in SKILL_DIR.rglob("*.md"):
        contents = markdown_file.read_text(encoding="utf-8")
        assert "npm install -g @box/cli" not in contents
        assert ".local/share/hermes-box-cli" not in contents
