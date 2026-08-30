"""The Skill Safety Rule must cover a load that did not arrive.

The existing rule covers a skill whose content was lost to COMPRESSION
([SKILL_PRUNED]). A skill whose body was removed at the persistence boundary
never arrived in the first place, and the receipt for it says so with
[SKILL_INCOMPLETE]. Without guidance naming that marker the model has no
instruction telling it the index receipt is not a loaded skill.
"""

from agent.prompt_builder import SKILLS_GUIDANCE


class TestIncompleteLoadSafetyRule:
    def test_the_guidance_names_the_incomplete_marker(self):
        assert "## Skill Safety Rule" in SKILLS_GUIDANCE
        assert "[SKILL_INCOMPLETE]" in SKILLS_GUIDANCE

    def test_the_guidance_says_an_incomplete_result_is_not_loaded(self):
        assert "NOT loaded" in SKILLS_GUIDANCE

    def test_the_guidance_names_section_as_the_way_back(self):
        assert "section=" in SKILLS_GUIDANCE

    def test_the_pruned_rule_survives_alongside_it(self):
        assert "[SKILL_PRUNED]" in SKILLS_GUIDANCE
        assert "context compression" in SKILLS_GUIDANCE


class TestLinkedFileGuidance:
    """A linked file is a different document from SKILL.md with its own
    heading index, so a continuation that drops file_path silently resolves
    against SKILL.md. Every surface that tells the model how to continue must
    say to keep it."""

    def test_the_guidance_tells_the_model_to_keep_file_path(self):
        assert "file_path" in SKILLS_GUIDANCE
        marker_at = SKILLS_GUIDANCE.index("[SKILL_INCOMPLETE]")
        tail = SKILLS_GUIDANCE[marker_at:]
        assert "file_path=" in tail
        assert "SKILL.md" in tail

    def test_the_section_parameter_description_tells_the_model_the_same(self):
        from tools.skills_tool import SKILL_VIEW_SCHEMA

        described = SKILL_VIEW_SCHEMA["parameters"]["properties"]["section"][
            "description"
        ]
        assert "file_path" in described
