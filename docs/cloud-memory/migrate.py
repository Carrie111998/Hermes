#!/usr/bin/env python3
"""One-time backfill (and catch-up): state.db -> MySQL mirror.

Reads all rows from a Hermes profile's SQLite state.db and copies them into
the MySQL mirror database. Safe to re-run: only rows with id > current MySQL
max id are copied for `messages`, and sessions/usages are UPSERTed, so this
script doubles as a catch-up after gateway downtime.

Configuration comes from the same MYSQL_MIRROR_* env vars (or profile .env)
used by the mirror module itself. See README for details.

Usage:
    python migrate.py --db ~/.hermes/profiles/foo/state.db
    python migrate.py --db ~/.hermes/state.db --machine laptop
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", required=True, help="path to state.db (SQLite)")
    ap.add_argument("--home", default=None,
                    help="Hermes profile home (for .env lookup); "
                         "default: parent of the profiles dir, or --db's "
                         "great-grandparent when laid out like "
                         "~/.hermes/profiles/<name>/state.db")
    ap.add_argument("--machine", default=None,
                    help="machine label; default: MYSQL_MIRROR_MACHINE env, "
                         "then hostname")
    ap.add_argument("--batch", type=int, default=500,
                    help="rows per commit (default 500)")
    args = ap.parse_args()

    db_path = os.path.abspath(os.path.expanduser(args.db))
    if not os.path.isfile(db_path):
        print(f"error: no such file: {db_path}", file=sys.stderr)
        return 1

    home = args.home
    if home is None:
        # ~/.hermes/profiles/<name>/state.db -> ~/.hermes/profiles/<name>
        guess = os.path.dirname(db_path)
        if os.path.isfile(os.path.join(guess, ".env")):
            home = guess
        else:
            home = os.path.expanduser("~/.hermes")
    load_profile_env(home)
    os.environ.setdefault("HERMES_HOME", home)

    import pymysql
    from mysql_mirror import _database_name, _env, _machine_id

    machine = args.machine or os.environ.get("MYSQL_MIRROR_MACHINE") or _machine_id()
    # The mirror module's own writers (mirror_session / mirror_usage) derive
    # the machine label from the env — keep it consistent with --machine.
    os.environ["MYSQL_MIRROR_MACHINE"] = machine
    database = _database_name()
    conn = pymysql.connect(
        host=_env("MYSQL_MIRROR_HOST"), port=int(_env("MYSQL_MIRROR_PORT", "3306")),
        user=_env("MYSQL_MIRROR_USER"), password=_env("MYSQL_MIRROR_PASSWORD"),
        database=database, charset="utf8mb4", autocommit=False,
        connect_timeout=5, read_timeout=60, write_timeout=60,
    )
    cur = conn.cursor()
    print(f"migrating {db_path}")
    print(f"  -> mysql db={database} machine={machine}")

    lite = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    lite.row_factory = sqlite3.Row

    total = {}

    # ---- messages: only id > current max (idempotent catch-up) ----
    cur.execute("SELECT COALESCE(MAX(id), 0) FROM messages WHERE machine = %s",
                (machine,))
    max_id = cur.fetchone()[0]
    rows = lite.execute(
        "SELECT * FROM messages WHERE id > ? ORDER BY id", (max_id,)).fetchall()
    cols = [d[0] for d in lite.execute("SELECT * FROM messages LIMIT 1").description] \
        if rows else []
    sql = ("REPLACE INTO messages ({}) VALUES ({})".format(
        ", ".join(["machine"] + cols),
        ", ".join(["%s"] * (len(cols) + 1))))
    n = 0
    for i in range(0, len(rows), args.batch):
        chunk = rows[i:i + args.batch]
        cur.executemany(sql, [
            tuple([machine] + [json_safe(r[c]) for c in cols]) for r in chunk])
        conn.commit()
        n += len(chunk)
        print(f"  messages {n}/{len(rows)}")
    total["messages"] = n

    # ---- sessions: full UPSERT (small table) ----
    from mysql_mirror import mirror_session
    rows = lite.execute("SELECT * FROM sessions").fetchall()
    for r in rows:
        mirror_session(dict(r))
    total["sessions"] = len(rows)

    # ---- session_model_usage: full UPSERT (small table) ----
    from mysql_mirror import mirror_usage
    rows = lite.execute("SELECT * FROM session_model_usage").fetchall()
    for r in rows:
        d = dict(r)
        mirror_usage(d.get("session_id"), d.get("model"), d)
    total["session_model_usage"] = len(rows)

    lite.close()
    conn.close()

    print("migration complete:")
    for k, v in total.items():
        print(f"  {k}: {v}")
    print("verify with: SELECT machine, MAX(id) FROM messages GROUP BY machine;")
    return 0


def json_safe(v):
    """Pass through; placeholder for possible type coercion."""
    return v


if __name__ == "__main__":
    sys.exit(main())
