from typing import Any, cast

import cli as cli_module
from cli import HermesCLI
from hermes_cli import active_sessions
from hermes_cli.active_sessions import (
    active_session_registry_snapshot,
    try_acquire_active_session,
)


def test_cli_claim_active_session_respects_global_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    cfg = {"max_concurrent_sessions": 1}
    held, message = try_acquire_active_session(
        session_id="held-session",
        surface="tui",
        config=cfg,
    )
    assert message is None
    assert held is not None

    cli = object.__new__(HermesCLI)
    cli.session_id = "new-cli-session"
    cli.config = cfg
    cli._active_session_lease = None
    printed: list[str] = []
    cli._console_print = lambda text: printed.append(text)

    try:
        assert cli._claim_active_session("cli") is False
        assert len(printed) == 1
        assert "active session limit (1/1)" in printed[0]
        # Names the holding surface ("tui"), not the blocked one.
        assert "Held by: tui" in printed[0]

        held.release()

        assert cli._claim_active_session("cli") is True
        assert [entry["session_id"] for entry in active_session_registry_snapshot()] == [
            "new-cli-session"
        ]
    finally:
        held.release()
        cli._release_active_session()


def test_cli_claim_runs_state_maintenance_after_owner_registration(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    cli = object.__new__(HermesCLI)
    cli.session_id = "current-session"
    cli.config = {}
    cli._active_session_lease = None
    cli._session_db = cast(Any, object())
    calls = []
    monkeypatch.setattr(
        cli_module,
        "_run_state_db_auto_maintenance",
        lambda session_db: calls.append(
            (
                session_db,
                [entry["session_id"] for entry in active_session_registry_snapshot()],
            )
        ),
    )

    try:
        assert cli._claim_active_session("cli") is True
    finally:
        cli._release_active_session()

    assert calls == [(cli._session_db, ["current-session"])]


def test_cli_claim_fails_closed_when_owner_identity_cannot_be_registered(
    monkeypatch,
):
    cli = object.__new__(HermesCLI)
    cli.session_id = "unregistered-session"
    cli.config = {}
    cli._active_session_lease = None
    printed = []
    cli._console_print = lambda text: printed.append(text)

    monkeypatch.setattr(
        active_sessions,
        "try_acquire_active_session",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("identity unavailable")),
    )

    assert cli._claim_active_session("cli") is False
    assert cli._active_session_lease is None
    assert len(printed) == 1
    assert "could not register process ownership" in printed[0].lower()


def test_cli_claim_rejects_missing_lease_without_error_message(monkeypatch):
    cli = object.__new__(HermesCLI)
    cli.session_id = "missing-lease"
    cli.config = {}
    cli._active_session_lease = None
    printed = []
    cli._console_print = lambda text: printed.append(text)
    monkeypatch.setattr(
        active_sessions,
        "try_acquire_active_session",
        lambda **_kwargs: (None, None),
    )

    assert cli._claim_active_session("cli") is False
    assert "ownership lease" in printed[0].lower()
