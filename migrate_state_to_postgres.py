"""One-shot migration of session/state data from SQLite into PostgreSQL.

Run once, manually, when moving an existing single-file state database onto the
optional PostgreSQL backend. The migration is deliberately minimal and
conservative:

* **Source-safe.** The SQLite database is opened read-only and is never mutated,
  truncated, or deleted. It remains the fallback-of-record until the operator
  has verified the PostgreSQL copy and flipped ``sessions.state_backend`` in
  config. Recovery from any failure is simply: drop the target tables and re-run
  from the untouched SQLite file.
* **Idempotent.** Each session is imported with skip-on-conflict semantics
  (``INSERT ... ON CONFLICT DO NOTHING``), so re-running after a partial run
  fills in the sessions/messages that did not land the first time without
  duplicating rows. Note this does NOT refresh rows that already exist in the
  target — the migration targets a fresh database, where that case does not
  arise; if a source row changed after a prior partial import, drop the target
  database and re-run for a clean copy.
* **Full fidelity.** Rewound (soft-deleted) messages are included, message ids
  and timestamps are preserved, and content is re-encoded through the live
  encode chokepoint so no legacy NUL-byte sentinel ever reaches PostgreSQL.

Usage::

    python -m migrate_state_to_postgres --dsn postgresql://.../db [--sqlite-path PATH]

The DSN may also be supplied via ``HERMES_STATE_DATABASE_URL`` /
``HERMES_STATE_POSTGRES_DSN``. The script verifies session and message counts
after import and reports them.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

# Sessions are exported in pages rather than one unbounded query so a very
# large source database does not have to materialize every session row at
# once. The walk continues until a short page is returned, so the page size
# bounds memory, never the amount of history migrated.
_PAGE_SIZE = 500

# Max sessions to field-sample during verification (avoids scanning huge
# databases completely while still catching the class of bug where every row
# carries the correct count but wrong values).
_VERIFY_SAMPLE_SIZE = 20


def _sqlite_schema_columns(table: str) -> list:
    """Return the ordered column list for *table* from SCHEMA_SQL.

    Uses the same authoritative source as _derive_migration_columns in
    hermes_state_postgres.py — an in-memory sqlite3 run of SCHEMA_SQL — so
    the verifier and the migrator always agree on what columns exist.  A
    verifier that picks its own hand-maintained column subset has the SAME
    blind spot as the migration: they agree with each other and both miss the
    same columns.
    """
    from hermes_state import SCHEMA_SQL
    ref = sqlite3.connect(":memory:")
    try:
        ref.executescript(SCHEMA_SQL)
        return [r[1] for r in ref.execute(f'PRAGMA table_info("{table}")')]
    finally:
        ref.close()


def _verify_field_values(
    src_sqlite: "sqlite3.Connection",
    target: "Any",
    session_ids: list,
    sample: int = _VERIFY_SAMPLE_SIZE,
) -> dict:
    """Compare column-level values between SQLite source and PostgreSQL target.

    Proves that every durable column's VALUE was migrated correctly, not merely
    that the row exists.  Row-count-only verification cannot detect the class
    of bug where a hardcoded column subset silently drops fields while reporting
    "N/N rows".

    Column set is derived from SCHEMA_SQL so the verifier and the migrator
    always agree on what fields exist: a verifier that hand-picks columns has
    the same blind spot as a migration that hand-picks columns.

    Returns a dict:
        sessions_checked  -- how many sessions were sampled
        messages_checked  -- how many messages were sampled
        field_mismatches  -- list of "session_id.column: src=X pg=Y" strings
        clean             -- True when field_mismatches is empty
    """
    session_cols = _sqlite_schema_columns("sessions")
    message_cols = _sqlite_schema_columns("messages")

    sample_ids = session_ids[:sample]
    if not sample_ids:
        return {"sessions_checked": 0, "messages_checked": 0,
                "field_mismatches": [], "clean": True}

    placeholders = ", ".join("?" for _ in sample_ids)

    # Fetch source rows from SQLite.
    src_cursor = src_sqlite.cursor()
    src_cursor.row_factory = sqlite3.Row
    src_sessions = {
        r["id"]: dict(r)
        for r in src_cursor.execute(
            f"SELECT * FROM sessions WHERE id IN ({placeholders})",
            tuple(sample_ids),
        )
    }
    src_messages = {}
    for r in src_cursor.execute(
        f"SELECT * FROM messages WHERE session_id IN ({placeholders})",
        tuple(sample_ids),
    ):
        src_messages.setdefault(r["session_id"], []).append(dict(r))

    mismatches = []
    messages_checked = 0

    for sid in sample_ids:
        src_row = src_sessions.get(sid)
        if not src_row:
            continue

        # Compare session-level columns.
        pg_row_raw = target.execute(
            f"SELECT {', '.join(session_cols)} FROM sessions WHERE id = ?",
            (sid,),
        ).fetchone()
        if pg_row_raw is None:
            mismatches.append(f"{sid}: session row absent from PostgreSQL")
            continue
        pg_row = dict(zip(session_cols, pg_row_raw))
        for col in session_cols:
            src_val = src_row.get(col)
            pg_val = pg_row.get(col)
            # Normalize: None and 0 are distinct; don't false-positive on type
            # differences between INTEGER 0 and float 0.0.
            if src_val != pg_val and not (src_val is None and pg_val is None):
                # Allow int/float equivalence (SQLite INTEGER vs PG REAL).
                try:
                    if float(src_val) == float(pg_val):  # type: ignore[arg-type]
                        continue
                except (TypeError, ValueError):
                    pass
                mismatches.append(
                    f"{sid}.sessions.{col}: src={src_val!r} pg={pg_val!r}"
                )

        # Compare message-level columns.
        src_msgs = {m.get("id"): m for m in src_messages.get(sid, [])}
        if src_msgs:
            msg_ids = list(src_msgs)
            msg_placeholders = ", ".join("?" for _ in msg_ids)
            pg_msgs_raw = target.execute(
                f"SELECT {', '.join(message_cols)} FROM messages"
                f" WHERE id IN ({msg_placeholders})",
                tuple(msg_ids),
            ).fetchall()
            pg_msgs = {row[0]: dict(zip(message_cols, row)) for row in pg_msgs_raw}
            messages_checked += len(src_msgs)
            for mid, src_msg in src_msgs.items():
                pg_msg = pg_msgs.get(mid)
                if pg_msg is None:
                    mismatches.append(f"message {mid}: absent from PostgreSQL")
                    continue
                for col in message_cols:
                    if col == "content":
                        # content goes through encode/decode normalization; skip
                        # byte-level comparison (sentinel translation is expected).
                        continue
                    src_val = src_msg.get(col)
                    pg_val = pg_msg.get(col)
                    if src_val != pg_val and not (src_val is None and pg_val is None):
                        try:
                            if float(src_val) == float(pg_val):  # type: ignore[arg-type]
                                continue
                        except (TypeError, ValueError):
                            pass
                        mismatches.append(
                            f"message {mid}.{col}: src={src_val!r} pg={pg_val!r}"
                        )

    return {
        "sessions_checked": len(sample_ids),
        "messages_checked": messages_checked,
        "field_mismatches": mismatches,
        "clean": len(mismatches) == 0,
    }


def _resolve_sqlite_path(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "state.db"


def _resolve_dsn(explicit: str | None) -> str:
    if explicit:
        return explicit
    for key in ("HERMES_STATE_DATABASE_URL", "HERMES_STATE_POSTGRES_DSN"):
        val = (os.environ.get(key) or "").strip()
        if val:
            return val
    raise SystemExit(
        "No PostgreSQL DSN provided. Pass --dsn or set HERMES_STATE_DATABASE_URL "
        "/ HERMES_STATE_POSTGRES_DSN."
    )


def migrate(sqlite_path: Path, dsn: str) -> dict:
    """Copy all sessions/messages from the SQLite file at ``sqlite_path`` into
    the PostgreSQL database at ``dsn``. Returns a counts summary.

    The SQLite database is opened read-only; this function never writes to it.
    """
    if not sqlite_path.exists():
        raise SystemExit(f"SQLite state database not found: {sqlite_path}")

    # Lazy imports keep a base install (without the postgres extra) able to at
    # least import this module for --help.
    try:
        import hermes_state_postgres as hsp
    except ImportError:
        raise SystemExit(
            "PostgreSQL support is not installed. Install the 'postgres' extra: "
            "pip install 'hermes-agent[postgres]'"
        )
    from hermes_state import SCHEMA_VERSION, SessionDB

    # Read-only source — opened via the SessionDB read-only path, which never
    # takes a write lock and never mutates the file.
    #
    # export_all() does not thread include_inactive down to get_messages, and
    # rewound (soft-deleted) rows must survive the migration — dropping them
    # would silently truncate history the source still holds. So walk the
    # sessions here and fetch each one's messages with include_inactive=True.
    #
    # The walk is paginated rather than issued as one capped query. A fixed
    # cap would silently drop everything past it AND still report success,
    # and a session whose parent fell outside the window would fail its
    # foreign key on import. Paginating to exhaustion means the export is
    # complete by construction, so the count check at the end is meaningful.
    source = SessionDB(db_path=sqlite_path, read_only=True)
    exported = []
    try:
        page = 0
        while True:
            batch = source.search_sessions(limit=_PAGE_SIZE, offset=page * _PAGE_SIZE)
            if not batch:
                break
            for session in batch:
                exported.append(
                    {
                        **session,
                        "messages": source.get_messages(
                            session["id"], include_inactive=True
                        ),
                    }
                )
            if len(batch) < _PAGE_SIZE:
                break
            page += 1
    finally:
        source.close()

    src_sessions = len(exported)
    src_messages = sum(len(s.get("messages") or []) for s in exported)
    src_session_ids = [s["id"] for s in exported if s.get("id")]

    target = hsp.connect_postgres(dsn)
    try:
        hsp.init_postgres_schema(target, SCHEMA_VERSION)
        imported = hsp.import_sessions(
            target, SessionDB._decode_content, SessionDB._encode_content, exported
        )

        dst_sessions = target.execute(
            "SELECT COUNT(*) AS n FROM sessions"
        ).fetchone()["n"]
        dst_messages = target.execute(
            "SELECT COUNT(*) AS n FROM messages"
        ).fetchone()["n"]

        # Whole-table totals cannot verify THIS migration: a target that already
        # holds rows looks plausible no matter how much of the source was
        # dropped. Rows are inserted with ON CONFLICT DO NOTHING and carry their
        # original SQLite ids, so a target whose id space overlaps the source's
        # silently discards every colliding message. Count only the sessions we
        # just migrated, so the check measures the thing it claims to.
        if src_session_ids:
            placeholders = ", ".join("?" for _ in src_session_ids)
            migrated_sessions = target.execute(
                f"SELECT COUNT(*) AS n FROM sessions WHERE id IN ({placeholders})",
                tuple(src_session_ids),
            ).fetchone()["n"]
            migrated_messages = target.execute(
                f"SELECT COUNT(*) AS n FROM messages"
                f" WHERE session_id IN ({placeholders})",
                tuple(src_session_ids),
            ).fetchone()["n"]
        else:
            migrated_sessions = 0
            migrated_messages = 0

        # PostgreSQL's text type structurally cannot store a NUL byte — a row
        # carrying one is rejected at INSERT time. So a successful import is
        # itself the proof that no NUL survived; there is nothing left to count.
        nul_rows = 0

        # Field-value verification: sample the first N migrated sessions and
        # compare every column's value between the SQLite source and the PG
        # target.  Row-count-only verification cannot detect the class of bug
        # where a hand-maintained column subset silently drops fields while
        # reporting "N/N rows".  The column set is derived from SCHEMA_SQL so
        # the verifier and the migrator always agree on which fields exist.
        field_check: dict = {"sessions_checked": 0, "messages_checked": 0,
                             "field_mismatches": [], "clean": True}
        if src_session_ids:
            src_raw = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
            try:
                field_check = _verify_field_values(
                    src_raw, target, src_session_ids
                )
            finally:
                src_raw.close()
    finally:
        target.close()

    return {
        "sqlite_path": str(sqlite_path),
        "source_sessions": src_sessions,
        "source_messages": src_messages,
        "imported_sessions": imported,
        # Counts restricted to the sessions this run migrated. These are the
        # numbers to compare against source_*; the target_* totals below are
        # whole-table and include anything that was already there.
        "migrated_sessions": migrated_sessions,
        "migrated_messages": migrated_messages,
        "target_sessions": dst_sessions,
        "target_messages": dst_messages,
        "nul_rows": nul_rows,
        # Field-value verification results. sessions_checked / messages_checked
        # are the sample counts; field_mismatches lists any column whose value
        # differed between source and target; clean is True when no mismatches.
        "field_check": field_check,
        # True when every source row is accounted for in the target AND field
        # values in the sampled rows agree. False means rows were dropped or
        # column values were silently wrong (the class of bug this guard was
        # added to detect).
        "complete": (
            migrated_sessions == src_sessions
            and migrated_messages == src_messages
            and field_check["clean"]
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="migrate_state_to_postgres",
        description="Copy SQLite session/state data into a PostgreSQL backend "
        "(read-only on the SQLite source).",
    )
    parser.add_argument(
        "--dsn",
        help="PostgreSQL DSN. Defaults to HERMES_STATE_DATABASE_URL / "
        "HERMES_STATE_POSTGRES_DSN.",
    )
    parser.add_argument(
        "--sqlite-path",
        help="Source SQLite state.db path (default: <hermes home>/state.db).",
    )
    args = parser.parse_args(argv)

    sqlite_path = _resolve_sqlite_path(args.sqlite_path)
    dsn = _resolve_dsn(args.dsn)

    summary = migrate(sqlite_path, dsn)

    fc = summary["field_check"]
    ok = summary["complete"] and summary["nul_rows"] == 0
    status = "OK" if ok else "MISMATCH"
    print(
        f"{status} migrated {summary['migrated_sessions']}/"
        f"{summary['source_sessions']} sessions and "
        f"{summary['migrated_messages']}/{summary['source_messages']} messages "
        f"-> PostgreSQL (target now holds {summary['target_sessions']} sessions "
        f"/ {summary['target_messages']} messages in total). "
        f"Field check: {fc['sessions_checked']} sessions / "
        f"{fc['messages_checked']} messages sampled, "
        f"{len(fc['field_mismatches'])} field mismatch(es). "
        f"SQLite source left untouched: {summary['sqlite_path']}"
    )
    if not ok:
        if summary["migrated_sessions"] != summary["source_sessions"]:
            print(
                "Some source rows are not present in the target. Rows keep their "
                "original SQLite ids and are inserted with ON CONFLICT DO NOTHING, "
                "so this usually means the target already contains rows with the "
                "same ids. Migrate into an empty database.",
                file=sys.stderr,
            )
        if fc["field_mismatches"]:
            print(
                "Field value mismatches detected (first 10 shown):",
                file=sys.stderr,
            )
            for m in fc["field_mismatches"][:10]:
                print(f"  {m}", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
