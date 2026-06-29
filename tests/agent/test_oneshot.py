"""Tests for agent.oneshot — shared one-off (stateless) LLM requests."""

from unittest.mock import MagicMock, patch

import pytest

from agent.oneshot import (
    PROMPT_TEMPLATES,
    render_template,
    run_oneshot,
    _strip_code_fence,
    _truncate,
)


class TestRenderTemplate:


    def test_branch_name_template_is_registered(self):
        render_template("branch_name", {"description": "add oneshot branch name template"})
        assert "branch_name" in PROMPT_TEMPLATES

    def test_branch_name_user_input_contains_description(self):
        _, user = render_template(
            "branch_name",
            {"description": "add a reusable one-shot branch name template"},
        )
        assert "add a reusable one-shot branch name template" in user

    def test_branch_name_avoid_appended(self):
        _, user = render_template(
            "branch_name",
            {
                "description": "add a reusable one-shot branch name template",
                "avoid": "feat-oneshot, add-branch-name",
            },
        )
        assert "Avoid: feat-oneshot, add-branch-name" in user

    def test_branch_name_no_avoid(self):
        _, user = render_template(
            "branch_name",
            {"description": "add a reusable one-shot branch name template"},
        )
        assert "Avoid" not in user

    def test_commit_message_includes_diff_and_recent(self):
        instructions, user = render_template(
            "commit_message",
            {"diff": "diff --git a/x b/x\n+new", "recent_commits": "feat: a\nfix: b"},
        )
        # Instructions describe the contract (conventional commits), not a snapshot.
        assert "Conventional Commits" in instructions
        assert "diff --git a/x b/x" in user
        assert "feat: a" in user



    def test_commit_message_avoid_forces_new_message(self):
        # Passing the previous message must instruct the model not to repeat it,
        # so "regenerate" yields a different result even on greedy models.
        _, plain = render_template("commit_message", {"diff": "d"})
        _, regen = render_template("commit_message", {"diff": "d", "avoid": "feat: prior"})
        assert "feat: prior" in regen
        assert "do not repeat" in regen
        assert "feat: prior" not in plain


class TestRunOneshot:
    def _mock_response(self, content):
        resp = MagicMock()
        resp.choices = [MagicMock()]
        resp.choices[0].message.content = content
        resp.choices[0].message.reasoning = None
        resp.choices[0].message.reasoning_content = None
        resp.choices[0].message.reasoning_details = None
        return resp


    def test_explicit_instructions_path(self):
        with patch(
            "agent.oneshot.call_llm",
            return_value=self._mock_response("hello"),
        ) as llm:
            out = run_oneshot(instructions="be brief", user_input="say hi")

        assert out == "hello"
        messages = llm.call_args.kwargs["messages"]
        assert messages[0]["content"] == "be brief"
        assert messages[1]["content"] == "say hi"


    def test_strips_wrapping_code_fence(self):
        with patch(
            "agent.oneshot.call_llm",
            return_value=self._mock_response("```\nfix: bug\n```"),
        ):
            assert run_oneshot(instructions="x", user_input="y") == "fix: bug"


class TestPrDescriptionTemplate:
    def test_diff_only(self):
        """Minimal call with just diff — user_input contains diff text."""
        instructions, user = render_template("pr_description", {"diff": "diff --git a/x b/x\n+new line"})
        assert "diff --git a/x b/x" in user
        assert "+new line" in user
        assert "one-line summary" in instructions

    def test_with_all_variables(self):
        """branch_name + recent_commits appear in user_input."""
        _, user = render_template(
            "pr_description",
            {
                "diff": "diff --git a/x b/x",
                "branch_name": "feat/add-pr-template",
                "recent_commits": "feat: add pr template\nfix: typo",
            },
        )
        assert "feat/add-pr-template" in user
        assert "feat: add pr template" in user
        assert "fix: typo" in user

    def test_avoid_appended(self):
        """avoid text appears in user_input when provided."""
        _, user = render_template(
            "pr_description",
            {"diff": "d", "avoid": "This PR adds a new feature."},
        )
        assert "This PR adds a new feature." in user
        assert "do not repeat" in user

    def test_large_diff_truncated(self):
        """diff > 14,000 chars is truncated; instructions text unchanged."""
        large_diff = "x" * 20000
        instructions, user = render_template("pr_description", {"diff": large_diff})
        # Diff should be truncated
        assert len(user) < 20000
        assert "…(truncated)" in user
        # Instructions should remain intact
        assert "one-line summary" in instructions
        assert "## Changes" in instructions

    def test_missing_diff_handled(self):
        """Empty diff yields fallback string, not exception."""
        instructions, user = render_template("pr_description", {})
        assert "(no diff available)" in user
        assert instructions  # Instructions should still be present

    def test_run_oneshot_pr_description_resolves(self):
        """run_oneshot with template='pr_description' resolves without KeyError."""
        with patch(
            "agent.oneshot.call_llm",
            return_value=MagicMock(
                choices=[
                    MagicMock(
                        message=MagicMock(
                            content="Add feature X.\n\n## Changes\n- Add X",
                            reasoning=None,
                            reasoning_content=None,
                            reasoning_details=None,
                        )
                    )
                ]
            ),
        ) as llm:
            out = run_oneshot(
                template="pr_description",
                variables={"diff": "diff --git a/x b/x"},
                main_runtime=None,
            )

        assert out == "Add feature X.\n\n## Changes\n- Add X"
        messages = llm.call_args.kwargs["messages"]
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"


class TestHelpers:
    def test_truncate_under_limit_unchanged(self):
        assert _truncate("short", 100) == "short"

    def test_truncate_over_limit_marks_truncation(self):
        out = _truncate("x" * 200, 50)
        assert out.endswith("…(truncated)")
        assert len(out) < 200

    def test_strip_code_fence_without_fence_is_noop(self):
        assert _strip_code_fence("plain text") == "plain text"
