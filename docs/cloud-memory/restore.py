#!/usr/bin/env python3
"""Disaster recovery: MySQL mirror -> fresh state.db.

Rebuilds a Hermes state.db from the MySQL mirror database — for a new
machine, a corrupted local db, or a fresh profile that should inherit an
existing machine's history.

Rows are re-inserted with their ORIGINAL ids (so ids continue seamlessly
afterwards) and `sqlite_sequence` is bumped to max(id), which makes SQLite
autoincrement continue from max+1 instead of restarting at 1.

The base schema (tables, FTS triggers, runtime tables) is created by the
Hermes code itself when it opens a fresh state.db — so run this script
against a NEW/EMPTY state.db path before starting the gateway, or let the
gateway create it once and then restore into it.

Usage:
    python restore.py --db ~/.hermes/profiles/foo/state.db --machine desktop
    python restore.py --db ~/state.db --machine desktop --days 30
"""

import argparse
import os
import sqlite3
import sys
import time

# Allow running straight from a checkout: the mirror module lives in
# tools/ relative to this script's docs/cloud-memory/ location.
_here = os.path.dirname(os.path.abspath(__file__))
for _cand in (
    os.path.join(_here),                       # patch-kit layout (flat)
    os.path.join(_here, "..", "..", "tools"),  # fork layout (docs/cloud-memory -> tools)
):
    if os.path.isfile(os.path.join(_cand, "mysql_mirror.py")):
        sys.path.insert(0, os.path.normpath(_cand))
        break


def load_profile_env(hermes_home: str) -> None:
    """Load MYSQL_MIRROR_* vars from a Hermes profile .env if not set."""
    env_path = os.path.join(hermes_home, ".env")
    if not os.path.isfile(env_path):
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key.startswith("MYSQL_MIRROR_") and key not in os.environ:
                os.environ[key] = value.strip()


MESSAGE_COLS = [
    "id", "session_id", "role", "content", "tool_call_id", "tool_calls",
    "tool_name", "timestamp", "token_count", "finish_reason", "reasoning",
    "reasoning_content", "reasoning_details", "codex_reasoning_items",
    "codex_message_items", "platform_message_id", "observed", "active",
    "effect_disposition", "compacted", "api_content", "display_kind",
    "display_metadata",
]

SESSION_COLS = [
    "id", "source", "user_id", "model", "model_config", "system_prompt",
    "parent_session_id", "started_at", "ended_at", "end_reason",
    "message_count", "tool_call_count", "input_tokens", "output_tokens",
    "cache_read_tokens", "cache_write_tokens", "reasoning_tokens",
    "billing_provider", "billing_base_url", "billing_mode",
    "estimated_cost_usd", "actual_cost_usd", "cost_status", "cost_source",
    "pricing_version", "title", "api_call_count", "handoff_state",
    "handoff_platform", "handoff_error", "cwd", "rewind_count", "archived",
    "session_key", "chat_id", "chat_type", "thread_id", "display_name",
    "origin_json", "expiry_finalized", "git_branch", "git_repo_root",
    "compression_failure_cooldown_until", "compression_failure_error",
    "compression_fallback_streak", "compression_ineffective_count",
    "profile_name", "pinned",
]

USAGE_COLS = [
    "session_id", "model", "billing_provider", "billing_base_url",
    "billing_mode", "task", "api_call_count", "input_tokens",
    "output_tokens", "cache_read_tokens", "cache_write_tokens",
    "reasoning_tokens", "estimated_cost_usd", "actual_cost_usd",
    "cost_status", "cost_source", "first_seen", "last_seen",
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", required=True, help="path to state.db (SQLite)")
    ap.add_argument("--machine", required=True,
                    help="machine label to restore from (WHERE machine = ?)")
    ap.add_argument("--home", default=None,
                    help="Hermes profile home (for .env lookup); "
                         "default: --db's directory if it has a .env, else ~/.hermes")
    ap.add_argument("--days", type=float, default=None,
                    help="only restore sessions/messages from the last N days")
    ap.add_argument("--since", type=float, default=None,
                    help="only restore rows with timestamp >= this Unix epoch")
    ap.add_argument("--batch", type=int, default=500)
    args = ap.parse_args()

    db_path = os.path.abspath(os.path.expanduser(args.db))
    if not os.path.isfile(db_path):
        print(f"error: {db_path} does not exist. Create it first by letting "
              f"Hermes open the profile once (or copy a fresh empty schema), "
              f"then re-run.", file=sys.stderr)
        return 1

    home = args.home or os.environ.get("HERMES_HOME") or (
        os.path.dirname(db_path)
        if os.path.isfile(os.path.join(os.path.dirname(db_path), ".env"))
        else os.path.expanduser("~/.hermes"))
    load_profile_env(home)
    os.environ.setdefault("HERMES_HOME", home)

    import pymysql
    from mysql_mirror import _database_name, _env

    database = _database_name()
    my = pymysql.connect(
        host=_env("MYSQL_MIRROR_HOST"), port=int(_env("MYSQL_MIRROR_PORT", "3306")),
        user=_env("MYSQL_MIRROR_USER"), password=_env("MYSQL_MIRROR_PASSWORD"),
        database=database, charset="utf8mb4", autocommit=False,
        connect_timeout=5, read_timeout=120, write_timeout=120,
    )
    cur = my.cursor()
    print(f"restoring mysql db={database} machine={args.machine}")
    print(f"  -> {db_path}")

    lite = sqlite3.connect(db_path)
    lite.row_factory = sqlite3.Row
    lite.execute("PRAGMA foreign_keys = OFF")

    # ---- messages ----
    where = "machine = %s"
    params = [args.machine]
    if args.since is not None:
        where += " AND timestamp >= %s"
        params.append(args.since)
    if args.days is not None:
        where += " AND timestamp >= %s"
        params.append(time.time() - args.days * 86400)

    # restore whole sessions: find session ids in window, then all their msgs
    if args.days is not None or args.since is not None:
        cur.execute(f"SELECT id FROM sessions WHERE {where}", params)
        session_ids = [r[0] for r in cur.fetchall()]
    else:
        cur.execute("SELECT id FROM sessions WHERE machine = %s", (args.machine,))
        session_ids = [r[0] for r in cur.fetchall()]

    total = {}
    n = 0
    ins = ("INSERT OR REPLACE INTO messages ({}) VALUES ({})".format(
        ", ".join(MESSAGE_COLS), ", ".join(["?"] * len(MESSAGE_COLS))))
    for i in range(0, len(session_ids), 100):
        ids = session_ids[i:i + 100]
        ph = ", ".join(["%s"] * len(ids))
        cur.execute(
            f"SELECT {', '.join(MESSAGE_COLS)} FROM messages "
            f"WHERE machine = %s AND session_id IN ({ph})",
            [args.machine] + ids)
        rows = cur.fetchall()
        lite.executemany(ins, rows)
        lite.commit()
        n += len(rows)
        print(f"  messages {n}")
    total["messages"] = n

    # ---- sessions ----
    ins = ("INSERT OR REPLACE INTO sessions ({}) VALUES ({})".format(
        ", ".join(SESSION_COLS), ", ".join(["?"] * len(SESSION_COLS))))
    n = 0
    for i in range(0, len(session_ids), 100):
        ids = session_ids[i:i + 100]
        ph = ", ".join(["%s"] * len(ids))
        cur.execute(
            f"SELECT {', '.join(SESSION_COLS)} FROM sessions "
            f"WHERE machine = %s AND id IN ({ph})",
            [args.machine] + ids)
        rows = cur.fetchall()
        lite.executemany(ins, rows)
        lite.commit()
        n += len(rows)
    total["sessions"] = n

    # ---- session_model_usage ----
    cur.execute(
        f"SELECT {', '.join(USAGE_COLS)} FROM session_model_usage "
        f"WHERE machine = %s", (args.machine,))
    rows = cur.fetchall()
    ins = ("INSERT OR REPLACE INTO session_model_usage ({}) VALUES ({})".format(
        ", ".join(USAGE_COLS), ", ".join(["?"] * len(USAGE_COLS))))
    lite.executemany(ins, rows)
    lite.commit()
    total["session_model_usage"] = len(rows)

    # ---- bump sqlite_sequence so autoincrement continues from max(id)+1 ----
    # Only `messages` uses an AUTOINCREMENT id in Hermes; sessions.id is a
    # client-generated string.
    has_seq = lite.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name='sqlite_sequence'").fetchone()
    if has_seq:
        row = lite.execute("SELECT MAX(id) FROM messages").fetchone()
        max_id = row[0] if row else 0
        if max_id:
            existing = lite.execute(
                "SELECT seq FROM sqlite_sequence WHERE name = ?",
                ("messages",)).fetchone()
            if existing:
                if existing[0] < max_id:
                    lite.execute(
                        "UPDATE sqlite_sequence SET seq = ? WHERE name = ?",
                        (max_id, "messages"))
            else:
                lite.execute(
                    "INSERT INTO sqlite_sequence(name, seq) VALUES (?, ?)",
                    ("messages", max_id))
        lite.commit()

    lite.close()
    my.close()

    print("restore complete:")
    for k, v in total.items():
        print(f"  {k}: {v}")
    print("next steps: set MYSQL_MIRROR_MACHINE in the profile .env to the "
          "restored machine name, then start the gateway.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
