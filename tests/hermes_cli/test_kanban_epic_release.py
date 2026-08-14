"""Typed persistence tests for immutable Epic release snapshots."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
import sqlite3

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_epic_release import (
    EpicReadiness,
    EpicReleaseMember,
    EpicReleaseSnapshot,
    EpicTerminalSource,
    derive_epic_readiness,
    epic_release_member_from_row,
    epic_release_snapshot_from_row,
)


EPIC_SHA = "1" * 40
TARGET_SHA = "2" * 40
RELEASE_SHA = "3" * 40
SOURCE_SHA = "4" * 40
MEMBER_CANDIDATE_SHA = "5" * 40
PUSHED_SHA = "6" * 40
CONTRACT_DIGEST = "7" * 64


def _insert_snapshot(
    conn: sqlite3.Connection,
    *,
    epic_id: str = "epic-1",
    status: str = "awaiting_push",
    pushed_sha: str | None = None,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO epic_release_snapshots (
            epic_id, epic_tip_sha, target_branch, target_pre_sha,
            release_candidate_sha, candidate_ref,
            aggregate_verification_event_id, repository_contract_digest,
            status, pushed_sha, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            epic_id,
            EPIC_SHA,
            "main",
            TARGET_SHA,
            RELEASE_SHA,
            "refs/hermes/releases/epic-1",
            71,
            CONTRACT_DIGEST,
            status,
            pushed_sha,
            100,
            110,
        ),
    )
    return int(cursor.lastrowid)


def _insert_member(conn: sqlite3.Connection, snapshot_id: int) -> None:
    conn.execute(
        """
        INSERT INTO epic_release_members (
            snapshot_id, epic_id, story_id, source_sha,
            candidate_sha, integrated_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            snapshot_id,
            "epic-1",
            "story-1",
            SOURCE_SHA,
            MEMBER_CANDIDATE_SHA,
            90,
        ),
    )


def test_epic_release_schema_has_exact_snapshot_and_member_columns(tmp_path):
    with kb.connect(tmp_path / "fresh.db") as conn:
        snapshot_info = conn.execute(
            "PRAGMA table_info(epic_release_snapshots)"
        ).fetchall()
        member_info = conn.execute(
            "PRAGMA table_info(epic_release_members)"
        ).fetchall()

    assert tuple(row["name"] for row in snapshot_info) == (
        "id",
        "epic_id",
        "epic_tip_sha",
        "target_branch",
        "target_pre_sha",
        "release_candidate_sha",
        "candidate_ref",
        "aggregate_verification_event_id",
        "repository_contract_digest",
        "status",
        "pushed_sha",
        "created_at",
        "updated_at",
    )
    assert tuple(row["name"] for row in member_info) == (
        "snapshot_id",
        "epic_id",
        "story_id",
        "source_sha",
        "candidate_sha",
        "integrated_at",
    )
    assert {row["name"]: row["pk"] for row in member_info if row["pk"]} == {
        "snapshot_id": 1,
        "story_id": 2,
    }


def test_epic_release_schema_round_trips_frozen_snapshot_and_member(tmp_path):
    with kb.connect(tmp_path / "fresh.db") as conn:
        snapshot_id = _insert_snapshot(conn, status="ci_pending", pushed_sha=PUSHED_SHA)
        _insert_member(conn, snapshot_id)
        snapshot_row = conn.execute(
            "SELECT * FROM epic_release_snapshots WHERE id = ?", (snapshot_id,)
        ).fetchone()
        member_row = conn.execute(
            "SELECT * FROM epic_release_members WHERE snapshot_id = ?", (snapshot_id,)
        ).fetchone()

    snapshot = epic_release_snapshot_from_row(snapshot_row)
    member = epic_release_member_from_row(member_row)

    assert snapshot == EpicReleaseSnapshot(
        id=snapshot_id,
        epic_id="epic-1",
        epic_tip_sha=EPIC_SHA,
        target_branch="main",
        target_pre_sha=TARGET_SHA,
        release_candidate_sha=RELEASE_SHA,
        candidate_ref="refs/hermes/releases/epic-1",
        aggregate_verification_event_id=71,
        repository_contract_digest=CONTRACT_DIGEST,
        status="ci_pending",
        pushed_sha=PUSHED_SHA,
        created_at=100,
        updated_at=110,
    )
    assert member == EpicReleaseMember(
        snapshot_id=snapshot_id,
        epic_id="epic-1",
        story_id="story-1",
        source_sha=SOURCE_SHA,
        candidate_sha=MEMBER_CANDIDATE_SHA,
        integrated_at=90,
    )
    with pytest.raises(FrozenInstanceError):
        snapshot.status = "released"  # type: ignore[misc]


def test_epic_release_schema_allows_only_one_active_snapshot_per_epic(tmp_path):
    with kb.connect(tmp_path / "fresh.db") as conn:
        first_id = _insert_snapshot(conn)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_snapshot(conn, status="ci_failed")

        conn.execute(
            "UPDATE epic_release_snapshots SET status = 'invalidated' WHERE id = ?",
            (first_id,),
        )
        second_id = _insert_snapshot(conn, status="ci_failed")

    assert second_id != first_id


@pytest.mark.parametrize("status", ["pending", "done", ""])
def test_epic_release_schema_refuses_illegal_status(tmp_path, status):
    with kb.connect(tmp_path / "fresh.db") as conn:
        with pytest.raises(sqlite3.IntegrityError):
            _insert_snapshot(conn, status=status)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("epic_tip_sha", "abc"),
        ("target_pre_sha", "B" * 40),
        ("release_candidate_sha", "3" * 39),
        ("pushed_sha", "not-a-sha"),
        ("status", "pending"),
    ],
)
def test_epic_release_snapshot_parser_refuses_malformed_sha_or_status(field, value):
    row = {
        "id": 1,
        "epic_id": "epic-1",
        "epic_tip_sha": EPIC_SHA,
        "target_branch": "main",
        "target_pre_sha": TARGET_SHA,
        "release_candidate_sha": RELEASE_SHA,
        "candidate_ref": "refs/hermes/releases/epic-1",
        "aggregate_verification_event_id": 71,
        "repository_contract_digest": CONTRACT_DIGEST,
        "status": "awaiting_push",
        "pushed_sha": None,
        "created_at": 100,
        "updated_at": 100,
    }
    row[field] = value

    with pytest.raises(ValueError):
        epic_release_snapshot_from_row(row)


@pytest.mark.parametrize("field", ["source_sha", "candidate_sha"])
def test_epic_release_member_parser_refuses_malformed_sha(field):
    row = {
        "snapshot_id": 1,
        "epic_id": "epic-1",
        "story_id": "story-1",
        "source_sha": SOURCE_SHA,
        "candidate_sha": MEMBER_CANDIDATE_SHA,
        "integrated_at": 90,
    }
    row[field] = "short"

    with pytest.raises(ValueError):
        epic_release_member_from_row(row)


def _readiness_member(
    conn: sqlite3.Connection,
    *,
    source_sha: str = SOURCE_SHA,
    candidate_sha: str = MEMBER_CANDIDATE_SHA,
) -> tuple[str, str]:
    epic_id = kb.create_task(conn, title="Epic", work_item_kind="epic")
    story_id = kb.create_task(
        conn,
        title="Story",
        workflow_template_id="product",
        current_step_key="done",
    )
    kb.add_epic_membership(conn, epic_id=epic_id, task_id=story_id)
    conn.execute(
        "UPDATE tasks SET status='done', current_step_key='done', running=0, "
        "blocked=0, current_run_id=NULL WHERE id=?",
        (story_id,),
    )
    conn.execute(
        "INSERT INTO story_integration_intents ("
        "epic_id, story_id, source_sha, source_branch, review_run_id, "
        "review_base_sha, status, candidate_sha, created_at, updated_at"
        ") VALUES (?, ?, ?, 'story/one', 17, ?, 'integrated', ?, 90, 90)",
        (epic_id, story_id, source_sha, TARGET_SHA, candidate_sha),
    )
    conn.execute(
        "INSERT INTO epic_story_integrations "
        "(epic_id, story_id, source_sha, candidate_sha, integrated_at) "
        "VALUES (?, ?, ?, ?, 90)",
        (epic_id, story_id, source_sha, candidate_sha),
    )
    return epic_id, story_id


def _derive_ready(
    conn: sqlite3.Connection,
    epic_id: str,
    story_id: str,
    *,
    terminal_source_sha: str | None = SOURCE_SHA,
    governed_non_empty: bool = True,
    contains=lambda _descendant, _ancestor: True,
) -> EpicReadiness:
    return derive_epic_readiness(
        conn,
        epic_id,
        epic_tip_sha=EPIC_SHA,
        current_terminal_source=lambda requested: (
            EpicTerminalSource(terminal_source_sha, governed_non_empty)
            if requested == story_id and terminal_source_sha is not None
            else None
        ),
        commit_contains=contains,
    )


def test_fact_derived_readiness_accepts_exact_current_member_fact_and_candidate(tmp_path):
    with kb.connect(tmp_path / "ready.db") as conn:
        epic_id, story_id = _readiness_member(conn)

        result = _derive_ready(conn, epic_id, story_id)

    assert result.ready is True
    assert result.blockers == ()
    assert tuple(member.story_id for member in result.members) == (story_id,)
    assert result.members[0].source_sha == SOURCE_SHA
    assert result.members[0].candidate_sha == MEMBER_CANDIDATE_SHA


@pytest.mark.parametrize(
    ("mutate", "terminal_source_sha", "blocker"),
    [
        (
            lambda conn, _epic, story: conn.execute(
                "UPDATE tasks SET status='review', current_step_key='integration_pending' "
                "WHERE id=?",
                (story,),
            ),
            SOURCE_SHA,
            "nonterminal_member",
        ),
        (
            lambda conn, _epic, story: conn.execute(
                "INSERT INTO task_runs (task_id, step_key, status, started_at) "
                "VALUES (?, 'review', 'running', 100)",
                (story,),
            ),
            SOURCE_SHA,
            "active_review",
        ),
        (
            lambda conn, _epic, story: conn.execute(
                "INSERT INTO product_rework_directives ("
                "task_id, origin_kind, origin_phase, target_phase, findings_json, status, created_at"
                ") VALUES (?, 'integration', 'review', 'development', '[]', 'active', 100)",
                (story,),
            ),
            SOURCE_SHA,
            "active_directive",
        ),
        (
            lambda conn, epic, story: conn.execute(
                "INSERT INTO story_integration_intents ("
                "epic_id, story_id, source_sha, source_branch, review_run_id, review_base_sha, "
                "status, created_at, updated_at"
                ") VALUES (?, ?, ?, 'story/new', 18, ?, 'pending', 100, 100)",
                (epic, story, "8" * 40, TARGET_SHA),
            ),
            SOURCE_SHA,
            "active_intent",
        ),
        (lambda conn, _epic, _story: None, None, "missing_terminal_source"),
        (
            lambda conn, _epic, _story: None,
            "8" * 40,
            "missing_integration_fact",
        ),
        (
            lambda conn, _epic, story: conn.execute(
                "DELETE FROM epic_story_integrations WHERE story_id=?", (story,)
            ),
            SOURCE_SHA,
            "missing_integration_fact",
        ),
        (
            lambda conn, _epic, story: conn.execute(
                "DELETE FROM story_integration_intents WHERE story_id=?", (story,)
            ),
            SOURCE_SHA,
            "missing_integrated_intent",
        ),
        (
            lambda conn, _epic, story: conn.execute(
                "UPDATE epic_story_integrations SET candidate_sha='short' WHERE story_id=?",
                (story,),
            ),
            SOURCE_SHA,
            "invalid_candidate",
        ),
        (
            lambda conn, _epic, story: conn.execute(
                "UPDATE story_integration_intents SET candidate_sha=? WHERE story_id=?",
                ("9" * 40, story),
            ),
            SOURCE_SHA,
            "candidate_mismatch",
        ),
    ],
)
def test_fact_derived_readiness_reports_each_member_blocker(
    tmp_path, mutate, terminal_source_sha, blocker
):
    with kb.connect(tmp_path / f"{blocker}.db") as conn:
        epic_id, story_id = _readiness_member(conn)
        mutate(conn, epic_id, story_id)

        result = _derive_ready(
            conn,
            epic_id,
            story_id,
            terminal_source_sha=terminal_source_sha,
        )

    assert result.ready is False
    assert f"{story_id}:{blocker}" in result.blockers


def test_fact_derived_readiness_requires_governed_non_empty_contribution(tmp_path):
    with kb.connect(tmp_path / "empty.db") as conn:
        epic_id, story_id = _readiness_member(conn)
        result = _derive_ready(
            conn,
            epic_id,
            story_id,
            governed_non_empty=False,
        )

    assert result.ready is False
    assert result.blockers == (f"{story_id}:ungoverned_contribution",)


def test_fact_derived_readiness_requires_members(tmp_path):
    with kb.connect(tmp_path / "empty.db") as conn:
        epic_id = kb.create_task(conn, title="Empty Epic", work_item_kind="epic")

        result = derive_epic_readiness(
            conn,
            epic_id,
            epic_tip_sha=EPIC_SHA,
            current_terminal_source=lambda _story: None,
            commit_contains=lambda _descendant, _ancestor: True,
        )

    assert result.ready is False
    assert result.blockers == ("no_members",)


def test_fact_derived_readiness_requires_candidate_lineage_and_epic_containment(tmp_path):
    with kb.connect(tmp_path / "ancestry.db") as conn:
        epic_id, story_id = _readiness_member(conn)

        result = _derive_ready(
            conn,
            epic_id,
            story_id,
            contains=lambda descendant, ancestor: (
                descendant == EPIC_SHA and ancestor == MEMBER_CANDIDATE_SHA
            ),
        )

    assert result.ready is False
    assert result.blockers == (f"{story_id}:candidate_missing_source",)


def test_fact_derived_readiness_requires_epic_tip_to_contain_candidate(tmp_path):
    with kb.connect(tmp_path / "tip.db") as conn:
        epic_id, story_id = _readiness_member(conn)

        result = _derive_ready(
            conn,
            epic_id,
            story_id,
            contains=lambda descendant, ancestor: (
                descendant == MEMBER_CANDIDATE_SHA and ancestor == SOURCE_SHA
            ),
        )

    assert result.ready is False
    assert result.blockers == (f"{story_id}:epic_tip_missing_candidate",)


def test_fact_derived_readiness_blocks_when_ancestry_is_unavailable(tmp_path):
    def unavailable(_descendant, _ancestor):
        raise RuntimeError("repository unavailable")

    with kb.connect(tmp_path / "unavailable.db") as conn:
        epic_id, story_id = _readiness_member(conn)

        result = _derive_ready(
            conn,
            epic_id,
            story_id,
            contains=unavailable,
        )

    assert result.ready is False
    assert result.blockers == (f"{story_id}:ancestry_unavailable",)


def test_fact_derived_readiness_ignores_pruned_story_verification_events(tmp_path):
    with kb.connect(tmp_path / "pruned.db") as conn:
        epic_id, story_id = _readiness_member(conn)
        event_id = conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, 'repository_verification', '{}', 80)",
            (story_id,),
        ).lastrowid
        before = _derive_ready(conn, epic_id, story_id)
        conn.execute("DELETE FROM task_events WHERE id=?", (event_id,))

        after = _derive_ready(conn, epic_id, story_id)

    assert before == after
    assert after.ready is True
