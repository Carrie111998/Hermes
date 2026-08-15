"""Safety contracts for automatic Kanban worker dispatch."""

from __future__ import annotations

import asyncio


def test_gateway_dispatcher_requires_explicit_opt_in(monkeypatch):
    from gateway.run import GatewayRunner
    import hermes_cli.config as config

    runner = object.__new__(GatewayRunner)
    runner._running = True
    monkeypatch.setattr(config, "load_config", lambda: {"kanban": {}})

    asyncio.run(runner._kanban_dispatcher_watcher())
    assert not hasattr(runner, "_kanban_dispatcher_lock_handle")


def test_host_worker_cap_is_shared_across_boards(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from hermes_cli import kanban_db as kb
    from hermes_cli import profiles

    kb._INITIALIZED_PATHS.clear()
    kb.create_board("second")
    monkeypatch.setattr(profiles, "profile_exists", lambda _name: True)
    pids = iter(range(910_000, 910_010))

    def spawn(*_args, **_kwargs):
        return next(pids)

    with kb.connect_closing(board="default") as default_conn:
        for index in range(2):
            kb.create_task(default_conn, title=f"default-{index}", assignee="worker")
        first = kb.dispatch_once(
            default_conn,
            board="default",
            spawn_fn=spawn,
            max_concurrent_workers=3,
        )

    with kb.connect_closing(board="second") as second_conn:
        for index in range(2):
            kb.create_task(second_conn, title=f"second-{index}", assignee="worker")
        second = kb.dispatch_once(
            second_conn,
            board="second",
            spawn_fn=spawn,
            max_concurrent_workers=3,
        )
        second_running = second_conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE status = 'running'"
        ).fetchone()[0]
        second_ready = second_conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE status = 'ready'"
        ).fetchone()[0]

    assert len(first.spawned) == 2
    assert len(second.spawned) == 1
    assert second_running == 1
    assert second_ready == 1