"""Safety contracts for automatic Kanban worker dispatch."""

from __future__ import annotations

import asyncio
from pathlib import Path


def test_gateway_dispatcher_requires_explicit_opt_in(monkeypatch):
    from gateway.run import GatewayRunner
    import hermes_cli.config as config

    runner = object.__new__(GatewayRunner)
    runner._running = True
    monkeypatch.setattr(config, "load_config", lambda: {"kanban": {}})

    asyncio.run(runner._kanban_dispatcher_watcher())
    assert not hasattr(runner, "_kanban_dispatcher_lock_handle")


def test_gateway_dispatcher_positive_env_override_enables(monkeypatch):
    from gateway.run import GatewayRunner
    import gateway.kanban_watchers as watchers
    import hermes_cli.config as config

    runner = object.__new__(GatewayRunner)
    runner._running = False
    acquired = []
    monkeypatch.setenv("HERMES_KANBAN_DISPATCH_IN_GATEWAY", "1")
    monkeypatch.setattr(config, "load_config", lambda: {"kanban": {}})
    monkeypatch.setattr(
        watchers,
        "_acquire_singleton_lock",
        lambda path: (acquired.append(path), "held"),
    )

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(watchers.asyncio, "sleep", no_sleep)
    asyncio.run(runner._kanban_dispatcher_watcher())
    assert acquired, "positive runtime override must reach dispatcher startup"


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


def test_host_worker_cap_counts_siblings_from_pinned_worker_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from hermes_cli import kanban_db as kb
    from hermes_cli import profiles

    kb._INITIALIZED_PATHS.clear()
    kb.create_board("second")
    monkeypatch.setattr(profiles, "profile_exists", lambda _name: True)

    with kb.connect_closing(board="second") as second_conn:
        for index in range(2):
            task_id = kb.create_task(
                second_conn, title=f"running-{index}", assignee="worker"
            )
            second_conn.execute(
                "UPDATE tasks SET status='running', worker_pid=? WHERE id=?",
                (920_000 + index, task_id),
            )
        second_conn.commit()

    default_path = kb.kanban_db_path(board="default")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(default_path))
    with kb.connect_closing(board="default") as default_conn:
        for index in range(2):
            kb.create_task(default_conn, title=f"ready-{index}", assignee="worker")
        result = kb.dispatch_once(
            default_conn,
            board="default",
            spawn_fn=lambda *_args, **_kwargs: 930_000,
            max_concurrent_workers=3,
        )

    assert len(result.spawned) == 1


def test_host_worker_cap_fails_closed_when_capacity_lock_cannot_open(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from hermes_cli import kanban_db as kb
    from hermes_cli import profiles

    kb._INITIALIZED_PATHS.clear()
    monkeypatch.setattr(profiles, "profile_exists", lambda _name: True)
    with kb.connect_closing() as conn:
        kb.create_task(conn, title="ready", assignee="worker")
        original_open = Path.open

        def guarded_open(path, *args, **kwargs):
            if path.name == ".worker-capacity.dispatch.lock":
                raise PermissionError("capacity lock denied")
            return original_open(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", guarded_open)
        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda *_args, **_kwargs: 940_000,
            max_concurrent_workers=3,
        )

    assert result.skipped_locked is True
    assert result.spawned == []


def test_dashboard_dispatch_passes_host_worker_cap(monkeypatch):
    from hermes_cli import kanban_db as kb
    import hermes_cli.config as config
    from plugins.kanban.dashboard import plugin_api

    captured = {}

    class FakeConn:
        def close(self):
            pass

    monkeypatch.setattr(config, "load_config", lambda: {"kanban": {"max_concurrent_workers": 2}})
    monkeypatch.setattr(plugin_api, "_conn", lambda **_kwargs: FakeConn())
    monkeypatch.setattr(
        plugin_api.kanban_db,
        "dispatch_once",
        lambda _conn, **kwargs: captured.update(kwargs) or kb.DispatchResult(),
    )

    plugin_api.dispatch(dry_run=False, max_n=8, board="default")
    assert captured["max_concurrent_workers"] == 2
