"""Atomic board-wide parent-worker capacity enforcement.

The cap belongs at the claim boundary, not only in dispatcher budgeting: gateway,
Desktop/API, CLI/manual, and review owners can race through different entry
points while sharing one board.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _running_ids(conn):
    return {
        row["id"]
        for row in conn.execute("SELECT id FROM tasks WHERE status = 'running'")
    }


def test_simultaneous_claim_owners_share_the_last_parent_slot(kanban_home):
    """Two independent owners racing from 2/3 capacity claim exactly one task."""
    with kb.connect() as conn:
        existing = [
            kb.create_task(conn, title=f"running-{i}", assignee="default")
            for i in range(2)
        ]
        for task_id in existing:
            assert kb.claim_task(conn, task_id) is not None
        candidates = [
            kb.create_task(conn, title=f"candidate-{i}", assignee="default")
            for i in range(2)
        ]

    barrier = threading.Barrier(2)
    outcomes: list[tuple[str, bool]] = []
    errors: list[BaseException] = []

    def attempt(task_id: str, owner: str) -> None:
        try:
            with kb.connect() as conn:
                barrier.wait(timeout=5)
                claimed = kb.claim_task(
                    conn,
                    task_id,
                    claimer=owner,
                    max_in_progress=3,
                )
                outcomes.append((task_id, claimed is not None))
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [
        threading.Thread(target=attempt, args=(task_id, f"owner-{i}:1"))
        for i, task_id in enumerate(candidates)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    assert all(not thread.is_alive() for thread in threads)
    assert sorted(success for _, success in outcomes) == [False, True]
    with kb.connect() as conn:
        assert len(_running_ids(conn)) == 3


def test_manual_claim_uses_the_configured_parent_cap(kanban_home, monkeypatch):
    """The direct/manual claim path cannot bypass configured max_in_progress."""
    monkeypatch.setattr(kb, "configured_max_in_progress", lambda: 3)
    with kb.connect() as conn:
        running = [
            kb.create_task(conn, title=f"running-{i}", assignee="default")
            for i in range(3)
        ]
        for task_id in running:
            assert kb.claim_task(conn, task_id, max_in_progress=None) is not None
        candidate = kb.create_task(conn, title="manual", assignee="default")

        assert kb.claim_task(conn, candidate, claimer="manual:1") is None
        assert kb.get_task(conn, candidate).status == "ready"
        event = conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (candidate,),
        ).fetchone()

    assert event["kind"] == "claim_rejected"
    assert '"reason": "max_in_progress"' in event["payload"]
    assert '"observed": 3' in event["payload"]
    assert '"cap": 3' in event["payload"]


def test_manual_claim_cli_reports_capacity_diagnostic(
    kanban_home, monkeypatch,
):
    from hermes_cli import kanban as kanban_cli

    monkeypatch.setattr(kb, "configured_max_in_progress", lambda: 3)
    with kb.connect() as conn:
        running = [
            kb.create_task(conn, title=f"running-{i}", assignee="default")
            for i in range(3)
        ]
        for task_id in running:
            assert kb.claim_task(conn, task_id, max_in_progress=None) is not None
        candidate = kb.create_task(conn, title="manual-cli", assignee="default")

    output = kanban_cli.run_slash(f"claim {candidate}")

    assert "max_in_progress" in output
    assert "observed=3" in output
    assert "cap=3" in output


def test_cli_dispatch_reports_configured_capacity(
    kanban_home, monkeypatch,
):
    from hermes_cli import config as config_module
    from hermes_cli import kanban as kanban_cli

    monkeypatch.setattr(
        config_module,
        "load_config",
        lambda *args, **kwargs: {"kanban": {"max_in_progress": 3}},
    )
    with kb.connect() as conn:
        for i in range(3):
            task_id = kb.create_task(
                conn, title=f"running-{i}", assignee="default"
            )
            assert kb.claim_task(conn, task_id, max_in_progress=None) is not None
        kb.create_task(conn, title="waiting", assignee="default")

    output = kanban_cli.run_slash("dispatch --json")

    assert '"capacity_exhausted"' in output
    assert "3" in output


def test_review_claim_shares_parent_cap_without_reclaiming_active_work(
    kanban_home,
):
    """Review dispatch is refused at capacity and preserves active workers."""
    with kb.connect() as conn:
        running = [
            kb.create_task(conn, title=f"running-{i}", assignee="default")
            for i in range(3)
        ]
        for task_id in running:
            assert kb.claim_task(conn, task_id) is not None
        review_id = kb.create_task(conn, title="review", assignee="reviewer")
        conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (review_id,))
        conn.commit()

        assert kb.claim_review_task(
            conn,
            review_id,
            claimer="review-owner:1",
            max_in_progress=3,
        ) is None
        assert kb.get_task(conn, review_id).status == "review"
        assert _running_ids(conn) == set(running)
        reclaimed = conn.execute(
            "SELECT COUNT(*) AS n FROM task_events WHERE kind = 'reclaimed'"
        ).fetchone()["n"]

    assert reclaimed == 0


def test_dispatch_reports_full_capacity_without_reclaiming(kanban_home):
    with kb.connect() as conn:
        running = [
            kb.create_task(conn, title=f"running-{i}", assignee="default")
            for i in range(3)
        ]
        for task_id in running:
            assert kb.claim_task(conn, task_id) is not None
        waiting = kb.create_task(conn, title="waiting", assignee="default")

        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda *_args, **_kwargs: pytest.fail("must not spawn"),
            max_in_progress=3,
        )

        assert result.capacity_exhausted == (3, 3)
        assert kb.get_task(conn, waiting).status == "ready"
        assert _running_ids(conn) == set(running)
