from cli import HermesCLI
from hermes_cli.active_sessions import (
    active_session_registry_snapshot,
    try_acquire_active_session,
)
from hermes_cli import active_sessions


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


def test_cli_release_retains_lease_after_transient_registry_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    lease, _ = try_acquire_active_session(
        session_id="retry-cli", surface="cli", config={"max_concurrent_sessions": 1}
    )
    cli = object.__new__(HermesCLI)
    cli._active_session_lease = lease
    real_write = active_sessions._write_entries
    calls = 0

    def fail_once(path, entries):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("transient")
        return real_write(path, entries)

    monkeypatch.setattr(active_sessions, "_write_entries", fail_once)
    cli._release_active_session()
    assert cli._active_session_lease is lease
    cli._release_active_session()
    assert cli._active_session_lease is None
    assert active_session_registry_snapshot() == []
