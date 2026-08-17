"""Read-only replay/drift verification for the ARCH-003 journal."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
from pathlib import Path
import sqlite3
from typing import Optional

from runtime_state.key_custody import AuthJsonKeyCustody, KeyUnavailable
from runtime_state.schema import DIGEST_PARAMETER_ID, TABLE_BUSINESS_KEY

OK = "OK"
DRIFT = "DRIFT"
UNKNOWN = "UNKNOWN"

_TERMINAL = {
    "completed", "failed", "cancelled", "succeeded", "approved", "denied", "expired", "disabled"
}


@dataclass(frozen=True)
class VerificationResult:
    status: str
    code: str
    events_checked: int = 0
    detail: str = ""


def _digest(key: bytes, *parts: object) -> str:
    payload = "\0".join(str(part) for part in parts).encode("utf-8")
    return hmac.new(key, payload, hashlib.sha256).hexdigest()[:32]


def _open_read_only(path: Path) -> sqlite3.Connection:
    uri = path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def verify_entity(db_path: str | Path, profile_name: str, table: str,
                  business_key: str, *, key_custody: Optional[AuthJsonKeyCustody] = None) -> VerificationResult:
    if table not in TABLE_BUSINESS_KEY:
        return VerificationResult(UNKNOWN, "HISTORY_MALFORMED", detail="unknown entity category")
    path = Path(db_path)
    custody = key_custody or AuthJsonKeyCustody(path.with_name("auth.json"))
    try:
        key = custody.load()
    except KeyUnavailable:
        return VerificationResult(UNKNOWN, "KEY_UNAVAILABLE")
    try:
        conn = _open_read_only(path)
        conn.execute("BEGIN DEFERRED")
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = {"runtime_state_journal", "runtime_state_journal_meta", table}
        if not required.issubset(tables):
            return VerificationResult(UNKNOWN, "UNSUPPORTED_VERSION", detail="journal schema is unavailable")
        meta = conn.execute("SELECT journal_schema_version, current_generation, downgrade_unsafe FROM runtime_state_journal_meta WHERE id=1").fetchone()
        if meta is None or int(meta[0]) != 1:
            return VerificationResult(UNKNOWN, "UNSUPPORTED_VERSION")
        current_generation = int(meta[1])
        if int(meta[2]):
            return VerificationResult(UNKNOWN, "GENERATION_MISMATCH")
        digest = _digest(key, profile_name, table, business_key)
        key_check = _digest(key, "hermes-runtime-state-key-check-v1")
        events = conn.execute(
            "SELECT event_kind, entity_digest, digest_parameter_id, key_check, origin_marker, entity_seq, "
            "lifecycle_state_before, lifecycle_state_after, owner_version_before, owner_version_after, "
            "writer_generation, materialized_write_counter_after, materialized_write_counter_before "
            "FROM runtime_state_journal WHERE profile_name=? AND entity_category=? AND entity_digest=? "
            "ORDER BY entity_seq", (profile_name, table, digest)).fetchall()
        key_column = TABLE_BUSINESS_KEY[table]
        row = conn.execute(f"SELECT * FROM {table} WHERE profile_name=? AND {key_column}=?",
                           (profile_name, business_key)).fetchone()
        if not events:
            if row is None:
                return VerificationResult(UNKNOWN, "EMPTY_HISTORY")
            return VerificationResult(UNKNOWN, "EMPTY_HISTORY", detail="materialized row has no journal")
        if row is None:
            return VerificationResult(UNKNOWN, "MATERIALIZED_STATE_ASYMMETRY", len(events))
        for expected, event in enumerate(events, 1):
            if event[0] not in {"genesis", "mutation", "baseline"} or event[5] != expected:
                return VerificationResult(UNKNOWN, "SEQUENCE_INVALID", len(events))
            if event[1] != digest:
                return VerificationResult(UNKNOWN, "HISTORY_MALFORMED", len(events))
            if event[2] != DIGEST_PARAMETER_ID:
                return VerificationResult(UNKNOWN, "DIGEST_PARAMETER_MISMATCH", len(events))
            if event[3] != key_check:
                return VerificationResult(UNKNOWN, "KEY_CHECK_MISMATCH", len(events))
            if event[10] != current_generation:
                return VerificationResult(UNKNOWN, "GENERATION_MISMATCH", len(events))
            if expected > 1 and event[6] in _TERMINAL and event[7] != event[6]:
                return VerificationResult(UNKNOWN, "POST_TERMINAL_EVENT", len(events))
        columns = [item[1] for item in conn.execute(f"PRAGMA table_info({table})")]
        values = dict(zip(columns, row))
        latest = events[-1]
        if values.get("materialized_writer_generation") != current_generation:
            return VerificationResult(UNKNOWN, "GENERATION_MISMATCH", len(events))
        if values.get("materialized_write_counter") != latest[11]:
            return VerificationResult(UNKNOWN, "WRITE_COUNTER_GAP", len(events))
        state_column = {"session_state": "status", "task_state": "status",
                        "approval_state": "approval_status", "compression_state": "compression_status"}[table]
        if values.get(state_column) != latest[7] or values.get("owner_version") != latest[9]:
            return VerificationResult(DRIFT, "DRIFT_DETECTED", len(events))
        return VerificationResult(OK, OK, len(events))
    except (OSError, sqlite3.DatabaseError) as exc:
        return VerificationResult(UNKNOWN, "SNAPSHOT_FAILURE", detail=type(exc).__name__)
    finally:
        try:
            conn.close()
        except UnboundLocalError:
            pass


def verify_all(db_path: str | Path, profile_name: str, *, key_custody: Optional[AuthJsonKeyCustody] = None):
    """Verify every materialized entity with a stable read-only snapshot."""
    path = Path(db_path)
    conn = _open_read_only(path)
    try:
        results = []
        for table, key_column in TABLE_BUSINESS_KEY.items():
            for row in conn.execute(f"SELECT {key_column} FROM {table} WHERE profile_name=?", (profile_name,)):
                results.append((table, row[0], verify_entity(path, profile_name, table, row[0], key_custody=key_custody)))
        return results
    finally:
        conn.close()
