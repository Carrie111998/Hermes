"""Regression coverage for CLI async-delegation completion ownership."""

import queue
import sqlite3
from types import SimpleNamespace

import pytest

from cli import HermesCLI, _ProcessNotificationInput


def _cli(session_id="visible-session"):
    cli = HermesCLI.__new__(HermesCLI)
    cli.session_id = session_id
    cli._pending_input = queue.Queue()
    return cli


class _Registry:
    def __init__(self, event, text="completion payload"):
        self.event = event
        self.text = text
        self.calls = []
        self.completion_queue = queue.Queue()

    def drain_notifications(self, *, session_key="", owns_event=None):
        self.calls.append((session_key, owns_event(self.event)))
        return [(self.event, self.text)]


def _claim(state, claim_id=""):
    return SimpleNamespace(state=state, claim_id=claim_id)


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (None, False),
        ({"completed": False}, False),
        ({"completed": True}, True),
        ({"completed": True, "interrupted": True}, False),
        ({"completed": True, "error": "provider failed"}, False),
        ({"completed": True, "failed": True}, False),
        ({"completed": True, "partial": True}, False),
    ],
)
def test_cli_notification_ack_requires_genuinely_completed_turn(result, expected):
    assert HermesCLI._process_notification_turn_succeeded(result) is expected


def test_cli_completion_drain_uses_visible_session_identity_without_early_ack(
    monkeypatch,
):
    """Queueing alone must not acknowledge a turn the agent has not run."""
    cli = _cli()
    event = {
        "type": "async_delegation",
        "delegation_id": "deleg_visible",
        "session_key": "visible-session",
    }
    registry = _Registry(event)
    calls = []

    monkeypatch.setattr("tools.process_registry.process_registry", registry)
    monkeypatch.setattr(
        "tools.async_delegation.claim_event_delivery_state",
        lambda evt, consumer: (
            calls.append(("claim", evt, consumer))
            or _claim("claimed", "claim-token")
        ),
    )
    monkeypatch.setattr(
        "tools.async_delegation.complete_event_delivery",
        lambda evt, token: calls.append(("complete", evt, token)) or True,
    )

    cli._drain_process_notifications("cli-idle")

    assert registry.calls == [("visible-session", True)]
    notification = cli._pending_input.get_nowait()
    assert isinstance(notification, _ProcessNotificationInput)
    assert notification.text == "completion payload"
    assert calls == []

    assert cli._claim_process_notification(notification)
    calls.append(("turn", notification.text))
    cli._settle_process_notification(notification, True)
    assert calls == [
        ("claim", event, "cli-idle"),
        ("turn", "completion payload"),
        ("complete", event, "claim-token"),
    ]
    assert registry.completion_queue.empty()


@pytest.mark.parametrize(
    ("state", "expected_message", "expected_requeue"),
    [
        ("busy_pending", None, True),
        ("terminal_consumed", None, False),
        ("legacy_non_durable", "completion payload", False),
    ],
)
def test_cli_completion_drain_honors_non_claimed_states(
    monkeypatch, state, expected_message, expected_requeue
):
    cli = _cli()
    event = {
        "type": "async_delegation",
        "delegation_id": "deleg-state",
        "session_key": cli.session_id,
    }
    registry = _Registry(event)
    completed = []
    monkeypatch.setattr("tools.process_registry.process_registry", registry)
    monkeypatch.setattr(
        "tools.async_delegation.claim_event_delivery_state",
        lambda *_args: _claim(state),
    )
    monkeypatch.setattr(
        "tools.async_delegation.complete_event_delivery",
        lambda *args: completed.append(args) or True,
    )

    cli._drain_process_notifications("cli-idle")
    notification = cli._pending_input.get_nowait()
    should_run = cli._claim_process_notification(notification)

    if expected_message is None:
        assert not should_run
    else:
        assert should_run
        assert notification.text == expected_message
        cli._settle_process_notification(notification, True)
    if expected_requeue:
        assert registry.completion_queue.get_nowait() is event
    assert registry.completion_queue.empty()
    assert completed == []


@pytest.mark.parametrize(
    ("durable_state", "expected_requeue"),
    [
        ("busy_pending", True),
        ("terminal_consumed", False),
    ],
)
def test_cli_false_ack_never_consumes_and_only_requeues_live_state(
    monkeypatch, durable_state, expected_requeue
):
    cli = _cli()
    event = {
        "type": "async_delegation",
        "delegation_id": "deleg-false-ack",
        "session_key": cli.session_id,
    }
    registry = _Registry(event)
    released = []
    monkeypatch.setattr("tools.process_registry.process_registry", registry)
    monkeypatch.setattr(
        "tools.async_delegation.claim_event_delivery_state",
        lambda *_args: _claim("claimed", "claim-token"),
    )
    monkeypatch.setattr(
        "tools.async_delegation.complete_event_delivery",
        lambda *_args: False,
    )
    monkeypatch.setattr(
        "tools.async_delegation.release_event_delivery",
        lambda *args: released.append(args) or False,
    )
    monkeypatch.setattr(
        "tools.async_delegation.inspect_event_delivery_state",
        lambda _event: durable_state,
    )

    notification = _ProcessNotificationInput("completion payload", event, "cli-idle")
    assert cli._claim_process_notification(notification)
    cli._settle_process_notification(notification, True)

    assert released == [(event, "claim-token")]
    if expected_requeue:
        assert registry.completion_queue.get_nowait() is event
    assert registry.completion_queue.empty()


def test_cli_failed_durable_turn_releases_without_ack_and_requeues(monkeypatch):
    cli = _cli()
    event = {
        "type": "async_delegation",
        "delegation_id": "deleg-turn-failed",
        "session_key": cli.session_id,
    }
    registry = _Registry(event)
    completed = []
    released = []
    monkeypatch.setattr("tools.process_registry.process_registry", registry)
    monkeypatch.setattr(
        "tools.async_delegation.claim_event_delivery_state",
        lambda *_args: _claim("claimed", "claim-token"),
    )
    monkeypatch.setattr(
        "tools.async_delegation.complete_event_delivery",
        lambda *args: completed.append(args) or True,
    )
    monkeypatch.setattr(
        "tools.async_delegation.release_event_delivery",
        lambda *args: released.append(args) or True,
    )
    monkeypatch.setattr(
        "tools.async_delegation.inspect_event_delivery_state",
        lambda _event: "busy_pending",
    )

    notification = _ProcessNotificationInput("completion payload", event, "cli-idle")
    assert cli._claim_process_notification(notification)
    cli._settle_process_notification(notification, False)

    assert completed == []
    assert released == [(event, "claim-token")]
    assert registry.completion_queue.get_nowait() is event
    assert registry.completion_queue.empty()


def test_cli_failed_legacy_turn_requeues_without_durable_ack(monkeypatch):
    cli = _cli()
    event = {"type": "completion", "session_id": "legacy-process"}
    registry = _Registry(event)
    completed = []
    monkeypatch.setattr("tools.process_registry.process_registry", registry)
    monkeypatch.setattr(
        "tools.async_delegation.claim_event_delivery_state",
        lambda *_args: _claim("legacy_non_durable"),
    )
    monkeypatch.setattr(
        "tools.async_delegation.complete_event_delivery",
        lambda *args: completed.append(args) or True,
    )

    notification = _ProcessNotificationInput("completion payload", event, "cli-idle")
    assert cli._claim_process_notification(notification)
    cli._settle_process_notification(notification, False)

    assert completed == []
    assert registry.completion_queue.get_nowait() is event
    assert registry.completion_queue.empty()


def test_cli_claim_exception_preserves_live_event_without_injection(monkeypatch):
    cli = _cli()
    event = {
        "type": "async_delegation",
        "delegation_id": "deleg-claim-error",
        "session_key": cli.session_id,
    }
    registry = _Registry(event)
    monkeypatch.setattr("tools.process_registry.process_registry", registry)
    monkeypatch.setattr(
        "tools.async_delegation.claim_event_delivery_state",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("claim unavailable")),
    )
    monkeypatch.setattr(
        "tools.async_delegation.inspect_event_delivery_state",
        lambda _event: "busy_pending",
    )

    notification = _ProcessNotificationInput("completion payload", event, "cli-idle")
    assert not cli._claim_process_notification(notification)

    assert registry.completion_queue.get_nowait() is event
    assert registry.completion_queue.empty()


def test_cli_ack_exception_releases_and_preserves_live_event(monkeypatch):
    cli = _cli()
    event = {
        "type": "async_delegation",
        "delegation_id": "deleg-ack-error",
        "session_key": cli.session_id,
    }
    registry = _Registry(event)
    released = []
    monkeypatch.setattr("tools.process_registry.process_registry", registry)
    monkeypatch.setattr(
        "tools.async_delegation.claim_event_delivery_state",
        lambda *_args: _claim("claimed", "claim-token"),
    )
    monkeypatch.setattr(
        "tools.async_delegation.complete_event_delivery",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("ack unavailable")),
    )
    monkeypatch.setattr(
        "tools.async_delegation.release_event_delivery",
        lambda *args: released.append(args) or True,
    )
    monkeypatch.setattr(
        "tools.async_delegation.inspect_event_delivery_state",
        lambda _event: "busy_pending",
    )

    notification = _ProcessNotificationInput("completion payload", event, "cli-idle")
    assert cli._claim_process_notification(notification)
    cli._settle_process_notification(notification, True)

    assert released == [(event, "claim-token")]
    assert registry.completion_queue.get_nowait() is event
    assert registry.completion_queue.empty()


def test_cli_real_stale_claim_converges_after_successor_ack(monkeypatch, tmp_path):
    """A stale in-memory event must not replay after a real SQLite rollover."""
    from tools import async_delegation

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    cli = _cli("rollover-owner")
    event = {
        "type": "async_delegation",
        "delegation_id": "cli-real-rollover",
        "_durable_delivery": True,
        "session_key": cli.session_id,
        "status": "completed",
        "completed_at": 2.0,
    }
    async_delegation._persist_dispatch(
        {
            "delegation_id": event["delegation_id"],
            "session_key": event["session_key"],
            "origin_ui_session_id": "",
            "parent_session_id": None,
            "dispatched_at": 1.0,
        }
    )
    async_delegation._persist_completion(
        event, {"status": "completed", "summary": "done"}
    )
    stale_claim = async_delegation.claim_event_delivery(event, "stale-cli")
    assert stale_claim
    with sqlite3.connect(tmp_path / "state.db") as conn:
        conn.execute(
            """UPDATE async_delegations SET delivery_claimed_at=0
               WHERE delegation_id=?""",
            (event["delegation_id"],),
        )
    successor = async_delegation.claim_event_delivery_state(event, "successor")
    assert successor.state == "claimed"
    assert async_delegation.complete_event_delivery(event, successor.claim_id)

    registry = _Registry(event)
    monkeypatch.setattr("tools.process_registry.process_registry", registry)
    notification = _ProcessNotificationInput("completion payload", event, "cli-idle")
    assert not cli._claim_process_notification(notification)

    assert registry.completion_queue.empty()
    assert async_delegation.inspect_event_delivery_state(event) == "terminal_consumed"


def test_cli_completion_ownership_rejects_foreign_session():
    cli = _cli()
    cli._session_db = None

    assert not cli._owns_process_notification(
        {"type": "async_delegation", "session_key": "foreign-session"}
    )


def test_cli_completion_ownership_accepts_compression_lineage():
    cli = _cli()

    class FakeSessionDB:
        def resolve_resume_session_id(self, session_id):
            assert session_id == "pre-compression-session"
            return "visible-session"

    cli._session_db = FakeSessionDB()

    assert cli._owns_process_notification(
        {
            "type": "async_delegation",
            "session_key": "pre-compression-session",
        }
    )
