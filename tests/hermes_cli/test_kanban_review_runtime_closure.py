"""Native guarded-review closure for factory-created Kanban cards."""

from __future__ import annotations

import threading
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import projects_db as projects


@pytest.fixture
def conn(tmp_path: Path):
    connection = kb.connect(tmp_path / "kanban.db")
    try:
        yield connection
    finally:
        connection.close()


SHA_1 = "1" * 40
SHA_2 = "2" * 40
SHA_3 = "3" * 40


def _guarded_task(conn, *, key: str = "source:one") -> str:
    return kb.create_task(
        conn,
        title="Implement guarded runtime",
        assignee="executor",
        idempotency_key=key,
        review_required=True,
        reviewer_profile="verifier",
        canonical_implementer="executor",
    )


def _request_review(conn, task_id: str, candidate_sha: str):
    implementation = kb.claim_task(conn, task_id, claimer="executor:test")
    assert implementation is not None
    assert implementation.current_run_id is not None
    assert kb.request_review(
        conn,
        task_id,
        summary=f"candidate {candidate_sha}",
        metadata=_handoff(candidate_sha, implementation.current_run_id),
        candidate_sha=candidate_sha,
        expected_run_id=implementation.current_run_id,
    )
    review = kb.claim_review_task(conn, task_id, claimer="verifier:test")
    assert review is not None
    return review


def _finding(candidate_sha: str, finding_id: str = "F-1") -> dict:
    return {
        "reason_code": "correctness",
        "finding_ids": [finding_id],
        "evidence_refs": ["tests/failing-case"],
        "rejected_candidate_sha": candidate_sha,
        "required_corrections": ["repair the failing boundary"],
    }


def _handoff(candidate_sha: str, run_id: int) -> dict:
    return {
        "candidate_sha": candidate_sha,
        "current_run_id": run_id,
        "repository": "nousresearch/hermes-agent",
        "branch": "factory/test-candidate",
        "local_gates": ["targeted-tests:pass"],
    }


def _approval(candidate_sha: str) -> dict:
    evidence = {
        "provider_review": "provider-review:approved",
        "ci": "provider-ci:green",
        "protected_merge": "provider-merge:exact-head",
        "default_branch_containment": "provider-default:contains-head",
        "cleanup": "local-cleanup:verified",
    }
    return {
        "candidate_sha": candidate_sha,
        **{
            field: {"candidate_sha": candidate_sha, "evidence_ref": reference}
            for field, reference in evidence.items()
        },
    }


def test_guarded_review_persists_roles_candidate_and_full_lifecycle(conn) -> None:
    task_id = _guarded_task(conn)
    created = kb.get_task(conn, task_id)
    assert created is not None
    assert created.review_required is True
    assert created.reviewer_profile == "verifier"
    assert created.canonical_implementer == "executor"
    assert created.review_cycle == 0
    assert created.candidate_sha is None

    implementation = kb.claim_task(conn, task_id, claimer="executor:one")
    assert implementation is not None
    assert implementation.current_run_id is not None
    ok, reason = kb.request_review(
        conn,
        task_id,
        summary="first candidate",
        metadata=_handoff(SHA_1, implementation.current_run_id),
        candidate_sha=SHA_1,
        expected_run_id=implementation.current_run_id,
        with_reason=True,
    )
    assert (ok, reason) == (True, None)
    awaiting = kb.get_task(conn, task_id)
    assert awaiting is not None
    assert awaiting.assignee == "verifier"
    assert awaiting.review_cycle == 0
    assert awaiting.candidate_sha == SHA_1
    assert kb.reopen_review_task(conn, task_id) is False
    still_awaiting = kb.get_task(conn, task_id)
    assert still_awaiting is not None
    assert still_awaiting.status == "review"

    review = kb.claim_review_task(conn, task_id, claimer="verifier:one")
    assert review is not None
    assert kb.request_changes(
        conn,
        task_id,
        reason="repair the race",
        **_finding(SHA_1),
        expected_run_id=review.current_run_id,
    ) == (True, "executor")
    repair = kb.get_task(conn, task_id)
    assert repair is not None
    assert repair.status == "ready"
    assert repair.assignee == "executor"
    assert repair.review_cycle == 1

    repaired_review = _request_review(conn, task_id, SHA_2)
    assert kb.complete_task(
        conn,
        task_id,
        summary="approved independently",
        metadata=_approval(SHA_2),
        approved_candidate_sha=SHA_2,
        expected_run_id=repaired_review.current_run_id,
    )
    completed = kb.get_task(conn, task_id)
    assert completed is not None
    assert completed.status == "done"
    assert completed.candidate_sha == SHA_2


def test_guarded_request_review_requires_full_candidate_sha_without_mutation(conn) -> None:
    task_id = _guarded_task(conn)
    implementation = kb.claim_task(conn, task_id)
    assert implementation is not None

    for candidate_sha in (None, "abc1234", "g" * 40):
        ok, reason = kb.request_review(
            conn,
            task_id,
            summary="candidate",
            candidate_sha=candidate_sha,
            expected_run_id=implementation.current_run_id,
            with_reason=True,
        )
        assert ok is False
        assert "candidate_sha" in (reason or "")
        unchanged = kb.get_task(conn, task_id)
        assert unchanged is not None
        assert unchanged.status == "running"
        assert unchanged.current_run_id == implementation.current_run_id
        assert unchanged.candidate_sha is None

    assert not [
        event for event in kb.list_events(conn, task_id)
        if event.kind == "review_requested"
    ]


def test_guarded_request_review_requires_active_canonical_implementer_run(conn) -> None:
    task_id = _guarded_task(conn)
    events_before = kb.list_events(conn, task_id)
    ok, reason = kb.request_review(
        conn,
        task_id,
        summary="unclaimed factory bypass",
        candidate_sha=SHA_1,
        force=True,
        with_reason=True,
    )
    assert ok is False
    assert "canonical_implementer" in (reason or "")
    assert kb.list_events(conn, task_id) == events_before
    assert kb.get_task(conn, task_id).status == "ready"

    implementation = kb.claim_task(conn, task_id)
    assert implementation is not None
    conn.execute(
        "UPDATE task_runs SET profile = 'verifier' WHERE id = ?",
        (implementation.current_run_id,),
    )
    conn.commit()
    ok, reason = kb.request_review(
        conn,
        task_id,
        summary="wrong run owner",
        candidate_sha=SHA_1,
        expected_run_id=implementation.current_run_id,
        with_reason=True,
    )
    assert ok is False
    assert "canonical_implementer" in (reason or "")
    assert kb.get_task(conn, task_id).status == "running"


def test_candidate_sha_is_preserved_and_compared_exactly(conn) -> None:
    candidate_sha = "Aa" * 20
    task_id = _guarded_task(conn)
    implementation = kb.claim_task(conn, task_id)
    assert implementation is not None
    assert implementation.current_run_id is not None
    assert kb.request_review(
        conn,
        task_id,
        summary="mixed-case object id",
        metadata=_handoff(candidate_sha, implementation.current_run_id),
        candidate_sha=candidate_sha,
        expected_run_id=implementation.current_run_id,
    )
    assert kb.get_task(conn, task_id).candidate_sha == candidate_sha
    review = kb.claim_review_task(conn, task_id)
    assert review is not None
    assert not kb.complete_task(
        conn,
        task_id,
        summary="case-folded evidence is not exact",
        approved_candidate_sha=candidate_sha.lower(),
        expected_run_id=review.current_run_id,
    )
    assert kb.complete_task(
        conn,
        task_id,
        summary="exact evidence approved",
        metadata=_approval(candidate_sha),
        approved_candidate_sha=candidate_sha,
        expected_run_id=review.current_run_id,
    )


def test_guarded_completion_denies_runless_implementer_stale_and_mismatch(conn) -> None:
    task_id = _guarded_task(conn)
    implementation = kb.claim_task(conn, task_id)
    assert implementation is not None
    assert implementation.current_run_id is not None

    events_before = kb.list_events(conn, task_id)
    assert not kb.complete_task(
        conn,
        task_id,
        summary="implementer bypass with untrusted completion claims",
        created_cards=["t_deadbeef"],
        approved_candidate_sha=SHA_1,
        expected_run_id=implementation.current_run_id,
    )
    assert kb.list_events(conn, task_id) == events_before

    assert not kb.complete_task(
        conn,
        task_id,
        summary="implementer bypass",
        approved_candidate_sha=SHA_1,
        expected_run_id=implementation.current_run_id,
    )
    assert not kb.complete_task(conn, task_id, summary="direct bypass")

    assert kb.request_review(
        conn,
        task_id,
        summary="candidate",
        metadata=_handoff(SHA_1, implementation.current_run_id),
        candidate_sha=SHA_1,
        expected_run_id=implementation.current_run_id,
    )
    assert not kb.complete_task(
        conn,
        task_id,
        summary="parked bypass",
        approved_candidate_sha=SHA_1,
    )

    review = kb.claim_review_task(conn, task_id)
    assert review is not None
    assert not kb.complete_task(
        conn,
        task_id,
        summary="exact SHA without provider-tail evidence",
        approved_candidate_sha=SHA_1,
        expected_run_id=review.current_run_id,
    )
    for run_id, candidate_sha in (
        (None, SHA_1),
        (implementation.current_run_id, SHA_1),
        (review.current_run_id, SHA_2),
    ):
        assert not kb.complete_task(
            conn,
            task_id,
            summary="invalid approval",
            approved_candidate_sha=candidate_sha,
            expected_run_id=run_id,
        )
        current = kb.get_task(conn, task_id)
        assert current is not None
        assert current.status == "running"
        assert current.current_run_id == review.current_run_id


def test_guarded_genuine_decision_is_typed_while_routine_changes_are_silent(conn) -> None:
    decision_id = _guarded_task(conn, key="decision")
    decision_review = _request_review(conn, decision_id, SHA_1)
    assert kb.block_task(
        conn,
        decision_id,
        reason="maintainer authority is required",
        kind="authority",
        expected_run_id=decision_review.current_run_id,
    )
    decision = [
        event for event in kb.list_events(conn, decision_id)
        if event.kind == "blocked"
    ][-1]
    assert decision.payload is not None
    assert decision.payload["alert_required"] is True
    assert decision.payload["terminal_type"] == "GENUINE_DECISION"

    routine_id = _guarded_task(conn, key="routine")
    routine_review = _request_review(conn, routine_id, SHA_1)
    assert kb.request_changes(
        conn,
        routine_id,
        reason="add one regression",
        **_finding(SHA_1),
        expected_run_id=routine_review.current_run_id,
    ) == (True, "executor")
    routine = [
        event for event in kb.list_events(conn, routine_id)
        if event.kind == "changes_requested"
    ][-1]
    assert routine.payload is not None
    assert "terminal_type" not in routine.payload
    assert "alert_required" not in routine.payload


def test_third_finding_atomically_exhausts_once_under_replay_and_concurrency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "kanban.db"
    with kb.connect(db_path) as setup:
        task_id = _guarded_task(setup)
        for candidate_sha in (SHA_1, SHA_2):
            review = _request_review(setup, task_id, candidate_sha)
            assert kb.request_changes(
                setup,
                task_id,
                reason=f"finding for {candidate_sha}",
                **_finding(candidate_sha),
                expected_run_id=review.current_run_id,
            ) == (True, "executor")
        final_review = _request_review(setup, task_id, SHA_3)
        final_run_id = final_review.current_run_id

    lifecycle_calls: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(
        kb,
        "_fire_kanban_lifecycle_hook",
        lambda name, hooked_task_id, **kwargs: lifecycle_calls.append(
            (name, hooked_task_id, kwargs)
        ),
    )
    barrier = threading.Barrier(2)
    results: list[tuple[bool, str | None]] = []

    def reject() -> None:
        with kb.connect(db_path) as connection:
            barrier.wait()
            results.append(
                kb.request_changes(
                    connection,
                    task_id,
                    reason="third independent finding",
                    **_finding(SHA_3, "F-3"),
                    expected_run_id=final_run_id,
                )
            )

    threads = [threading.Thread(target=reject) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    with kb.connect(db_path) as check:
        task = kb.get_task(check, task_id)
        assert task is not None
        assert task.status == "blocked"
        assert task.result == "recovery_exhausted"
        assert task.block_kind == "recovery_exhausted"
        assert task.review_cycle == 2
        exhausted = [
            event for event in kb.list_events(check, task_id)
            if event.kind == "review_budget_exhausted"
        ]
        assert len(exhausted) == 1
        assert exhausted[0].run_id == final_run_id
        exhausted_payload = exhausted[0].payload
        assert exhausted_payload is not None
        assert exhausted_payload == {
            "alert_required": True,
            "candidate_sha": SHA_3,
            "evidence_refs": ["tests/failing-case"],
            "finding_ids": ["F-3"],
            "finding_identity": exhausted_payload["finding_identity"],
            "reason": "third independent finding",
            "reason_code": "correctness",
            "rejected_candidate_sha": SHA_3,
            "required_corrections": ["repair the failing boundary"],
            "review_cycle": 2,
            "terminal_type": "RECOVERY_EXHAUSTED",
        }
        assert kb.request_changes(
            check,
            task_id,
            reason="replay",
            **_finding(SHA_3, "F-3"),
            expected_run_id=final_run_id,
        )[0] is False
        assert len([
            event for event in kb.list_events(check, task_id)
            if event.kind == "review_budget_exhausted"
        ]) == 1
        assert kb.promote_task(
            check,
            task_id,
            actor="operator",
            force=True,
        ) == (False, "guarded review recovery is exhausted")
        assert kb.unblock_task(check, task_id) is False
        assert kb.recompute_ready(check) == 0
        assert kb.schedule_task(check, task_id, reason="later") is False
        with pytest.raises(RuntimeError, match="recovery is exhausted"):
            kb.assign_task(check, task_id, "executor")
        fenced = kb.get_task(check, task_id)
        assert fenced is not None
        assert fenced.status == "blocked"
        assert fenced.assignee == "verifier"
        from plugins.kanban.dashboard.plugin_api import _set_status_direct

        assert _set_status_direct(check, task_id, "ready") is False
        dashboard_fenced = kb.get_task(check, task_id)
        assert dashboard_fenced is not None
        assert dashboard_fenced.status == "blocked"

    assert sorted(ok for ok, _detail in results) == [False, True]
    assert len(lifecycle_calls) == 1
    assert lifecycle_calls[0][0:2] == ("kanban_task_blocked", task_id)
    assert lifecycle_calls[0][2]["run_id"] == final_run_id
    assert lifecycle_calls[0][2]["reason"] == "third independent finding"


def test_legacy_cards_keep_existing_review_and_completion_behavior(conn) -> None:
    task_id = kb.create_task(conn, title="legacy", assignee="worker")
    assert kb.complete_task(conn, task_id, summary="manual completion")

    review_id = kb.create_task(conn, title="legacy review", assignee="worker")
    implementation = kb.claim_task(conn, review_id)
    assert implementation is not None
    assert kb.request_review(
        conn,
        review_id,
        summary="legacy handoff without sha",
        reviewer="reviewer",
        expected_run_id=implementation.current_run_id,
    )
    assert kb.complete_task(conn, review_id, summary="manual approval")


def test_factory_intake_is_project_scoped_permanent_and_same_card(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    with projects.connect_closing() as project_conn:
        project_id = projects.create_project(
            project_conn,
            name="Runtime",
            folders=[str(repo)],
        )

    with kb.connect_closing() as connection:
        first = kb.create_factory_task(
            connection,
            title="Close the native runtime",
            project_id=project_id,
            idempotency_key="factory:5929747",
        )
        with projects.connect_closing() as project_conn:
            assert projects.delete_project(project_conn, project_id)
        replay = kb.create_factory_task(
            connection,
            title="Replay must not replace immutable intake",
            project_id=project_id,
            idempotency_key="factory:5929747",
        )
        assert replay == first
        task = kb.get_task(connection, first)
        assert task is not None
        assert task.title == "Close the native runtime"
        assert task.assignee == "executor"
        assert task.reviewer_profile == "verifier"
        assert task.canonical_implementer == "executor"
        assert task.review_required is True
        assert task.project_id == project_id
        assert task.workspace_kind == "worktree"
        assert task.workspace_path == str(repo / ".worktrees" / first)
        assert kb.child_ids(connection, first) == []
        assert connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1


def test_factory_cli_defaults_to_executor_and_verifier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_cli import kanban as cli

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    with projects.connect_closing() as project_conn:
        project_id = projects.create_project(
            project_conn,
            name="Runtime CLI",
            folders=[str(repo)],
        )

    output = cli.run_slash(
        "factory 'Ship guarded lane' "
        f"--project {project_id} --idempotency-key source:event-1 --json"
    )
    payload = __import__("json").loads(output)
    assert payload["review_required"] is True
    assert payload["assignee"] == "executor"
    assert payload["canonical_implementer"] == "executor"
    assert payload["reviewer_profile"] == "verifier"
    assert payload["project_id"] == project_id
