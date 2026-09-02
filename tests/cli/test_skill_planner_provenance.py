"""Slash-skill provenance handoff tests for the classic CLI."""

from queue import Queue
from types import SimpleNamespace

import cli as cli_module


def test_skill_dispatch_queues_model_text_with_separate_clean_planner_intent(monkeypatch):
    cli = object.__new__(cli_module.HermesCLI)
    cli._pending_input = Queue()
    cli.agent = SimpleNamespace(toolset_registry=None)
    cli.config = {}
    cli.session_id = "session-1"

    scaffold = (
        "[IMPORTANT: The user has invoked the \"private\" skill.]\n"
        "PRIVATE_SKILL_BODY"
    )
    monkeypatch.setattr(cli_module, "get_skill_bundles", lambda: {})
    monkeypatch.setattr(
        cli_module,
        "_ensure_skill_commands",
        lambda: {"/private": {"name": "private"}},
    )
    monkeypatch.setattr(cli_module, "_get_plugin_cmd_handler_names", lambda: set())
    monkeypatch.setattr(
        cli_module,
        "build_skill_invocation_message",
        lambda *_args, **_kwargs: scaffold,
    )

    assert cli.process_command("/private fix the scheduler") is True

    queued = cli._pending_input.get_nowait()
    assert isinstance(queued, cli_module._SkillInvocationInput)
    assert queued.text == scaffold
    assert queued.planner_user_message == "fix the scheduler"
