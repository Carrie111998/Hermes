"""Last-resort page-level salvage for an unreadable session database schema.

``hermes sessions recover --allow-partial`` normally copies rows through SQL,
which requires the ``sessions`` and ``messages`` table *schemas* to be
readable. When the schema page itself is damaged, SQL-level salvage is
impossible — but the row payloads frequently survive on their b-tree pages.

The SQLite command-line shell ships a page-level ``.recover`` command that
walks raw pages and rebuilds rows it cannot attribute to a schema into
``lost_and_found`` tables of the shape::

    lost_and_found(rootpgno, pgno, nfield, id, c0, c1, ..., cN)

This module shells out to that CLI (it is a shell feature, NOT available via
the Python ``sqlite3`` module) and then heuristically maps ``lost_and_found``
rows back into a fresh current-schema Hermes session database.

Everything produced through this lane is explicitly **best effort**: column
mapping is heuristic (field counts plus sentinel values), fabricated parent
sessions are stubbed for orphaned child rows rather than deleting salvaged
data, and derived FTS indexes are rebuilt from scratch.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import subprocess
import tempfile
from math import isfinite
from pathlib import Path
from typing import Any, Optional

from hermes_cli.session_reference_contract import (
    ASYNC_DELEGATION_SESSION_COLUMNS,
    STATE_META_SESSION_NAMESPACES,
)

# Hermes session ids are timestamps: 20260812_135332_ab12cd. This is the
# strongest sentinel available for classifying schema-less rows.
SESSION_ID_PATTERN = re.compile(r"^\d{8}_\d{6}_")

MESSAGE_ROLES = frozenset({"user", "assistant", "tool", "system"})

# Values observed in sessions.source across gateway platforms and tooling.
KNOWN_SOURCES = frozenset({
    "cli", "telegram", "discord", "slack", "whatsapp", "signal", "matrix",
    "irc", "email", "x", "twitter", "api", "gateway", "web", "dashboard",
    "tool", "subagent", "cron", "recovered", "imported", "acp",
})

# Historical physical layouts of the sessions table. Columns are only ever
# appended (ALTER TABLE ADD COLUMN), so an older record is a strict prefix of
# the current column order.
SESSIONS_LAYOUT_NFIELDS = frozenset({55, 54, 52})
SESSIONS_LEGACY_MINIMAL_NFIELD = 14
SESSION_MODEL_USAGE_NFIELD = 18

# Plausible unix-epoch window for started_at heuristics on legacy layouts.
_EPOCH_LOW = 1_000_000_000.0   # 2001
_EPOCH_HIGH = 4_000_000_000.0  # 2096

SQLITE3_CLI_GUIDANCE = (
    "A last-resort page-level salvage is available when a `.recover`-capable "
    "`sqlite3` command-line shell is installed: its `.recover` command can "
    "rebuild rows into lost_and_found tables even when the table schemas are "
    "unreadable (this is a CLI-only feature, not part of Python's sqlite3 "
    "module, and some distro builds lack it — the shell must include the "
    "sqlite_dbpage extension, as the official builds from sqlite.org do). "
    "Install such a sqlite3 CLI (e.g. `brew install sqlite` or the "
    "precompiled sqlite-tools from sqlite.org) so it is on PATH, then re-run "
    "with --allow-partial."
)


class LostAndFoundError(RuntimeError):
    """Raised when the CLI .recover pass cannot produce a usable database."""


def find_sqlite3_cli() -> Optional[str]:
    """Return a ``.recover``-capable sqlite3 CLI path, or None.

    PATH presence is not enough: distro builds (e.g. Ubuntu's) can ship a
    sqlite3 shell compiled without the ``sqlite_dbpage`` virtual table that
    ``.recover`` requires — those fail every recovery with
    ``no such table: sqlite_dbpage``. Probe capability on a scratch DB once
    instead of discovering it mid-recovery.
    """

    binary = shutil.which("sqlite3")
    if binary is None:
        return None
    return binary if _cli_supports_recover(binary) else None


def _cli_supports_recover(binary: str) -> bool:
    """True when ``binary`` can run ``.recover`` (has sqlite_dbpage)."""

    scratch_dir = tempfile.mkdtemp(prefix="hermes-recover-probe-")
    scratch = Path(scratch_dir) / "probe.db"
    try:
        conn = sqlite3.connect(str(scratch))
        try:
            conn.execute("CREATE TABLE t (x)")
            conn.execute("INSERT INTO t VALUES (1)")
            conn.commit()
        finally:
            conn.close()
        probe = subprocess.run(
            [binary, "-readonly", str(scratch), ".recover"],
            capture_output=True,
            timeout=30,
        )
        if probe.returncode != 0:
            return False
        return b"sqlite_dbpage" not in probe.stderr
    except (OSError, subprocess.SubprocessError, sqlite3.Error):
        return False
    finally:
        shutil.rmtree(scratch_dir, ignore_errors=True)


def run_cli_lost_and_found_recover(
    source: Path,
    lf_path: Path,
    sqlite3_bin: str,
    *,
    timeout: float = 3600.0,
) -> dict[str, Any]:
    """Run ``sqlite3 <source> .recover`` streamed into a fresh scratch DB.

    ``--ignore-freelist`` avoids resurrecting deleted rows; older shells
    without that option fall back to a plain ``.recover``.
    """

    attempts: list[dict[str, Any]] = []
    for command in (".recover --ignore-freelist", ".recover"):
        if lf_path.exists():
            lf_path.unlink()
        dump = subprocess.Popen(
            [sqlite3_bin, "-readonly", str(source), command],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        load = subprocess.Popen(
            [sqlite3_bin, str(lf_path)],
            stdin=dump.stdout,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        assert dump.stdout is not None
        dump.stdout.close()  # let dump receive SIGPIPE if load dies
        try:
            _, load_err = load.communicate(timeout=timeout)
            dump_err = dump.stderr.read() if dump.stderr is not None else b""
            dump.wait(timeout=60)
        except subprocess.TimeoutExpired:
            dump.kill()
            load.kill()
            raise LostAndFoundError(
                f"sqlite3 .recover timed out after {timeout:.0f}s"
            )
        attempt = {
            "command": command,
            "dump_returncode": dump.returncode,
            "load_returncode": load.returncode,
            "dump_stderr_tail": dump_err.decode("utf-8", "replace")[-2000:],
            "load_stderr_tail": load_err.decode("utf-8", "replace")[-2000:],
        }
        attempts.append(attempt)
        if _lost_and_found_db_usable(lf_path):
            attempt["usable"] = True
            return {"binary": sqlite3_bin, "attempts": attempts}
        attempt["usable"] = False

    raise LostAndFoundError(
        "sqlite3 .recover did not produce a usable lost_and_found database: "
        + "; ".join(
            f"[{a['command']}] dump rc={a['dump_returncode']} "
            f"load rc={a['load_returncode']} "
            f"{a['dump_stderr_tail'] or a['load_stderr_tail']}".strip()
            for a in attempts
        )
    )


def _lost_and_found_db_usable(lf_path: Path) -> bool:
    if not lf_path.exists() or lf_path.stat().st_size == 0:
        return False
    try:
        conn = sqlite3.connect(str(lf_path))
        try:
            tables = [
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            ]
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        return False
    return bool(tables)


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")')]


def _notnull_defaults(conn: sqlite3.Connection, table: str) -> dict[int, Any]:
    """Map column index -> substitute value for NOT NULL columns.

    Page-level salvage can hand back records with NULL in positions that the
    live schema declares NOT NULL (torn cells, historical rows). Dropping the
    whole row over one damaged optional counter would defeat the lane, so
    NULLs in such positions are replaced by the schema default (or '' / 0
    when no default is declared).
    """

    substitutes: dict[int, Any] = {}
    for index, row in enumerate(conn.execute(f'PRAGMA table_info("{table}")')):
        if not row[3]:  # notnull flag
            continue
        default = row[4]
        if default is None:
            declared = str(row[2] or "").upper()
            substitutes[index] = 0 if ("INT" in declared or "REAL" in declared) else ""
            continue
        text = str(default)
        if text.startswith("'") and text.endswith("'"):
            substitutes[index] = text[1:-1]
        else:
            try:
                substitutes[index] = int(text)
            except ValueError:
                try:
                    substitutes[index] = float(text)
                except ValueError:
                    substitutes[index] = text
    return substitutes


def _is_session_id(value: Any) -> bool:
    return isinstance(value, str) and bool(SESSION_ID_PATTERN.match(value))


def _looks_like_source(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    return value in KNOWN_SOURCES or bool(re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", value))


def classify_lost_and_found_row(
    nfield: int,
    cells: tuple[Any, ...],
) -> Optional[str]:
    """Classify one lost_and_found record by field count + sentinel values.

    Returns a recognized canonical table name or None.
    """

    if (
        nfield == 4
        and len(cells) == 4
        and isinstance(cells[0], str)
        and bool(cells[0])
        and isinstance(cells[1], str)
        and bool(cells[1])
        and isinstance(cells[2], str)
        and re.fullmatch(r"[0-9a-f]{64}", cells[2]) is not None
        and type(cells[3]) in {int, float}
        and isfinite(float(cells[3]))
    ):
        return "cold_archive_tombstones"

    if len(cells) >= 3 and cells[0] is None:
        # Rowid-alias tables store their INTEGER PRIMARY KEY as NULL in the
        # record; messages is the only canonical table shaped like that with
        # a session id second and a role third.
        if (
            isinstance(cells[1], str)
            and cells[1]
            and isinstance(cells[2], str)
            and cells[2] in MESSAGE_ROLES
        ):
            return "messages"
        return None

    if not _is_session_id(cells[0] if cells else None):
        return None

    if nfield == SESSION_MODEL_USAGE_NFIELD:
        # 18 fields, session id first, model string second.
        if len(cells) > 1 and isinstance(cells[1], str) and cells[1]:
            return "session_model_usage"
        return None

    if nfield in SESSIONS_LAYOUT_NFIELDS or nfield == SESSIONS_LEGACY_MINIMAL_NFIELD:
        if len(cells) > 1 and _looks_like_source(cells[1]):
            return "sessions"
        return None

    # Unknown historical sessions layout: session-id first cell plus a
    # recognizable source string is still strong enough for a prefix map.
    if nfield >= 30 and len(cells) > 1 and _looks_like_source(cells[1]):
        return "sessions"
    return None


def _heuristic_started_at(cells: tuple[Any, ...]) -> float:
    for value in cells:
        if isinstance(value, (int, float)) and _EPOCH_LOW <= float(value) <= _EPOCH_HIGH:
            return float(value)
    return 0.0


def _insert_prefix_row(
    dest: sqlite3.Connection,
    table: str,
    dest_columns: list[str],
    values: list[Any],
    notnull_substitutes: Optional[dict[int, Any]] = None,
) -> bool:
    if notnull_substitutes:
        values = [
            notnull_substitutes[index]
            if value is None and index in notnull_substitutes
            else value
            for index, value in enumerate(values)
        ]
    columns = dest_columns[: len(values)]
    quoted = ", ".join(f'"{column}"' for column in columns)
    placeholders = ", ".join("?" for _ in columns)
    cursor = dest.execute(
        f'INSERT OR IGNORE INTO "{table}" ({quoted}) VALUES ({placeholders})',
        values,
    )
    return cursor.rowcount == 1


def _copy_direct_tables(
    lf_conn: sqlite3.Connection,
    dest: sqlite3.Connection,
) -> dict[str, int]:
    """Copy rows .recover managed to attribute to real canonical tables."""

    copied: dict[str, int] = {}
    for table in (
        "system_prompts",
        "sessions",
        "messages",
        "session_model_usage",
        "state_meta",
        "gateway_routing",
        "cold_archive_tombstones",
        "compression_locks",
        "async_delegations",
    ):
        source_columns = _table_columns(lf_conn, table)
        if not source_columns:
            continue
        dest_columns = _table_columns(dest, table)
        columns = [c for c in dest_columns if c in source_columns]
        if not columns:
            continue
        quoted = ", ".join(f'"{c}"' for c in columns)
        placeholders = ", ".join("?" for _ in columns)
        rows = lf_conn.execute(f'SELECT {quoted} FROM "{table}"').fetchall()
        if not rows:
            copied[table] = 0
            continue
        before = int(dest.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
        dest.executemany(
            f'INSERT OR IGNORE INTO "{table}" ({quoted}) VALUES ({placeholders})',
            rows,
        )
        after = int(dest.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
        copied[table] = after - before
    return copied


def reconcile_cold_archive_tombstones(
    destination: sqlite3.Connection,
    *,
    rejected_prompt_hashes: set[str] | None = None,
) -> dict[str, int]:
    """Remove recovered hot rows whose IDs carry cold-archive authority."""

    result = {
        "sessions_removed": 0,
        "sessions_parent_cleared": 0,
        "system_prompts_removed": 0,
        "gateway_routes_removed": 0,
        "gateway_routes_unverifiable": 0,
        "state_meta_removed": 0,
        "async_delegations_removed": 0,
        "messages_removed": 0,
        "session_model_usage_removed": 0,
        "compression_locks_removed": 0,
        "telegram_dm_topic_bindings_removed": 0,
    }
    if not _table_columns(destination, "cold_archive_tombstones"):
        return result

    destination.execute("SAVEPOINT reconcile_cold_archive_tombstones")
    try:
        tombstoned_ids = {
            str(row[0])
            for row in destination.execute(
                "SELECT session_id FROM cold_archive_tombstones"
            ).fetchall()
        }
        prompt_hashes = {
            str(row[0])
            for row in destination.execute(
                "SELECT DISTINCT system_prompt_hash FROM sessions "
                "WHERE id IN (SELECT session_id FROM cold_archive_tombstones) "
                "AND system_prompt_hash IS NOT NULL"
            ).fetchall()
        }
        prompt_hashes.update(rejected_prompt_hashes or ())
        parent_cursor = destination.execute(
            "UPDATE sessions SET parent_session_id = NULL "
            "WHERE parent_session_id IN ("
            "SELECT session_id FROM cold_archive_tombstones)"
        )
        result["sessions_parent_cleared"] = parent_cursor.rowcount

        if _table_columns(destination, "state_meta"):
            for namespace in STATE_META_SESSION_NAMESPACES:
                cursor = destination.execute(
                    "DELETE FROM state_meta WHERE key IN ("
                    "SELECT ? || ':' || session_id "
                    "FROM cold_archive_tombstones)",
                    (namespace,),
                )
                result["state_meta_removed"] += cursor.rowcount

        if _table_columns(destination, "async_delegations"):
            predicates = " OR ".join(
                f'"{column}" IN (SELECT session_id '
                "FROM cold_archive_tombstones)"
                for column in ASYNC_DELEGATION_SESSION_COLUMNS
            )
            cursor = destination.execute(
                f"DELETE FROM async_delegations WHERE {predicates}"
            )
            result["async_delegations_removed"] = cursor.rowcount

        if _table_columns(destination, "gateway_routing"):
            route_rows = destination.execute(
                "SELECT scope, session_key, entry_json FROM gateway_routing"
            ).fetchall()
            for scope, session_key, entry_json in route_rows:
                try:
                    entry = json.loads(entry_json)
                except (TypeError, json.JSONDecodeError):
                    result["gateway_routes_unverifiable"] += 1
                    continue
                session_id = entry.get("session_id") if isinstance(entry, dict) else None
                if not isinstance(session_id, str):
                    result["gateway_routes_unverifiable"] += 1
                    continue
                if session_id not in tombstoned_ids:
                    continue
                cursor = destination.execute(
                    "DELETE FROM gateway_routing WHERE scope = ? AND session_key = ?",
                    (scope, session_key),
                )
                result["gateway_routes_removed"] += cursor.rowcount

        for table, report_key in (
            ("messages", "messages_removed"),
            ("session_model_usage", "session_model_usage_removed"),
            ("compression_locks", "compression_locks_removed"),
            (
                "telegram_dm_topic_bindings",
                "telegram_dm_topic_bindings_removed",
            ),
        ):
            if not _table_columns(destination, table):
                continue
            cursor = destination.execute(
                f'DELETE FROM "{table}" WHERE session_id IN ('
                "SELECT session_id FROM cold_archive_tombstones)"
            )
            result[report_key] = cursor.rowcount

        sessions_cursor = destination.execute(
            "DELETE FROM sessions WHERE id IN ("
            "SELECT session_id FROM cold_archive_tombstones)"
        )
        result["sessions_removed"] = sessions_cursor.rowcount
        for prompt_hash in prompt_hashes:
            prompt_cursor = destination.execute(
                "DELETE FROM system_prompts WHERE hash = ? AND NOT EXISTS ("
                "SELECT 1 FROM sessions WHERE system_prompt_hash = ?)",
                (prompt_hash, prompt_hash),
            )
            result["system_prompts_removed"] += prompt_cursor.rowcount
        destination.execute("RELEASE SAVEPOINT reconcile_cold_archive_tombstones")
    except BaseException:
        destination.execute("ROLLBACK TO SAVEPOINT reconcile_cold_archive_tombstones")
        destination.execute("RELEASE SAVEPOINT reconcile_cold_archive_tombstones")
        raise
    return result


def map_lost_and_found_rows(
    lf_conn: sqlite3.Connection,
    dest: sqlite3.Connection,
) -> dict[str, Any]:
    """Best-effort mapping of a .recover output DB into a fresh SessionDB.

    Handles both rows .recover attributed to real tables and unattributed
    ``lost_and_found`` rows classified by field count + sentinel columns.
    """

    report: dict[str, Any] = {
        "direct_table_rows": {},
        "mapped": {
            "sessions": 0,
            "messages": 0,
            "session_model_usage": 0,
            "cold_archive_tombstones": 0,
        },
        "legacy_minimal_sessions": 0,
        "unmapped_rows": 0,
        "insert_conflicts": 0,
        "lost_and_found_tables": [],
    }
    rejected_session_prompts: set[tuple[str, str]] = set()

    dest.execute("BEGIN IMMEDIATE")
    try:
        report["direct_table_rows"] = _copy_direct_tables(lf_conn, dest)

        sessions_columns = _table_columns(dest, "sessions")
        messages_columns = _table_columns(dest, "messages")
        usage_columns = _table_columns(dest, "session_model_usage")
        tombstone_columns = _table_columns(dest, "cold_archive_tombstones")
        try:
            system_prompt_hash_index = sessions_columns.index("system_prompt_hash")
        except ValueError:
            system_prompt_hash_index = -1
        sessions_defaults = _notnull_defaults(dest, "sessions")
        messages_defaults = _notnull_defaults(dest, "messages")
        usage_defaults = _notnull_defaults(dest, "session_model_usage")
        # Never fabricate identity fields: a row whose session id / role /
        # source cell is genuinely NULL was already rejected by
        # classify_lost_and_found_row, so these substitutions only fill
        # NOT NULL bookkeeping counters and flag columns.
        for defaults, protected in (
            (sessions_defaults, (0, 1)),
            (messages_defaults, (1, 2)),
            (usage_defaults, (0, 1)),
        ):
            for index in protected:
                defaults.pop(index, None)

        lf_tables = [
            str(row[0])
            for row in lf_conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name LIKE 'lost_and_found%'"
            )
        ]
        report["lost_and_found_tables"] = lf_tables

        for lf_table in lf_tables:
            lf_columns = _table_columns(lf_conn, lf_table)
            if lf_columns[:3] != ["rootpgno", "pgno", "nfield"]:
                continue
            for row in lf_conn.execute(f'SELECT * FROM "{lf_table}"'):
                try:
                    nfield = int(row[2]) if row[2] is not None else 0
                except (TypeError, ValueError):
                    report["unmapped_rows"] += 1
                    continue
                lf_rowid = row[3]
                cells = tuple(row[4 : 4 + max(nfield, 0)])
                kind = classify_lost_and_found_row(nfield, cells)
                if kind is None:
                    report["unmapped_rows"] += 1
                    continue
                rejected_prompt_candidate: tuple[str, str] | None = None
                if (
                    kind == "sessions"
                    and system_prompt_hash_index >= 0
                    and len(cells) > system_prompt_hash_index
                    and isinstance(cells[0], str)
                    and isinstance(cells[system_prompt_hash_index], str)
                ):
                    rejected_prompt_candidate = (
                        cells[0],
                        cells[system_prompt_hash_index],
                    )
                try:
                    if kind == "cold_archive_tombstones":
                        inserted = _insert_prefix_row(
                            dest,
                            "cold_archive_tombstones",
                            tombstone_columns,
                            list(cells),
                        )
                    elif kind == "messages":
                        values = [lf_rowid, *cells[1 : min(nfield, len(messages_columns))]]
                        inserted = _insert_prefix_row(
                            dest, "messages", messages_columns, values,
                            messages_defaults,
                        )
                    elif kind == "session_model_usage":
                        values = list(cells[: len(usage_columns)])
                        inserted = _insert_prefix_row(
                            dest, "session_model_usage", usage_columns, values,
                            usage_defaults,
                        )
                    elif nfield == SESSIONS_LEGACY_MINIMAL_NFIELD:
                        # A pre-modern layout whose column order is unknown:
                        # salvage identity + timing rather than guessing 14
                        # positional meanings.
                        inserted = bool(
                            dest.execute(
                                "INSERT OR IGNORE INTO sessions "
                                "(id, source, started_at, title) "
                                "VALUES (?, ?, ?, ?)",
                                (
                                    cells[0],
                                    cells[1] if _looks_like_source(cells[1])
                                    else "recovered",
                                    _heuristic_started_at(cells),
                                    "[best-effort recovered] legacy session "
                                    "row (layout unknown)",
                                ),
                            ).rowcount
                            == 1
                        )
                        if inserted:
                            report["legacy_minimal_sessions"] += 1
                    else:
                        values = list(cells[: min(nfield, len(sessions_columns))])
                        inserted = _insert_prefix_row(
                            dest, "sessions", sessions_columns, values,
                            sessions_defaults,
                        )
                except sqlite3.DatabaseError:
                    if rejected_prompt_candidate is not None:
                        rejected_session_prompts.add(rejected_prompt_candidate)
                    report["unmapped_rows"] += 1
                    continue
                if inserted:
                    report["mapped"][
                        "sessions" if kind == "sessions" else kind
                    ] += 1
                else:
                    if rejected_prompt_candidate is not None:
                        rejected_session_prompts.add(rejected_prompt_candidate)
                    report["insert_conflicts"] += 1
        tombstoned_ids = {
            str(row[0])
            for row in dest.execute(
                "SELECT session_id FROM cold_archive_tombstones"
            ).fetchall()
        }
        rejected_prompt_hashes = {
            prompt_hash
            for session_id, prompt_hash in rejected_session_prompts
            if session_id in tombstoned_ids
        }
        report["tombstone_reconciliation"] = reconcile_cold_archive_tombstones(
            dest,
            rejected_prompt_hashes=rejected_prompt_hashes,
        )
        dest.execute("COMMIT")
    except BaseException:
        dest.execute("ROLLBACK")
        raise
    return report


def stub_missing_parent_sessions(dest: sqlite3.Connection) -> dict[str, Any]:
    """Fabricate placeholder parents for salvaged child rows.

    Salvaged children (messages, model-usage rows) are NEVER deleted for
    foreign-key cleanup — a fabricated parent is cheaper than losing the only
    surviving copy of the user's data. Stubs are clearly marked.
    """

    result: dict[str, Any] = {
        "sessions_stubbed": 0,
        "messages_retained": 0,
        "usage_rows_retained": 0,
    }
    dest.execute("BEGIN IMMEDIATE")
    try:
        orphan_ids: dict[str, dict[str, Any]] = {}
        for session_id, first_ts, count in dest.execute(
            "SELECT m.session_id, MIN(m.timestamp), COUNT(*) FROM messages AS m "
            "WHERE m.session_id IS NOT NULL AND NOT EXISTS "
            "(SELECT 1 FROM sessions WHERE sessions.id = m.session_id) "
            "GROUP BY m.session_id"
        ):
            orphan_ids[str(session_id)] = {
                "started_at": float(first_ts) if first_ts is not None else 0.0,
                "message_count": int(count),
            }
        for (session_id,) in dest.execute(
            "SELECT DISTINCT u.session_id FROM session_model_usage AS u "
            "WHERE u.session_id IS NOT NULL AND NOT EXISTS "
            "(SELECT 1 FROM sessions WHERE sessions.id = u.session_id)"
        ):
            orphan_ids.setdefault(
                str(session_id), {"started_at": 0.0, "message_count": 0}
            )

        sequence = 1
        for session_id, info in sorted(orphan_ids.items()):
            while True:
                title = (
                    f"[best-effort recovered {sequence}] session metadata "
                    "was unreadable"
                )
                sequence += 1
                if (
                    dest.execute(
                        "SELECT 1 FROM sessions WHERE title = ? LIMIT 1",
                        (title,),
                    ).fetchone()
                    is None
                ):
                    break
            dest.execute(
                "INSERT INTO sessions (id, source, started_at, title, "
                "message_count) VALUES (?, 'recovered', ?, ?, ?)",
                (
                    session_id,
                    info["started_at"],
                    title,
                    info["message_count"],
                ),
            )
            result["sessions_stubbed"] += 1
            result["messages_retained"] += info["message_count"]

        result["usage_rows_retained"] = int(
            dest.execute("SELECT COUNT(*) FROM session_model_usage").fetchone()[0]
        )

        # Repair dangling intra-sessions references without deleting rows.
        dest.execute(
            "UPDATE sessions SET parent_session_id = NULL "
            "WHERE parent_session_id IS NOT NULL AND NOT EXISTS "
            "(SELECT 1 FROM sessions AS p WHERE p.id = sessions.parent_session_id)"
        )
        dest.execute(
            "UPDATE sessions SET system_prompt_hash = NULL "
            "WHERE system_prompt_hash IS NOT NULL AND NOT EXISTS "
            "(SELECT 1 FROM system_prompts "
            "WHERE system_prompts.hash = sessions.system_prompt_hash)"
        )
        dest.execute("COMMIT")
    except BaseException:
        dest.execute("ROLLBACK")
        raise
    return result


def rebuild_fts_indexes(dest: sqlite3.Connection) -> dict[str, str]:
    """Rebuild derived FTS indexes from the salvaged canonical rows."""

    results: dict[str, str] = {}
    for table in ("messages_fts", "messages_fts_trigram", "messages_fts_cjk"):
        if not _table_columns(dest, table):
            continue
        try:
            dest.execute(f'INSERT INTO "{table}" ("{table}") VALUES (\'rebuild\')')
            results[table] = "rebuilt"
        except sqlite3.DatabaseError as exc:
            results[table] = f"rebuild failed: {exc}"
    return results
