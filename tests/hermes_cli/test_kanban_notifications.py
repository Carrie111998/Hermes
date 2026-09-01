"""Behavior contracts for shared Kanban notification projection."""

from hermes_cli.kanban_db import Event
from hermes_cli.kanban_notifications import (
    coalesce_notification_events,
    format_gave_up_notification,
)


def _event(
    event_id: int,
    kind: str,
    payload: dict | None = None,
    *,
    run_id: int | None = None,
) -> Event:
    return Event(
        id=event_id,
        task_id="t_timeout",
        kind=kind,
        payload=payload,
        created_at=100,
        run_id=run_id,
    )


def test_standalone_timeout_is_preserved_for_retry_alert():
    timed_out = _event(
        10,
        "timed_out",
        {"pid": 1, "limit_seconds": 30, "retry_status": "ready"},
        run_id=1,
    )

    assert coalesce_notification_events([timed_out]) == [timed_out]


def test_only_adjacent_terminal_timeout_is_superseded_in_repeated_batch():
    earlier = _event(
        10,
        "timed_out",
        {"pid": 1, "retry_status": "ready"},
        run_id=1,
    )
    terminal = _event(
        11,
        "timed_out",
        {"pid": 2, "retry_status": "ready"},
        run_id=2,
    )
    gave_up = _event(
        12,
        "gave_up",
        {
            "pid": 2,
            "retry_status": "ready",
            "trigger_outcome": "timed_out",
            "failures": 2,
        },
    )

    assert coalesce_notification_events([earlier, terminal, gave_up]) == [
        earlier,
        gave_up,
    ]


def test_intervening_event_prevents_timeout_suppression():
    timed_out = _event(
        20,
        "timed_out",
        {"pid": 3, "retry_status": "ready"},
        run_id=3,
    )
    crashed = _event(21, "crashed", {"pid": 4}, run_id=4)
    gave_up = _event(
        22,
        "gave_up",
        {
            "pid": 3,
            "retry_status": "ready",
            "trigger_outcome": "timed_out",
            "failures": 2,
        },
    )

    events = [timed_out, crashed, gave_up]
    assert coalesce_notification_events(events) == events


def test_spawn_failure_breaker_does_not_supersede_timeout():
    timed_out = _event(30, "timed_out", {"pid": 5}, run_id=5)
    gave_up = _event(
        31,
        "gave_up",
        {"trigger_outcome": "spawn_failed", "failures": 2},
    )

    assert coalesce_notification_events([timed_out, gave_up]) == [
        timed_out,
        gave_up,
    ]


def test_gave_up_wording_distinguishes_timeout_and_spawn_failure():
    timeout_text = format_gave_up_notification(
        "[main] ",
        "@worker ",
        "t_timeout",
        {"trigger_outcome": "timed_out", "failures": 2},
    )
    spawn_text = format_gave_up_notification(
        "[main] ",
        "@worker ",
        "t_spawn",
        {
            "trigger_outcome": "spawn_failed",
            "failures": 3,
            "error": "profile worker not found",
        },
    )

    assert "timed out after 2 attempts and is blocked" in timeout_text
    assert "will retry" not in timeout_text
    assert "spawn" not in timeout_text
    assert "gave up after 3 spawn failures and is blocked" in spawn_text
    assert "profile worker not found" in spawn_text
    assert "timed out" not in spawn_text
