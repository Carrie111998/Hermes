from __future__ import annotations

import json
from pathlib import Path

from tools.skill_linter import lint_skill
from tools.skills_hub import _referenced_support_paths
from tools.skills_tool import reset_skill_view_dedup, skill_view

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = REPO_ROOT / "skills" / "research" / "venture-signal-research"
SKILL_MD = SKILL_DIR / "SKILL.md"
SUPPORT_PATHS = {
    "references/evidence-contract.md",
    "references/source-routing.md",
}


def _view(name: str, file_path: str | None = None) -> dict:
    reset_skill_view_dedup()
    return json.loads(skill_view(name, file_path=file_path))


def test_bundled_skill_is_discoverable_with_grounding_relationship() -> None:
    payload = _view("venture-signal-research")

    assert payload["success"] is True
    assert payload["name"] == "venture-signal-research"
    assert payload["related_skills"] == ["grounded-citations"]
    assert payload["readiness_status"] == "available"
    assert payload["setup_needed"] is False


def test_installer_resolves_the_complete_support_bundle() -> None:
    payload = _view("venture-signal-research")

    assert _referenced_support_paths(payload["content"]) == SUPPORT_PATHS
    assert set(payload["linked_files"]["references"]) == SUPPORT_PATHS

    for relative_path in SUPPORT_PATHS:
        support = _view("venture-signal-research", relative_path)
        assert support["success"] is True
        assert support["content"].strip()


def test_skill_passes_repository_authoring_conventions() -> None:
    assert lint_skill(SKILL_MD) == []
