"""Safe inspection/recovery helpers for stuck session compression state."""

from __future__ import annotations

import datetime as _dt
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from hermes_state import SessionDB, _compression_lock_holder_process_is_dead


def _as_dict(row: Any) -> Optional[dict[str, Any]]:
    if row is None:
        return None
    return dict(row) if isinstance(row, sqlite3.Row) else dict(row)


def _iso(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    try:
        return _dt.datetime.fromtimestamp(float(ts)).isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError):
        return None


def _stamp() -> str:
    return _dt.datetime.now().strftime("%Y%m%d_%H%M%S")


def _backup_live_db(db: SessionDB, backup_path: Optional[Path] = None) -> str:
    """Create a consistent SQLite backup without raw-copying a live DB file."""

    source = Path(db.db_path)
    dest = backup_path or source.with_name(
        f"{source.name}.compression-recovery-backup-{_stamp()}"
    )
    if dest.exists():
        raise FileExistsError(f"backup path already exists: {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with db._lock:  # SessionDB owns this connection; keep the backup atomic.
        backup_conn = sqlite3.connect(dest)
        try:
            db._conn.backup(backup_conn)
        finally:
            backup_conn.close()
    return str(dest)


def inspect_stuck_compression(
    db: SessionDB,
    session_id: str,
    *,
    now: Optional[float] = None,
) -> dict[str, Any]:
    """Return diagnostics for a session that may be blocked by compression."""

    now = float(time.time() if now is None else now)
    with db._lock:
        session = _as_dict(
            db._conn.execute(
                """
                SELECT id, source, session_key, ended_at, end_reason,
                       compression_failure_cooldown_until,
                       compression_failure_error
                FROM sessions WHERE id = ?
                """,
                (session_id,),
            ).fetchone()
        )
        lock = _as_dict(
            db._conn.execute(
                """
                SELECT session_id, holder, acquired_at, expires_at
                FROM compression_locks WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
        )
        counts = _as_dict(
            db._conn.execute(
                """
                SELECT COUNT(*) AS total_messages,
                       SUM(CASE WHEN COALESCE(active, 1) = 1 THEN 1 ELSE 0 END)
                           AS active_messages,
                       SUM(CASE WHEN COALESCE(compacted, 0) = 1 THEN 1 ELSE 0 END)
                           AS compacted_messages
                FROM messages WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
        )
        route_rows = [
            dict(r)
            for r in db._conn.execute(
                "SELECT scope, session_key, entry_json, updated_at FROM gateway_routing"
            ).fetchall()
        ]

    route_matches: list[dict[str, Any]] = []
    route_session_key = session.get("session_key") if session else None
    for row in route_rows:
        try:
            entry = json.loads(row.get("entry_json") or "{}")
        except json.JSONDecodeError:
            entry = {}
        if entry.get("session_id") == session_id or (
            route_session_key and row.get("session_key") == route_session_key
        ):
            route_matches.append({
                "scope": row.get("scope") or "",
                "session_key": row.get("session_key"),
                "entry_session_id": entry.get("session_id"),
                "last_prompt_tokens": entry.get("last_prompt_tokens"),
                "updated_at": row.get("updated_at"),
            })

    lock_state = "missing"
    if lock:
        expires_at = float(lock.get("expires_at") or 0.0)
        holder = str(lock.get("holder") or "")
        if expires_at < now:
            lock_state = "expired"
        elif _compression_lock_holder_process_is_dead(holder):
            lock_state = "owner_dead"
        else:
            lock_state = "active"

    cooldown_state = "missing"
    cooldown_until = None
    if session and session.get("compression_failure_cooldown_until") is not None:
        cooldown_until = float(session["compression_failure_cooldown_until"])
        cooldown_state = "active" if cooldown_until > now else "expired"

    return {
        "session_id": session_id,
        "session_exists": session is not None,
        "session": session,
        "messages": counts or {},
        "compression_lock": lock,
        "compression_lock_state": lock_state,
        "compression_failure_cooldown_state": cooldown_state,
        "compression_failure_cooldown_until_iso": _iso(cooldown_until),
        "gateway_routing_matches": route_matches,
        "recoverable": bool(
            session is not None
            and lock_state in {"missing", "expired", "owner_dead"}
            and cooldown_state in {"missing", "expired"}
        ),
    }


def recover_stuck_compression(
    db: SessionDB,
    session_id: str,
    *,
    apply: bool = False,
    backup: bool = True,
    backup_path: Optional[Path] = None,
    now: Optional[float] = None,
) -> dict[str, Any]:
    """Clear stale compression blockers and stale route token counters.

    This deliberately does not summarize or delete transcript rows. It is the
    small, safe runtime recovery step: remove stale compression state, preserve
    history, and make the next normal compression/turn path possible.
    """

    now = float(time.time() if now is None else now)
    before = inspect_stuck_compression(db, session_id, now=now)
    report: dict[str, Any] = {
        "session_id": session_id,
        "applied": bool(apply),
        "before": before,
        "backup_path": None,
        "compression_locks_removed": 0,
        "cooldown_cleared": False,
        "gateway_routing_entries_reset": 0,
    }
    if not before["session_exists"]:
        report["error"] = "session not found"
        return report
    if before["compression_lock_state"] == "active":
        report["error"] = "active compression lock is still owned; refusing recovery"
        return report
    if before["compression_failure_cooldown_state"] == "active":
        report["error"] = "compression failure cooldown is still active; refusing recovery"
        return report
    if not apply:
        return report

    if backup:
        report["backup_path"] = _backup_live_db(db, backup_path)

    route_session_key = (before.get("session") or {}).get("session_key")

    def _do(conn):
        removed = conn.execute(
            "DELETE FROM compression_locks WHERE session_id = ?",
            (session_id,),
        ).rowcount
        cleared = conn.execute(
            """
            UPDATE sessions
               SET compression_failure_cooldown_until = NULL,
                   compression_failure_error = NULL
             WHERE id = ?
               AND compression_failure_cooldown_until IS NOT NULL
            """,
            (session_id,),
        ).rowcount > 0

        reset_routes = 0
        rows = conn.execute(
            "SELECT scope, session_key, entry_json FROM gateway_routing"
        ).fetchall()
        for row in rows:
            scope = row["scope"] if isinstance(row, sqlite3.Row) else row[0]
            session_key = row["session_key"] if isinstance(row, sqlite3.Row) else row[1]
            raw = row["entry_json"] if isinstance(row, sqlite3.Row) else row[2]
            try:
                entry = json.loads(raw or "{}")
            except json.JSONDecodeError:
                continue
            if entry.get("session_id") != session_id and session_key != route_session_key:
                continue
            if int(entry.get("last_prompt_tokens") or 0) <= 0:
                continue
            entry["last_prompt_tokens"] = 0
            conn.execute(
                """
                UPDATE gateway_routing
                   SET entry_json = ?, updated_at = ?
                 WHERE scope = ? AND session_key = ?
                """,
                (json.dumps(entry, sort_keys=True), now, scope, session_key),
            )
            reset_routes += 1
        return removed, cleared, reset_routes

    removed, cleared, reset_routes = db._execute_write(_do)
    report["compression_locks_removed"] = int(removed or 0)
    report["cooldown_cleared"] = bool(cleared)
    report["gateway_routing_entries_reset"] = int(reset_routes or 0)
    report["after"] = inspect_stuck_compression(db, session_id, now=now)
    return report
