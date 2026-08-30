from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from agent.fusion.models import FusionParticipantSpec, FusionResult, FusionRequest, FusionSpikeRun
from cli import HermesCLI
from hermes_cli.commands import GATEWAY_KNOWN_COMMANDS, resolve_command
from hermes_cli.fusion_command import parse_fusion_command, render_fusion_result
from toolsets import resolve_toolset


def test_fusion_registered_and_moa_unchanged():
    cmd = resolve_command("fusion")
    assert cmd is not None
    assert cmd.name == "fusion"
    assert "fusion" in GATEWAY_KNOWN_COMMANDS
    assert "mixture_of_agents" in resolve_toolset("moa")


def test_parse_fusion_command_defaults_to_five_bounded_round_trips():
    req = parse_fusion_command('/fusion plan "check auth"')
    assert req.debate_rounds == 5
    assert req.convergence_rounds == 5


def test_parse_fusion_command_with_models_and_debate_options():
    req = parse_fusion_command('/fusion review "check auth" --participants 2 --models zai:glm-5.2@xhigh,deepseek:deepseek-v4-pro@xhigh --debate-rounds 1 --convergence-rounds 2 --reasoning xhigh --timeout 60 --repo /tmp/repo')
    assert req.mode == "review"
    assert req.task == "check auth"
    assert req.participants == 2
    assert req.model_specs == ["zai:glm-5.2@xhigh", "deepseek:deepseek-v4-pro@xhigh"]
    assert req.debate_rounds == 1
    assert req.convergence_rounds == 2
    assert req.reasoning_effort == "xhigh"
    assert req.timeout_seconds == 60
    assert req.repo_path == "/tmp/repo"


def test_render_operator_decision_says_no_final_plan_and_lists_models(tmp_path):
    result = FusionResult(
        status="operator_decision",
        request=FusionRequest(mode="plan", task="x"),
        run_dir=str(tmp_path),
        model_diversity={
            "required_distinct_models": 2,
            "distinct_count": 3,
            "participants": [
                {"slug": "glm-max", "provider": "zai", "model": "glm-5.2", "reasoning_effort": "xhigh"},
            ],
        },
        routing={"task_kind": "bug_unknown_root", "locate_required": True},
        coverage={"requested": 3, "draft_successful": 2, "degraded": True},
        decision="operator_decision",
        spikes=[FusionSpikeRun(round_index=1, phase="spike-1", available=True, cleanup_ok=True)],
    )
    rendered = render_fusion_result(result)
    assert "No final plan was emitted" in rendered
    assert "zai:glm-5.2" in rendered
    assert "Route: `bug_unknown_root`" in rendered
    assert "Coverage: 2/3 degraded" in rendered
    assert "Spikes: 1/1 worktrees available, cleanup 1/1" in rendered


def test_render_model_diversity_error(tmp_path):
    result = FusionResult(
        status="model_diversity_error",
        request=FusionRequest(mode="plan", task="x"),
        run_dir=str(tmp_path),
        error="configure more models",
    )
    rendered = render_fusion_result(result)
    assert "No participant execution happened" in rendered
    assert "configure more models" in rendered


@contextmanager
def _noop_busy(_text):
    yield


def test_cli_process_command_dispatches_fusion(monkeypatch):
    cli = HermesCLI.__new__(HermesCLI)
    cli.config = {}
    cli.agent = None
    cli._command_running = False
    cli._busy_command = _noop_busy
    cli._slow_command_status = lambda cmd: cmd
    printed = []
    cli._console_print = printed.append

    def fake_handle(command, *, cli=None):
        cli._console_print(f"handled {command}")

    monkeypatch.setattr("hermes_cli.fusion_command.handle_fusion_command", fake_handle)
    assert cli.process_command("/fus plan something") is True
    assert printed == ["handled /fusion plan something"]
