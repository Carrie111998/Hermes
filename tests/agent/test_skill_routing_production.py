"""Production behavior contracts for deterministic BM25 skill routing."""

from copy import deepcopy
import json

import pytest


def _skill(name: str, **overrides):
    skill = {
        "qualified_name": f"testing/{name}",
        "name": name,
        "category": "testing",
        "description": "generic helper",
        "display_description": "generic helper",
        "triggers": [],
        "tags": [],
        "related_skills": [],
        "required_commands": [],
        "required_environment_variables": [],
    }
    skill.update(overrides)
    return skill


def test_routing_card_fingerprint_is_stable_private_and_source_bound():
    from agent import skill_routing

    make_card = getattr(skill_routing, "build_routing_card", None)
    assert callable(make_card), (
        "build_routing_card must create internal source-hashed cards"
    )
    source = _skill("deploy-helper", triggers=["deploy workloads"])
    first = make_card(source)
    second = make_card(dict(source))

    assert first == second
    assert len(first["source_fingerprint"]) == 64
    assert first["source_fingerprint"] == first["source_fingerprint"].lower()
    assert set(first["source_fingerprint"]) <= set("0123456789abcdef")

    for excluded, changed in {
        "display_description": "presentation changed",
        "path": "/private/secret/SKILL.md",
        "source_path": "/private/other/SKILL.md",
        "body": "private body material",
    }.items():
        variant = dict(source, **{excluded: changed})
        assert make_card(variant)["source_fingerprint"] == first["source_fingerprint"]

    for field, changed in {
        "name": "changed-name",
        "qualified_name": "changed/qualified",
        "category": "changed-category",
        "description": "changed description",
        "triggers": ["changed trigger"],
        "tags": ["changed-tag"],
        "related_skills": ["changed-related"],
        "required_commands": ["changed-command"],
        "required_environment_variables": ["CHANGED_ENV"],
    }.items():
        variant = deepcopy(source)
        variant[field] = changed
        assert make_card(variant)["source_fingerprint"] != first["source_fingerprint"]


def test_approved_bm25_description_signal_beats_name_only_distractor():
    from agent.skill_routing import rank_skills

    candidates = [
        _skill(
            "git-recovery",
            description="repair orphaned branch pruning behavior safely",
            triggers=["repair stale branches"],
            tags=["git"],
        ),
        _skill(
            "orphaned-branch-pruning",
            description="format a status label",
            triggers=["format status"],
            tags=["formatting"],
        ),
        _skill("repair", description="generic repair helper", triggers=["repair data"]),
    ]

    result = rank_skills(candidates, "repair orphaned branch pruning behavior", limit=8)

    assert result["skills"][0]["name"] == "git-recovery"


def test_duplicate_exact_names_keep_all_ambiguity_candidates():
    from agent.skill_routing import rank_skills

    candidates = [
        _skill(
            "duplicate",
            qualified_name="testing/duplicate-strong",
            description="duplicate duplicate duplicate duplicate duplicate",
        ),
        *[
            _skill("duplicate", qualified_name=f"testing/duplicate-{index}")
            for index in range(4)
        ],
    ]

    result = rank_skills(candidates, "duplicate", limit=8)

    assert len(result["skills"]) == 5


def test_default_adaptive_depth_and_non_default_exact_limits():
    from agent.skill_routing import rank_skills

    candidates = [_skill(f"other-{index:02d}") for index in range(9)]
    candidates.append(_skill("deploy-helper", triggers=["deploy kubernetes workload"]))

    exact = rank_skills(candidates, "DEPLOY_helper", limit=8)
    strong = rank_skills(candidates, "deploy kubernetes workload", limit=8)
    ambiguous = rank_skills(candidates, "generic", limit=8)
    unmatched = rank_skills(candidates, "quantum orchard", limit=8)

    assert 1 <= len(exact["skills"]) <= 3
    assert exact["skills"][0]["name"] == "deploy-helper"
    assert len(strong["skills"]) == 1
    assert strong["skills"][0]["name"] == "deploy-helper"
    assert all(skill["score"] > 0.0 for skill in strong["skills"])
    assert len(ambiguous["skills"]) == 8
    assert unmatched["skills"] == []
    assert len(rank_skills(candidates, "deploy-helper", limit=4)["skills"]) == 4
    assert rank_skills(candidates[:2], "unmatched", limit=8)["skills"] == []
    assert rank_skills([], "unmatched", limit=8)["skills"] == []


def test_index_cache_reuse_isolation_and_fingerprint_invalidation():
    from agent import skill_routing

    candidates = [_skill("alpha"), _skill("beta", tags=["needle"])]
    skill_routing._build_index.cache_clear()
    try:
        first = skill_routing.rank_skills(candidates, "needle", limit=2)
        info_one = skill_routing._build_index.cache_info()
        reversed_result = skill_routing.rank_skills(
            list(reversed(candidates)), "other", limit=2
        )
        info_two = skill_routing._build_index.cache_info()
        presentation = deepcopy(candidates)
        presentation[1]["display_description"] = "changed display"
        display_result = skill_routing.rank_skills(presentation, "needle", limit=2)
        info_three = skill_routing._build_index.cache_info()
        changed = deepcopy(candidates)
        changed[1]["tags"] = ["changed"]
        changed_result = skill_routing.rank_skills(changed, "changed", limit=2)
        info_four = skill_routing._build_index.cache_info()

        assert info_one.misses == 1
        assert info_two.hits == 1
        assert info_three.hits == 2
        assert info_four.misses == 2
        assert first["index_fingerprint"] == reversed_result["index_fingerprint"]
        assert first["index_fingerprint"] == display_result["index_fingerprint"]
        assert first["index_fingerprint"] != changed_result["index_fingerprint"]
        assert "source_fingerprint" not in json.dumps(first)
    finally:
        skill_routing._build_index.cache_clear()


@pytest.mark.parametrize("query", ["café", "CAFE\u0301"])
def test_unicode_normalization_and_input_order_are_deterministic(query):
    from agent.skill_routing import rank_skills

    candidates = [_skill("zeta"), _skill("café")]
    forward = rank_skills(candidates, query, limit=8)
    reverse = rank_skills(list(reversed(candidates)), query, limit=8)

    assert forward == reverse
    assert forward["skills"][0]["name"] == "café"
    assert len(forward["skills"]) == 1
