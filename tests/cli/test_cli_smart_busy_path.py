import queue
from unittest.mock import MagicMock

from hermes_cli.smart_orchestrator import (
    ROUTE_AMBIGUOUS,
    ROUTE_DEPENDENT,
    ROUTE_INDEPENDENT,
    ROUTE_RELATED,
    SmartRouteDecision,
)


def _decision(route):
    return SmartRouteDecision(
        route=route, confidence=0.95, reason=f"reason-{route}", source="classifier"
    )


def _cli():
    from cli import HermesCLI

    instance = object.__new__(HermesCLI)
    instance._agent_running = True
    instance._pending_input = queue.Queue()
    instance._interrupt_queue = queue.Queue()
    instance._smart_cli_input_queue = queue.Queue()
    instance._smart_cli_worker_lock = MagicMock()
    instance.agent = MagicMock()
    instance.agent.steer.return_value = True
    instance.agent.messages = [{"role": "user", "content": "Fix the gateway"}]
    instance.agent.get_activity_summary.return_value = {"current_tool": "terminal"}
    return instance


def test_cli_smart_related_steers_without_interrupt_or_next_turn_queue(monkeypatch):
    cli = _cli()
    monkeypatch.setattr(
        "hermes_cli.smart_orchestrator.classify_smart_message",
        lambda **_kwargs: (_decision(ROUTE_RELATED), "add this requirement"),
    )

    route = cli._route_smart_cli_input("add this requirement")

    assert route == ROUTE_RELATED
    cli.agent.steer.assert_called_once_with("add this requirement")
    assert cli._pending_input.empty()
    assert cli._interrupt_queue.empty()


def test_cli_smart_independent_injects_parallel_directive(monkeypatch):
    cli = _cli()
    monkeypatch.setattr(
        "hermes_cli.smart_orchestrator.classify_smart_message",
        lambda **_kwargs: (_decision(ROUTE_INDEPENDENT), "research another market"),
    )

    route = cli._route_smart_cli_input("research another market")

    assert route == ROUTE_INDEPENDENT
    injected = cli.agent.steer.call_args.args[0]
    assert "SMART ORCHESTRATOR" in injected
    assert "delegate_task" in injected
    assert "research another market" in injected
    assert cli._pending_input.empty()
    assert cli._interrupt_queue.empty()


def test_cli_smart_dependent_and_ambiguous_queue_losslessly(monkeypatch):
    for route in (ROUTE_DEPENDENT, ROUTE_AMBIGUOUS):
        cli = _cli()
        original = f"request-{route}"
        monkeypatch.setattr(
            "hermes_cli.smart_orchestrator.classify_smart_message",
            lambda route=route, **_kwargs: (_decision(route), original),
        )

        returned = cli._route_smart_cli_input(original)

        assert returned == route
        assert cli._pending_input.get_nowait() == original
        assert cli._interrupt_queue.empty()
        cli.agent.interrupt.assert_not_called()


def test_cli_smart_steer_race_falls_back_to_next_turn_queue(monkeypatch):
    cli = _cli()
    cli.agent.steer.return_value = False
    monkeypatch.setattr(
        "hermes_cli.smart_orchestrator.classify_smart_message",
        lambda **_kwargs: (_decision(ROUTE_RELATED), "late correction"),
    )

    route = cli._route_smart_cli_input("late correction")

    assert route == ROUTE_AMBIGUOUS
    assert cli._pending_input.get_nowait() == "late correction"
    assert cli._interrupt_queue.empty()


def test_cli_smart_no_longer_active_queues_without_attempting_steer(monkeypatch):
    cli = _cli()
    cli._agent_running = False
    monkeypatch.setattr(
        "hermes_cli.smart_orchestrator.classify_smart_message",
        lambda **_kwargs: (_decision(ROUTE_RELATED), "arrived at boundary"),
    )

    route = cli._route_smart_cli_input("arrived at boundary")

    assert route == ROUTE_AMBIGUOUS
    cli.agent.steer.assert_not_called()
    assert cli._pending_input.get_nowait() == "arrived at boundary"
    assert cli._interrupt_queue.empty()
