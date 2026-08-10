"""Background-review wiring for SkillHub refresh support."""

from unittest.mock import MagicMock, patch

from agent.background_review import spawn_background_review_thread


def test_background_review_target_forwards_skill_review_flag():
    agent = MagicMock()
    agent._SKILL_REVIEW_PROMPT = "review skills"
    snapshot = [{"role": "user", "content": "hello"}]

    with patch("agent.background_review._run_review_in_thread") as run_review:
        target, prompt = spawn_background_review_thread(
            agent,
            snapshot,
            review_memory=False,
            review_skills=True,
        )
        target()

    assert prompt == "review skills"
    run_review.assert_called_once_with(agent, snapshot, "review skills", True)


def test_memory_only_review_does_not_request_skill_refresh():
    agent = MagicMock()
    agent._MEMORY_REVIEW_PROMPT = "review memory"

    with patch("agent.background_review._run_review_in_thread") as run_review:
        target, _ = spawn_background_review_thread(
            agent,
            [],
            review_memory=True,
            review_skills=False,
        )
        target()

    assert run_review.call_args.args[-1] is False
