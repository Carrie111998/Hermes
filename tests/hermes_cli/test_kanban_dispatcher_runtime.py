from __future__ import annotations

import sqlite3

import pytest


def _lease(tmp_path, monkeypatch):
    from hermes_cli.dispatcher_authority import acquire_machine_dispatcher

    monkeypatch.setenv("HERMES_STATE_ROOT", str(tmp_path))
    acquired = acquire_machine_dispatcher("runtime-test")
    assert acquired.lease is not None
    return acquired.lease


def test_invalid_runtime_config_fails_before_board_discovery(monkeypatch, tmp_path):
    from hermes_cli import kanban_dispatcher as runtime

    lease = _lease(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "hermes_cli.kanban_db.list_boards",
        lambda **_kw: pytest.fail("board discovery reached"),
    )
    try:
        with pytest.raises(RuntimeError, match="max_spawn"):
            runtime.run_dispatcher_tick(lease, config={"max_spawn": "many"})
    finally:
        lease.release()


def test_one_board_failure_does_not_stop_sibling(monkeypatch, tmp_path):
    from hermes_cli import kanban_dispatcher as runtime

    runtime.reset_runtime_health_for_tests()
    lease = _lease(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "hermes_cli.kanban_db.list_boards",
        lambda **_kw: [{"slug": "bad"}, {"slug": "good"}],
    )

    class Conn:
        def __init__(self, slug):
            self.slug = slug

        def close(self):
            pass

    monkeypatch.setattr("hermes_cli.kanban_db.connect", lambda board=None: Conn(board))
    calls = []

    def dispatch(_lease, conn, **_kw):
        calls.append(conn.slug)
        if conn.slug == "bad":
            raise RuntimeError("broken")
        return "ok"

    monkeypatch.setattr("hermes_cli.kanban_db.dispatch_once_authorized", dispatch)
    try:
        result = runtime.run_dispatcher_tick(lease, config={})
    finally:
        lease.release()
    assert calls == ["bad", "good"]
    assert result[-1] == ("good", "ok")


def test_semantic_database_corruption_quarantines_immediately(monkeypatch, tmp_path):
    from hermes_cli import kanban_dispatcher as runtime

    runtime.reset_runtime_health_for_tests()
    lease = _lease(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "hermes_cli.kanban_db.list_boards", lambda **_kw: [{"slug": "broken"}]
    )

    class Conn:
        def close(self):
            pass

    monkeypatch.setattr("hermes_cli.kanban_db.connect", lambda **_kw: Conn())
    monkeypatch.setattr(
        "hermes_cli.kanban_db.dispatch_once_authorized",
        lambda *_a, **_kw: (_ for _ in ()).throw(sqlite3.DatabaseError("malformed")),
    )
    try:
        runtime.run_dispatcher_tick(lease, config={})
        second = runtime.run_dispatcher_tick(lease, config={})
    finally:
        lease.release()
    assert second[0][1]["status"] == "quarantined"
