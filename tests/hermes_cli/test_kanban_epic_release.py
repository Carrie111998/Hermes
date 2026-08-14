"""Typed persistence tests for immutable Epic release snapshots."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
import sqlite3

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_epic_release import (
    EpicReleaseMember,
    EpicReleaseSnapshot,
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
