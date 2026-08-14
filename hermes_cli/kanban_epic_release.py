"""Frozen row types for immutable Epic release snapshots.

Lifecycle transitions, candidate preparation, and CI observation are introduced
by later cards. This module is intentionally limited to strict persistence
parsing.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Literal, Mapping, TypeAlias, cast


EpicReleaseStatus: TypeAlias = Literal[
    "awaiting_push",
    "ci_pending",
    "ci_failed",
    "released",
    "invalidated",
]

_EPIC_RELEASE_STATUSES = frozenset(
    {"awaiting_push", "ci_pending", "ci_failed", "released", "invalidated"}
)
_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
Row: TypeAlias = Mapping[str, object] | sqlite3.Row


@dataclass(frozen=True)
class EpicReleaseSnapshot:
    id: int
    epic_id: str
    epic_tip_sha: str
    target_branch: str
    target_pre_sha: str
    release_candidate_sha: str
    candidate_ref: str
    aggregate_verification_event_id: int
    repository_contract_digest: str
    status: EpicReleaseStatus
    pushed_sha: str | None
    created_at: int
    updated_at: int


@dataclass(frozen=True)
class EpicReleaseMember:
    snapshot_id: int
    epic_id: str
    story_id: str
    source_sha: str
    candidate_sha: str
    integrated_at: int


def _value(row: Row, field: str) -> object:
    try:
        return row[field]
    except (KeyError, IndexError) as exc:
        raise ValueError(f"Epic release row is missing {field}") from exc


def _text(row: Row, field: str) -> str:
    value = _value(row, field)
    if not isinstance(value, str):
        raise ValueError(f"Epic release {field} must be text")
    return value


def _integer(row: Row, field: str) -> int:
    value = _value(row, field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"Epic release {field} must be an integer")
    return value


def _full_sha(row: Row, field: str, *, nullable: bool = False) -> str | None:
    value = _value(row, field)
    if nullable and value is None:
        return None
    if not isinstance(value, str) or _FULL_SHA_RE.fullmatch(value) is None:
        raise ValueError(f"Epic release {field} must be a full lowercase SHA")
    return value


def epic_release_snapshot_from_row(row: Row) -> EpicReleaseSnapshot:
    """Parse one snapshot without normalizing malformed release authority."""

    status = _text(row, "status")
    if status not in _EPIC_RELEASE_STATUSES:
        raise ValueError(f"invalid Epic release status: {status!r}")

    epic_tip_sha = _full_sha(row, "epic_tip_sha")
    target_pre_sha = _full_sha(row, "target_pre_sha")
    release_candidate_sha = _full_sha(row, "release_candidate_sha")
    assert (
        epic_tip_sha is not None
        and target_pre_sha is not None
        and release_candidate_sha is not None
    )

    return EpicReleaseSnapshot(
        id=_integer(row, "id"),
        epic_id=_text(row, "epic_id"),
        epic_tip_sha=epic_tip_sha,
        target_branch=_text(row, "target_branch"),
        target_pre_sha=target_pre_sha,
        release_candidate_sha=release_candidate_sha,
        candidate_ref=_text(row, "candidate_ref"),
        aggregate_verification_event_id=_integer(
            row, "aggregate_verification_event_id"
        ),
        repository_contract_digest=_text(row, "repository_contract_digest"),
        status=cast(EpicReleaseStatus, status),
        pushed_sha=_full_sha(row, "pushed_sha", nullable=True),
        created_at=_integer(row, "created_at"),
        updated_at=_integer(row, "updated_at"),
    )


def epic_release_member_from_row(row: Row) -> EpicReleaseMember:
    """Parse one immutable member pin from a release snapshot."""

    source_sha = _full_sha(row, "source_sha")
    candidate_sha = _full_sha(row, "candidate_sha")
    assert source_sha is not None and candidate_sha is not None

    return EpicReleaseMember(
        snapshot_id=_integer(row, "snapshot_id"),
        epic_id=_text(row, "epic_id"),
        story_id=_text(row, "story_id"),
        source_sha=source_sha,
        candidate_sha=candidate_sha,
        integrated_at=_integer(row, "integrated_at"),
    )
