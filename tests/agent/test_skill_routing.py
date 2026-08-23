"""Behavioral tests for deterministic local skill routing."""

import json

import pytest


def _skill(skill_name: str, **overrides):
    skill = {
        "qualified_name": f"testing/{skill_name}",
        "name": skill_name,
        "category": "testing",
        "description": "Generic skill description",
        "triggers": [],
        "tags": [],
        "related_skills": [],
        "required_commands": [],
        "required_environment_variables": [],
    }
    skill.update(overrides)
    return skill


def test_ranking_uses_routing_metadata_deterministically():
    from agent import skill_routing

    candidates = [
        _skill("other"),
        _skill("deployment-helper", triggers=["deploy kubernetes workloads"]),
    ]

    first = skill_routing.rank_skills(candidates, "kubernetes", limit=2)
    second = skill_routing.rank_skills(
        list(reversed(candidates)), "kubernetes", limit=2
    )

    assert first == second
    assert [item["name"] for item in first["skills"]] == [
        "deployment-helper",
        "other",
    ]
    assert first["skills"][0]["score"] > first["skills"][1]["score"]
    assert first["total_candidates"] == 2


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("qualified_name", "testing/needle-route"),
        ("name", "needle-helper"),
        ("category", "needle-category"),
        ("description", "Handles the needle workflow"),
        ("triggers", ["route needle requests"]),
        ("tags", ["needle"]),
        ("related_skills", ["needle-companion"]),
        ("required_commands", ["needle-cli"]),
        ("required_environment_variables", ["NEEDLE_TOKEN"]),
    ],
)
def test_every_canonical_routing_field_contributes_to_ranking(field, value):
    from agent import skill_routing

    relevant = _skill("relevant", **{field: value})
    result = skill_routing.rank_skills(
        [_skill("irrelevant"), relevant], "needle", limit=2
    )

    assert result["skills"][0]["name"] == relevant["name"]
    assert result["skills"][0]["score"] > 0


def test_exact_normalized_skill_name_precedes_higher_bm25_score():
    from agent import skill_routing

    result = skill_routing.rank_skills(
        [
            _skill("code-review"),
            _skill(
                "review-expert",
                description="code review code review code review code review",
                tags=["code", "review"],
            ),
        ],
        "  CODE_review  ",
        limit=2,
    )

    assert result["skills"][0]["name"] == "code-review"


def test_equal_scores_use_qualified_name_tie_breaker():
    from agent import skill_routing

    result = skill_routing.rank_skills(
        [
            _skill("alpha", qualified_name="plugin-b:alpha"),
            _skill("zeta", qualified_name="plugin-a:zeta"),
        ],
        "unmatched",
        limit=2,
    )

    assert [item["name"] for item in result["skills"]] == ["zeta", "alpha"]
    assert [item["score"] for item in result["skills"]] == [0.0, 0.0]


def test_fingerprint_and_ranked_output_ignore_paths_and_input_order():
    from agent import skill_routing

    first_candidates = [
        _skill("alpha", source_path="/private/one/SKILL.md"),
        _skill("beta", tags=["needle"], source_path="/private/two/SKILL.md"),
    ]
    second_candidates = [
        dict(first_candidates[1], source_path="/different/private/two/SKILL.md"),
        dict(first_candidates[0], source_path="/different/private/one/SKILL.md"),
    ]

    first = skill_routing.rank_skills(first_candidates, "needle", limit=2)
    second = skill_routing.rank_skills(second_candidates, "needle", limit=2)

    assert first == second
    assert len(first["index_fingerprint"]) == 16
    serialized = json.dumps(first)
    assert "/private/" not in serialized
    assert "source_path" not in serialized


def test_ranked_output_keeps_display_description_with_its_skill():
    from agent import skill_routing

    result = skill_routing.rank_skills(
        [
            _skill("zeta", display_description="Display for zeta"),
            _skill("alpha", display_description="Display for alpha"),
        ],
        "unmatched",
        limit=2,
    )

    assert [(item["name"], item["description"]) for item in result["skills"]] == [
        ("alpha", "Display for alpha"),
        ("zeta", "Display for zeta"),
    ]


def test_index_cache_reuses_corpus_and_invalidates_on_routing_change():
    from agent import skill_routing

    candidates = [_skill("alpha"), _skill("beta", tags=["needle"])]
    skill_routing._build_index.cache_clear()
    try:
        first = skill_routing.rank_skills(candidates, "needle", limit=2)
        after_first = skill_routing._build_index.cache_info()

        skill_routing.rank_skills(list(reversed(candidates)), "other", limit=2)
        after_second = skill_routing._build_index.cache_info()

        changed = [dict(candidates[0]), dict(candidates[1], tags=["changed"])]
        third = skill_routing.rank_skills(changed, "changed", limit=2)
        after_third = skill_routing._build_index.cache_info()

        assert after_first.misses == 1
        assert after_second.hits == 1
        assert after_second.misses == 1
        assert after_third.misses == 2
        assert first["index_fingerprint"] != third["index_fingerprint"]
    finally:
        skill_routing._build_index.cache_clear()
