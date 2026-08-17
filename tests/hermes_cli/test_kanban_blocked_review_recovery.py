"""Regression coverage for blocked implementation review recovery."""

from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


HEAD_SHA = "e4b9de58701f9b90c5e2329b33eec8a9f2229c07"
OTHER_SHA = "1111111111111111111111111111111111111111"


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(home / "kanban.db"))
    kb.init_db()
    return home


def evidence(*, reviewer: str | None = "hermes-review") -> dict:
    value: dict = {
        "review_evidence": {
            "provider": "github",
            "repository": "example/repo",
            "branch": "wt/blocked-fix",
            "head_sha": HEAD_SHA,
            "pr_url": "https://github.com/example/repo/pull/6",
            "pr_number": 6,
            "base_branch": "main",
        }
    }
    if reviewer is not None:
        value["reviewer"] = reviewer
    return value


def provider_state(expected_head: str = HEAD_SHA, **pr_overrides):
    pr = {
        "state": "open",
        "draft": False,
        "number": 6,
        "head": {"sha": expected_head, "ref": "wt/blocked-fix"},
        "base": {"ref": "main"},
    }
    pr.update(pr_overrides)
    return {
        "provider": "github",
        "pr": pr,
        "check_runs": [{"status": "completed", "conclusion": "success"}],
    }


def _blocked_task(conn, metadata: dict | None = None) -> tuple[str, int]:
    task_id = kb.create_task(
        conn, title="blocked implementation", assignee="hermes-coding"
    )
    claimed = kb.claim_task(conn, task_id, claimer="hermes-coding:test")
    assert claimed is not None and claimed.current_run_id is not None
    assert kb.block_task(
        conn,
        task_id,
        reason="initial worker run stopped before handoff",
        metadata=metadata,
        expected_run_id=claimed.current_run_id,
    )
    return task_id, int(claimed.current_run_id)


def test_blocked_completion_routes_same_card_and_dispatches_only_reviewer(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import hermes_cli.config as config
    import hermes_cli.profiles as profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda _name: True)
    monkeypatch.setattr(
        config, "load_config", lambda *a, **k: {"kanban": {"review_dispatch": True}}
    )
    with kb.connect() as conn:
        task_id, run_id = _blocked_task(conn, evidence())
        spawned: list[tuple[str, str]] = []

        def spawn(task, _workspace):
            spawned.append((task.id, task.assignee or ""))
            return None

        def live_provider(_candidate):
            return provider_state()

        monkeypatch.setattr(
            "hermes_cli.kanban_review_recovery.fetch_live_review_state",
            live_provider,
        )

        # The worker's late completion is accepted even though block_task
        # already closed its active run, and uses the immutable blocked run id.
        assert kb.complete_task(
            conn,
            task_id,
            summary="PR is ready for independent review",
            metadata=evidence(),
            expected_run_id=run_id,
        )
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "review"
        assert task.assignee == "hermes-review"
        assert not any(who == "hermes-coding" for _tid, who in spawned)

        # A dispatcher tick claims the review lane, never the implementation
        # lane. The subsequent tick sees the active review claim and cannot
        # create a duplicate worker.
        # The direct completion above used the real provider path, so replace
        # it with an already-routed idempotent tick; no network is needed.
        first = kb.dispatch_once(conn, spawn_fn=spawn)
        assert [item[0] for item in first.spawned] == [task_id]
        assert spawned == [(task_id, "hermes-review")]
        second = kb.dispatch_once(conn, spawn_fn=spawn)
        assert second.spawned == []
        assert spawned == [(task_id, "hermes-review")]

        routed = [
            e
            for e in kb.list_events(conn, task_id)
            if e.kind == "review_recovery_routed"
        ]
        assert len(routed) == 1
        assert routed[0].payload is not None
        assert routed[0].payload["lane"] == "same_card"


def test_dispatcher_recovers_durable_blocked_evidence_and_spawns_review(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import hermes_cli.config as config
    import hermes_cli.profiles as profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda _name: True)
    monkeypatch.setattr(
        config, "load_config", lambda *a, **k: {"kanban": {"review_dispatch": True}}
    )
    with kb.connect() as conn:
        task_id, _run_id = _blocked_task(conn, evidence())
        spawned: list[tuple[str, str]] = []

        def spawn(task, _workspace):
            spawned.append((task.id, task.assignee or ""))
            return None

        result = kb.dispatch_once(
            conn,
            spawn_fn=spawn,
            review_provider=lambda _candidate: provider_state(),
        )
        assert result.review_recovered == [(task_id, "same_card")]
        assert result.review_recovery_blocked == []
        assert spawned == [(task_id, "hermes-review")]
        task = kb.get_task(conn, task_id)
        assert task is not None and task.status == "running"


def test_negative_provider_states_remain_blocked_with_deduplicated_diagnostic(
    kanban_home: Path,
) -> None:
    cases = {
        "mismatched_head": provider_state(expected_head=OTHER_SHA),
        "draft": provider_state(draft=True),
        "closed": provider_state(state="closed"),
        "red": {
            **provider_state(),
            "check_runs": [{"status": "completed", "conclusion": "failure"}],
        },
        "pending": {
            **provider_state(),
            "check_runs": [{"status": "in_progress", "conclusion": None}],
        },
        "unknown": {"state": "unknown", "diagnostic": "provider unavailable"},
    }
    for name, live_state in cases.items():
        with kb.connect() as conn:
            task_id, _run_id = _blocked_task(conn, evidence())
            first = kb.recover_blocked_completion(
                conn,
                task_id,
                provider=lambda _candidate, state=live_state: state,
            )
            second = kb.recover_blocked_completion(
                conn,
                task_id,
                provider=lambda _candidate, state=live_state: state,
            )
            assert first[0] is False, name
            assert second[0] is False, name
            task = kb.get_task(conn, task_id)
            assert task is not None and task.status == "blocked"
            diagnostics = [
                event
                for event in kb.list_events(conn, task_id)
                if event.kind == "review_recovery_blocked"
            ]
            assert len(diagnostics) == 1, name
            assert diagnostics[0].payload is not None
            assert diagnostics[0].payload["diagnostic"], name

    with kb.connect() as conn:
        task_id, _run_id = _blocked_task(conn, metadata=None)
        called = False

        def should_not_query(_candidate):
            nonlocal called
            called = True
            return provider_state()

        assert kb.recover_blocked_completion(
            conn, task_id, provider=should_not_query
        ) == (False, None)
        assert called is False
        task = kb.get_task(conn, task_id)
        assert task is not None and task.status == "blocked"

        assert kb.complete_task(conn, task_id, summary="without evidence") is False
        task = kb.get_task(conn, task_id)
        assert task is not None and task.status == "blocked"

        malformed = evidence()
        del malformed["review_evidence"]["head_sha"]
        task_id, _run_id = _blocked_task(conn, metadata=None)
        result = kb.recover_blocked_completion(
            conn,
            task_id,
            metadata=malformed,
            provider=should_not_query,
        )
        assert result[0] is False
        task = kb.get_task(conn, task_id)
        assert task is not None and task.status == "blocked"


def test_downstream_review_child_is_the_only_review_lane(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import hermes_cli.config as config
    import hermes_cli.profiles as profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda _name: True)
    monkeypatch.setattr(
        config, "load_config", lambda *a, **k: {"kanban": {"review_dispatch": True}}
    )
    with kb.connect() as conn:
        task_id, _run_id = _blocked_task(conn, evidence())
        child_id = kb.create_task(
            conn,
            title="Review implementation PR",
            assignee="hermes-review",
        )
        kb.link_tasks(conn, task_id, child_id)

        recovered, lane = kb.recover_blocked_completion(
            conn,
            task_id,
            provider=lambda _candidate: provider_state(),
        )
        assert (recovered, lane) == (True, "downstream")
        parent = kb.get_task(conn, task_id)
        child = kb.get_task(conn, child_id)
        assert parent is not None and parent.status == "done"
        assert child is not None and child.status == "ready"
        assert [
            e for e in kb.list_events(conn, task_id) if e.kind == "review_requested"
        ] == []

        spawned: list[str] = []

        def spawn(task, _workspace):
            spawned.append(task.id)
            return None

        result = kb.dispatch_once(conn, spawn_fn=spawn)
        assert spawned == [child_id]
        assert result.review_recovered == []


def test_conflicting_review_graph_stays_blocked(
    kanban_home: Path,
) -> None:
    with kb.connect() as conn:
        task_id, _run_id = _blocked_task(conn, evidence())
        child_id = kb.create_task(
            conn, title="Review implementation", assignee="hermes-review"
        )
        kb.link_tasks(conn, task_id, child_id)
        # A prior same-card handoff plus a downstream review child is an
        # ambiguous graph; recovery must not choose one silently.
        with kb.write_txn(conn):
            conn.execute(
                "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
                (
                    task_id,
                    "review_requested",
                    json.dumps({
                        "implementer": "hermes-coding",
                        "reviewer": "hermes-review",
                    }),
                    1,
                ),
            )
        ok, diagnostic = kb.recover_blocked_completion(
            conn,
            task_id,
            provider=lambda _candidate: provider_state(),
        )
        assert ok is False
        assert diagnostic and "both" in diagnostic
        task = kb.get_task(conn, task_id)
        assert task is not None and task.status == "blocked"


def test_recovery_is_idempotent_across_concurrent_callers_and_reopen(
    kanban_home: Path,
) -> None:
    with kb.connect() as conn:
        task_id, _run_id = _blocked_task(conn, evidence())

    def recover_once(_index: int):
        with kb.connect() as connection:
            return kb.recover_blocked_completion(
                connection,
                task_id,
                provider=lambda _candidate: provider_state(),
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(recover_once, (1, 2)))
    assert all(outcome[0] for outcome in outcomes)

    with kb.connect() as conn:
        task = kb.get_task(conn, task_id)
        assert task is not None and task.status == "review"
        routed = [
            e
            for e in kb.list_events(conn, task_id)
            if e.kind == "review_recovery_routed"
        ]
        assert len(routed) == 1
        # A fresh connection sees the durable route and does not create another
        # review handoff even if the provider is no longer queried.
        assert kb.recover_blocked_completion(
            conn,
            task_id,
            provider=lambda _candidate: (_ for _ in ()).throw(
                AssertionError("queried")
            ),
        ) == (True, "same_card")


def test_active_pr_guard_still_blocks_fresh_ready_work_but_allows_rework(
    kanban_home: Path,
) -> None:
    with kb.connect() as conn:
        fresh = kb.create_task(
            conn, title="fresh existing PR", assignee="hermes-coding"
        )
        kb.add_comment(
            conn,
            fresh,
            author="hermes-coding",
            body="Opened https://github.com/example/repo/pull/6",
        )
        assert kb.check_respawn_guard(conn, fresh) == "active_pr"

        rework = kb.create_task(conn, title="same PR rework", assignee="hermes-coding")
        comment_id = kb.add_comment(
            conn,
            rework,
            author="hermes-coding",
            body="Opened https://github.com/example/repo/pull/7",
        )
        claimed = kb.claim_task(conn, rework, claimer="hermes-coding:test")
        assert claimed is not None
        assert kb.request_review(
            conn,
            rework,
            summary="review me",
            reviewer="hermes-review",
            expected_run_id=claimed.current_run_id,
        )
        review = kb.claim_review_task(conn, rework, claimer="hermes-review:test")
        assert review is not None
        assert kb.request_changes(
            conn,
            rework,
            reason="fix one thing",
            expected_run_id=review.current_run_id,
        ) == (True, "hermes-coding")
        now = int(kb.time.time())
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_comments SET created_at = ? WHERE id = ?",
                (now - 10, comment_id),
            )
            conn.execute(
                "UPDATE task_events SET created_at = ? WHERE task_id = ? AND kind = 'changes_requested'",
                (now - 5, rework),
            )
        assert kb.check_respawn_guard(conn, rework) is None


def test_structured_pr_evidence_survives_manual_unblock_guard(
    kanban_home: Path,
) -> None:
    with kb.connect() as conn:
        task_id, _run_id = _blocked_task(conn, evidence())
        assert kb.unblock_task(conn, task_id)
        task = kb.get_task(conn, task_id)
        assert task is not None and task.status == "ready"
        assert kb.check_respawn_guard(conn, task_id) == "active_pr"


def test_cli_complete_uses_persisted_recovery_route(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The human CLI path shares the blocked-completion safety gate."""
    import hermes_cli.kanban as cli

    monkeypatch.setattr(
        "hermes_cli.kanban_review_recovery.fetch_live_review_state",
        lambda _candidate: provider_state(),
    )
    with kb.connect() as conn:
        task_id, _run_id = _blocked_task(conn, evidence())

    args = argparse.Namespace(
        task_ids=[task_id],
        result=None,
        summary="CLI recovered the verified PR for review",
        metadata=json.dumps(evidence()),
    )
    assert cli._cmd_complete(args) == 0
    assert "Completed" in capsys.readouterr().out
    with kb.connect() as conn:
        task = kb.get_task(conn, task_id)
        assert task is not None and task.status == "review"


def test_release_notes_child_is_not_a_review_lane(kanban_home: Path) -> None:
    with kb.connect() as conn:
        parent_id, _run_id = _blocked_task(conn, evidence())
        child_id = kb.create_task(
            conn,
            title="Prepare the cumulative MVP release-candidate PR",
            assignee="hermes-coding",
        )
        kb.link_tasks(conn, parent_id, child_id)
        assert kb._review_child_ids(conn, parent_id) == []
        recovered, lane = kb.recover_blocked_completion(
            conn,
            parent_id,
            provider=lambda _candidate: provider_state(),
        )
        assert (recovered, lane) == (True, "same_card")
        parent = kb.get_task(conn, parent_id)
        child = kb.get_task(conn, child_id)
        assert parent is not None and parent.status == "review"
        assert child is not None and child.status != "ready"


def test_title_review_fallback_still_selects_downstream_child(
    kanban_home: Path,
) -> None:
    with kb.connect() as conn:
        parent_id, _run_id = _blocked_task(conn, evidence())
        child_id = kb.create_task(
            conn,
            title="Independent review of the published candidate",
            assignee="hermes-coding",
        )
        kb.link_tasks(conn, parent_id, child_id)
        assert kb._review_child_ids(conn, parent_id) == [child_id]


def test_sdlc_review_skill_marks_review_child(kanban_home: Path) -> None:
    with kb.connect() as conn:
        parent_id, _run_id = _blocked_task(conn, evidence())
        child_id = kb.create_task(
            conn,
            title="Validate the implementation",
            assignee="hermes-coding",
            skills=["sdlc-review"],
        )
        kb.link_tasks(conn, parent_id, child_id)
        assert kb._review_child_ids(conn, parent_id) == [child_id]


def test_stale_pr_comment_outside_window_is_not_scanned(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 5_000_000
    monkeypatch.setattr(kb.time, "time", lambda: now)
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="old pr comment", assignee="hermes-coding")
        kb.add_comment(
            conn,
            task_id,
            author="hermes-coding",
            body="Opened https://github.com/example/repo/pull/6",
        )
        conn.execute(
            "UPDATE task_comments SET created_at = ? WHERE task_id = ?",
            (now - kb._RESPAWN_GUARD_PR_WINDOW - 10, task_id),
        )
        conn.commit()
        assert kb.check_respawn_guard(conn, task_id) is None

