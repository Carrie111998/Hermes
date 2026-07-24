from __future__ import annotations

import pytest

from gateway.ultrastudio_skill_routing import (
    format_allowed_skills,
    workflow_routing,
)


def workflow(name: str, priority: int) -> dict:
    return {
        "name": name,
        "description": f"{name} description.",
        "category": "workflow-generation",
        "routing": {
            "priority": priority,
            "triggers": [f"{name} one", f"{name} two", f"{name} three"],
            "negative": [f"not {name}"],
        },
    }


def test_workflow_routing_requires_complete_metadata():
    assert workflow_routing(workflow("specific", 80)) == (
        80,
        ["specific one", "specific two", "specific three"],
        ["not specific"],
    )
    for routing in (
        None,
        {"priority": 101, "triggers": ["a", "b", "c"], "negative": ["d"]},
        {"priority": 50, "triggers": ["a"], "negative": ["d"]},
        {"priority": 50, "triggers": ["a", "b", "c"], "negative": []},
    ):
        item = workflow("broken", 50)
        item["routing"] = routing
        with pytest.raises(ValueError, match="routing metadata"):
            workflow_routing(item)


def test_allowed_skill_index_orders_specific_routes_before_fallbacks():
    discovered = [
        workflow("fallback", 10),
        workflow("specific", 80),
        {
            "name": "helper",
            "description": "Supporting guidance.",
            "category": "creative",
        },
    ]

    prompt = format_allowed_skills(
        {"fallback", "specific", "helper"},
        discovered,
    )

    assert prompt.index("- specific:") < prompt.index("- fallback:")
    assert prompt.index("- fallback:") < prompt.index("- helper:")
    assert "priority=80" in prompt
    assert "applies=specific one; specific two; specific three" in prompt
    assert "not=not specific" in prompt
    assert "- helper: Supporting guidance." in prompt


def test_allowed_skill_index_fails_closed_for_missing_or_unroutable_workflow():
    with pytest.raises(ValueError, match="allowed skills unavailable: missing"):
        format_allowed_skills({"missing"}, [])
    with pytest.raises(ValueError, match="no routing metadata"):
        format_allowed_skills(
            {"broken"},
            [{
                "name": "broken",
                "description": "Broken.",
                "category": "workflow-generation",
            }],
        )
