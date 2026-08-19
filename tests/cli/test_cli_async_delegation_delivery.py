"""Regression coverage for CLI async-delegation completion delivery.

Two invariants: a CLI window only claims completions it owns, and a claimed
delegation completion reaches ``_pending_input`` carrying its structured
display typing so the persisted turn is an internal control row rather than
an ordinary user message.
"""

import queue

from cli import HermesCLI, _SyntheticNotificationMessage


def test_cli_completion_drain_uses_visible_session_identity(monkeypatch):
    """A CLI window must not claim another window's restored completion."""
    cli = HermesCLI.__new__(HermesCLI)
    cli.session_id = "visible-session"
    cli._pending_input = queue.Queue()

    event = {
        "type": "async_delegation",
        "delegation_id": "deleg_visible",
        "session_key": "visible-session",
    }
    calls = []

    class FakeRegistry:
        def drain_notifications(self, *, session_key="", owns_event=None):
            calls.append((session_key, owns_event(event)))
            return [(event, "completion payload")]

    claimed = []
    completed = []

    monkeypatch.setattr(
        "tools.process_registry.process_registry",
        FakeRegistry(),
    )
    monkeypatch.setattr(
        "tools.async_delegation.claim_event_delivery",
        lambda evt, consumer: claimed.append((evt, consumer)) or "claim-token",
    )
    monkeypatch.setattr(
        "tools.async_delegation.complete_event_delivery",
        lambda evt, token: completed.append((evt, token)),
    )

    cli._drain_process_notifications("cli-idle")

    assert calls == [("visible-session", True)]
    queued = cli._pending_input.get_nowait()
    assert str(queued) == "completion payload"
    assert claimed == [(event, "cli-idle")]
    assert completed == [(event, "claim-token")]


def test_cli_delegation_completion_carries_display_typing(monkeypatch):
    """The queued turn must reach chat() typed as an internal control row.

    Without the wrapper the CLI persists the completion block as a plain
    ``role=user`` row (``display_kind=NULL``) and it hydrates as a full user
    bubble — the direct-path half of #82888. The payload must match what the
    TUI poller injects for the same event.
    """
    cli = HermesCLI.__new__(HermesCLI)
    cli.session_id = "visible-session"
    cli._pending_input = queue.Queue()

    event = {
        "type": "async_delegation",
        "delegation_id": "deleg_visible",
        "session_key": "visible-session",
        "results": [{"status": "completed"}, {"status": "failed"}],
        "total_duration_seconds": 12.5,
    }

    class FakeRegistry:
        def drain_notifications(self, *, session_key="", owns_event=None):
            return [(event, "[ASYNC DELEGATION BATCH COMPLETE - deleg_visible]")]

    monkeypatch.setattr("tools.process_registry.process_registry", FakeRegistry())
    monkeypatch.setattr(
        "tools.async_delegation.claim_event_delivery",
        lambda evt, consumer: "claim-token",
    )
    monkeypatch.setattr(
        "tools.async_delegation.complete_event_delivery",
        lambda evt, token: None,
    )

    cli._drain_process_notifications("cli-idle")

    queued = cli._pending_input.get_nowait()
    assert isinstance(queued, _SyntheticNotificationMessage)
    assert queued.text == "[ASYNC DELEGATION BATCH COMPLETE - deleg_visible]"
    assert queued.display_kind == "async_delegation_complete"

    from tools.process_registry import async_delegation_display_metadata

    assert queued.display_metadata == async_delegation_display_metadata(event)
    assert queued.display_metadata["delegation_id"] == "deleg_visible"
    assert queued.display_metadata["task_count"] == 2


def test_cli_ordinary_notification_stays_an_untyped_user_turn(monkeypatch):
    """Only delegation completions are retyped.

    A background-process completion is a genuine notification the user asked
    for, so it keeps the plain-string path and renders as a normal turn.
    """
    cli = HermesCLI.__new__(HermesCLI)
    cli.session_id = "visible-session"
    cli._pending_input = queue.Queue()

    event = {"type": "process_complete", "pid": 4242}

    class FakeRegistry:
        def drain_notifications(self, *, session_key="", owns_event=None):
            return [(event, "[background process 4242 finished]")]

    monkeypatch.setattr("tools.process_registry.process_registry", FakeRegistry())
    monkeypatch.setattr(
        "tools.async_delegation.claim_event_delivery",
        lambda evt, consumer: "claim-token",
    )
    monkeypatch.setattr(
        "tools.async_delegation.complete_event_delivery",
        lambda evt, token: None,
    )

    cli._drain_process_notifications("cli-idle")

    assert cli._pending_input.get_nowait() == "[background process 4242 finished]"


def test_cli_completion_ownership_rejects_foreign_session():
    cli = HermesCLI.__new__(HermesCLI)
    cli.session_id = "visible-session"
    cli._session_db = None

    assert not cli._owns_process_notification(
        {"type": "async_delegation", "session_key": "foreign-session"}
    )


def test_cli_completion_ownership_accepts_compression_lineage():
    cli = HermesCLI.__new__(HermesCLI)
    cli.session_id = "visible-session"

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
