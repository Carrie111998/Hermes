"""RED tests for gateway-wide kanban worker caps across real board DBs."""

from __future__ import annotations

import os
import multiprocessing
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def multi_board_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    kb._INITIALIZED_PATHS.clear()
    kb._DISPATCH_CAPACITY_RESERVATIONS.clear()
    kb._DISPATCH_CAPACITY_SNAPSHOTS.clear()

    for profile in ("alpha", "beta"):
        (home / "profiles" / profile).mkdir(parents=True)
    for board in ("board-a", "board-b"):
        kb.create_board(board)
    return ("board-a", "board-b")


def _process_dispatch_one_board(home, board, start, results):
    os.environ["HERMES_HOME"] = home
    os.environ["HERMES_KANBAN_HOME"] = home
    os.environ.pop("HERMES_KANBAN_BOARD", None)
    kb._INITIALIZED_PATHS.clear()
    capacity = kb.DispatchSweepCapacity((board,), max_in_progress=1)
    start.wait(timeout=5)

    def slow_spawn(*_args, **_kwargs):
        time.sleep(0.5)
        return os.getpid()

    with kb.connect_closing(board=board) as conn:
        result = kb.dispatch_once(
            conn,
            board=board,
            spawn_fn=slow_spawn,
            reconcile_orphans=False,
            sweep_capacity=capacity,
        )
    results.put(len(result.spawned))


def _create_ready(board: str, *, assignee: str, count: int) -> None:
    with kb.connect_closing(board=board) as conn:
        for index in range(count):
            kb.create_task(
                conn,
                title=f"{board}-{assignee}-{index}",
                assignee=assignee,
                board=board,
            )


def _create_review(board: str, *, assignee: str) -> None:
    with kb.connect_closing(board=board) as conn:
        task_id = kb.create_task(
            conn,
            title=f"{board}-{assignee}-review",
            assignee=assignee,
            board=board,
        )
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None
        assert kb.request_review(
            conn,
            task_id,
            summary="ready for review",
            expected_run_id=claimed.current_run_id,
        )


def _dispatch_board(board: str, **kwargs):
    with kb.connect_closing(board=board) as conn:
        return kb.dispatch_once(
            conn,
            board=board,
            spawn_fn=lambda *_args, **_kwargs: os.getpid(),
            reconcile_orphans=False,
            **kwargs,
        )


def _gateway_style_sweep(boards: tuple[str, ...], **kwargs):
    """Use the same independent-connect, per-board loop as the gateway."""
    capacity = kb.DispatchSweepCapacity(
        boards,
        max_in_progress=kwargs.get("max_in_progress"),
        max_in_progress_per_profile=kwargs.get("max_in_progress_per_profile"),
    )
    return [
        _dispatch_board(board, sweep_capacity=capacity, **kwargs)
        for board in boards
    ]


def _running(boards: tuple[str, ...], *, assignee: str | None = None) -> int:
    total = 0
    for board in boards:
        with kb.connect_closing(board=board) as conn:
            if assignee is None:
                row = conn.execute(
                    "SELECT COUNT(*) FROM tasks WHERE status = 'running'"
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) FROM tasks "
                    "WHERE status = 'running' AND assignee = ?",
                    (assignee,),
                ).fetchone()
            total += int(row[0])
    return total


def test_single_board_global_cap_still_limits_live_workers(multi_board_home):
    board = multi_board_home[0]
    _create_ready(board, assignee="alpha", count=3)

    result = _dispatch_board(board, max_in_progress=2)

    assert len(result.spawned) == 2
    assert _running((board,)) == 2


def test_gateway_sweep_applies_global_cap_across_all_board_dbs(multi_board_home):
    boards = multi_board_home
    for board in boards:
        _create_ready(board, assignee="alpha", count=2)

    results = _gateway_style_sweep(boards, max_in_progress=2)

    assert sum(len(result.spawned) for result in results) == 2
    assert _running(boards) == 2


def test_gateway_sweep_applies_profile_cap_across_all_board_dbs(multi_board_home):
    boards = multi_board_home
    for board in boards:
        _create_ready(board, assignee="alpha", count=1)
        _create_ready(board, assignee="beta", count=1)

    results = _gateway_style_sweep(
        boards,
        max_in_progress=4,
        max_in_progress_per_profile=1,
    )

    assert _running(boards, assignee="alpha") == 1
    assert _running(boards, assignee="beta") == 1
    assert sum(len(result.spawned) for result in results) == 2


def test_concurrent_board_claims_atomically_share_global_reservation(
    multi_board_home,
):
    boards = multi_board_home
    for board in boards:
        _create_ready(board, assignee="alpha", count=1)

    both_claimed = threading.Barrier(len(boards))

    def spawn_after_competing_claims(*_args, **_kwargs):
        try:
            both_claimed.wait(timeout=1)
        except threading.BrokenBarrierError:
            pass
        return os.getpid()

    def dispatch(board: str):
        with kb.connect_closing(board=board) as conn:
            return kb.dispatch_once(
                conn,
                board=board,
                spawn_fn=spawn_after_competing_claims,
                max_in_progress=1,
                reconcile_orphans=False,
                sweep_capacity=capacity,
            )

    capacity = kb.DispatchSweepCapacity(boards, max_in_progress=1)
    with ThreadPoolExecutor(max_workers=len(boards)) as pool:
        results = list(pool.map(dispatch, boards))

    assert sum(len(result.spawned) for result in results) == 1
    assert _running(boards) == 1


def test_overlapping_capacity_instances_coordinate_the_last_global_slot(
    multi_board_home,
):
    boards = multi_board_home
    for board in boards:
        _create_ready(board, assignee="alpha", count=1)

    capacities = [
        kb.DispatchSweepCapacity(boards, max_in_progress=1)
        for _ in boards
    ]
    both_spawns = threading.Barrier(len(boards))

    def dispatch(item):
        board, capacity = item

        def spawn(*_args, **_kwargs):
            try:
                both_spawns.wait(timeout=1)
            except threading.BrokenBarrierError:
                pass
            return os.getpid()

        with kb.connect_closing(board=board) as conn:
            return kb.dispatch_once(
                conn,
                board=board,
                spawn_fn=spawn,
                reconcile_orphans=False,
                sweep_capacity=capacity,
            )

    with ThreadPoolExecutor(max_workers=len(boards)) as pool:
        results = list(pool.map(dispatch, zip(boards, capacities)))

    assert sum(len(result.spawned) for result in results) == 1
    assert _running(boards) == 1


def test_separate_processes_dispatching_different_boards_share_global_cap(
    multi_board_home,
):
    boards = multi_board_home
    for board in boards:
        _create_ready(board, assignee="alpha", count=1)

    ctx = multiprocessing.get_context("spawn")
    start = ctx.Event()
    results = ctx.Queue()
    processes = [
        ctx.Process(
            target=_process_dispatch_one_board,
            args=(str(kb.kanban_home()), board, start, results),
        )
        for board in boards
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0

    assert sum(results.get(timeout=2) for _ in processes) == 1
    assert _running(boards) == 1


def test_unreadable_board_uses_last_count_without_blocking_healthy_board(
    multi_board_home,
    monkeypatch,
):
    healthy, unreadable = multi_board_home
    _create_ready(healthy, assignee="alpha", count=2)
    _create_ready(unreadable, assignee="beta", count=1)
    with kb.connect_closing(board=unreadable) as conn:
        task_id = conn.execute("SELECT id FROM tasks").fetchone()[0]
        assert kb.claim_task(conn, task_id) is not None

    capacity = kb.DispatchSweepCapacity(multi_board_home, max_in_progress=2)
    reservation, blocked, _ = capacity.reserve(
        board=healthy,
        task_id="snapshot-probe",
        profile="alpha",
    )
    assert blocked is None
    assert reservation is not None
    reservation.release()

    original_connect_closing = kb.connect_closing

    def fail_only_unreadable(*args, board=None, **kwargs):
        if board == unreadable:
            raise sqlite3.DatabaseError("database disk image is malformed")
        return original_connect_closing(*args, board=board, **kwargs)

    monkeypatch.setattr(kb, "connect_closing", fail_only_unreadable)

    result = _dispatch_board(
        healthy,
        max_in_progress=2,
        sweep_capacity=capacity,
    )

    assert len(result.spawned) == 1
    assert _running((healthy,)) == 1


def test_sweep_capacity_dry_run_reports_only_globally_bounded_profile_slots(
    multi_board_home,
):
    boards = multi_board_home
    for board in boards:
        _create_ready(board, assignee="alpha", count=1)
        _create_ready(board, assignee="beta", count=1)

    results = _gateway_style_sweep(
        boards,
        dry_run=True,
        max_in_progress=2,
        max_in_progress_per_profile=1,
    )

    would_spawn = [spawn for result in results for spawn in result.spawned]
    assert len(would_spawn) == 2
    assert {assignee for _, assignee, _ in would_spawn} == {"alpha", "beta"}
    assert _running(boards) == 0
    for board in boards:
        with kb.connect_closing(board=board) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE status = 'ready'"
            ).fetchone()[0] == 2


def test_gateway_sweep_shares_capacity_between_ready_and_review_lanes(
    multi_board_home,
):
    boards = multi_board_home
    _create_ready(boards[0], assignee="alpha", count=1)
    _create_review(boards[1], assignee="beta")

    results = _gateway_style_sweep(boards, max_in_progress=1)

    assert sum(len(result.spawned) for result in results) == 1
    assert _running(boards) == 1


@pytest.mark.parametrize("failure_stage", ["lost_claim", "workspace", "spawn"])
def test_failed_claim_or_launch_releases_shared_capacity_for_later_board(
    multi_board_home,
    monkeypatch,
    failure_stage,
):
    boards = multi_board_home
    for board in boards:
        _create_ready(board, assignee="alpha", count=1)
    capacity = kb.DispatchSweepCapacity(boards, max_in_progress=1)

    original_claim = kb.claim_task
    original_workspace = kb.resolve_workspace
    if failure_stage == "lost_claim":
        monkeypatch.setattr(
            kb,
            "claim_task",
            lambda conn, task_id, **kwargs: (
                None
                if conn.execute(
                    "SELECT title FROM tasks WHERE id = ?", (task_id,)
                ).fetchone()[0].startswith(boards[0])
                else original_claim(conn, task_id, **kwargs)
            ),
        )
    elif failure_stage == "workspace":
        def fail_first_workspace(task, *, board=None):
            if board == boards[0]:
                raise RuntimeError("workspace failed")
            return original_workspace(task, board=board)

        monkeypatch.setattr(kb, "resolve_workspace", fail_first_workspace)

    def spawn(*_args, **_kwargs):
        if failure_stage == "spawn":
            raise RuntimeError("spawn failed")
        return os.getpid()

    with kb.connect_closing(board=boards[0]) as conn:
        first = kb.dispatch_once(
            conn,
            board=boards[0],
            spawn_fn=spawn,
            failure_limit=2,
            reconcile_orphans=False,
            sweep_capacity=capacity,
        )
    with kb.connect_closing(board=boards[1]) as conn:
        second = kb.dispatch_once(
            conn,
            board=boards[1],
            spawn_fn=lambda *_args, **_kwargs: os.getpid(),
            reconcile_orphans=False,
            sweep_capacity=capacity,
        )

    assert first.spawned == []
    assert len(second.spawned) == 1
    assert _running(boards) == 1


@pytest.mark.parametrize("lane", ["ready", "review"])
@pytest.mark.parametrize(
    "failure_stage",
    ["claim_before", "claim_after", "workspace_path", "branch_name", "scratch_tip"],
)
def test_dispatch_exception_always_releases_capacity_reservation(
    multi_board_home,
    monkeypatch,
    lane,
    failure_stage,
):
    first_board, second_board = multi_board_home
    if lane == "review":
        _create_review(first_board, assignee="alpha")
    else:
        _create_ready(first_board, assignee="alpha", count=1)
    _create_ready(second_board, assignee="beta", count=1)

    with kb.connect_closing(board=first_board) as conn:
        first_id = conn.execute("SELECT id FROM tasks").fetchone()[0]
        if failure_stage == "branch_name":
            conn.execute(
                "UPDATE tasks SET workspace_kind = 'worktree' WHERE id = ?",
                (first_id,),
            )

    claim_name = "claim_review_task" if lane == "review" else "claim_task"
    original_claim = getattr(kb, claim_name)
    original_set_workspace_path = kb.set_workspace_path
    original_set_branch_name = kb.set_branch_name
    original_resolve_worktree = kb._resolve_worktree_workspace
    original_scratch_tip = kb._maybe_emit_scratch_tip

    def fail_claim(conn, task_id, **kwargs):
        if failure_stage == "claim_after":
            original_claim(conn, task_id, **kwargs)
        raise RuntimeError(failure_stage)

    if failure_stage.startswith("claim_"):
        monkeypatch.setattr(kb, claim_name, fail_claim)
    elif failure_stage == "workspace_path":
        monkeypatch.setattr(
            kb,
            "set_workspace_path",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError(failure_stage)
            ),
        )
    elif failure_stage == "branch_name":
        monkeypatch.setattr(
            kb,
            "_resolve_worktree_workspace",
            lambda task, **_kwargs: (kb.workspaces_root(first_board) / task.id, "wt/test"),
        )
        monkeypatch.setattr(
            kb,
            "set_branch_name",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError(failure_stage)
            ),
        )
    else:
        monkeypatch.setattr(
            kb,
            "_maybe_emit_scratch_tip",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError(failure_stage)
            ),
        )

    capacity = kb.DispatchSweepCapacity(multi_board_home, max_in_progress=1)
    with kb.connect_closing(board=first_board) as conn:
        with pytest.raises(RuntimeError, match=failure_stage):
            kb.dispatch_once(
                conn,
                board=first_board,
                spawn_fn=lambda *_args, **_kwargs: os.getpid(),
                reconcile_orphans=False,
                sweep_capacity=capacity,
            )

    assert not kb._DISPATCH_CAPACITY_RESERVATIONS
    with kb.connect_closing(board=first_board) as conn:
        first = kb.get_task(conn, first_id)
        assert first is not None
        assert first.consecutive_failures == 0
        if first.status == "running":
            assert kb.reclaim_task(conn, first_id, reason="test cleanup")

    monkeypatch.setattr(kb, claim_name, original_claim)
    monkeypatch.setattr(kb, "set_workspace_path", original_set_workspace_path)
    monkeypatch.setattr(kb, "set_branch_name", original_set_branch_name)
    monkeypatch.setattr(kb, "_resolve_worktree_workspace", original_resolve_worktree)
    monkeypatch.setattr(kb, "_maybe_emit_scratch_tip", original_scratch_tip)
    with kb.connect_closing(board=second_board) as conn:
        second = kb.dispatch_once(
            conn,
            board=second_board,
            spawn_fn=lambda *_args, **_kwargs: os.getpid(),
            reconcile_orphans=False,
            sweep_capacity=capacity,
        )

    assert len(second.spawned) == 1
