"""Regression guard for locally retired bundled messaging platforms."""

from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
REMOVED_PLATFORM = "pho" + "ton"
REMOVED_ID_PATTERN = re.compile(
    rf"(?<![a-z0-9_]){re.escape(REMOVED_PLATFORM)}(?![a-z0-9_])"
)
INERT_RETIREMENT_REFERENCES = frozenset(
    {
        "apps/desktop/src/lib/session-source.test.ts",
        "gateway/retired_platforms.py",
        "scripts/skills_index_policy.py",
        "tests/gateway/test_platform_registry.py",
        "tests/gateway/test_session_load_bool.py",
        "tests/gateway/test_status.py",
        "tests/hermes_cli/test_plugins.py",
        "tests/scripts/test_build_skills_index_health.py",
        "tests/website/test_extract_skills.py",
    }
)


def _tracked_surfaces() -> list[Path]:
    files: list[Path] = [
        REPO_ROOT / "Dockerfile",
        REPO_ROOT / "package.json",
        REPO_ROOT / "package-lock.json",
        REPO_ROOT / "website" / "sidebars.ts",
        REPO_ROOT
        / "skills"
        / "autonomous-ai-agents"
        / "hermes-agent"
        / "references"
        / "cli-reference.md",
    ]
    for root, patterns in (
        (REPO_ROOT / "gateway", ("*.py",)),
        (REPO_ROOT / "hermes_cli", ("*.py",)),
        (REPO_ROOT / "tools", ("*.py",)),
        (REPO_ROOT / "plugins" / "platforms", ("*.py", "*.yaml", "*.yml", "*.json")),
        (REPO_ROOT / "tests", ("*.py",)),
        (REPO_ROOT / "scripts", ("*.py", "*.js", "*.mjs")),
        (REPO_ROOT / ".github" / "workflows", ("*.yml", "*.yaml")),
        (REPO_ROOT / ".plans", ("*.md",)),
        (REPO_ROOT / "apps" / "desktop" / "src", ("*.ts", "*.tsx")),
        (REPO_ROOT / "website" / "docs", ("*.md", "*.mdx")),
        (REPO_ROOT / "website" / "i18n", ("*.md", "*.mdx")),
    ):
        for pattern in patterns:
            files.extend(root.rglob(pattern))
    return sorted(
        {
            path
            for path in files
            if path.is_file() and path != REPO_ROOT / "scripts" / "release.py"
        }
    )


def test_retired_platform_payloads_are_absent() -> None:
    assert not (REPO_ROOT / "plugins" / "platforms" / REMOVED_PLATFORM).exists()
    assert not (
        REPO_ROOT / "tests" / "plugins" / "platforms" / REMOVED_PLATFORM
    ).exists()
    assert not (
        REPO_ROOT
        / "website"
        / "docs"
        / "user-guide"
        / "messaging"
        / f"{REMOVED_PLATFORM}.md"
    ).exists()


def test_retired_platform_has_no_runtime_build_or_documentation_registration() -> None:
    remaining: list[str] = []
    for path in _tracked_surfaces():
        relative_path = str(path.relative_to(REPO_ROOT))
        if relative_path in INERT_RETIREMENT_REFERENCES:
            continue
        text = path.read_text(encoding="utf-8").lower()
        if path == REPO_ROOT / "apps" / "desktop" / "src" / "lib" / "session-source.ts":
            tombstone = f"export const retired_session_source_ids = ['{REMOVED_PLATFORM}']"
            assert text.count(REMOVED_PLATFORM) == 1
            text = text.replace(tombstone, "")
        if REMOVED_ID_PATTERN.search(text):
            remaining.append(relative_path)
    assert remaining == []


def test_retired_platform_references_are_explicitly_inert() -> None:
    for relative_path in INERT_RETIREMENT_REFERENCES:
        assert (REPO_ROOT / relative_path).is_file()

    from gateway.retired_platforms import (
        RETIRED_PLATFORM_IDS,
        is_retired_platform_id,
    )
    from scripts.skills_index_policy import is_retired_platform_catalog_entry

    assert RETIRED_PLATFORM_IDS == frozenset({REMOVED_PLATFORM})
    assert is_retired_platform_id(REMOVED_PLATFORM)
    assert not is_retired_platform_id("telegram")
    assert is_retired_platform_catalog_entry(
        {
            "identifier": f"skills-sh/{REMOVED_PLATFORM}-hq/skills/spectrum",
            "repo": f"{REMOVED_PLATFORM}-hq/skills",
        }
    )
    assert not is_retired_platform_catalog_entry(
        {
            "identifier": "finance-photonics-cpo",
            "description": "Co-packaged optics and photonics supply chains",
            "tags": ["photonics-cpo"],
        }
    )

    desktop_regression = (
        REPO_ROOT / "apps" / "desktop" / "src" / "lib" / "session-source.test.ts"
    ).read_text(encoding="utf-8")
    assert (
        f"handoffOriginSource('completed', '{REMOVED_PLATFORM}')).toBeNull()"
        in desktop_regression
    )


def test_retired_adapter_leaves_no_orphaned_send_message_reaction_api() -> None:
    from tools.send_message_tool import SEND_MESSAGE_SCHEMA

    properties = SEND_MESSAGE_SCHEMA["parameters"]["properties"]
    assert properties["action"]["enum"] == ["send", "list"]
    assert "emoji" not in properties
    assert "message_id" not in properties
