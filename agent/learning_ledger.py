"""Profile-scoped evidence ledger for learned memory and skill changes.

The ledger is deliberately separate from the session database.  A mutable
candidate row provides transactional status and deduplication while an
append-only event table preserves the lifecycle audit trail.  Pending JSON
files in :mod:`tools.write_approval` remain the replay payload; this module
never applies memory or skill mutations.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Optional

from hermes_constants import get_hermes_home

SCHEMA_VERSION = 1
_MAX_EVIDENCE_CHARS = 500
_MAX_JSON_CHARS = 16_384
_RISKS = {"low", "medium", "high", "unknown"}
_CONFIDENCE = {"low", "medium", "high", "unknown"}
# Candidate IDs may be namespaced (for example ``pastoral:<uuid>``).
# Keep the namespace separator explicit rather than permitting arbitrary
# punctuation in IDs used by paths and CLI surfaces.
_CANDIDATE_ID_RE = re.compile(r"^(?:[A-Za-z0-9_-]+:)?[A-Za-z0-9_-]{1,64}$")
_VALID_STATUSES = {"pending", "applying", "active", "validated", "rolling_back", "rolled_back", "rejected"}
_VALID_TRANSITIONS = {
    "pending": {"applying", "active", "rejected"},
    "applying": {"pending", "active", "rejected"},
    "active": {"validated", "rolling_back"},
    "validated": {"rolling_back"},
    "rolling_back": {"rolled_back", "active", "validated"},
    "rolled_back": set(),
    "rejected": set(),
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ledger_path() -> Path:
    return get_hermes_home() / "learning" / "ledger.db"


def _validate_candidate_id(candidate_id: str) -> str:
    value = str(candidate_id or "")
    if not _CANDIDATE_ID_RE.fullmatch(value):
        raise ValueError("candidate id contains invalid characters")
    return value


def ledger_exists() -> bool:
    path = _ledger_path()
    return path.exists() and path.is_file() and not path.is_symlink()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _bounded_json(value: Any, *, label: str) -> str:
    text = _canonical_json(value)
    if len(text) > _MAX_JSON_CHARS:
        raise ValueError(f"{label} exceeds {_MAX_JSON_CHARS} characters")
    return text


def canonical_payload_fingerprint(subsystem: str, payload: Mapping[str, Any]) -> str:
    material = _canonical_json({"subsystem": subsystem, "payload": dict(payload)})
    return "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def candidate_dedup_key(subsystem: str, payload: Mapping[str, Any]) -> str:
    """Return an exact deterministic mutation identity.

    Only mutation semantics are included.  Presentation/provenance fields such
    as summaries, timestamps, candidate IDs, and evidence are intentionally
    absent because they must not make an equivalent proposal look new.
    """

    if subsystem == "memory":
        semantic_keys = ("action", "target", "content", "old_text", "operations")
    elif subsystem == "skills":
        semantic_keys = (
            "action",
            "name",
            "content",
            "old_string",
            "new_string",
            "replace_all",
            "file_path",
            "file_content",
            "absorbed_into",
        )
    else:
        semantic_keys = tuple(sorted(payload))
    semantic = {key: payload.get(key) for key in semantic_keys if key in payload}
    return canonical_payload_fingerprint(subsystem, semantic)


def sanitize_evidence(evidence: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    from agent.redact import redact_sensitive_text

    raw = dict(evidence or {})
    raw_excerpt = str(raw.get("excerpt") or "")
    excerpt_hash = "sha256:" + hashlib.sha256(raw_excerpt.encode("utf-8")).hexdigest()
    trust = str(raw.get("source_trust") or raw.get("trust") or "unknown")[:64]
    try:
        excerpt = redact_sensitive_text(raw_excerpt, force=True)
        hypothesis = redact_sensitive_text(str(raw.get("hypothesis") or ""), force=True)
    except Exception:
        excerpt = ""
        hypothesis = ""
    if trust in {"untrusted_external", "user_supplied_unverified", "unknown"}:
        excerpt = ""
    risk = str(raw.get("risk") or "unknown").lower()
    confidence = str(raw.get("confidence") or "unknown").lower()
    return {
        "status": str(raw.get("status") or "missing")[:64],
        "trigger": str(raw.get("trigger") or "unknown")[:64],
        "source_trust": trust,
        "excerpt": excerpt[:_MAX_EVIDENCE_CHARS],
        "excerpt_hash": excerpt_hash,
        "hypothesis": hypothesis[:_MAX_EVIDENCE_CHARS],
        "risk": risk if risk in _RISKS else "unknown",
        "confidence": confidence if confidence in _CONFIDENCE else "unknown",
    }


def _connect() -> sqlite3.Connection:
    from hermes_state import apply_wal_with_fallback

    path = _ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.is_symlink() or (path.exists() and path.is_symlink()):
        raise RuntimeError("learning ledger path must not be a symlink")
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    conn = sqlite3.connect(path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        current = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if current > SCHEMA_VERSION:
            raise RuntimeError(
                f"learning ledger uses newer schema {current}; this runtime supports {SCHEMA_VERSION}"
            )
        # Set the lock wait before journal-mode negotiation: two fresh
        # processes can initialize the same profile concurrently, and the WAL
        # pragma itself needs to honor the wait rather than fail immediately.
        conn.execute("PRAGMA busy_timeout=5000")
        apply_wal_with_fallback(conn, db_label="learning/ledger.db")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("BEGIN IMMEDIATE")
        _ensure_schema(conn)
        try:
            path.chmod(0o600)
        except OSError:
            pass
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(path) + suffix)
            if sidecar.exists():
                try:
                    sidecar.chmod(0o600)
                except OSError:
                    pass
    except Exception:
        conn.close()
        raise
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    current = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if current > SCHEMA_VERSION:
        raise RuntimeError(
            f"learning ledger uses newer schema {current}; this runtime supports {SCHEMA_VERSION}"
        )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS learning_candidates (
            candidate_id TEXT PRIMARY KEY,
            subsystem TEXT NOT NULL,
            action TEXT NOT NULL,
            status TEXT NOT NULL,
            payload_fingerprint TEXT NOT NULL,
            dedup_key TEXT NOT NULL,
            pending_relpath TEXT,
            proposal_json TEXT NOT NULL,
            source_json TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            precondition_json TEXT NOT NULL,
            revision INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS learning_dedup_latches (
            dedup_key TEXT PRIMARY KEY,
            candidate_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(candidate_id) REFERENCES learning_candidates(candidate_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS learning_events (
            event_id TEXT PRIMARY KEY,
            candidate_id TEXT NOT NULL,
            event TEXT NOT NULL,
            event_json TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            FOREIGN KEY(candidate_id) REFERENCES learning_candidates(candidate_id)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_learning_candidates_dedup
        ON learning_candidates(dedup_key, status)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_learning_events_candidate
        ON learning_events(candidate_id, occurred_at, event_id)
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS learning_events_no_update
        BEFORE UPDATE ON learning_events
        BEGIN SELECT RAISE(ABORT, 'learning events are immutable'); END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS learning_events_no_delete
        BEFORE DELETE ON learning_events
        BEGIN SELECT RAISE(ABORT, 'learning events are immutable'); END
        """
    )
    if current == 0:
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    conn.commit()


@contextmanager
def _transaction(*, immediate: bool = False) -> Iterator[sqlite3.Connection]:
    conn = _connect()
    try:
        if immediate:
            conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
    finally:
        conn.close()


def _event_row(
    candidate_id: str,
    event: str,
    detail: Optional[Mapping[str, Any]] = None,
    *,
    event_id: Optional[str] = None,
) -> tuple[str, str, str, str, str]:
    if not event:
        raise ValueError("event is required")
    occurred_at = _utc_now()
    from agent.redact import redact_sensitive_text

    def clean(value: Any, *, depth: int = 0) -> Any:
        if depth > 6:
            return "[TRUNCATED]"
        if isinstance(value, str):
            return redact_sensitive_text(value, force=True)[:_MAX_EVIDENCE_CHARS]
        if isinstance(value, Mapping):
            return {
                str(key)[:128]: clean(item, depth=depth + 1)
                for key, item in list(value.items())[:64]
            }
        if isinstance(value, (list, tuple)):
            return [clean(item, depth=depth + 1) for item in list(value)[:64]]
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return redact_sensitive_text(str(value), force=True)[:_MAX_EVIDENCE_CHARS]

    body = clean(dict(detail or {}))
    return event_id or uuid.uuid4().hex, candidate_id, event, _bounded_json(body, label="event detail"), occurred_at


def create_candidate(candidate: Mapping[str, Any]) -> dict[str, Any]:
    item = dict(candidate)
    candidate_id = _validate_candidate_id(str(item.get("candidate_id") or "").strip())
    subsystem = str(item.get("subsystem") or "").strip()
    action = str(item.get("action") or "").strip()
    if not subsystem or not action:
        raise ValueError("subsystem and action are required")
    now = _utc_now()
    evidence = sanitize_evidence(item.get("evidence"))
    proposal = dict(item.get("proposal") or {})
    source = dict(item.get("source") or {})
    precondition = dict(item.get("precondition") or {})
    status = str(item.get("status") or "pending")
    fingerprint = str(item.get("payload_fingerprint") or "")
    dedup_key = str(item.get("dedup_key") or fingerprint)
    pending_relpath = item.get("pending_relpath")

    with _transaction(immediate=True) as conn:
        conn.execute(
            """
            INSERT INTO learning_candidates(
                candidate_id, subsystem, action, status, payload_fingerprint,
                dedup_key, pending_relpath, proposal_json, source_json,
                evidence_json, precondition_json, revision, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
            """,
            (
                candidate_id,
                subsystem,
                action,
                status,
                fingerprint,
                dedup_key,
                str(pending_relpath) if pending_relpath else None,
                _bounded_json(proposal, label="proposal"),
                _bounded_json(source, label="source"),
                _bounded_json(evidence, label="evidence"),
                _bounded_json(precondition, label="precondition"),
                now,
                now,
            ),
        )
        conn.execute(
            "INSERT INTO learning_events(event_id, candidate_id, event, event_json, occurred_at) VALUES (?, ?, ?, ?, ?)",
            _event_row(candidate_id, "candidate_created", {"status": status}),
        )
        if str(source.get("origin") or "") == "background_review":
            conn.execute(
                "INSERT INTO learning_dedup_latches(dedup_key, candidate_id, created_at) VALUES (?, ?, ?)",
                (dedup_key, candidate_id, now),
            )
    created = get_candidate(candidate_id)
    if created is None:  # pragma: no cover - defensive
        raise RuntimeError("candidate transaction committed but row is unavailable")
    return created


def transition_candidate(
    candidate_id: str,
    *,
    from_status: str,
    to_status: str,
    event: str,
    detail: Optional[Mapping[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    candidate_id = _validate_candidate_id(candidate_id)
    event_row = _event_row(candidate_id, event, detail)
    now = _utc_now()
    with _transaction(immediate=True) as conn:
        if from_status not in _VALID_STATUSES or to_status not in _VALID_STATUSES:
            raise ValueError(f"invalid status: {from_status} -> {to_status}")
        if to_status not in _VALID_TRANSITIONS.get(from_status, set()):
            raise ValueError(f"invalid transition: {from_status} -> {to_status}")
        cursor = conn.execute(
            """
            UPDATE learning_candidates
            SET status = ?, revision = revision + 1, updated_at = ?
            WHERE candidate_id = ? AND status = ?
            """,
            (to_status, now, candidate_id, from_status),
        )
        if cursor.rowcount != 1:
            return None
        conn.execute(
            "INSERT INTO learning_events(event_id, candidate_id, event, event_json, occurred_at) VALUES (?, ?, ?, ?, ?)",
            event_row,
        )
    return get_candidate(candidate_id)


def _candidate_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "candidate_id": row["candidate_id"],
        "subsystem": row["subsystem"],
        "action": row["action"],
        "status": row["status"],
        "payload_fingerprint": row["payload_fingerprint"],
        "dedup_key": row["dedup_key"],
        "pending_relpath": row["pending_relpath"],
        "proposal": json.loads(row["proposal_json"]),
        "source": json.loads(row["source_json"]),
        "evidence": json.loads(row["evidence_json"]),
        "precondition": json.loads(row["precondition_json"]),
        "revision": row["revision"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def get_candidate(candidate_id: str) -> Optional[dict[str, Any]]:
    candidate_id = _validate_candidate_id(candidate_id)
    if not ledger_exists():
        return None
    with _transaction() as conn:
        row = conn.execute(
            "SELECT * FROM learning_candidates WHERE candidate_id = ?",
            (candidate_id,),
        ).fetchone()
    return _candidate_from_row(row) if row is not None else None


def list_candidates(*, statuses: Optional[Iterable[str]] = None) -> list[dict[str, Any]]:
    if not ledger_exists():
        return []
    params: list[Any] = []
    where = ""
    if statuses is not None:
        wanted = list(statuses)
        if not wanted:
            return []
        where = " WHERE status IN (" + ",".join("?" for _ in wanted) + ")"
        params.extend(wanted)
    with _transaction() as conn:
        rows = conn.execute(
            "SELECT * FROM learning_candidates" + where + " ORDER BY created_at, candidate_id",
            params,
        ).fetchall()
    return [_candidate_from_row(row) for row in rows]


def find_candidate_by_dedup(
    dedup_key: str,
    *,
    statuses: Optional[Iterable[str]] = None,
) -> Optional[dict[str, Any]]:
    if not ledger_exists():
        return None
    params: list[Any] = [dedup_key]
    where = "dedup_key = ?"
    if statuses is not None:
        wanted = list(statuses)
        if not wanted:
            return None
        where += " AND status IN (" + ",".join("?" for _ in wanted) + ")"
        params.extend(wanted)
    with _transaction() as conn:
        row = conn.execute(
            "SELECT * FROM learning_candidates WHERE " + where
            + " ORDER BY updated_at DESC, candidate_id DESC LIMIT 1",
            params,
        ).fetchone()
    return _candidate_from_row(row) if row is not None else None


_OUTCOMES = {
    "verification_succeeded",
    "verification_failed",
    "user_corrected",
    "retry_failed",
    "rolled_back",
    "goal_completed",
    "repeated_error_reduced",
}


def record_outcome(
    candidate_id: str,
    outcome: str,
    *,
    detail: Optional[Mapping[str, Any]] = None,
    attempt_id: Optional[str] = None,
) -> dict[str, Any]:
    """Append an outcome receipt and apply only explicit lifecycle effects."""
    if outcome not in _OUTCOMES:
        raise ValueError(f"unsupported learning outcome: {outcome}")
    candidate_id = _validate_candidate_id(candidate_id)
    stable_event_id = None
    if attempt_id:
        stable_event_id = "outcome-" + hashlib.sha256(
            f"{candidate_id}\0{outcome}\0{attempt_id}".encode("utf-8")
        ).hexdigest()
    event_row = _event_row(
        candidate_id,
        f"outcome_{outcome}",
        detail,
        event_id=stable_event_id,
    )
    with _transaction(immediate=True) as conn:
        if stable_event_id and conn.execute(
            "SELECT 1 FROM learning_events WHERE event_id = ?", (stable_event_id,)
        ).fetchone() is not None:
            row = conn.execute(
                "SELECT * FROM learning_candidates WHERE candidate_id = ?", (candidate_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"unknown learning candidate: {candidate_id}")
            return _candidate_from_row(row)
        row = conn.execute(
            "SELECT status FROM learning_candidates WHERE candidate_id = ?",
            (candidate_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown learning candidate: {candidate_id}")
        current = str(row["status"])
        new_status = current
        if outcome == "verification_succeeded" and current == "active":
            new_status = "validated"
        elif outcome == "rolled_back" and current in {"active", "validated", "rolling_back"}:
            new_status = "rolled_back"
        conn.execute(
            """
            UPDATE learning_candidates
            SET status = ?, revision = revision + 1, updated_at = ?
            WHERE candidate_id = ?
            """,
            (new_status, event_row[4], candidate_id),
        )
        conn.execute(
            "INSERT INTO learning_events(event_id, candidate_id, event, event_json, occurred_at) VALUES (?, ?, ?, ?, ?)",
            event_row,
        )
    candidate = get_candidate(candidate_id)
    if candidate is None:  # pragma: no cover
        raise RuntimeError("outcome committed but candidate is unavailable")
    return candidate


def list_events(*, candidate_id: Optional[str] = None, limit: Optional[int] = None) -> list[dict[str, Any]]:
    if not ledger_exists():
        return []
    where = ""
    params: list[Any] = []
    if candidate_id is not None:
        candidate_id = _validate_candidate_id(candidate_id)
        where = " WHERE candidate_id = ?"
        params.append(candidate_id)
    query = (
        "SELECT event_id, candidate_id, event, event_json, occurred_at "
        "FROM learning_events" + where + " ORDER BY occurred_at, event_id"
    )
    if limit is not None:
        query += " LIMIT ?"
        params.append(max(0, int(limit)))
    with _transaction() as conn:
        rows = conn.execute(query, params).fetchall()
    return [
        {
            "event_id": row["event_id"],
            "candidate_id": row["candidate_id"],
            "event": row["event"],
            "detail": json.loads(row["event_json"]),
            "occurred_at": row["occurred_at"],
        }
        for row in rows
    ]


def purge_candidate_evidence(candidate_id: str, *, reason: str = "retention") -> bool:
    """Remove persisted excerpts/hypotheses while retaining their digest and labels."""
    candidate_id = _validate_candidate_id(candidate_id)
    with _transaction(immediate=True) as conn:
        row = conn.execute(
            "SELECT evidence_json FROM learning_candidates WHERE candidate_id = ?", (candidate_id,)
        ).fetchone()
        if row is None:
            return False
        evidence = json.loads(row["evidence_json"])
        evidence["excerpt"] = ""
        evidence["hypothesis"] = ""
        evidence["status"] = "purged"
        now = _utc_now()
        conn.execute(
            "UPDATE learning_candidates SET evidence_json = ?, revision = revision + 1, updated_at = ? WHERE candidate_id = ?",
            (_bounded_json(evidence, label="evidence"), now, candidate_id),
        )
        conn.execute(
            "INSERT INTO learning_events(event_id, candidate_id, event, event_json, occurred_at) VALUES (?, ?, ?, ?, ?)",
            _event_row(candidate_id, "candidate_evidence_purged", {"reason": reason}),
        )
    return True


def purge_expired_evidence(*, retention_days: int = 30) -> int:
    """Apply the bounded local evidence-retention policy on explicit audit/review."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(0, int(retention_days)))
    purged = 0
    for candidate in list_candidates():
        try:
            created = datetime.fromisoformat(str(candidate["created_at"]).replace("Z", "+00:00"))
        except Exception:
            continue
        evidence = candidate.get("evidence", {})
        if created < cutoff and (evidence.get("excerpt") or evidence.get("hypothesis")):
            purged += int(purge_candidate_evidence(candidate["candidate_id"], reason="retention_expired"))
    return purged


def update_candidate_proposal_fields(
    candidate_id: str,
    fields: Mapping[str, Any],
) -> Optional[dict[str, Any]]:
    """Merge additional identity fields into a candidate's proposal JSON.

    Used by evaluation to bind skill_name and target_file into the ledger so
    rollback can verify snapshot identity after the pending JSON is deleted.
    """
    candidate_id = _validate_candidate_id(candidate_id)
    now = _utc_now()
    with _transaction(immediate=True) as conn:
        row = conn.execute(
            "SELECT proposal_json FROM learning_candidates WHERE candidate_id = ?",
            (candidate_id,),
        ).fetchone()
        if row is None:
            return None
        proposal = json.loads(str(row["proposal_json"]))
        proposal.update(fields)
        conn.execute(
            """
            UPDATE learning_candidates
            SET proposal_json = ?, revision = revision + 1, updated_at = ?
            WHERE candidate_id = ?
            """,
            (_bounded_json(proposal, label="proposal"), now, candidate_id),
        )
    return get_candidate(candidate_id)
