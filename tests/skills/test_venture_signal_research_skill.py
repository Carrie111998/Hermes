from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

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
    with patch("tools.skills_tool.SKILLS_DIR", REPO_ROOT / "skills"):
        return json.loads(skill_view(name, file_path=file_path))


def _normalized(content: str) -> str:
    return " ".join(content.split())


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


def test_loaded_skill_exposes_the_mission_and_handoff_contract() -> None:
    content = _view("venture-signal-research")["content"]
    normalized = _normalized(content)

    assert "single factual lookup" in content
    assert "Load and use `grounded-citations`" in normalized
    assert "Scout owns retrieval" in content
    assert "Sentinel reviews" in content
    assert "Quant consumes only cited" in content
    assert "Orchestrator advances" in content
    assert "rendered Sources list inside" in normalized
    assert "`render --style plain`" in content
    assert normalized.index("**Decision summary**") < normalized.index(
        "**Evidence Matrix**"
    )
    assert normalized.index("**Evidence Matrix**") < normalized.index(
        "**Contradictions and uncertainty**"
    )
    assert normalized.index("**Contradictions and uncertainty**") < normalized.index(
        "**Coverage report**"
    )


def test_evidence_contract_preserves_provenance_privacy_and_citations() -> None:
    content = _view("venture-signal-research", "references/evidence-contract.md")["content"]
    normalized = _normalized(content)

    assert "`primary`, `independent`, or `community`" in content
    assert (
        "`demand`, `pain`, `pricing`, `competition`, `buyer_language`, `risk`, "
        "or `counter_evidence`" in content
    )
    assert "grounded-citation ledger identifiers" in content
    assert "personal contact details" in normalized
    assert "sensitive personal attributes" in normalized
    assert "- id:" not in content


def test_source_route_has_one_total_fallback_and_safe_gap_behavior() -> None:
    content = _view("venture-signal-research", "references/source-routing.md")["content"]

    assert "one fallback total" in content
    assert "`web_extract`" in content
    assert "`browser_navigate`" in content
    assert "record a coverage gap" in content
    assert "Do not install software, authenticate, reuse cookies" in content
