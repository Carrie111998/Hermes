"""Production-path coverage for profile-scoped Kanban fallback routing."""

from __future__ import annotations

import asyncio
import json

import yaml


def test_profile_dispatchers_scope_fallback_but_keep_explicit_dispatch(
    tmp_path,
    monkeypatch,
):
    """The singleton winner's profile fallback cannot bleed across boards.

    Both profile gateway configs coexist in the same installation.  Each is
    exercised as the singleton-lock winner because the production contract
    intentionally permits only one dispatcher loop at a time.
    """
    root = tmp_path / "hermes"
    profile_a_home = root / "profiles" / "profile-a"
    profile_b_home = root / "profiles" / "profile-b"
    profile_a_home.mkdir(parents=True)
    profile_b_home.mkdir(parents=True)

    def _write_profile_config(home, assignee, boards):
        (home / "config.yaml").write_text(
            yaml.safe_dump({
                "kanban": {
                    "dispatch_in_gateway": True,
                    "dispatch_interval_seconds": 1,
                    "auto_decompose": False,
                    "default_assignee": assignee,
                    "default_assignee_boards": boards,
                }
            }),
            encoding="utf-8",
        )

    _write_profile_config(profile_a_home, "profile-a", ["board-a"])
    _write_profile_config(profile_b_home, "profile-b", ["board-b"])

    monkeypatch.setenv("HERMES_HOME", str(root))
    from gateway import kanban_watchers
    from hermes_cli import kanban_db as kb
    from hermes_cli.profiles import get_active_profile_name

    kb.create_board("board-a", name="Board A")
    kb.create_board("board-b", name="Board B")
    with kb.connect_closing(board="board-a") as conn:
        board_a_unassigned = kb.create_task(
            conn,
            title="Board A unassigned",
            assignee=None,
        )
        board_a_explicit = kb.create_task(
            conn,
            title="Board A explicitly assigned to B",
            assignee="profile-b",
        )
    with kb.connect_closing(board="board-b") as conn:
        board_b_unassigned = kb.create_task(
            conn,
            title="Board B unassigned",
            assignee=None,
        )

    spawned: list[tuple[str, str, str]] = []

    def _fake_spawn(task, _workspace, *, board=None):
        spawned.append((board or kb.DEFAULT_BOARD, task.id, task.assignee or ""))
        return None

    monkeypatch.setattr(kb, "_default_spawn", _fake_spawn)
    monkeypatch.setattr(kb, "reap_worker_zombies", lambda: [])

    class _Runner(kanban_watchers.GatewayKanbanWatchersMixin):
        def __init__(self):
            self._running = True

        @staticmethod
        def _active_profile_name():
            return get_active_profile_name()

    async def _no_sleep(_delay):
        return None

    monkeypatch.setattr(kanban_watchers.asyncio, "sleep", _no_sleep)

    def _run_one_tick(profile_home):
        monkeypatch.setenv("HERMES_HOME", str(profile_home))
        runner = _Runner()

        async def _inline_to_thread(fn, *args, **kwargs):
            result = fn(*args, **kwargs)
            if getattr(fn, "__name__", "") == "_tick_once":
                runner._running = False
            return result

        monkeypatch.setattr(
            kanban_watchers.asyncio,
            "to_thread",
            _inline_to_thread,
        )
        asyncio.run(runner._kanban_dispatcher_watcher())

    # Profile B wins the singleton lock.  It may dispatch explicit work on
    # every board, but its fallback is authorized only on Board B.
    _run_one_tick(profile_b_home)

    with kb.connect_closing(board="board-a") as conn:
        denied = kb.get_task(conn, board_a_unassigned)
        explicit = kb.get_task(conn, board_a_explicit)
    with kb.connect_closing(board="board-b") as conn:
        allowed_b = kb.get_task(conn, board_b_unassigned)
        event_b = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'assigned'",
            (board_b_unassigned,),
        ).fetchone()

    assert denied is not None and denied.assignee is None and denied.status == "ready"
    assert explicit is not None and explicit.assignee == "profile-b"
    assert explicit.status == "running"
    assert allowed_b is not None and allowed_b.assignee == "profile-b"
    assert allowed_b.status == "running"
    assert ("board-a", board_a_explicit, "profile-b") in spawned
    assert ("board-b", board_b_unassigned, "profile-b") in spawned
    payload_b = json.loads(event_b["payload"])
    assert payload_b["dispatcher_profile"] == "profile-b"
    assert payload_b["routing_rule"] == "kanban.default_assignee_boards:board-b"

    # When A is the singleton winner, the same production path can claim the
    # card only because A's config explicitly authorizes Board A.
    _run_one_tick(profile_a_home)
    with kb.connect_closing(board="board-a") as conn:
        allowed_a = kb.get_task(conn, board_a_unassigned)
        event_a = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'assigned'",
            (board_a_unassigned,),
        ).fetchone()
    assert allowed_a is not None and allowed_a.assignee == "profile-a"
    assert allowed_a.status == "running"
    payload_a = json.loads(event_a["payload"])
    assert payload_a["dispatcher_profile"] == "profile-a"
    assert payload_a["routing_rule"] == "kanban.default_assignee_boards:board-a"

    # Wildcard is an explicit migration escape hatch for operators who
    # deliberately want the singleton winner's fallback on every board.
    with kb.connect_closing(board="board-a") as conn:
        wildcard_task = kb.create_task(
            conn,
            title="Legacy all-board fallback",
            assignee=None,
        )
    _write_profile_config(profile_b_home, "profile-b", ["*"])
    _run_one_tick(profile_b_home)
    with kb.connect_closing(board="board-a") as conn:
        wildcard = kb.get_task(conn, wildcard_task)
        wildcard_event = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'assigned'",
            (wildcard_task,),
        ).fetchone()
    assert wildcard is not None and wildcard.assignee == "profile-b"
    wildcard_payload = json.loads(wildcard_event["payload"])
    assert wildcard_payload["dispatcher_profile"] == "profile-b"
    assert wildcard_payload["routing_rule"] == "kanban.default_assignee_boards:*"
