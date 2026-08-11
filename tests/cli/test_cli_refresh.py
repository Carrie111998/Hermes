"""Behavioral tests for the native classic-CLI ``/refresh`` command."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch


def _make_cli():
    import cli as cli_mod

    obj = object.__new__(cli_mod.HermesCLI)
    obj.session_id = "session-original"
    obj.conversation_history = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]
    obj.agent = MagicMock()
    return obj


def test_soft_refresh_preserves_session_and_history_and_queues_tail_context(capsys):
    cli = _make_cli()
    before_history = [dict(message) for message in cli.conversation_history]
    result = SimpleNamespace(
        context_note="[fresh MEMORY.md and USER.md context]",
        report="Refreshed skills and memory. Gateway not restarted.",
    )

    with patch("agent.session_refresh.build_soft_refresh", return_value=result):
        cli._handle_refresh_command("/refresh")

    assert cli.session_id == "session-original"
    assert cli.conversation_history == before_history
    assert [record["note"] for record in cli._pending_refresh_notes] == [result.context_note]
    cli.agent._invalidate_system_prompt.assert_not_called()
    assert "Gateway not restarted" in capsys.readouterr().out


def test_repeated_soft_refreshes_queue_fifo_without_overwrite():
    cli = _make_cli()
    results = [
        SimpleNamespace(context_note="NOTE-1", report="first"),
        SimpleNamespace(context_note="NOTE-2", report="second"),
    ]
    with patch("agent.session_refresh.build_soft_refresh", side_effect=results):
        cli._handle_refresh_command("/refresh")
        cli._handle_refresh_command("/refresh")

    assert [record["note"] for record in cli._pending_refresh_notes] == [
        "NOTE-1",
        "NOTE-2",
    ]


def test_refresh_branch_reuses_existing_branch_handler():
    cli = _make_cli()
    cli._handle_branch_command = MagicMock()

    cli._handle_refresh_command("/refresh --branch")

    cli._handle_branch_command.assert_called_once_with("/branch")


def test_refresh_branch_syncs_memory_prompt_and_compatibility_skill_cache():
    import cli as cli_module

    cli = _make_cli()
    memory_store = MagicMock()
    cli.agent._memory_store = memory_store
    refreshed_commands = {
        "/fresh-skill": {"name": "fresh-skill", "description": "fresh"}
    }

    order = []
    memory_store.load_from_disk.side_effect = lambda: order.append("memory")

    def branch(_command):
        order.append("branch")
        cli.session_id = "session-branch"
        cli.conversation_history = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]

    cli._handle_branch_command = MagicMock(side_effect=branch)

    with patch(
        "agent.skill_commands.reload_skills", side_effect=lambda: order.append("skills")
    ) as reload_skills, patch(
        "agent.skill_commands.get_skill_commands", return_value=refreshed_commands
    ):
        cli._handle_refresh_command("/refresh --branch")

    assert cli.session_id == "session-branch"
    assert cli.conversation_history[-1] == {"role": "assistant", "content": "hi"}
    memory_store.load_from_disk.assert_called_once_with()
    reload_skills.assert_called_once_with()
    cli.agent._invalidate_system_prompt.assert_called_once_with()
    assert cli_module._skill_commands == refreshed_commands
    assert order == ["memory", "skills", "branch"]


def test_refresh_is_exposed_by_central_registry_on_cli_and_gateway():
    from hermes_cli.commands import GATEWAY_KNOWN_COMMANDS, resolve_command

    command = resolve_command("refresh")

    assert command is not None
    assert command.name == "refresh"
    assert command.args_hint == "[--branch]"
    assert not command.cli_only and not command.gateway_only
    assert "refresh" in GATEWAY_KNOWN_COMMANDS
