from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from hermes_cli import kanban_coderabbit as cr
from hermes_cli import kanban_db as kb


REPO = "NousResearch/hermes-agent"
CANONICAL_REPO = "nousresearch/hermes-agent"
PR_NUMBER = 79683
HEAD_A = "a" * 40
HEAD_B = "b" * 40
NOW = 1_800_000_000


class FakeCodeRabbitProvider:
    def __init__(self, snapshot: cr.CodeRabbitSnapshot):
        self.snapshot = snapshot
        self.calls: list[tuple[str, int, str]] = []

    def read_review(
        self,
        *,
        repository: str,
        pr_number: int,
        expected_head_sha: str,
    ) -> cr.CodeRabbitSnapshot:
        self.calls.append((repository, pr_number, expected_head_sha))
        return self.snapshot


def _read_provider(
    provider: cr.CodeRabbitSnapshotProvider,
    *,
    head_sha: str,
) -> cr.CodeRabbitSnapshot:
    return provider.read_review(
        repository=REPO,
        pr_number=PR_NUMBER,
        expected_head_sha=head_sha,
    )


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    path = home / "kanban.db"
    with kb.connect(path):
        pass
    return path


def _summary(
    state: cr.AssessmentState,
    actionable_count: int = 0,
) -> cr.CodeRabbitReviewSummary:
    return cr.CodeRabbitReviewSummary(
        state=state,
        actionable_count=actionable_count,
    )


def _thread(
    thread_id: str,
    *,
    head_sha: str = HEAD_A,
    state: cr.FindingState = "open",
    actionable: bool = True,
) -> cr.CodeRabbitThread:
    return cr.CodeRabbitThread(
        thread_id=thread_id,
        head_sha=head_sha,
        state=state,
        actionable=actionable,
    )


def _comment(
    comment_id: str,
    *,
    thread_id: str | None = None,
    head_sha: str = HEAD_A,
    state: cr.FindingState = "open",
    actionable: bool = True,
) -> cr.CodeRabbitComment:
    return cr.CodeRabbitComment(
        comment_id=comment_id,
        thread_id=thread_id,
        head_sha=head_sha,
        state=state,
        actionable=actionable,
    )


def _snapshot(
    observation_id: str,
    *,
    head_sha: str = HEAD_A,
    generation: int = 1,
    observed_at: int = NOW,
    check_status: cr.CheckStatus = "success",
    summary: cr.CodeRabbitReviewSummary | None = None,
    comments: tuple[cr.CodeRabbitComment, ...] = (),
    threads: tuple[cr.CodeRabbitThread, ...] = (),
) -> cr.CodeRabbitSnapshot:
    return cr.CodeRabbitSnapshot(
        provider="CodeRabbit",
        observation_id=observation_id,
        repository=REPO,
        pr_number=PR_NUMBER,
        head_sha=head_sha,
        review_generation=generation,
        observed_at=observed_at,
        check_status=check_status,
        summary=summary,
        comments=comments,
        threads=threads,
    )


def _record(
    conn,
    *,
    snapshot: cr.CodeRabbitSnapshot,
    current_head_sha: str,
    current_head_observed_at: int | None = None,
    now: int | None = None,
) -> cr.ObservationReceipt:
    return cr.record_snapshot(
        conn,
        snapshot=snapshot,
        current_head_sha=current_head_sha,
        current_head_observed_at=(
            snapshot.observed_at
            if current_head_observed_at is None
            else current_head_observed_at
        ),
        now=now,
    )


def test_schema_and_read_only_provider_protocol_are_additive(db_path: Path):
    snapshot = _snapshot("review-protocol", summary=_summary("clean"))
    provider = FakeCodeRabbitProvider(snapshot)

    observed = _read_provider(provider, head_sha=HEAD_A)

    assert observed is snapshot
    assert observed.repository == CANONICAL_REPO
    assert provider.calls == [(REPO, PR_NUMBER, HEAD_A)]
    with kb.connect(db_path) as conn:
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        indexes = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
        assert {
            "coderabbit_pr_heads",
            "coderabbit_review_observations",
            "coderabbit_head_assessments",
            "coderabbit_correction_attempts",
        } <= tables
        assert {
            "uq_coderabbit_observation_provider_id",
            "idx_coderabbit_observation_pr_head",
            "uq_coderabbit_correction_work",
            "uq_coderabbit_correction_attempt_number",
        } <= indexes
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_green_status_with_current_actionable_comment_and_thread_is_actionable(
    db_path: Path,
):
    snapshot = _snapshot(
        "review-actionable",
        summary=_summary("clean"),
        threads=(_thread("thread-current", actionable=False),),
        comments=(
            _comment(
                "comment-current",
                thread_id="thread-current",
                actionable=True,
            ),
        ),
    )

    direct = cr.assess_snapshot(snapshot, current_head_sha=HEAD_A, now=NOW)
    assert direct.state == "actionable"
    assert direct.actionable_count == 1
    assert direct.unresolved_count == 1
    assert direct.actionable_finding_ids == ("thread:thread-current",)
    assert direct.to_human_review_disposition() == {
        "status": "actionable",
        "disposition": "",
        "actionable_count": 1,
        "unresolved_count": 1,
    }

    with kb.connect(db_path) as conn:
        receipt = _record(
            conn,
            snapshot=snapshot,
            current_head_sha=HEAD_A,
            now=NOW,
        )
        assert receipt.created is True
        assert receipt.applied is True
        assert receipt.assessment.state == "actionable"
        assert (
            cr.get_current_assessment(
                conn,
                repository=REPO,
                pr_number=PR_NUMBER,
            )
            == receipt.assessment
        )


def test_clean_summary_with_resolved_outdated_superseded_and_nonblocking_history(
    db_path: Path,
):
    snapshot = _snapshot(
        "review-clean-history",
        summary=_summary("clean"),
        threads=(
            _thread("resolved", state="resolved"),
            _thread("outdated", state="outdated"),
            _thread("superseded", state="superseded"),
            _thread("non-blocking", actionable=False),
        ),
        comments=(
            _comment(
                "old-open-comment",
                head_sha=HEAD_B,
                actionable=True,
            ),
        ),
    )

    assessment = cr.assess_snapshot(snapshot, current_head_sha=HEAD_A, now=NOW)

    assert assessment.state == "no_actionable_comments"
    assert assessment.actionable_count == 0
    assert assessment.resolved_count == 1
    assert assessment.outdated_count == 2
    assert assessment.superseded_count == 1
    assert assessment.non_blocking_count == 1
    with kb.connect(db_path) as conn:
        receipt = _record(
            conn,
            snapshot=snapshot,
            current_head_sha=HEAD_A,
            now=NOW,
        )
        assert receipt.assessment.state == "no_actionable_comments"


def test_success_status_without_semantic_summary_remains_pending():
    assessment = cr.assess_snapshot(
        _snapshot("green-status-only"),
        current_head_sha=HEAD_A,
        now=NOW,
    )

    assert assessment.state == "pending"
    assert assessment.reason == "success_without_semantic_review_summary"


@pytest.mark.parametrize("status", ["rate_limited", "skipped"])
def test_limit_states_are_explicit_and_never_clean(
    status: cr.CheckStatus,
):
    assessment = cr.assess_snapshot(
        _snapshot(
            f"review-{status}",
            check_status=status,
        ),
        current_head_sha=HEAD_A,
        now=NOW,
    )

    assert assessment.state == status
    assert assessment.actionable_count == 0


def test_duplicate_review_event_and_semantic_replay_are_idempotent(db_path: Path):
    snapshot = _snapshot("review-duplicate", summary=_summary("clean"))
    semantic_replay = replace(
        snapshot,
        observation_id="review-duplicate-redelivery",
        observed_at=NOW,
    )

    with kb.connect(db_path) as conn:
        first = _record(
            conn,
            snapshot=snapshot,
            current_head_sha=HEAD_A,
            now=NOW,
        )
        duplicate_id = _record(
            conn,
            snapshot=snapshot,
            current_head_sha=HEAD_A,
            now=NOW + 1,
        )
        duplicate_snapshot = _record(
            conn,
            snapshot=semantic_replay,
            current_head_sha=HEAD_A,
            now=NOW + 2,
        )

        assert first.created is True and first.applied is True
        assert duplicate_id.outcome == "duplicate_observation"
        assert duplicate_id.created is False
        assert duplicate_snapshot.outcome == "duplicate_snapshot"
        assert duplicate_snapshot.created is True
        assert duplicate_snapshot.applied is False
        assert cr.list_observation_ids(
            conn,
            repository=REPO,
            pr_number=PR_NUMBER,
        ) == (
            "review-duplicate",
            "review-duplicate-redelivery",
        )


def test_reopened_actionable_snapshot_is_not_dropped_by_historical_deduplication(
    db_path: Path,
):
    actionable = _snapshot(
        "review-open",
        summary=_summary("clean"),
        threads=(_thread("reopened-thread"),),
    )
    resolved = _snapshot(
        "review-resolved",
        observed_at=NOW + 1,
        summary=_summary("clean"),
        threads=(_thread("reopened-thread", state="resolved"),),
    )
    reopened = replace(
        actionable,
        observation_id="review-reopened",
        observed_at=NOW + 2,
    )

    with kb.connect(db_path) as conn:
        assert (
            _record(
                conn,
                snapshot=actionable,
                current_head_sha=HEAD_A,
                now=NOW,
            ).assessment.state
            == "actionable"
        )
        assert (
            _record(
                conn,
                snapshot=resolved,
                current_head_sha=HEAD_A,
                now=NOW + 1,
            ).assessment.state
            == "no_actionable_comments"
        )
        final = _record(
            conn,
            snapshot=reopened,
            current_head_sha=HEAD_A,
            now=NOW + 2,
        )

        assert final.created is True
        assert final.applied is True
        assert final.assessment.state == "actionable"
        assert cr.list_observation_ids(
            conn,
            repository=REPO,
            pr_number=PR_NUMBER,
        ) == ("review-open", "review-resolved", "review-reopened")


def test_out_of_order_observation_cannot_roll_back_newer_assessment(db_path: Path):
    newer = _snapshot(
        "review-generation-3",
        generation=3,
        observed_at=NOW + 30,
        summary=_summary("clean"),
    )
    older = _snapshot(
        "review-generation-2-late-delivery",
        generation=2,
        observed_at=NOW + 40,
        summary=_summary("actionable", actionable_count=2),
    )

    with kb.connect(db_path) as conn:
        current = _record(
            conn,
            snapshot=newer,
            current_head_sha=HEAD_A,
            now=NOW + 30,
        )
        late = _record(
            conn,
            snapshot=older,
            current_head_sha=HEAD_A,
            now=NOW + 40,
        )

        assert current.assessment.state == "clean"
        assert late.created is True
        assert late.applied is False
        assert late.outcome == "out_of_order"
        assert late.assessment.state == "clean"
        assert late.assessment.review_generation == 3
        assert (
            conn.execute(
                "SELECT assessment_state FROM coderabbit_review_observations "
                "WHERE observation_id=?",
                (older.observation_id,),
            ).fetchone()[0]
            == "stale"
        )


def test_full_head_supersession_stales_old_assessment_until_new_head_review(
    db_path: Path,
):
    head_a_clean = _snapshot(
        "review-head-a",
        generation=1,
        summary=_summary("clean"),
    )
    stale_head_a_event = _snapshot(
        "review-head-a-after-push",
        generation=2,
        observed_at=NOW + 1,
        check_status="pending",
        summary=_summary("pending"),
    )
    head_b_clean = _snapshot(
        "review-head-b",
        head_sha=HEAD_B,
        generation=1,
        observed_at=NOW + 2,
        summary=_summary("clean"),
    )

    with kb.connect(db_path) as conn:
        _record(
            conn,
            snapshot=head_a_clean,
            current_head_sha=HEAD_A,
            now=NOW,
        )
        superseded = _record(
            conn,
            snapshot=stale_head_a_event,
            current_head_sha=HEAD_B,
            now=NOW + 1,
        )

        assert superseded.applied is False
        assert superseded.outcome == "head_superseded"
        assert superseded.assessment.state == "stale"
        assert (
            cr.get_current_assessment(
                conn,
                repository=REPO,
                pr_number=PR_NUMBER,
            )
            is None
        )
        old = cr.get_head_assessment(
            conn,
            repository=REPO,
            pr_number=PR_NUMBER,
            head_sha=HEAD_A,
        )
        assert old is not None and old.state == "stale"

        current = _record(
            conn,
            snapshot=head_b_clean,
            current_head_sha=HEAD_B,
            now=NOW + 2,
        )
        assert current.applied is True
        assert current.assessment.state == "clean"
        assert current.assessment.head_sha == HEAD_B


def test_older_trusted_head_observation_cannot_regress_current_head(db_path: Path):
    head_a = _snapshot(
        "review-head-a-first",
        summary=_summary("clean"),
    )
    head_b = _snapshot(
        "review-head-b-current",
        head_sha=HEAD_B,
        observed_at=NOW + 2,
        summary=_summary("clean"),
    )
    late_old_head = _snapshot(
        "review-head-a-late",
        generation=2,
        observed_at=NOW + 3,
        check_status="pending",
        summary=_summary("pending"),
    )

    with kb.connect(db_path) as conn:
        _record(
            conn,
            snapshot=head_a,
            current_head_sha=HEAD_A,
            current_head_observed_at=NOW,
            now=NOW,
        )
        _record(
            conn,
            snapshot=head_b,
            current_head_sha=HEAD_B,
            current_head_observed_at=NOW + 2,
            now=NOW + 2,
        )
        late = _record(
            conn,
            snapshot=late_old_head,
            current_head_sha=HEAD_A,
            current_head_observed_at=NOW + 1,
            now=NOW + 3,
        )

        assert late.applied is False
        assert late.assessment.state == "stale"
        current = cr.get_current_assessment(
            conn,
            repository=REPO,
            pr_number=PR_NUMBER,
        )
        assert current is not None
        assert current.head_sha == HEAD_B
        assert current.state == "clean"
        head_row = conn.execute(
            "SELECT current_head_sha, observed_at FROM coderabbit_pr_heads "
            "WHERE repository=? AND pr_number=?",
            (CANONICAL_REPO, PR_NUMBER),
        ).fetchone()
        assert head_row is not None
        assert (head_row["current_head_sha"], int(head_row["observed_at"])) == (
            HEAD_B,
            NOW + 2,
        )


def test_correction_metadata_is_one_bounded_work_item_per_pr_head(db_path: Path):
    actionable = _snapshot(
        "review-correction-1",
        summary=_summary("actionable", actionable_count=2),
        threads=(
            _thread("finding-a"),
            _thread("finding-b"),
        ),
    )
    next_generation = _snapshot(
        "review-correction-2",
        generation=2,
        observed_at=NOW + 1,
        summary=_summary("actionable", actionable_count=3),
        threads=(
            _thread("finding-a"),
            _thread("finding-b"),
            _thread("finding-c"),
        ),
    )

    with kb.connect(db_path) as conn:
        first = _record(
            conn,
            snapshot=actionable,
            current_head_sha=HEAD_A,
            now=NOW,
        ).assessment
        assert first.correction.max_attempts == 1
        assert first.correction.attempt_count == 0
        assert first.correction.correction_work_key.endswith(HEAD_A)

        reserved = cr.reserve_correction_attempt(
            conn,
            repository=REPO,
            pr_number=PR_NUMBER,
            head_sha=HEAD_A,
            loop_prevention_key=first.correction.loop_prevention_key,
            now=NOW,
        )
        duplicate = cr.reserve_correction_attempt(
            conn,
            repository=REPO,
            pr_number=PR_NUMBER,
            head_sha=HEAD_A,
            loop_prevention_key=first.correction.loop_prevention_key,
            now=NOW + 1,
        )
        assert reserved.created is True
        assert reserved.attempt_number == 1
        assert duplicate.created is False
        assert duplicate.reason == "duplicate_loop_key"

        newer = _record(
            conn,
            snapshot=next_generation,
            current_head_sha=HEAD_A,
            now=NOW + 1,
        ).assessment
        assert (
            newer.correction.correction_work_key == first.correction.correction_work_key
        )
        assert (
            newer.correction.loop_prevention_key != first.correction.loop_prevention_key
        )
        assert newer.correction.attempt_count == 1
        exhausted = cr.reserve_correction_attempt(
            conn,
            repository=REPO,
            pr_number=PR_NUMBER,
            head_sha=HEAD_A,
            loop_prevention_key=newer.correction.loop_prevention_key,
            now=NOW + 2,
        )
        assert exhausted.created is False
        assert exhausted.reason == "max_attempts_reached"
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM coderabbit_correction_attempts"
            ).fetchone()[0]
            == 1
        )
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_reused_observation_identity_with_changed_payload_is_rejected(db_path: Path):
    first = _snapshot("review-conflict", summary=_summary("clean"))
    changed = replace(
        first,
        summary=_summary("no_actionable_comments"),
    )

    with kb.connect(db_path) as conn:
        _record(
            conn,
            snapshot=first,
            current_head_sha=HEAD_A,
            now=NOW,
        )
        with pytest.raises(cr.CodeRabbitReplayConflict, match="observation_id"):
            _record(
                conn,
                snapshot=changed,
                current_head_sha=HEAD_A,
                now=NOW + 1,
            )


def test_full_sha_and_typed_summary_invariants_are_enforced():
    with pytest.raises(cr.CodeRabbitBoundaryError, match="full 40-character"):
        _snapshot("review-short-head", head_sha="deadbeef")
    with pytest.raises(cr.CodeRabbitBoundaryError, match="requires actionable_count"):
        _summary("actionable")
    with pytest.raises(cr.CodeRabbitBoundaryError, match="cannot report actionable"):
        _summary("clean", actionable_count=1)
