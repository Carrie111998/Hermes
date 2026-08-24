"""Security and routing contracts for native same-card review v2."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def conn(tmp_path: Path):
    db = kb.connect(tmp_path / "kanban.db")
    try:
        yield db
    finally:
        db.close()


def _frozen() -> dict:
    return {
        "commit": "a" * 40,
        "tree": "b" * 40,
        "artifacts": [{"path": "dist/release.tar", "sha256": "c" * 64}],
    }


def _event_count(conn, task_id: str) -> int:
    return int(conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id = ?", (task_id,)
    ).fetchone()[0])


def test_same_writer_review_rejected_without_mutation(conn) -> None:
    task_id = kb.create_task(conn, title="independent review", assignee="writer")
    run = kb.claim_task(conn, task_id)
    assert run is not None
    before = _event_count(conn, task_id)

    ok, reason = kb.request_review(
        conn,
        task_id,
        reviewer="writer",
        artifacts=_frozen(),
        actor_profile="writer",
        expected_run_id=run.current_run_id,
        with_reason=True,
    )

    assert ok is False
    assert "independent" in (reason or "")
    task = kb.get_task(conn, task_id)
    assert task is not None
    assert task.status == "running"
    assert task.current_run_id == run.current_run_id
    assert task.review_assignee is None
    assert _event_count(conn, task_id) == before


def test_request_review_preserves_writer_and_freezes_artifacts(conn) -> None:
    task_id = kb.create_task(conn, title="freeze me", assignee="writer")
    run = kb.claim_task(conn, task_id)
    assert run is not None
    frozen = _frozen()

    assert kb.request_review(
        conn,
        task_id,
        reviewer="reviewer",
        artifacts=frozen,
        actor_profile="writer",
        expected_run_id=run.current_run_id,
    )
    task = kb.get_task(conn, task_id)
    assert task is not None
    assert task.status == "review"
    assert task.assignee == "writer"
    assert task.review_assignee == "reviewer"
    assert task.review_artifacts == frozen
    event = [e for e in kb.list_events(conn, task_id) if e.kind == "review_requested"][-1]
    assert event.payload["implementer"] == "writer"
    assert event.payload["reviewer"] == "reviewer"
    assert event.payload["artifacts"] == frozen

    # The frozen identity is immutable for the current review cycle.
    with pytest.raises(RuntimeError, match="frozen"):
        kb.assign_task(conn, task_id, "other-writer")


@pytest.mark.parametrize(
    "artifacts",
    [
        None,
        {},
        {"commit": "nope", "tree": "b" * 40, "artifacts": []},
        {"commit": "a" * 40, "tree": "b" * 40, "artifacts": [{"path": "x"}]},
        {
            "commit": "a" * 40,
            "tree": "b" * 40,
            "artifacts": [{"path": "../escape", "sha256": "c" * 64}],
        },
    ],
)
def test_frozen_artifact_validation_is_fail_closed(conn, artifacts) -> None:
    task_id = kb.create_task(conn, title="bad identity", assignee="writer")
    run = kb.claim_task(conn, task_id)
    assert run is not None
    before = _event_count(conn, task_id)
    ok, _reason = kb.request_review(
        conn,
        task_id,
        reviewer="reviewer",
        artifacts=artifacts,
        actor_profile="writer",
        expected_run_id=run.current_run_id,
        with_reason=True,
    )
    assert ok is False
    assert kb.get_task(conn, task_id).status == "running"
    assert _event_count(conn, task_id) == before


def test_authenticated_reviewer_verdicts_and_generic_complete_bypass(conn) -> None:
    task_id = kb.create_task(conn, title="verdict", assignee="writer")
    writer_run = kb.claim_task(conn, task_id)
    assert writer_run is not None
    assert kb.request_review(
        conn,
        task_id,
        reviewer="reviewer",
        artifacts=_frozen(),
        actor_profile="writer",
        expected_run_id=writer_run.current_run_id,
    )
    review_run = kb.claim_review_task(conn, task_id, actor_profile="reviewer")
    assert review_run is not None

    assert not kb.complete_task(
        conn, task_id, expected_run_id=review_run.current_run_id
    )
    assert not kb.pass_review(
        conn,
        task_id,
        summary="wrong identity",
        actor_profile="intruder",
        expected_run_id=review_run.current_run_id,
    )[0]
    assert not kb.pass_review(
        conn,
        task_id,
        summary="stale token",
        actor_profile="reviewer",
        expected_run_id=review_run.current_run_id + 1,
    )[0]

    ok, reason = kb.pass_review(
        conn,
        task_id,
        summary="independently verified",
        actor_profile="reviewer",
        expected_run_id=review_run.current_run_id,
    )
    assert ok is True, reason
    done = kb.get_task(conn, task_id)
    assert done.status == "done"
    assert done.assignee == "writer"
    assert [e.kind for e in kb.list_events(conn, task_id)][-1] == "review_passed"


def test_request_changes_requires_bound_reviewer_and_restores_writer(conn) -> None:
    task_id = kb.create_task(conn, title="changes", assignee="writer")
    writer_run = kb.claim_task(conn, task_id)
    assert writer_run is not None
    assert kb.request_review(
        conn,
        task_id,
        reviewer="reviewer",
        artifacts=_frozen(),
        actor_profile="writer",
        expected_run_id=writer_run.current_run_id,
    )
    review_run = kb.claim_review_task(conn, task_id, actor_profile="reviewer")
    assert review_run is not None

    ok, reason = kb.request_changes(
        conn,
        task_id,
        reason="fix boundary",
        actor_profile="intruder",
        expected_run_id=review_run.current_run_id,
    )
    assert ok is False
    assert "reviewer" in (reason or "")

    assert kb.request_changes(
        conn,
        task_id,
        reason="fix boundary",
        actor_profile="reviewer",
        expected_run_id=review_run.current_run_id,
    ) == (True, "writer")
    task = kb.get_task(conn, task_id)
    assert task.status == "ready"
    assert task.assignee == "writer"
    assert task.review_assignee == "reviewer"
    assert task.review_artifacts is None


def test_native_request_review_argument_omission_fails_closed(conn) -> None:
    """Direct module callers cannot select legacy authority by omitting args."""
    task_id = kb.create_task(conn, title="no downgrade", assignee="writer")
    writer_run = kb.claim_task(conn, task_id)
    assert writer_run is not None
    before = _event_count(conn, task_id)

    ok, reason = kb.request_review(
        conn,
        task_id,
        reviewer="reviewer",
        artifacts=_frozen(),
        expected_run_id=writer_run.current_run_id,
        with_reason=True,
    )

    assert ok is False
    assert "authenticated writer" in (reason or "")
    assert _event_count(conn, task_id) == before

    ok, reason = kb.request_review(
        conn,
        task_id,
        reviewer="reviewer",
        artifacts=_frozen(),
        actor_profile="writer",
        with_reason=True,
    )

    assert ok is False
    assert "run token" in (reason or "")
    unchanged = kb.get_task(conn, task_id)
    assert unchanged is not None
    assert unchanged.status == "running"
    assert unchanged.current_run_id == writer_run.current_run_id
    assert unchanged.assignee == "writer"
    assert unchanged.review_assignee is None
    assert unchanged.review_artifacts is None
    assert _event_count(conn, task_id) == before


def test_native_request_changes_argument_omission_fails_closed(conn) -> None:
    task_id = kb.create_task(conn, title="no verdict downgrade", assignee="writer")
    writer_run = kb.claim_task(conn, task_id)
    assert writer_run is not None
    assert kb.request_review(
        conn,
        task_id,
        reviewer="reviewer",
        artifacts=_frozen(),
        actor_profile="writer",
        expected_run_id=writer_run.current_run_id,
    )
    review_run = kb.claim_review_task(conn, task_id, actor_profile="reviewer")
    assert review_run is not None
    before = _event_count(conn, task_id)

    ok, reason = kb.request_changes(
        conn,
        task_id,
        reason="unauthenticated",
        expected_run_id=review_run.current_run_id,
    )

    assert ok is False
    assert "authenticated reviewer" in (reason or "")
    assert _event_count(conn, task_id) == before

    ok, reason = kb.request_changes(
        conn,
        task_id,
        reason="missing token",
        actor_profile="reviewer",
    )

    assert ok is False
    assert "run token" in (reason or "")
    unchanged = kb.get_task(conn, task_id)
    assert unchanged is not None
    assert unchanged.status == "running"
    assert unchanged.current_run_id == review_run.current_run_id
    assert unchanged.assignee == "writer"
    assert unchanged.review_assignee == "reviewer"
    assert unchanged.review_artifacts == _frozen()
    assert _event_count(conn, task_id) == before


@pytest.mark.parametrize("actor", [None, "", "intruder", "writer"])
def test_native_review_claim_requires_bound_independent_actor(conn, actor) -> None:
    task_id = kb.create_task(conn, title="claim authority", assignee="writer")
    writer_run = kb.claim_task(conn, task_id)
    assert writer_run is not None
    assert kb.request_review(
        conn,
        task_id,
        reviewer="reviewer",
        artifacts=_frozen(),
        actor_profile="writer",
        expected_run_id=writer_run.current_run_id,
    )
    before = _event_count(conn, task_id)

    assert kb.claim_review_task(conn, task_id, actor_profile=actor) is None
    unchanged = kb.get_task(conn, task_id)
    assert unchanged is not None
    assert unchanged.status == "review"
    assert unchanged.current_run_id is None
    assert _event_count(conn, task_id) == before


def test_native_review_claim_accepts_exact_actor_and_legacy_is_explicit(conn) -> None:
    native_id = kb.create_task(conn, title="native claim", assignee="writer")
    writer_run = kb.claim_task(conn, native_id)
    assert writer_run is not None
    assert kb.request_review(
        conn,
        native_id,
        reviewer="reviewer",
        artifacts=_frozen(),
        actor_profile="writer",
        expected_run_id=writer_run.current_run_id,
    )
    claimed = kb.claim_review_task(conn, native_id, actor_profile=" reviewer ")
    assert claimed is not None
    assert kb.latest_run(conn, native_id).profile == "reviewer"

    legacy_id = kb.create_task(conn, title="pre-migration review", assignee="writer")
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status = 'review', assignee = 'reviewer', "
            "review_protocol = 'legacy' WHERE id = ?",
            (legacy_id,),
        )
    legacy_claim = kb.claim_review_task(conn, legacy_id)
    assert legacy_claim is not None
    assert kb.latest_run(conn, legacy_id).profile == "reviewer"


def test_native_review_claim_rejects_stale_or_malformed_routing(conn) -> None:
    for mutation in (
        "review_assignee = NULL",
        "review_assignee = 'writer'",
        "review_artifacts = NULL",
        "review_artifacts = '{\"commit\":\"bad\"}'",
    ):
        task_id = kb.create_task(conn, title="stale routing", assignee="writer")
        writer_run = kb.claim_task(conn, task_id)
        assert writer_run is not None
        assert kb.request_review(
            conn,
            task_id,
            reviewer="reviewer",
            artifacts=_frozen(),
            actor_profile="writer",
            expected_run_id=writer_run.current_run_id,
        )
        with kb.write_txn(conn):
            conn.execute(f"UPDATE tasks SET {mutation} WHERE id = ?", (task_id,))
        before = _event_count(conn, task_id)

        assert kb.claim_review_task(
            conn, task_id, actor_profile="reviewer"
        ) is None
        assert kb.get_task(conn, task_id).status == "review"
        assert _event_count(conn, task_id) == before


def test_gateway_stuck_probe_uses_dispatchers_exact_review_gate() -> None:
    source = (
        Path(__file__).resolve().parents[2] / "gateway" / "kanban_watchers.py"
    ).read_text(encoding="utf-8")
    assert "_review_probe = _kb.review_dispatch_enabled()" in source
    assert "if _review_probe and _kb.has_spawnable_review(conn):" in source


@pytest.mark.parametrize(
    ("config_value", "expected"),
    [
        ({}, False),
        ({"kanban": {}}, False),
        ({"kanban": {"review_dispatch": False}}, False),
        ({"kanban": {"review_dispatch": True}}, True),
        ({"kanban": {"review_dispatch": None}}, False),
        ({"kanban": {"review_dispatch": 0}}, False),
        ({"kanban": {"review_dispatch": 1}}, False),
        ({"kanban": {"review_dispatch": "true"}}, False),
        ({"kanban": []}, False),
        ([], False),
    ],
)
def test_review_dispatch_gate_requires_explicit_boolean_true(
    monkeypatch: pytest.MonkeyPatch,
    config_value,
    expected: bool,
) -> None:
    from hermes_cli import config as cfgmod

    monkeypatch.setattr(cfgmod, "load_config", lambda: config_value)
    assert kb.review_dispatch_enabled() is expected


def test_review_dispatch_gate_fails_closed_on_loader_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_cli import config as cfgmod

    def broken_loader():
        raise RuntimeError("unreadable managed config")

    monkeypatch.setattr(cfgmod, "load_config", broken_loader)
    assert kb.review_dispatch_enabled() is False


def test_dispatcher_does_not_claim_review_until_explicitly_enabled(
    conn,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_cli import config as cfgmod
    from hermes_cli import profiles

    task_id = kb.create_task(conn, title="staged dispatch", assignee="writer")
    writer_run = kb.claim_task(conn, task_id)
    assert writer_run is not None
    assert kb.request_review(
        conn,
        task_id,
        reviewer="reviewer",
        artifacts=_frozen(),
        actor_profile="writer",
        expected_run_id=writer_run.current_run_id,
    )
    claimed_before = len([
        event for event in kb.list_events(conn, task_id) if event.kind == "claimed"
    ])
    spawned = []
    monkeypatch.setattr(profiles, "profile_exists", lambda profile: profile == "reviewer")

    monkeypatch.setattr(cfgmod, "load_config", lambda: {})
    disabled = kb.dispatch_once(
        conn,
        spawn_fn=lambda task, workspace: spawned.append(task.assignee) or 123,
    )
    assert disabled.spawned == []
    assert spawned == []
    assert kb.get_task(conn, task_id).status == "review"
    assert len([
        event for event in kb.list_events(conn, task_id) if event.kind == "claimed"
    ]) == claimed_before

    def broken_loader():
        raise RuntimeError("config unavailable")

    monkeypatch.setattr(cfgmod, "load_config", broken_loader)
    failed_closed = kb.dispatch_once(
        conn,
        spawn_fn=lambda task, workspace: spawned.append(task.assignee) or 124,
    )
    assert failed_closed.spawned == []
    assert spawned == []
    assert kb.get_task(conn, task_id).status == "review"

    monkeypatch.setattr(
        cfgmod,
        "load_config",
        lambda: {"kanban": {"review_dispatch": True}},
    )
    enabled = kb.dispatch_once(
        conn,
        spawn_fn=lambda task, workspace: spawned.append(task.assignee) or 125,
    )
    assert [item[0] for item in enabled.spawned] == [task_id]
    assert spawned == ["reviewer"]
    running = kb.get_task(conn, task_id)
    assert running is not None
    assert running.status == "running"
    assert running.assignee == "writer"
    assert kb.latest_run(conn, task_id).profile == "reviewer"


@pytest.mark.parametrize("provenance", ["missing", "stale"])
def test_review_reclaim_without_current_run_fails_closed_to_review(
    conn, provenance: str,
) -> None:
    task_id = kb.create_task(conn, title="lost review bookkeeping", assignee="writer")
    writer_run = kb.claim_task(conn, task_id)
    assert writer_run is not None
    assert kb.request_review(
        conn, task_id, reviewer="reviewer", artifacts=_frozen(),
        actor_profile="writer", expected_run_id=writer_run.current_run_id,
    )
    assert kb.claim_review_task(conn, task_id, actor_profile="reviewer") is not None
    with kb.write_txn(conn):
        current_run_id = None if provenance == "missing" else writer_run.current_run_id
        conn.execute(
            "UPDATE tasks SET current_run_id = ? WHERE id = ?",
            (current_run_id, task_id),
        )

    assert kb.reclaim_task(conn, task_id, reason="repair missing run")
    recovered = kb.get_task(conn, task_id)
    assert recovered is not None
    assert recovered.status == "review"
    assert recovered.assignee == "writer"
    assert recovered.review_assignee == "reviewer"


@pytest.mark.parametrize(
    "mutation",
    [
        "review_assignee = NULL",
        "review_assignee = 'writer'",
        "review_artifacts = NULL",
        "review_artifacts = '{\"commit\":\"bad\"}'",
    ],
)
def test_malformed_review_cannot_reserve_capacity_ahead_of_valid_ready(
    conn, monkeypatch: pytest.MonkeyPatch, mutation: str,
) -> None:
    from hermes_cli import config as cfgmod
    from hermes_cli import profiles

    malformed = kb.create_task(
        conn, title="malformed review", assignee="writer", priority=100
    )
    writer_run = kb.claim_task(conn, malformed)
    assert writer_run is not None
    assert kb.request_review(
        conn, malformed, reviewer="reviewer", artifacts=_frozen(),
        actor_profile="writer", expected_run_id=writer_run.current_run_id,
    )
    with kb.write_txn(conn):
        conn.execute(f"UPDATE tasks SET {mutation} WHERE id = ?", (malformed,))
    valid = kb.create_task(conn, title="valid work", assignee="worker", priority=1)
    malformed_events = _event_count(conn, malformed)
    spawned = []
    monkeypatch.setattr(
        cfgmod, "load_config", lambda: {"kanban": {"review_dispatch": True}}
    )
    monkeypatch.setattr(
        profiles, "profile_exists", lambda profile: profile in {"reviewer", "worker"}
    )

    result = kb.dispatch_once(
        conn,
        spawn_fn=lambda task, workspace: spawned.append(task.id) or 123,
        max_spawn=1,
    )

    assert [item[0] for item in result.spawned] == [valid]
    assert spawned == [valid]
    assert kb.get_task(conn, malformed).status == "review"
    assert _event_count(conn, malformed) == malformed_events


@pytest.mark.parametrize(
    ("config_value", "expected"),
    [
        ({}, False),
        ({"kanban": {}}, False),
        ({"kanban": {"native_scheduling": None}}, False),
        ({"kanban": {"native_scheduling": False}}, False),
        ({"kanban": {"native_scheduling": 0}}, False),
        ({"kanban": {"native_scheduling": 1}}, False),
        ({"kanban": {"native_scheduling": "false"}}, False),
        ({"kanban": {"native_scheduling": "true"}}, False),
        ({"kanban": {"native_scheduling": []}}, False),
        ({"kanban": {"native_scheduling": {}}}, False),
        ({"kanban": {"native_scheduling": True}}, True),
        ({"kanban": []}, False),
        ([], False),
    ],
)
def test_native_scheduling_gate_requires_explicit_boolean_true(
    monkeypatch: pytest.MonkeyPatch, config_value, expected: bool,
) -> None:
    from hermes_cli import config as cfgmod

    monkeypatch.setattr(cfgmod, "load_config", lambda: config_value)
    assert kb.native_scheduling_enabled() is expected


def test_generic_complete_cannot_approve_malformed_native_review(conn) -> None:
    """The generic-complete gate keys off protocol, not nullable artifacts."""
    task_id = kb.create_task(conn, title="malformed native review", assignee="writer")
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status = 'review', review_assignee = 'reviewer', "
            "review_artifacts = NULL WHERE id = ?",
            (task_id,),
        )

    assert kb.complete_task(conn, task_id) is False
    task = kb.get_task(conn, task_id)
    assert task is not None
    assert task.status == "review"
    assert not any(event.kind == "completed" for event in kb.list_events(conn, task_id))


def test_native_schedule_t15_due_restart_and_duplicate_ticks(tmp_path: Path) -> None:
    db_path = tmp_path / "scheduled.db"
    base = 2_000_000_000
    db = kb.connect(db_path)
    task_id = kb.create_task(
        db,
        title="future work",
        assignee="writer",
        scheduled_for=base,
        due_at=base + 3600,
    )
    task = kb.get_task(db, task_id)
    assert task.status == "scheduled"
    assert task.scheduled_for == base
    assert task.due_at == base + 3600

    assert kb.process_scheduled_tasks(db, now=base - 901) == {
        "noticed": [], "promoted": []
    }
    first = kb.process_scheduled_tasks(db, now=base - 900)
    assert first == {"noticed": [task_id], "promoted": []}
    assert kb.process_scheduled_tasks(db, now=base - 899) == {
        "noticed": [], "promoted": []
    }
    db.close()

    # Restart recovery: the durable marker suppresses duplicate notice and a
    # missed due tick promotes exactly once on the next open.
    db = kb.connect(db_path)
    try:
        due = kb.process_scheduled_tasks(db, now=base + 60)
        assert due == {"noticed": [], "promoted": [task_id]}
        assert kb.get_task(db, task_id).status == "ready"
        assert kb.process_scheduled_tasks(db, now=base + 60) == {
            "noticed": [], "promoted": []
        }
        kinds = [e.kind for e in kb.list_events(db, task_id)]
        assert kinds.count("scheduled_pre_notice") == 1
        assert kinds.count("scheduled_promoted") == 1
    finally:
        db.close()


def test_review_and_schedule_events_are_ordered_and_deduplicated_for_sub(conn) -> None:
    task_id = kb.create_task(conn, title="notify lifecycle", assignee="writer")
    kb.add_notify_sub(
        conn, task_id=task_id, platform="api_server", chat_id="controller"
    )
    writer_run = kb.claim_task(conn, task_id)
    assert writer_run is not None
    assert kb.request_review(
        conn,
        task_id,
        reviewer="reviewer",
        artifacts=_frozen(),
        actor_profile="writer",
        expected_run_id=writer_run.current_run_id,
    )
    review_run = kb.claim_review_task(conn, task_id, actor_profile="reviewer")
    assert review_run is not None
    assert kb.request_changes(
        conn,
        task_id,
        reason="structured correction",
        actor_profile="reviewer",
        expected_run_id=review_run.current_run_id,
    )[0]

    kinds = ("review_requested", "review_passed", "changes_requested")
    old, new, events = kb.claim_unseen_events_for_sub(
        conn,
        task_id=task_id,
        platform="api_server",
        chat_id="controller",
        kinds=kinds,
    )
    assert old < new
    assert [e.kind for e in events] == ["review_requested", "changes_requested"]
    ids = [e.id for e in events]
    assert ids == sorted(ids)
    # Atomic cursor claim prevents a second notifier from seeing the range.
    _old2, _new2, duplicate = kb.claim_unseen_events_for_sub(
        conn,
        task_id=task_id,
        platform="api_server",
        chat_id="controller",
        kinds=kinds,
    )
    assert duplicate == []
    # Rewind provides bounded retry without event loss.
    assert kb.rewind_notify_cursor(
        conn,
        task_id=task_id,
        platform="api_server",
        chat_id="controller",
        claimed_cursor=new,
        old_cursor=old,
    )
    _old3, _new3, retried = kb.claim_unseen_events_for_sub(
        conn,
        task_id=task_id,
        platform="api_server",
        chat_id="controller",
        kinds=kinds,
    )
    assert [e.id for e in retried] == ids


def test_end_to_end_distinct_writer_reviewer_changes_rereview_pass(conn) -> None:
    task_id = kb.create_task(conn, title="full cycle", assignee="writer")
    writer_run = kb.claim_task(conn, task_id)
    assert writer_run is not None
    assert kb.request_review(
        conn,
        task_id,
        reviewer="reviewer",
        artifacts=_frozen(),
        actor_profile="writer",
        expected_run_id=writer_run.current_run_id,
    )
    first_review = kb.claim_review_task(conn, task_id, actor_profile="reviewer")
    assert first_review is not None
    assert kb.request_changes(
        conn,
        task_id,
        reason="correct the boundary",
        actor_profile="reviewer",
        expected_run_id=first_review.current_run_id,
    ) == (True, "writer")

    second_writer = kb.claim_task(conn, task_id)
    assert second_writer is not None
    assert second_writer.assignee == "writer"
    assert kb.request_review(
        conn,
        task_id,
        reviewer=None,  # durable routing reuses the independently bound reviewer
        artifacts=_frozen(),
        actor_profile="writer",
        expected_run_id=second_writer.current_run_id,
    )
    second_review = kb.claim_review_task(conn, task_id, actor_profile="reviewer")
    assert second_review is not None
    assert kb.pass_review(
        conn,
        task_id,
        summary="verified corrected frozen tree",
        actor_profile="reviewer",
        expected_run_id=second_review.current_run_id,
    ) == (True, None)

    task = kb.get_task(conn, task_id)
    assert task.status == "done"
    assert task.assignee == "writer"
    assert task.review_assignee == "reviewer"
    kinds = [e.kind for e in kb.list_events(conn, task_id)]
    assert kinds.count("review_requested") == 2
    assert kinds.count("changes_requested") == 1
    assert kinds.count("review_passed") == 1
