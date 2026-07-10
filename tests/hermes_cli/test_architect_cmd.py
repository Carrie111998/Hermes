"""Tests for the /architect prompt-architect slash command."""

from hermes_cli.commands import GATEWAY_KNOWN_COMMANDS, resolve_command


class TestArchitectCommandRegistry:
    def test_prompt_and_compose_remain_the_cli_editor_command(self):
        prompt = resolve_command("prompt")
        compose = resolve_command("compose")

        assert prompt is not None
        assert prompt.name == "prompt"
        assert prompt.cli_only is True
        assert "EDITOR" in prompt.description
        assert compose is prompt
        assert resolve_command("architect") is not prompt

    def test_architect_is_registered_for_cli_and_gateway(self):
        cmd = resolve_command("architect")

        assert cmd is not None
        assert cmd.name == "architect"
        assert "--fast" in cmd.args_hint
        assert "--deep" in cmd.args_hint
        assert "[rough request]" in cmd.args_hint
        assert "architect" in GATEWAY_KNOWN_COMMANDS


class TestArchitectCommandHandler:
    def test_bare_architect_starts_adaptive_interview_seed(self):
        from hermes_cli.architect_cmd import handle_architect_command

        result = handle_architect_command("")

        assert result.agent_seed is not None
        assert "Prompt architect interview" in result.text
        assert "adaptive" in result.text.lower()
        seed = result.agent_seed
        assert "multiple-choice" in seed or "multiple choice" in seed.lower()
        assert "ready-to-run agent prompt" in seed
        assert "do not execute" in seed.lower()
        assert "explicitly approves" in seed.lower()
        assert "run it" in seed
        assert "build it" in seed
        assert "save it" in seed
        assert "recurring workflow" in seed

    def test_default_mode_seeds_agent_to_ask_targeted_questions_first(self):
        from hermes_cli.architect_cmd import handle_architect_command

        result = handle_architect_command("build a client intake workflow")

        assert result.agent_seed is not None
        assert "build a client intake workflow" in result.agent_seed
        assert "ask 3-7 targeted clarifying questions" in result.agent_seed
        assert "Do not generate the final optimized prompt yet" in result.agent_seed
        assert "offer to run or build from it" in result.agent_seed

    def test_fast_mode_generates_immediately_with_assumptions(self):
        from hermes_cli.architect_cmd import handle_architect_command

        result = handle_architect_command("--fast build a client intake workflow")

        assert result.agent_seed is not None
        assert "Mode: fast" in result.agent_seed
        assert "Do not ask clarifying questions first" in result.agent_seed
        assert "reasonable assumptions" in result.agent_seed
        assert "build a client intake workflow" in result.agent_seed

    def test_deep_mode_asks_deeper_scope_risk_and_source_questions(self):
        from hermes_cli.architect_cmd import handle_architect_command

        result = handle_architect_command("--deep audit my business ops")

        assert result.agent_seed is not None
        assert "Mode: deep" in result.agent_seed
        assert "scope" in result.agent_seed.lower()
        assert "risks" in result.agent_seed.lower()
        assert "data sources" in result.agent_seed.lower()
        assert "audit my business ops" in result.agent_seed

    def test_unknown_option_reports_error_without_seeding_agent(self):
        from hermes_cli.architect_cmd import handle_architect_command

        result = handle_architect_command("--nope build something")

        assert result.agent_seed is None
        assert "Unknown /architect option" in result.text
