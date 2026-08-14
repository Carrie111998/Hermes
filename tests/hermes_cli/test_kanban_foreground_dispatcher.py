from __future__ import annotations

import argparse

import pytest


def test_foreground_denial_happens_before_runtime_construction(monkeypatch):
    from hermes_cli import kanban as cli
    from hermes_cli.dispatcher_authority import AcquireResult, AcquireState

    monkeypatch.setattr(
        "hermes_cli.dispatcher_authority.acquire_machine_dispatcher",
        lambda _context: AcquireResult(AcquireState.CONTENDED, owner_hint="pid:7"),
    )
    monkeypatch.setattr(
        "hermes_cli.kanban_dispatcher.run_foreground_dispatcher",
        lambda **_kw: pytest.fail("runtime constructed"),
    )
    assert cli._cmd_dispatcher(argparse.Namespace(interval=0.01, once=True)) != 0


def test_runtime_enumerates_all_boards_and_passes_live_lease(monkeypatch, tmp_path):
    from hermes_cli.dispatcher_authority import acquire_machine_dispatcher
    from hermes_cli.kanban_dispatcher import run_dispatcher_tick

    monkeypatch.setenv("HERMES_STATE_ROOT", str(tmp_path))
    acquired = acquire_machine_dispatcher("test")
    calls = []
    monkeypatch.setattr(
        "hermes_cli.kanban_db.list_boards",
        lambda include_archived=False: [{"slug": "a"}, {"slug": "b"}],
    )

    class Conn:
        def close(self):
            pass

    monkeypatch.setattr("hermes_cli.kanban_db.connect", lambda board=None: Conn())
    monkeypatch.setattr(
        "hermes_cli.kanban_db.dispatch_once_authorized",
        lambda lease, conn, **kw: calls.append((lease, kw["board"])),
    )
    try:
        run_dispatcher_tick(acquired.lease, config={})
    finally:
        acquired.lease.release()
    assert [board for _lease, board in calls] == ["a", "b"]
    assert all(lease is acquired.lease for lease, _board in calls)


def test_guarded_dispatch_rejects_missing_lease_before_tick_lock(monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli.dispatcher_authority import DispatcherAuthorityError

    monkeypatch.setattr(kb, "_dispatch_tick_lock", lambda *_: pytest.fail("tick lock reached"))
    with pytest.raises(DispatcherAuthorityError):
        kb.dispatch_once_authorized(None, object())
