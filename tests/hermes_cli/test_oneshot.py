"""Tests for hermes_cli.oneshot — CLI one-shot execution."""

from unittest.mock import MagicMock, patch
import pytest

from hermes_cli.oneshot import (
    _normalize_skills,
    _normalize_toolsets,
    _run_agent,
    run_oneshot,
)


class TestNormalizeSkills:
    def test_none_returns_none(self):
        assert _normalize_skills(None) is None

    def test_single_string(self):
        assert _normalize_skills("research") == ["research"]

    def test_comma_separated_string(self):
        assert _normalize_skills("research, data-analysis, research") == [
            "research",
            "data-analysis",
        ]

    def test_list_with_duplicates_and_whitespace(self):
        assert _normalize_skills(["research", " data-analysis ", "research", ""]) == [
            "research",
            "data-analysis",
        ]

    def test_empty_string_returns_none(self):
        assert _normalize_skills("") is None
        assert _normalize_skills("  ") is None


class TestRunOneshot:
    def test_provider_without_model_returns_error(self, capsys):
        with patch.dict("os.environ", {}, clear=True):
            rc = run_oneshot("test prompt", provider="anthropic", model=None)
            assert rc == 2
            err = capsys.readouterr().err
            assert "--provider requires --model" in err

    def test_unknown_skill_returns_error(self, capsys):
        with patch(
            "agent.skill_commands.build_preloaded_skills_prompt",
            return_value=("", [], ["nonexistent-skill"]),
        ):
            rc = run_oneshot("test prompt", skills="nonexistent-skill")
            assert rc == 2
            err = capsys.readouterr().err
            assert "unknown skill(s): nonexistent-skill" in err

    def test_successful_run_prints_response_and_returns_0(self, capsys):
        with (
            patch(
                "agent.skill_commands.build_preloaded_skills_prompt",
                return_value=("Preloaded prompt content", ["research"], []),
            ),
            patch(
                "hermes_cli.oneshot._run_agent",
                return_value=("Hello world response", {"completed": True}),
            ) as mock_agent,
        ):
            rc = run_oneshot("say hi", skills="research")
            assert rc == 0
            out = capsys.readouterr().out
            assert "Hello world response" in out
            mock_agent.assert_called_once()
            _, kwargs = mock_agent.call_args
            assert kwargs.get("skills") == ["research"]

    def test_empty_response_returns_1(self, capsys):
        with patch(
            "hermes_cli.oneshot._run_agent",
            return_value=("", {"completed": True}),
        ):
            rc = run_oneshot("say hi")
            assert rc == 1
            err = capsys.readouterr().err
            assert "no final response was produced" in err


class TestRunAgentSkillsWiring:
    @patch("hermes_cli.runtime_provider.resolve_runtime_provider")
    @patch("hermes_cli.mcp_startup.ensure_mcp_discovery_before_agent_build")
    @patch("hermes_cli.oneshot._create_session_db_for_oneshot")
    @patch("run_agent.AIAgent")
    def test_run_agent_injects_ephemeral_system_prompt_when_skills_provided(
        self, mock_ai_agent_cls, mock_session_db, mock_mcp, mock_runtime
    ):
        mock_runtime.return_value = {
            "api_key": "test-key",
            "provider": "mock",
            "base_url": None,
            "requested_provider": None,
            "api_mode": None,
            "credential_pool": None,
        }
        mock_agent_instance = MagicMock()
        mock_agent_instance.run_conversation.return_value = {
            "final_response": "done",
            "completed": True,
        }
        mock_ai_agent_cls.return_value = mock_agent_instance

        with patch(
            "agent.skill_commands.build_preloaded_skills_prompt",
            return_value=("SYSTEM INSTRUCTIONS FOR SKILLS", ["sample_skill"], []),
        ):
            response, result = _run_agent("test query", skills=["sample_skill"])
            assert response == "done"
            mock_ai_agent_cls.assert_called_once()
            _, kwargs = mock_ai_agent_cls.call_args
            assert (
                kwargs.get("ephemeral_system_prompt")
                == "SYSTEM INSTRUCTIONS FOR SKILLS"
            )
