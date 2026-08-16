from __future__ import annotations

from cli import HermesCLI


def _make_cli() -> HermesCLI:
    cli = HermesCLI.__new__(HermesCLI)
    cli._pending_resume_sessions = None
    cli._pending_agent_seed = None
    return cli


def test_fast_wrap_command_queues_fast_wrap_seed():
    cli = _make_cli()

    assert cli.process_command("/fast-wrap") is True

    assert cli._pending_agent_seed == "fast wrap"
    assert cli._pending_resume_sessions is None


def test_fast_wrap_aliases_queue_fast_wrap_seed():
    for command in ("/fw", "/quick-wrap"):
        cli = _make_cli()
        assert cli.process_command(command) is True
        assert cli._pending_agent_seed == "fast wrap"


def test_full_wrap_command_queues_full_wrap_seed():
    cli = _make_cli()

    assert cli.process_command("/full-wrap") is True

    assert cli._pending_agent_seed == "full wrap"
    assert cli._pending_resume_sessions is None


def test_full_wrap_aliases_queue_full_wrap_seed():
    for command in ("/daily-wrap", "/wrap-full"):
        cli = _make_cli()
        assert cli.process_command(command) is True
        assert cli._pending_agent_seed == "full wrap"
