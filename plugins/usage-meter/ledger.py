"""Installation-wide privacy-safe usage ledger (SQLite).

Lives outside any profile session database so every profile contributes to
one meter. Events never store prompts, completions, tool results, credentials,
or authorization material — only token buckets, route identity, and pricing
status.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from hermes_constants import get_default_hermes_root, get_hermes_home

_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    profile TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    api_mode TEXT NOT NULL DEFAULT '',
    platform TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL DEFAULT '',
    task_id TEXT NOT NULL DEFAULT '',
    api_request_id TEXT NOT NULL DEFAULT '',
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
    estimated_cost_usd REAL,
    pricing_status TEXT NOT NULL DEFAULT 'unpriced',
    pricing_source TEXT NOT NULL DEFAULT 'none',
    request_count INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_usage_events_ts ON usage_events(ts);
CREATE INDEX IF NOT EXISTS idx_usage_events_provider_model
    ON usage_events(provider, model);
CREATE INDEX IF NOT EXISTS idx_usage_events_profile ON usage_events(profile);
"""

_lock = threading.RLock()
_db_path_override: Optional[Path] = None


def set_db_path_override(path: Optional[Path]) -> None:
    """Test hook: force the ledger path (None restores default resolution)."""
    global _db_path_override
    with _lock:
        _db_path_override = Path(path) if path is not None else None


def default_db_path() -> Path:
    """Shared installation path: ``<hermes-root>/usage-meter/ledger.db``."""
    if _db_path_override is not None:
        return _db_path_override
    try:
        root = get_default_hermes_root()
    except Exception:
        root = get_hermes_home()
    return Path(root) / "usage-meter" / "ledger.db"


def _connect(path: Optional[Path] = None) -> sqlite3.Connection:
    db_path = Path(path) if path is not None else default_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(_SCHEMA)
    return conn


@contextmanager
def open_db(path: Optional[Path] = None) -> Iterator[sqlite3.Connection]:
    conn = _connect(path)
    try:
        yield conn
    finally:
        conn.close()


def append_event(event: Dict[str, Any], *, path: Optional[Path] = None) -> int:
    """Insert one privacy-safe event. Returns the new row id. Thread-safe."""
    row = {
        "ts": float(event.get("ts") or time.time()),
        "profile": str(event.get("profile") or ""),
        "provider": str(event.get("provider") or ""),
        "model": str(event.get("model") or ""),
        "api_mode": str(event.get("api_mode") or ""),
        "platform": str(event.get("platform") or ""),
        "session_id": str(event.get("session_id") or ""),
        "task_id": str(event.get("task_id") or ""),
        "api_request_id": str(event.get("api_request_id") or ""),
        "input_tokens": int(event.get("input_tokens") or 0),
        "output_tokens": int(event.get("output_tokens") or 0),
        "cache_read_tokens": int(event.get("cache_read_tokens") or 0),
        "cache_write_tokens": int(event.get("cache_write_tokens") or 0),
        "reasoning_tokens": int(event.get("reasoning_tokens") or 0),
        "estimated_cost_usd": event.get("estimated_cost_usd"),
        "pricing_status": str(event.get("pricing_status") or "unpriced"),
        "pricing_source": str(event.get("pricing_source") or "none"),
        "request_count": int(event.get("request_count") or 1),
    }
    with _lock:
        with open_db(path) as conn:
            cur = conn.execute(
                """
                INSERT INTO usage_events (
                    ts, profile, provider, model, api_mode, platform,
                    session_id, task_id, api_request_id,
                    input_tokens, output_tokens, cache_read_tokens,
                    cache_write_tokens, reasoning_tokens,
                    estimated_cost_usd, pricing_status, pricing_source,
                    request_count
                ) VALUES (
                    :ts, :profile, :provider, :model, :api_mode, :platform,
                    :session_id, :task_id, :api_request_id,
                    :input_tokens, :output_tokens, :cache_read_tokens,
                    :cache_write_tokens, :reasoning_tokens,
                    :estimated_cost_usd, :pricing_status, :pricing_source,
                    :request_count
                )
                """,
                row,
            )
            return int(cur.lastrowid or 0)


def _empty_bucket() -> Dict[str, Any]:
    return {
        "calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "reasoning_tokens": 0,
        "estimated_cost_usd": 0.0,
        "unpriced_calls": 0,
        "included_calls": 0,
        "priced_calls": 0,
    }


def _accumulate(bucket: Dict[str, Any], row: sqlite3.Row) -> None:
    bucket["calls"] += 1
    bucket["input_tokens"] += int(row["input_tokens"] or 0)
    bucket["output_tokens"] += int(row["output_tokens"] or 0)
    bucket["cache_read_tokens"] += int(row["cache_read_tokens"] or 0)
    bucket["cache_write_tokens"] += int(row["cache_write_tokens"] or 0)
    bucket["reasoning_tokens"] += int(row["reasoning_tokens"] or 0)
    status = (row["pricing_status"] or "unpriced").lower()
    if status == "unpriced" or status == "unknown":
        bucket["unpriced_calls"] += 1
    elif status == "included":
        bucket["included_calls"] += 1
        cost = row["estimated_cost_usd"]
        if cost is not None:
            bucket["estimated_cost_usd"] += float(cost)
    else:
        bucket["priced_calls"] += 1
        cost = row["estimated_cost_usd"]
        if cost is not None:
            bucket["estimated_cost_usd"] += float(cost)


def _finalize(bucket: Dict[str, Any]) -> Dict[str, Any]:
    inp = bucket["input_tokens"]
    cache = bucket["cache_read_tokens"]
    denom = inp + cache
    bucket["cache_hit_rate"] = (cache / denom) if denom else 0.0
    # Sub-cent precision preserved; UI formats.
    bucket["estimated_cost_usd"] = float(bucket["estimated_cost_usd"])
    bucket["has_unpriced"] = bucket["unpriced_calls"] > 0
    return bucket


def summarize(
    *,
    since_ts: Optional[float] = None,
    until_ts: Optional[float] = None,
    path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Aggregate events in an optional time window."""
    clauses: List[str] = []
    params: List[Any] = []
    if since_ts is not None:
        clauses.append("ts >= ?")
        params.append(float(since_ts))
    if until_ts is not None:
        clauses.append("ts < ?")
        params.append(float(until_ts))
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""

    with _lock:
        with open_db(path) as conn:
            rows = conn.execute(
                f"SELECT * FROM usage_events{where} ORDER BY ts ASC",
                params,
            ).fetchall()

    total = _empty_bucket()
    by_route: Dict[tuple, Dict[str, Any]] = {}
    for row in rows:
        _accumulate(total, row)
        key = (
            str(row["provider"] or ""),
            str(row["model"] or ""),
            str(row["api_mode"] or ""),
        )
        if key not in by_route:
            by_route[key] = _empty_bucket()
            by_route[key]["provider"] = key[0]
            by_route[key]["model"] = key[1]
            by_route[key]["api_mode"] = key[2]
        _accumulate(by_route[key], row)

    routes = [_finalize(b) for b in by_route.values()]
    routes.sort(
        key=lambda r: (
            r["estimated_cost_usd"],
            r["input_tokens"] + r["output_tokens"] + r["cache_read_tokens"],
        ),
        reverse=True,
    )
    return {
        "summary": _finalize(total),
        "routes": routes,
        "event_count": len(rows),
    }


def recent_events(
    *,
    limit: int = 50,
    path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    limit = max(1, min(int(limit), 500))
    with _lock:
        with open_db(path) as conn:
            rows = conn.execute(
                """
                SELECT id, ts, profile, provider, model, api_mode, platform,
                       session_id, task_id, input_tokens, output_tokens,
                       cache_read_tokens, cache_write_tokens, reasoning_tokens,
                       estimated_cost_usd, pricing_status, pricing_source,
                       request_count
                FROM usage_events
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
    return [dict(r) for r in rows]
