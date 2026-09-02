"""Regression coverage for dispatcher's spawnability health predicate.

The stuck warning is only actionable when a ready task would pass the same
admission gates as ``dispatch_once``.  Guarded, malformed, unassigned, and
already-claimed rows are intentionally idle/non-admissible and must not keep
incrementing the zero-spawn window.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import time
from pathlib import Path
from typing import Any

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def all_profiles_real(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the profile admission gate deterministic without creating profiles."""
    import hermes_cli.profiles as profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda _name: True)


def _seed_active_pr(conn, task_id: str) -> None:
    kb.add_comment(
        conn,
        task_id,
        author="worker",
        body="Opened https://github.com/example/repo/pull/123.",
    )


def _health_context(**overrides: object) -> dict[str, Any]:
    """Pin admission inputs so tests exercise one deterministic dispatcher tick."""
    context: dict[str, object] = {
        "default_assignee": None,
        "max_spawn": None,
        "max_in_progress": None,
        "max_in_progress_per_profile": None,
        "board": None,
        "memory_pressure": "unknown",
        "review_dispatch": False,
    }
    context.update(overrides)
    return context


def _run_daemon_ticks(
    monkeypatch: pytest.MonkeyPatch,
    ticks: int,
    result: kb.DispatchResult,
) -> int:
    """Run the legacy --force daemon callback without sleeping."""
    import hermes_cli.kanban as kanban_cli

    def fake_run_daemon(**kwargs):
        on_tick = kwargs["on_tick"]
        for _ in range(ticks):
            on_tick(result)

    monkeypatch.setattr(kb, "run_daemon", fake_run_daemon)
    args = argparse.Namespace(
        force=True,
        pidfile=None,
        verbose=False,
        interval=5,
        max=None,
    )
    return kanban_cli._cmd_daemon(args)


def test_active_pr_ready_card_is_not_spawnable_or_stuck(
    kanban_home: Path,
    all_profiles_real: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="already has a PR", assignee="worker")
        _seed_active_pr(conn, task_id)
        assert kb.check_respawn_guard(conn, task_id) == "active_pr"
        assert kb.has_spawnable_ready(conn) is False

    assert _run_daemon_ticks(monkeypatch, 8, kb.DispatchResult()) == 0
    assert "dispatcher stuck" not in capsys.readouterr().err


def test_invalid_or_unassigned_ready_card_is_not_spawnable_or_stuck(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Malformed synthetic rows must fail closed in health and dispatch."""
    # ``profile_exists('..')`` used to return true when the profile-store
    # directory existed. Keep the real directory shape so this is a causal
    # regression rather than a test-only mock of the old false positive.
    (kanban_home / "profiles").mkdir()
    monkeypatch.setattr(kb, "_memory_pressure_level", lambda: "unknown")
    spawned: list[tuple[object, ...]] = []

    def fake_spawn(*args: object, **kwargs: object) -> int:
        spawned.append(args)
        return 123

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="synthetic malformed row")
        # Bypass the public normalizer to model a stale/synthetic DB row. ``..``
        # must never be accepted as a profile path, even if its parent exists.
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET assignee = ? WHERE id = ?",
                ("..", task_id),
            )
        assert (
            kb.has_spawnable_ready(conn, **_health_context(memory_pressure="unknown"))
            is False
        )
        result = kb.dispatch_once(
            conn,
            spawn_fn=fake_spawn,
            dry_run=False,
            max_spawn=None,
            max_in_progress=None,
            max_in_progress_per_profile=None,
        )
        assert task_id in result.skipped_nonspawnable

    assert spawned == []
    assert _run_daemon_ticks(monkeypatch, 8, kb.DispatchResult()) == 0
    assert "dispatcher stuck" not in capsys.readouterr().err


def test_unassigned_ready_card_uses_default_assignee_in_health_and_dispatch(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (kanban_home / "profiles" / "worker").mkdir(parents=True)
    monkeypatch.setattr(kb, "_memory_pressure_level", lambda: "unknown")

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="default-routed work")
        context = _health_context(default_assignee="worker")
        assert kb.has_spawnable_ready(conn, **context) is True

        result = kb.dispatch_once(
            conn,
            dry_run=True,
            default_assignee="worker",
            max_spawn=None,
            max_in_progress=None,
            max_in_progress_per_profile=None,
        )
        assert task_id in result.auto_assigned_default
        assert task_id in [task[0] for task in result.spawned]


def test_undone_parent_is_not_spawnable_in_health_or_dispatch(
    kanban_home: Path,
    all_profiles_real: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(kb, "_memory_pressure_level", lambda: "unknown")

    with kb.connect() as conn:
        parent_id = kb.create_task(conn, title="parent", assignee="worker")
        assert kb.claim_task(conn, parent_id) is not None
        child_id = kb.create_task(conn, title="child", assignee="worker")
        kb.link_tasks(conn, parent_id, child_id)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (child_id,))

        assert kb.has_spawnable_ready(conn, **_health_context()) is False
        result = kb.dispatch_once(
            conn,
            dry_run=True,
            max_spawn=None,
            max_in_progress=None,
            max_in_progress_per_profile=None,
        )
        assert child_id not in [task[0] for task in result.spawned]


def test_full_global_cap_is_not_spawnable_in_health_or_dispatch(
    kanban_home: Path,
    all_profiles_real: None,
) -> None:
    with kb.connect() as conn:
        running_id = kb.create_task(conn, title="running", assignee="worker")
        assert kb.claim_task(conn, running_id) is not None
        ready_id = kb.create_task(conn, title="at global cap", assignee="worker")

        context = _health_context(max_in_progress=1)
        assert kb.has_spawnable_ready(conn, **context) is False
        result = kb.dispatch_once(
            conn,
            dry_run=True,
            max_spawn=None,
            max_in_progress=1,
            max_in_progress_per_profile=None,
        )
        assert ready_id not in [task[0] for task in result.spawned]


def test_full_profile_cap_is_not_spawnable_in_health_or_dispatch(
    kanban_home: Path,
    all_profiles_real: None,
) -> None:
    with kb.connect() as conn:
        running_id = kb.create_task(conn, title="running", assignee="worker")
        assert kb.claim_task(conn, running_id) is not None
        ready_id = kb.create_task(conn, title="at profile cap", assignee="worker")

        context = _health_context(max_in_progress_per_profile=1)
        assert kb.has_spawnable_ready(conn, **context) is False
        result = kb.dispatch_once(
            conn,
            dry_run=True,
            max_spawn=None,
            max_in_progress=None,
            max_in_progress_per_profile=1,
        )
        assert ready_id in [task[0] for task in result.skipped_per_profile_capped]


def test_critical_memory_pressure_is_not_spawnable_in_health_or_dispatch(
    kanban_home: Path,
    all_profiles_real: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(kb, "_memory_pressure_level", lambda: "critical")

    with kb.connect() as conn:
        ready_id = kb.create_task(conn, title="memory constrained", assignee="worker")
        assert (
            kb.has_spawnable_ready(conn, **_health_context(memory_pressure="critical"))
            is False
        )
        result = kb.dispatch_once(
            conn,
            dry_run=True,
            max_spawn=None,
            max_in_progress=None,
            max_in_progress_per_profile=None,
        )
        assert ready_id not in [task[0] for task in result.spawned]
        assert result.memory_pressure == "critical"


def test_genuinely_dispatchable_ready_card_still_warns_after_threshold(
    kanban_home: Path,
    all_profiles_real: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with kb.connect() as conn:
        kb.create_task(conn, title="real work", assignee="worker")
        assert kb.has_spawnable_ready(conn) is True

    assert _run_daemon_ticks(monkeypatch, 5, kb.DispatchResult()) == 0
    assert "dispatcher stuck" not in capsys.readouterr().err

    # The sixth consecutive zero-spawn tick is the configured warning threshold.
    assert _run_daemon_ticks(monkeypatch, 6, kb.DispatchResult()) == 0
    warnings = [
        line
        for line in capsys.readouterr().err.splitlines()
        if "dispatcher stuck" in line
    ]
    assert len(warnings) == 1


def test_live_claim_and_empty_ready_queue_do_not_warn(
    kanban_home: Path,
    all_profiles_real: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="claimed work", assignee="worker")
        assert kb.claim_task(conn, task_id) is not None
        assert kb.has_spawnable_ready(conn) is False

    assert _run_daemon_ticks(monkeypatch, 8, kb.DispatchResult()) == 0
    assert "dispatcher stuck" not in capsys.readouterr().err

    # A fresh daemon callback with no ready row remains idle as before.
    assert _run_daemon_ticks(monkeypatch, 8, kb.DispatchResult()) == 0
    assert "dispatcher stuck" not in capsys.readouterr().err


def test_review_lane_keeps_active_pr_bypass(
    kanban_home: Path,
    all_profiles_real: None,
) -> None:
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="review existing PR", assignee="reviewer")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (task_id,))
        _seed_active_pr(conn, task_id)

        assert kb.check_respawn_guard(conn, task_id, lane="review") is None
        assert kb.has_spawnable_review(conn) is True


def test_guard_probe_error_keeps_warning_conservative(
    kanban_home: Path,
    all_profiles_real: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with kb.connect() as conn:
        kb.create_task(conn, title="guard unavailable", assignee="worker")
        monkeypatch.setattr(
            kb,
            "check_respawn_guard",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("probe failed")
            ),
        )
        # A failed guard probe must not silently hide a real stuck condition.
        assert kb.has_spawnable_ready(conn) is True


def _gateway_ticks(
    monkeypatch: pytest.MonkeyPatch,
    runner: object,
    min_ticks: int,
) -> None:
    """Run the embedded watcher inline for a bounded number of ticks."""
    import hermes_cli.kanban_db as _kb

    ticks = {"n": 0}

    async def fake_to_thread(fn, *args, **kwargs):
        result = fn(*args, **kwargs)
        target = (
            args[0] if getattr(fn, "__name__", "") == "_run_in_fresh_context" else fn
        )
        if getattr(target, "__name__", "") == "reap_worker_zombies":
            ticks["n"] += 1
            if ticks["n"] >= min_ticks:
                runner._running = False
        return result

    async def fake_sleep(_delay):
        return None

    monkeypatch.setattr("asyncio.to_thread", fake_to_thread)
    monkeypatch.setattr("asyncio.sleep", fake_sleep)
    monkeypatch.setattr(
        _kb,
        "list_boards",
        lambda include_archived=False: [{"slug": _kb.DEFAULT_BOARD}],
    )
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "kanban": {
                "dispatch_in_gateway": True,
                "dispatch_interval_seconds": 1,
                "auto_decompose": False,
            }
        },
    )

    asyncio.run(asyncio.wait_for(runner._kanban_dispatcher_watcher(), timeout=10.0))


def test_gateway_watcher_uses_same_guarded_ready_predicate(
    kanban_home: Path,
    all_profiles_real: None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from gateway.run import GatewayRunner

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="gateway guarded", assignee="worker")
        _seed_active_pr(conn, task_id)

    runner = object.__new__(GatewayRunner)
    runner._running = True
    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        _gateway_ticks(monkeypatch, runner, 8)

    assert [
        record
        for record in caplog.records
        if "kanban dispatcher stuck" in record.getMessage()
    ] == []
