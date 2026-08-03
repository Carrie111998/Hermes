"""Durable mission-state backend for Kanban long-running missions.

Provides SQLite-backed persistent storage for mission lifecycle state,
an idempotent operation journal, and a typed Python API for creating,
reading, and atomically transitioning missions.

Schema version: ``kanban-mission-state/v1`` (matches R1 contract).

This module sits alongside the existing K9 kanban tables (tasks,
task_links, etc.) in the same SQLite database.  It adds two new tables
(``mission_missions`` and ``mission_journal``) via idempotent
``CREATE TABLE IF NOT EXISTS`` and never modifies or removes K9 tables.

Concurrency model: WAL mode + ``BEGIN IMMEDIATE`` for every write
transaction, matching the proven pattern in ``kanban_db.py``.

Ownership
---------
- Tables, migration, and API live here.
- K9 tables remain owned by ``kanban_db.py``.
- Schema/policy/fixtures live in the R1 contract repository.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCHEMA_VERSION = "kanban-mission-state/v1"
DOCUMENT_TYPE_STATE = "mission_state"
DOCUMENT_TYPE_TRANSITION = "mission_transition"
DOCUMENT_TYPE_RESULT = "mission_transition_result"

VALID_STATUSES = frozenset({
    "planned", "active", "blocked", "human_gate",
    "queue_exhausted", "failed", "completed",
})

VALID_PHASES = frozenset({
    "planning", "execution", "review", "correction", "closing", "terminal",
})

TERMINAL_STATUSES = frozenset({"failed", "completed"})
SUSPENDED_STATUSES = frozenset({"blocked", "human_gate", "queue_exhausted"})


# ---------------------------------------------------------------------------
# Canonical fingerprint
# ---------------------------------------------------------------------------

def canonical_fingerprint(value: Any) -> str:
    """Deterministic SHA-256 fingerprint over canonical JSON.

    Algorithm (per senior consultation):
      1. ``json.dumps(value, sort_keys=True, separators=(',',':'),
         ensure_ascii=False)``
      2. UTF-8 encode.
      3. SHA-256 hexdigest (64 lowercase hex chars).

    ``sort_keys`` makes the fingerprint independent of dict insertion
    order.  ``ensure_ascii=False`` preserves non-ASCII characters
    faithfully.  ``separators=(',',':')`` removes whitespace for a
    compact, unambiguous representation.
    """
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Typed result types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CreateResult:
    """Result of :func:`create_mission`."""
    outcome: str  # "created" | "already-applied" | "conflict"
    mission_id: str
    generation: int
    state_fingerprint: str
    state: Optional[dict] = None
    error: Optional[dict] = None


@dataclass(frozen=True)
class TransitionResult:
    """Result of :func:`compare_and_transition`."""
    outcome: str  # "transitioned" | "already-applied" | "conflict" | "stale" | "not-found" | "invalid" | "failed"
    mission_id: str
    operation_id: str
    request_fingerprint: str
    generation: int
    state_fingerprint: str
    state: Optional[dict] = None
    error: Optional[dict] = None


@dataclass(frozen=True)
class MissionRecord:
    """Persistent mission row from ``mission_missions``."""
    mission_id: str
    schema_version: str
    status: str
    phase: str
    generation: int
    state_json: str
    state_fingerprint: str
    created_at: int
    updated_at: int


@dataclass(frozen=True)
class JournalRecord:
    """Persistent journal row from ``mission_journal``."""
    mission_id: str
    operation_id: str
    request_fingerprint: str
    result_generation: int
    result_status: str
    result_fingerprint: str
    created_at: int


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

MISSION_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS mission_missions (
    mission_id        TEXT PRIMARY KEY,
    schema_version    TEXT NOT NULL,
    status            TEXT NOT NULL,
    phase             TEXT NOT NULL,
    generation        INTEGER NOT NULL DEFAULT 0,
    state_json        TEXT NOT NULL,
    state_fingerprint TEXT NOT NULL,
    created_at        INTEGER NOT NULL,
    updated_at        INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS mission_journal (
    mission_id         TEXT NOT NULL,
    operation_id       TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    result_generation  INTEGER NOT NULL,
    result_status      TEXT NOT NULL,
    result_fingerprint TEXT NOT NULL,
    created_at         INTEGER NOT NULL,
    PRIMARY KEY (mission_id, operation_id)
);
"""


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------

def migrate_mission_state(conn: sqlite3.Connection) -> None:
    """Idempotently add mission-state tables to an existing database.

    Safe to call on:
    - a fresh database (creates both tables);
    - an existing K9 database (adds tables without touching K9);
    - a database that already has the tables (no-op).

    This function never drops, renames, or alters K9 tables or columns.
    """
    conn.executescript(MISSION_SCHEMA_SQL)


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------

@contextmanager
def _write_txn(conn: sqlite3.Connection):
    """IMMEDIATE write transaction with rollback on exception."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        raise
    else:
        conn.execute("COMMIT")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _new_mission_id() -> str:
    """Generate a short, URL-safe mission id."""
    return "m_" + secrets.token_hex(4)


def _now_ms() -> int:
    """Current time in milliseconds since epoch."""
    return int(time.time() * 1000)


def _row_to_mission_record(row: sqlite3.Row) -> MissionRecord:
    return MissionRecord(
        mission_id=row["mission_id"],
        schema_version=row["schema_version"],
        status=row["status"],
        phase=row["phase"],
        generation=row["generation"],
        state_json=row["state_json"],
        state_fingerprint=row["state_fingerprint"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_journal_record(row: sqlite3.Row) -> JournalRecord:
    return JournalRecord(
        mission_id=row["mission_id"],
        operation_id=row["operation_id"],
        request_fingerprint=row["request_fingerprint"],
        result_generation=row["result_generation"],
        result_status=row["result_status"],
        result_fingerprint=row["result_fingerprint"],
        created_at=row["created_at"],
    )


def _validate_state_shape(state: dict) -> list[str]:
    """Validate that *state* matches the R1 mission_state document shape.

    Returns a list of error strings; empty means valid.
    This is a structural/schema check, not a semantic oracle.
    """
    errors: list[str] = []
    if state.get("document_type") != DOCUMENT_TYPE_STATE:
        errors.append("document_type must be 'mission_state'")
    if state.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be '{SCHEMA_VERSION}'")
    mission_id = state.get("mission_id")
    if not mission_id or not isinstance(mission_id, str):
        errors.append("mission_id must be a non-empty string")
    status = state.get("status")
    if status not in VALID_STATUSES:
        errors.append(f"status must be one of {sorted(VALID_STATUSES)}")
    phase = state.get("phase")
    if phase not in VALID_PHASES:
        errors.append(f"phase must be one of {sorted(VALID_PHASES)}")
    generation = state.get("generation")
    if not isinstance(generation, int) or generation < 0:
        errors.append("generation must be a non-negative integer")
    # Terminal status invariant
    if status in TERMINAL_STATUSES:
        if phase != "terminal":
            errors.append(f"terminal status '{status}' requires phase 'terminal'")
        if state.get("next_safe_action") is not None:
            errors.append(f"terminal status '{status}' requires null next_safe_action")
    return errors


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def create_mission(
    conn: sqlite3.Connection,
    *,
    state: dict,
    operation_id: str,
) -> CreateResult:
    """Create a new durable mission from a full mission_state document.

    The *state* must be a complete ``mission_state`` document matching
    the R1 schema.  The ``generation`` field in *state* is ignored;
    creation always sets generation to 0.

    Idempotency: if *operation_id* has already been applied for this
    mission, returns ``already-applied`` with the original result.
    Conflict: if *operation_id* exists with a different fingerprint,
    returns ``conflict``.
    """
    errors = _validate_state_shape(state)
    if errors:
        return CreateResult(
            outcome="invalid",
            mission_id=state.get("mission_id", ""),
            generation=0,
            state_fingerprint="",
            error={"code": "invalid", "message": "; ".join(errors)},
        )

    mission_id = state["mission_id"]
    # Force generation to 0 for creation
    state_copy = dict(state)
    state_copy["generation"] = 0
    state_fingerprint = canonical_fingerprint(state_copy)
    request_fingerprint = canonical_fingerprint({
        "operation_id": operation_id,
        "state": state_copy,
    })
    now = _now_ms()

    with _write_txn(conn):
        # Check for existing mission
        existing = conn.execute(
            "SELECT mission_id FROM mission_missions WHERE mission_id = ?",
            (mission_id,),
        ).fetchone()
        if existing is not None:
            # Mission already exists — check journal for idempotency
            journal_row = conn.execute(
                "SELECT operation_id, request_fingerprint, result_generation, "
                "result_status, result_fingerprint "
                "FROM mission_journal "
                "WHERE mission_id = ? AND operation_id = ?",
                (mission_id, operation_id),
            ).fetchone()
            if journal_row is not None:
                if journal_row["request_fingerprint"] == request_fingerprint:
                    return CreateResult(
                        outcome="already-applied",
                        mission_id=mission_id,
                        generation=journal_row["result_generation"],
                        state_fingerprint=journal_row["result_fingerprint"],
                    )
                else:
                    return CreateResult(
                        outcome="conflict",
                        mission_id=mission_id,
                        generation=0,
                        state_fingerprint="",
                        error={
                            "code": "conflict",
                            "message": f"operation_id '{operation_id}' already used with different content",
                        },
                    )
            else:
                # Mission exists but this operation_id is new — still a
                # creation conflict (can't re-create an existing mission)
                return CreateResult(
                    outcome="conflict",
                    mission_id=mission_id,
                    generation=0,
                    state_fingerprint="",
                    error={
                        "code": "conflict",
                        "message": f"mission '{mission_id}' already exists",
                    },
                )

        # Insert mission
        conn.execute(
            "INSERT INTO mission_missions "
            "(mission_id, schema_version, status, phase, generation, "
            " state_json, state_fingerprint, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                mission_id,
                SCHEMA_VERSION,
                state_copy["status"],
                state_copy["phase"],
                0,
                json.dumps(state_copy, sort_keys=True, ensure_ascii=False),
                state_fingerprint,
                now,
                now,
            ),
        )

        # Insert journal entry
        conn.execute(
            "INSERT INTO mission_journal "
            "(mission_id, operation_id, request_fingerprint, "
            " result_generation, result_status, result_fingerprint, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                mission_id,
                operation_id,
                request_fingerprint,
                0,
                "created",
                state_fingerprint,
                now,
            ),
        )

    return CreateResult(
        outcome="created",
        mission_id=mission_id,
        generation=0,
        state_fingerprint=state_fingerprint,
        state=state_copy,
    )


def get_mission(
    conn: sqlite3.Connection,
    mission_id: str,
) -> Optional[MissionRecord]:
    """Read the current durable state of a mission.

    Returns ``None`` if the mission does not exist.
    """
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT mission_id, schema_version, status, phase, generation, "
        "state_json, state_fingerprint, created_at, updated_at "
        "FROM mission_missions WHERE mission_id = ?",
        (mission_id,),
    ).fetchone()
    if row is None:
        return None
    return _row_to_mission_record(row)


def compare_and_transition(
    conn: sqlite3.Connection,
    *,
    mission_id: str,
    expected_generation: int,
    operation_id: str,
    next_state: dict,
) -> TransitionResult:
    """Atomically compare-and-transition a mission.

    Implements optimistic concurrency control:

    1. Check journal for idempotency (same operation_id + fingerprint → replay).
    2. Verify the current generation matches *expected_generation*.
    3. Validate the next_state document.
    4. Within one IMMEDIATE transaction: update mission, write journal, increment
       generation.

    Outcomes:
    - ``transitioned``: generation advanced, new state persisted.
    - ``already-applied``: same operation_id + same fingerprint → replay.
    - ``conflict``: same operation_id + different fingerprint → rejected.
    - ``stale``: expected_generation doesn't match current.
    - ``not-found``: mission doesn't exist.
    - ``invalid``: next_state fails validation.
    - ``failed``: unexpected error.
    """
    errors = _validate_state_shape(next_state)
    if errors:
        return TransitionResult(
            outcome="invalid",
            mission_id=mission_id,
            operation_id=operation_id,
            request_fingerprint="",
            generation=0,
            state_fingerprint="",
            error={"code": "invalid", "message": "; ".join(errors)},
        )

    # Verify mission_id consistency
    if next_state.get("mission_id") != mission_id:
        return TransitionResult(
            outcome="invalid",
            mission_id=mission_id,
            operation_id=operation_id,
            request_fingerprint="",
            generation=0,
            state_fingerprint="",
            error={
                "code": "invalid",
                "message": f"next_state mission_id '{next_state.get('mission_id')}' "
                           f"does not match request mission_id '{mission_id}'",
            },
        )

    request_fingerprint = canonical_fingerprint({
        "operation_id": operation_id,
        "expected_generation": expected_generation,
        "next_state": next_state,
    })

    with _write_txn(conn):
        # 1. Check if mission exists
        current = conn.execute(
            "SELECT mission_id, generation, state_fingerprint, status "
            "FROM mission_missions WHERE mission_id = ?",
            (mission_id,),
        ).fetchone()
        if current is None:
            return TransitionResult(
                outcome="not-found",
                mission_id=mission_id,
                operation_id=operation_id,
                request_fingerprint=request_fingerprint,
                generation=0,
                state_fingerprint="",
                error={
                    "code": "not-found",
                    "message": f"mission '{mission_id}' does not exist",
                },
            )

        current_generation = current["generation"]

        # 2. Check journal for idempotency
        journal_row = conn.execute(
            "SELECT request_fingerprint, result_generation, "
            "result_status, result_fingerprint "
            "FROM mission_journal "
            "WHERE mission_id = ? AND operation_id = ?",
            (mission_id, operation_id),
        ).fetchone()

        if journal_row is not None:
            if journal_row["request_fingerprint"] == request_fingerprint:
                # Same operation + same fingerprint → replay
                return TransitionResult(
                    outcome="already-applied",
                    mission_id=mission_id,
                    operation_id=operation_id,
                    request_fingerprint=request_fingerprint,
                    generation=journal_row["result_generation"],
                    state_fingerprint=journal_row["result_fingerprint"],
                )
            else:
                # Same operation + different fingerprint → conflict
                return TransitionResult(
                    outcome="conflict",
                    mission_id=mission_id,
                    operation_id=operation_id,
                    request_fingerprint=request_fingerprint,
                    generation=current_generation,
                    state_fingerprint=current["state_fingerprint"],
                    error={
                        "code": "conflict",
                        "message": f"operation_id '{operation_id}' already used with different content",
                    },
                )

        # 3. CAS check: generation must match
        if current_generation != expected_generation:
            return TransitionResult(
                outcome="stale",
                mission_id=mission_id,
                operation_id=operation_id,
                request_fingerprint=request_fingerprint,
                generation=current_generation,
                state_fingerprint=current["state_fingerprint"],
                error={
                    "code": "stale",
                    "message": f"expected generation {expected_generation}, "
                               f"current is {current_generation}",
                },
            )

        # 4. Compute new state
        new_generation = current_generation + 1
        next_state_copy = dict(next_state)
        next_state_copy["generation"] = new_generation
        new_fingerprint = canonical_fingerprint(next_state_copy)
        now = _now_ms()

        # 5. Atomic update: mission + journal
        conn.execute(
            "UPDATE mission_missions SET "
            "status = ?, phase = ?, generation = ?, "
            "state_json = ?, state_fingerprint = ?, updated_at = ? "
            "WHERE mission_id = ?",
            (
                next_state_copy["status"],
                next_state_copy["phase"],
                new_generation,
                json.dumps(next_state_copy, sort_keys=True, ensure_ascii=False),
                new_fingerprint,
                now,
                mission_id,
            ),
        )

        conn.execute(
            "INSERT INTO mission_journal "
            "(mission_id, operation_id, request_fingerprint, "
            " result_generation, result_status, result_fingerprint, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                mission_id,
                operation_id,
                request_fingerprint,
                new_generation,
                next_state_copy["status"],
                new_fingerprint,
                now,
            ),
        )

    return TransitionResult(
        outcome="transitioned",
        mission_id=mission_id,
        operation_id=operation_id,
        request_fingerprint=request_fingerprint,
        generation=new_generation,
        state_fingerprint=new_fingerprint,
        state=next_state_copy,
    )


def list_journal(
    conn: sqlite3.Connection,
    mission_id: str,
) -> list[JournalRecord]:
    """List journal entries for a mission, ordered by creation time."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT mission_id, operation_id, request_fingerprint, "
        "result_generation, result_status, result_fingerprint, created_at "
        "FROM mission_journal WHERE mission_id = ? ORDER BY created_at",
        (mission_id,),
    ).fetchall()
    return [_row_to_journal_record(r) for r in rows]
