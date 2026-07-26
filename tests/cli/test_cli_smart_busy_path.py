import queue
import threading
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
    instance._smart_cli_turn_lock = threading.Lock()
    instance._smart_cli_turn_generation = 7
    instance._smart_cli_active_turn = (7, "Fix the gateway", instance.agent)
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


def test_cli_smart_independent_injects_directive_without_false_parallel_ack(monkeypatch):
    cli = _cli()
    monkeypatch.setattr(
        "hermes_cli.smart_orchestrator.classify_smart_message",
        lambda **_kwargs: (_decision(ROUTE_INDEPENDENT), "research another market"),
    )

    route = cli._route_smart_cli_input("research another market")

    assert route == ROUTE_RELATED
    injected = cli.agent.steer.call_args.args[0]
    assert "SMART ORCHESTRATOR" in injected
    assert "delegate_task" in injected
    assert "research another market" in injected
    assert cli._pending_input.empty()
    assert cli._interrupt_queue.empty()


def test_cli_smart_classifier_receives_immutable_active_prompt(monkeypatch):
    cli = _cli()
    seen = {}

    def classify(**kwargs):
        seen.update(kwargs)
        return _decision(ROUTE_DEPENDENT), "next"

    monkeypatch.setattr(
        "hermes_cli.smart_orchestrator.classify_smart_message",
        classify,
    )

    cli._route_smart_cli_input("next")

    assert seen["active_goal"] == "Fix the gateway"


def test_cli_smart_reused_agent_generation_change_queues_old_decision(monkeypatch):
    cli = _cli()

    def classify(**_kwargs):
        # The same cached agent starts turn N+1 while turn N's classification
        # is in flight. Identity still matches, but ownership does not.
        cli._smart_cli_turn_generation = 8
        cli._smart_cli_active_turn = (8, "New turn", cli.agent)
        return _decision(ROUTE_RELATED), "stale correction"

    monkeypatch.setattr(
        "hermes_cli.smart_orchestrator.classify_smart_message",
        classify,
    )

    route = cli._route_smart_cli_input("stale correction")

    assert route == ROUTE_AMBIGUOUS
    cli.agent.steer.assert_not_called()
    assert cli._pending_input.get_nowait() == "stale correction"


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


def test_cli_smart_enqueue_captures_turn_context_at_admission():
    """FIFO wait must not recapture a cached agent's later turn generation."""
    cli = _cli()
    cli._smart_cli_worker = MagicMock()
    cli._smart_cli_worker.is_alive.return_value = True

    snapshot = cli._begin_smart_cli_turn("turn N")
    cli._enqueue_smart_cli_input("message admitted during N")

    queued_text, route_context = cli._smart_cli_input_queue.get_nowait()
    assert queued_text == "message admitted during N"
    assert route_context.turn_snapshot is snapshot
    assert route_context.agent is cli.agent


def test_cli_smart_worker_uses_admission_context_instead_of_recapturing(monkeypatch):
    from cli import SmartCliRouteContext, SmartCliTurnSnapshot

    cli = _cli()
    admitted_context = SmartCliRouteContext(
        turn_snapshot=SmartCliTurnSnapshot(7, "turn N", object()),
        agent=object(),
        steer_generation=17,
        supports_generation=True,
    )
    routed = []
    cli._smart_cli_input_queue.put(("follow-up", admitted_context))
    cli._smart_cli_input_queue.put(None)
    monkeypatch.setattr(
        cli,
        "_route_smart_cli_input",
        lambda text, *, route_context=None: routed.append((text, route_context)),
    )

    cli._smart_cli_worker_loop()

    assert routed == [("follow-up", admitted_context)]
