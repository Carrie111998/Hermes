"""Regression coverage for CLI async-delegation completion ownership."""

import queue

import pytest

from cli import HermesCLI


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
    monkeypatch.setattr(
        "tools.process_registry.format_process_notification",
        lambda evt: "refreshed completion payload",
    )

    cli._drain_process_notifications("cli-idle")

    assert calls == [("visible-session", True)]
    assert cli._pending_input.get_nowait() == "refreshed completion payload"
    assert claimed == [(event, "cli-idle")]
    assert completed == [(event, "claim-token")]


def test_cli_reformats_each_claimed_completion_after_prior_ack(monkeypatch):
    cli = HermesCLI.__new__(HermesCLI)
    cli.session_id = "visible-session"
    cli._pending_input = queue.Queue()
    events = [
        {
            "type": "async_delegation",
            "delegation_id": delegation_id,
            "session_key": "visible-session",
        }
        for delegation_id in ("first", "second")
    ]

    class FakeRegistry:
        def drain_notifications(self, *, session_key="", owns_event=None):
            return [(event, "stale") for event in events]

    acknowledged = []
    monkeypatch.setattr("tools.process_registry.process_registry", FakeRegistry())
    monkeypatch.setattr(
        "tools.process_registry.format_process_notification",
        lambda evt: f"{evt['delegation_id']}: prior={len(acknowledged)}",
    )
    monkeypatch.setattr(
        "tools.async_delegation.claim_event_delivery",
        lambda evt, consumer: f"claim-{evt['delegation_id']}",
    )
    monkeypatch.setattr(
        "tools.async_delegation.complete_event_delivery",
        lambda evt, token: acknowledged.append(evt["delegation_id"]),
    )

    cli._drain_process_notifications("cli-idle")

    assert cli._pending_input.get_nowait() == "first: prior=0"
    assert cli._pending_input.get_nowait() == "second: prior=1"


def test_cli_passes_compression_lineage_to_completion_formatter(monkeypatch):
    cli = HermesCLI.__new__(HermesCLI)
    cli.session_id = "session-new"
    cli._pending_input = queue.Queue()
    cli._session_db = type(
        "FakeSessionDB",
        (),
        {"get_compression_lineage": lambda self, _key: ["session-old", "session-new"]},
    )()
    event = {
        "type": "async_delegation",
        "delegation_id": "deleg-lineage",
        "session_key": "session-old",
    }

    class FakeRegistry:
        def drain_notifications(self, **_kwargs):
            return [(event, "stale")]

    seen = []
    monkeypatch.setattr("tools.process_registry.process_registry", FakeRegistry())
    monkeypatch.setattr(
        "tools.async_delegation.claim_event_delivery", lambda *_args: "claim"
    )
    monkeypatch.setattr(
        "tools.async_delegation.complete_event_delivery", lambda *_args: None
    )
    monkeypatch.setattr(
        "tools.process_registry.format_process_notification",
        lambda evt: seen.append(set(evt["_owner_session_keys"])) or "formatted",
    )

    cli._drain_process_notifications("cli-idle")

    assert seen == [{"session-old", "session-new"}]


def test_cli_marks_owner_lineage_unknown_when_resolution_fails(monkeypatch):
    cli = HermesCLI.__new__(HermesCLI)
    cli.session_id = "session-new"
    cli._pending_input = queue.Queue()
    cli._session_db = type(
        "BrokenSessionDB",
        (),
        {
            "get_compression_lineage": lambda self, _key: (_ for _ in ()).throw(
                RuntimeError("db unavailable")
            )
        },
    )()
    event = {
        "type": "async_delegation",
        "delegation_id": "deleg-lineage-error",
        "session_key": "session-old",
    }

    class FakeRegistry:
        def drain_notifications(self, **_kwargs):
            return [(event, "stale")]

    seen = []
    monkeypatch.setattr("tools.process_registry.process_registry", FakeRegistry())
    monkeypatch.setattr(
        "tools.async_delegation.claim_event_delivery", lambda *_args: "claim"
    )
    monkeypatch.setattr(
        "tools.async_delegation.complete_event_delivery", lambda *_args: None
    )
    monkeypatch.setattr(
        "tools.process_registry.format_process_notification",
        lambda evt: seen.append(evt["_owner_session_lineage_known"]) or "formatted",
    )

    cli._drain_process_notifications("cli-idle")

    assert seen == [False]


@pytest.mark.parametrize("failure_stage", ["claim", "format", "ack"])
def test_cli_drain_requeues_event_when_delivery_step_fails(monkeypatch, failure_stage):
    cli = HermesCLI.__new__(HermesCLI)
    cli.session_id = "visible-session"
    cli._pending_input = queue.Queue()
    event = {
        "type": "async_delegation",
        "delegation_id": "deleg-retry",
        "session_key": "visible-session",
    }
    queued = queue.Queue()
    released = []

    class FakeRegistry:
        completion_queue = queued

        def drain_notifications(self, **_kwargs):
            return [(event, "stale")]

    monkeypatch.setattr("tools.process_registry.process_registry", FakeRegistry())

    def claim(_event, _consumer):
        if failure_stage == "claim":
            raise RuntimeError("claim failed")
        return "claim-token"

    def format_notification(_event):
        if failure_stage == "format":
            raise RuntimeError("format failed")
        return "formatted"

    def acknowledge(_event, _claim):
        if failure_stage == "ack":
            raise RuntimeError("ack failed")

    monkeypatch.setattr("tools.async_delegation.claim_event_delivery", claim)
    monkeypatch.setattr(
        "tools.async_delegation.release_event_delivery",
        lambda evt, token: released.append((evt, token)),
    )
    monkeypatch.setattr(
        "tools.async_delegation.complete_event_delivery", acknowledge
    )
    monkeypatch.setattr(
        "tools.process_registry.format_process_notification", format_notification
    )

    cli._drain_process_notifications("cli-idle")

    if failure_stage == "ack":
        assert queued.empty(), "accepted input must not be immediately duplicated"
    else:
        assert queued.get_nowait() is event
    expected_release = (
        []
        if failure_stage in {"claim", "ack"}
        else [(event, "claim-token")]
    )
    assert released == expected_release
    if failure_stage == "ack":
        assert cli._pending_input.get_nowait() == "formatted"
    else:
        assert cli._pending_input.empty()


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
