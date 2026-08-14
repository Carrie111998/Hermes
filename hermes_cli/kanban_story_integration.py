"""Frozen row types for durable Epic-member integration intents.

This module deliberately owns no enqueue, claim, lifecycle, or Git behavior.
It only turns persisted rows into strict immutable values for later coordinator
cards to consume.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Literal, Mapping, TypeAlias, cast


IntegrationStatus: TypeAlias = Literal[
    "pending",
    "running",
    "prepared",
    "rework_required",
    "attention_required",
    "integrated",
    "superseded",
]

_INTEGRATION_STATUSES = frozenset(
    {
        "pending",
        "running",
        "prepared",
        "rework_required",
        "attention_required",
        "integrated",
        "superseded",
    }
)
_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
Row: TypeAlias = Mapping[str, object] | sqlite3.Row


@dataclass(frozen=True)
class IntegrationKey:
    epic_id: str
    story_id: str
    source_sha: str


@dataclass(frozen=True)
class IntegrationIntent:
    key: IntegrationKey
    source_branch: str
    review_run_id: int
    review_base_sha: str
    status: IntegrationStatus
    claim_lock: str | None
    claim_expires: int | None
    attempt_count: int
    target_pre_sha: str | None
    candidate_sha: str | None
    candidate_ref: str | None
    # Audit-only after the composite integration fact is durable. Later claim,
    # recovery, readiness, and invalidation paths must not depend on this event.
    verification_event_id: int | None
    last_failure_code: str | None
    created_at: int
    updated_at: int


def _value(row: Row, field: str) -> object:
    try:
        return row[field]
    except (KeyError, IndexError) as exc:
        raise ValueError(f"integration intent row is missing {field}") from exc


def _text(row: Row, field: str) -> str:
    value = _value(row, field)
    if not isinstance(value, str):
        raise ValueError(f"integration intent {field} must be text")
    return value


def _nullable_text(row: Row, field: str) -> str | None:
    value = _value(row, field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"integration intent {field} must be text or null")
    return value


def _integer(row: Row, field: str) -> int:
    value = _value(row, field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"integration intent {field} must be an integer")
    return value


def _nullable_integer(row: Row, field: str) -> int | None:
    value = _value(row, field)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"integration intent {field} must be an integer or null")
    return value


def _full_sha(row: Row, field: str, *, nullable: bool = False) -> str | None:
    value = _value(row, field)
    if nullable and value is None:
        return None
    if not isinstance(value, str) or _FULL_SHA_RE.fullmatch(value) is None:
        raise ValueError(f"integration intent {field} must be a full lowercase SHA")
    return value


def integration_intent_from_row(row: Row) -> IntegrationIntent:
    """Parse one persisted intent without normalizing malformed authority facts."""

    status = _text(row, "status")
    if status not in _INTEGRATION_STATUSES:
        raise ValueError(f"invalid integration intent status: {status!r}")

    source_sha = _full_sha(row, "source_sha")
    review_base_sha = _full_sha(row, "review_base_sha")
    assert source_sha is not None and review_base_sha is not None

    return IntegrationIntent(
        key=IntegrationKey(
            epic_id=_text(row, "epic_id"),
            story_id=_text(row, "story_id"),
            source_sha=source_sha,
        ),
        source_branch=_text(row, "source_branch"),
        review_run_id=_integer(row, "review_run_id"),
        review_base_sha=review_base_sha,
        status=cast(IntegrationStatus, status),
        claim_lock=_nullable_text(row, "claim_lock"),
        claim_expires=_nullable_integer(row, "claim_expires"),
        attempt_count=_integer(row, "attempt_count"),
        target_pre_sha=_full_sha(row, "target_pre_sha", nullable=True),
        candidate_sha=_full_sha(row, "candidate_sha", nullable=True),
        candidate_ref=_nullable_text(row, "candidate_ref"),
        verification_event_id=_nullable_integer(row, "verification_event_id"),
        last_failure_code=_nullable_text(row, "last_failure_code"),
        created_at=_integer(row, "created_at"),
        updated_at=_integer(row, "updated_at"),
    )
