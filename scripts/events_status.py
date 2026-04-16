#!/usr/bin/env python3
"""Event bus operator inspection tool.

Reads the live SQLite event store and prints:
  - Total event counts (last 24h / 7d / all time)
  - Event type breakdown for the last 24h
  - Per-subscriber cursor position and lag (events behind head)
  - Top sources by event volume
  - Most recent 10 events

Honours HERMES_HOME so it picks up the profile-specific DB.  Read-only —
does not modify the database.

Usage:
    python scripts/events_status.py
    python scripts/events_status.py --tail 20         # show 20 recent events
    python scripts/events_status.py --since 1h        # last hour only
    HERMES_HOME=~/.hermes/profiles/main python scripts/events_status.py
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from hermes_constants import get_hermes_home


def _db_path() -> Path:
    return get_hermes_home() / "events" / "event_bus.db"


def _parse_since(spec: str) -> datetime:
    """Parse '1h', '24h', '7d' etc. into an absolute UTC cutoff."""
    if not spec:
        return datetime.now(timezone.utc) - timedelta(days=1)
    unit = spec[-1].lower()
    try:
        n = int(spec[:-1])
    except ValueError:
        raise SystemExit(f"bad --since value: {spec!r} (use e.g. '1h', '7d')")
    if unit == "h":
        delta = timedelta(hours=n)
    elif unit == "d":
        delta = timedelta(days=n)
    elif unit == "m":
        delta = timedelta(minutes=n)
    else:
        raise SystemExit(f"bad --since unit {unit!r} (use h, d, or m)")
    return datetime.now(timezone.utc) - delta


def _open_db() -> sqlite3.Connection:
    path = _db_path()
    if not path.exists():
        raise SystemExit(f"event_bus.db not found at {path}")
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def _section(title: str) -> None:
    print()
    print(title)
    print("-" * len(title))


def _count_events(conn: sqlite3.Connection, since_iso: str | None = None) -> int:
    if since_iso:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM events WHERE timestamp >= ?",
            (since_iso,),
        ).fetchone()
    else:
        row = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()
    return int(row["n"])


def _summary(conn: sqlite3.Connection) -> None:
    now = datetime.now(timezone.utc)
    h24 = (now - timedelta(hours=24)).isoformat()
    d7 = (now - timedelta(days=7)).isoformat()

    _section("SUMMARY")
    print(f"  Event bus DB: {_db_path()}")
    print(f"  Total events (all time):  {_count_events(conn):>6}")
    print(f"  Events in last 7 days:    {_count_events(conn, d7):>6}")
    print(f"  Events in last 24 hours:  {_count_events(conn, h24):>6}")


def _event_breakdown(conn: sqlite3.Connection, since_iso: str) -> None:
    _section(f"EVENT TYPES (since {since_iso[:19]}Z)")
    rows = conn.execute(
        """SELECT event_type, priority, COUNT(*) AS n
           FROM events WHERE timestamp >= ?
           GROUP BY event_type, priority
           ORDER BY n DESC, event_type""",
        (since_iso,),
    ).fetchall()
    if not rows:
        print("  (no events)")
        return
    for r in rows:
        print(f"  {r['n']:>5}  [{r['priority']:8s}]  {r['event_type']}")


def _top_sources(conn: sqlite3.Connection, since_iso: str, limit: int = 10) -> None:
    _section(f"TOP SOURCES (since {since_iso[:19]}Z)")
    rows = conn.execute(
        """SELECT source, COUNT(*) AS n
           FROM events WHERE timestamp >= ?
           GROUP BY source ORDER BY n DESC LIMIT ?""",
        (since_iso, limit),
    ).fetchall()
    if not rows:
        print("  (no sources)")
        return
    for r in rows:
        print(f"  {r['n']:>5}  {r['source']}")


def _subscriber_lag(conn: sqlite3.Connection) -> None:
    _section("SUBSCRIBER LAG")
    subs = conn.execute(
        "SELECT subscriber_id, last_rowid, updated_at FROM subscriber_cursors"
    ).fetchall()
    if not subs:
        print("  (no subscribers have polled yet)")
        return
    head_row = conn.execute("SELECT MAX(rowid) AS head FROM events").fetchone()
    head = int(head_row["head"]) if head_row and head_row["head"] is not None else 0
    print(f"  {'subscriber_id':<25} {'cursor':>10} {'head':>10} {'lag':>8}   last_polled")
    for s in subs:
        lag = head - int(s["last_rowid"])
        indicator = " BEHIND" if lag > 100 else ""
        print(
            f"  {s['subscriber_id']:<25} "
            f"{s['last_rowid']:>10} {head:>10} {lag:>8}  "
            f"{s['updated_at']}{indicator}"
        )


def _recent_events(conn: sqlite3.Connection, limit: int) -> None:
    _section(f"RECENT EVENTS (last {limit})")
    rows = conn.execute(
        """SELECT timestamp, source, event_type, priority, payload
           FROM events ORDER BY rowid DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    if not rows:
        print("  (no events)")
        return
    for r in rows:
        ts = r["timestamp"][:19]
        # Extract a short payload summary (first useful field)
        try:
            p = json.loads(r["payload"]) if isinstance(r["payload"], str) else {}
        except (json.JSONDecodeError, TypeError):
            p = {}
        hint = ""
        for key in ("company", "title", "job_name", "platform", "status", "error"):
            if key in p and p[key]:
                hint = f"  {key}={str(p[key])[:50]}"
                break
        print(f"  {ts}  [{r['priority']:8s}]  {r['event_type']:30s}  {r['source']}{hint}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect the Hermes event bus")
    parser.add_argument(
        "--since", default="24h",
        help="Time window for breakdown: '1h', '24h', '7d' (default: 24h)",
    )
    parser.add_argument(
        "--tail", type=int, default=10,
        help="Number of recent events to show (default: 10)",
    )
    args = parser.parse_args()

    since_dt = _parse_since(args.since)
    since_iso = since_dt.isoformat()

    conn = _open_db()
    try:
        _summary(conn)
        _event_breakdown(conn, since_iso)
        _top_sources(conn, since_iso)
        _subscriber_lag(conn)
        _recent_events(conn, args.tail)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
