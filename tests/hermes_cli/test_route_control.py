from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent.turn_routing_runtime import TurnRoutingSessionState
from hermes_cli.route_control import execute_route_command
from hermes_cli.commands import GATEWAY_KNOWN_COMMANDS, resolve_command


def _config(mode="observe"):
    return {
        "mode": mode,
        "budget": {"grok_weekly_limit": 3},
    }


def test_route_status_reports_backend_mode_affinity_and_fail_off():
    state = TurnRoutingSessionState(
        affinity_route="deep",
        affinity_remaining=2,
        fail_off=True,
        fail_off_reason="route_prepare_failed",
    )

    output = execute_route_command(
        "status",
        state=state,
        config_loader=lambda: _config(),
    )

    assert "Route mode: observe" in output
    assert "Affinity: deep (2 turns remaining)" in output
    assert "Automatic routing fail-off: route_prepare_failed" in output


def test_route_why_reads_only_latest_provenance_metadata():
    state = TurnRoutingSessionState(
        latest_event="route.completed",
        latest_payload={
            "route": "deep",
            "source": "classifier",
            "selection_reason_code": "classifier_deep",
            "turn_id": "turn-1",
        },
    )

    output = execute_route_command(
        "why",
        state=state,
        config_loader=lambda: _config(),
    )

    assert output == (
        "Latest route: deep via classifier "
        "(classifier_deep, route.completed, turn turn-1)"
    )


def test_route_auto_is_backend_locked_and_never_writes_config():
    writer = MagicMock()

    output = execute_route_command(
        "auto",
        state=TurnRoutingSessionState(),
        config_loader=lambda: _config("off"),
        mode_writer=writer,
    )

    writer.assert_not_called()
    assert output == "Automatic routing is locked; use /route observe"


def test_route_observe_mode_write_remains_backend_authoritative():
    writer = MagicMock()

    output = execute_route_command(
        "observe",
        state=TurnRoutingSessionState(),
        config_loader=lambda: _config("off"),
        mode_writer=writer,
    )

    writer.assert_called_once_with("observe")
    assert output == "Route mode set to observe"


def test_route_reset_clears_session_state_without_changing_mode():
    writer = MagicMock()
    state = TurnRoutingSessionState(
        affinity_route="deep",
        affinity_remaining=2,
        fail_off=True,
        consecutive_failures=3,
    )

    output = execute_route_command(
        "reset",
        state=state,
        config_loader=lambda: _config("observe"),
        mode_writer=writer,
    )

    assert output == "Session routing state reset; route mode remains observe"
    assert state.affinity_route is None
    assert state.affinity_remaining == 0
    assert state.fail_off is False
    assert state.consecutive_failures == 0
    writer.assert_not_called()


def test_route_budget_uses_read_only_snapshot_contract():
    snapshot = SimpleNamespace(
        weekly_limit=3,
        available_slots=1,
        used_slots=1,
        reserved_slots=1,
        cooldown_reason="provider_rate_limited",
        cooldown_until=123.0,
    )

    output = execute_route_command(
        "budget",
        state=TurnRoutingSessionState(),
        config_loader=lambda: _config(),
        budget_status_loader=lambda: snapshot,
    )

    assert output == (
        "Grok budget: 1/3 available; 1 used; 1 reserved; "
        "cooldown provider_rate_limited until 123"
    )


def test_route_command_rejects_unknown_arguments_without_writing():
    writer = MagicMock()

    output = execute_route_command(
        "auto extra",
        state=TurnRoutingSessionState(),
        config_loader=lambda: _config(),
        mode_writer=writer,
    )

    assert output.startswith("Usage: /route ")
    writer.assert_not_called()


def test_route_command_is_registered_for_cli_tui_and_gateway_dispatch():
    command = resolve_command("route")

    assert command is not None
    assert command.args_hint == "[status|off|observe|auto|why|budget|reset]"
    assert "route" in GATEWAY_KNOWN_COMMANDS


def test_classic_cli_dispatches_route_without_rebuilding_cached_agent():
    from cli import HermesCLI

    shell = HermesCLI.__new__(HermesCLI)
    cached_agent = object()
    shell.agent = cached_agent
    shell.config = {"routing": _config("observe")}
    shell._agent_running = False
    shell._pending_input = MagicMock()
    shell.console = MagicMock()

    with patch.object(shell, "_handle_route_command", create=True) as handler:
        assert shell.process_command("/route status") is True

    handler.assert_called_once_with("status")
    assert shell.agent is cached_agent


def test_classic_cli_route_mode_updates_profile_config_without_rebuilding_agent():
    import cli as cli_mod
    from cli import HermesCLI

    shell = HermesCLI.__new__(HermesCLI)
    cached_agent = object()
    shell.agent = cached_agent
    shell.config = {"routing": _config("off")}
    printed = []

    with patch.object(cli_mod, "save_config_value", return_value=True) as writer, patch.object(
        cli_mod, "_cprint", side_effect=lambda value: printed.append(str(value))
    ):
        shell._handle_route_command("observe")

    writer.assert_called_once_with("routing.mode", "observe")
    assert shell.config["routing"]["mode"] == "observe"
    assert printed == ["Route mode set to observe"]
    assert shell.agent is cached_agent
