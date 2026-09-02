"""Behavior contracts for the bundled systematic-debugging skill."""

import json
from pathlib import Path

from tools import skills_tool


SKILLS_ROOT = Path(__file__).resolve().parents[2] / "skills"


def test_reproduction_guidance_escalates_from_the_cheapest_capable_layer(
    monkeypatch,
) -> None:
    monkeypatch.setattr(skills_tool, "SKILLS_DIR", SKILLS_ROOT)

    result = json.loads(
        skills_tool.skill_view("systematic-debugging", preprocess=False)
    )

    assert result["success"] is True
    content = result["content"]
    guidance = content.split(
        "**Ways to construct a loop — try in roughly this order:**", 1
    )[1].split("**Tighten the loop once it exists:**", 1)[0]

    for layer in ("unit/component", "integration/E2E", "API/protocol", "browser/UI"):
        assert layer in guidance
    assert "cheapest layer" in guidance
    assert "only when" in guidance
    assert "first conclusive reproduction" in guidance
    assert "exact commands and outcomes" in guidance
    assert "localization evidence" in guidance
    assert "ownership" in guidance
