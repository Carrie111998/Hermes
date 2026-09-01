from __future__ import annotations

import threading

import pytest

from tui_gateway import server


class _ComputeHostSupervisor:
    def __init__(self) -> None:
        self.interrupts: list[tuple[str, str | None]] = []

    def interrupt(self, session_id: str, *, request_id: str | None = None) -> bool:
        self.interrupts.append((session_id, request_id))
        return True


class _FailingComputeHostSupervisor(_ComputeHostSupervisor):
    def interrupt(self, session_id: str, *, request_id: str | None = None) -> bool:
        self.interrupts.append((session_id, request_id))
        raise RuntimeError("host pipe unavailable")


def _compute_session(session_id: str) -> dict:
    return {
        "_sid": session_id,
        "_compute_host_active": True,
        "running": True,
        "history_lock": threading.RLock(),
        "session_key": f"stored-{session_id}",
    }


@pytest.mark.parametrize(
    "end_reason",
    ["tui_close", "tui_shutdown", "ws_disconnect"],
)
def test_compute_host_teardown_interrupts_admitted_turn(monkeypatch, end_reason):
    supervisor = _ComputeHostSupervisor()
    session = _compute_session("compute-turn")

    monkeypatch.setattr(server, "_get_compute_host_supervisor", lambda: supervisor)
    monkeypatch.setattr(server, "_clear_pending", lambda _sid: None)
    monkeypatch.setattr(server, "_teardown_session", lambda *_args, **_kwargs: None)

    assert server._teardown_popped_session(session, end_reason=end_reason) is True
    assert supervisor.interrupts == [("compute-turn", None)]
    assert session["_turn_cancel_requested"] is True
    assert session["running"] is True


@pytest.mark.parametrize(
    "end_reason",
    ["tui_close", "tui_shutdown", "ws_disconnect"],
)
def test_failed_compute_host_interrupt_does_not_skip_parent_teardown(
    monkeypatch, end_reason
):
    supervisor = _FailingComputeHostSupervisor()
    torn_down = []
    session = _compute_session("dead-compute-turn")

    monkeypatch.setattr(server, "_get_compute_host_supervisor", lambda: supervisor)
    monkeypatch.setattr(server, "_clear_pending", lambda _sid: None)
    monkeypatch.setattr(
        server,
        "_teardown_session",
        lambda claimed, *, end_reason: torn_down.append((claimed, end_reason)),
    )

    assert server._teardown_popped_session(session, end_reason=end_reason) is True
    assert supervisor.interrupts == [("dead-compute-turn", None)]
    assert torn_down == [(session, end_reason)]


def test_failed_compute_host_interrupt_does_not_truncate_shutdown(monkeypatch):
    supervisor = _FailingComputeHostSupervisor()
    torn_down = []
    sessions = {
        "compute-a": _compute_session("compute-a"),
        "compute-b": _compute_session("compute-b"),
    }
    for session in sessions.values():
        session.pop("_sid")

    monkeypatch.setattr(server, "_get_compute_host_supervisor", lambda: supervisor)
    monkeypatch.setattr(server, "_clear_pending", lambda _sid: None)
    monkeypatch.setattr(server, "_flush_sessions_before_exit", lambda: None)
    monkeypatch.setattr(server, "_release_gateway_wake_owner", lambda: None)
    monkeypatch.setattr(
        server,
        "_teardown_session",
        lambda claimed, *, end_reason: torn_down.append(
            (claimed["_sid"], end_reason)
        ),
    )
    server._sessions.update(sessions)

    try:
        server._shutdown_sessions()
    finally:
        for session_id in sessions:
            server._sessions.pop(session_id, None)

    assert supervisor.interrupts == [("compute-a", None), ("compute-b", None)]
    assert torn_down == [
        ("compute-a", "tui_shutdown"),
        ("compute-b", "tui_shutdown"),
    ]
