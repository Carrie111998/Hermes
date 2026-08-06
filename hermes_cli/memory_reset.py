"""Safe conversation-history reset helpers for ``hermes memory reset``.

Only the new ``conversations`` and ``everything`` targets are handled here.
The legacy ``all`` / ``memory`` / ``user`` targets continue through the
existing ``cmd_memory`` implementation so there is one production path for
existing behavior.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

# ``SessionDB.delete_sessions`` discovers delegate children with a query that
# binds each input ID twice. 250 keeps that query, plus the follow-up IN lists,
# comfortably below SQLite builds that retain the legacy 999-variable limit.
_SESSION_DELETE_BATCH = 250
_CONVERSATION_TARGETS = frozenset({"conversations", "everything"})
_UNKNOWN_RUNNING_PID = 0


def _close_db(db: Any) -> None:
    if db is None:
        return
    try:
        db.close()
    except Exception:
        pass


def _get_running_gateway_pid(hermes_home: Path) -> int | None:
    """Return the live gateway PID associated with the target Hermes home.

    Use the repository's complete liveness ladder rather than checking only
    ``gateway.pid``: launch-service-managed gateways can remain live with only
    ``gateway_state.json`` available. ``profile_dir`` also prevents one
    profile's reset from being blocked by another profile's gateway.

    A running gateway whose liveness source cannot expose a host PID returns
    ``_UNKNOWN_RUNNING_PID`` so the destructive command still fails closed.
    """
    from gateway.status import resolve_gateway_liveness

    liveness = resolve_gateway_liveness(
        profile_dir=hermes_home,
        use_cache=False,
    )
    if liveness.probe_error and not liveness.running:
        raise RuntimeError("gateway liveness probe was inconclusive")
    if not liveness.running:
        return None
    return liveness.pid if liveness.pid is not None else _UNKNOWN_RUNNING_PID


def _memory_files_for_target(target: str) -> list[tuple[str, str]]:
    if target == "conversations":
        return []
    if target == "everything":
        return [
            ("MEMORY.md", "agent notes"),
            ("USER.md", "user profile"),
        ]
    raise ValueError(f"unsupported conversation reset target: {target}")


def _preflight_memory_file_deletion(
    memories_dir: Path,
    existing_files: list[tuple[str, str]],
) -> None:
    """Fail before DB mutation when selected memory files cannot be removed.

    Unlink permission is controlled by the parent directory on POSIX. Windows
    additionally refuses deletion of common read-only files, so check the file
    write bit there as a best-effort preflight. The actual unlink remains
    guarded because permissions can still change after this check.
    """
    if not existing_files:
        return
    if not os.access(memories_dir, os.W_OK | os.X_OK):
        raise PermissionError(f"memory directory is not writable: {memories_dir}")
    if os.name == "nt":
        for name, _description in existing_files:
            path = memories_dir / name
            if not os.access(path, os.W_OK):
                raise PermissionError(f"memory file is read-only: {path}")


def _collect_session_ids(db: Any) -> list[str]:
    """Take a stable, complete snapshot of session IDs before deletion.

    The reset refuses to continue if paging returns duplicate or malformed rows.
    This catches a live writer reordering the listing instead of chasing and
    deleting sessions created while reset is already running.
    """
    session_ids: list[str] = []
    seen: set[str] = set()
    offset = 0

    while True:
        rows = db.list_sessions_rich(
            limit=_SESSION_DELETE_BATCH,
            offset=offset,
            include_children=True,
            project_compression_tips=False,
            include_archived=True,
            compact_rows=True,
        )
        if not rows:
            break

        page_ids = [
            row.get("id")
            for row in rows
            if isinstance(row, dict) and isinstance(row.get("id"), str)
        ]
        if len(page_ids) != len(rows) or len(set(page_ids)) != len(page_ids):
            raise RuntimeError("session listing returned malformed or duplicate rows")
        if seen.intersection(page_ids):
            raise RuntimeError(
                "session listing changed while reset was preparing; "
                "stop all Hermes processes and retry"
            )

        session_ids.extend(page_ids)
        seen.update(page_ids)
        offset += len(rows)
        if len(rows) < _SESSION_DELETE_BATCH:
            break

    return session_ids


def _delete_conversations(
    db: Any,
    sessions_dir: Path,
    session_ids: list[str],
    *,
    expected_session_count: int,
) -> int:
    """Delete the captured sessions through bounded SessionDB transactions.

    ``SessionDB.delete_sessions`` owns the SQL contract: messages and sessions
    are removed with FTS triggers active, session-model usage cascades, orphaned
    child references are repaired, unreferenced system prompts are cleaned, and
    legacy transcript/request-dump files are removed. Unrelated tables are not
    touched.

    The operation is intentionally batched to stay below SQLite's bind-variable
    limits. Every batch is atomic; the final zero-row verification refuses to
    report success after a partial reset. The gateway guard and stable snapshot
    make a partial result unlikely, but another non-gateway Hermes writer can
    still race this destructive maintenance command and cause a failure.
    """
    for start in range(0, len(session_ids), _SESSION_DELETE_BATCH):
        db.delete_sessions(
            session_ids[start : start + _SESSION_DELETE_BATCH],
            sessions_dir=sessions_dir,
        )

    remaining_sessions = db.session_count(include_archived=True)
    remaining_messages = db.message_count()
    if remaining_sessions or remaining_messages:
        raise RuntimeError(
            f"{remaining_sessions} session(s) and {remaining_messages} message(s) "
            "remained; stop all Hermes processes and retry"
        )
    return expected_session_count


def cmd_memory_reset(args: Any) -> int:
    """Reset persisted conversations, optionally with built-in memory files."""
    from hermes_constants import display_hermes_home, get_hermes_home

    target = getattr(args, "target", None)
    if target not in _CONVERSATION_TARGETS:
        print(f"\n  ✗ Unsupported conversation reset target: {target!r}\n")
        return 2

    hermes_home = Path(get_hermes_home())
    try:
        running_pid = _get_running_gateway_pid(hermes_home)
    except Exception as exc:
        print(f"\n  ✗ Could not verify gateway status: {exc}\n")
        return 1
    if running_pid is not None:
        pid_detail = f" (PID {running_pid})" if running_pid else ""
        print(
            f"\n  ✗ The gateway is running{pid_detail}. "
            "Stop it before clearing conversation history:\n"
            "      hermes gateway stop\n"
        )
        return 1

    memories_dir = hermes_home / "memories"
    sessions_dir = hermes_home / "sessions"
    db_path = hermes_home / "state.db"

    selected_files = _memory_files_for_target(target)
    existing_files = [
        (name, description)
        for name, description in selected_files
        if (memories_dir / name).is_file()
    ]
    try:
        _preflight_memory_file_deletion(memories_dir, existing_files)
    except (OSError, PermissionError) as exc:
        print(f"\n  ✗ Could not prepare memory-file reset: {exc}\n")
        return 1

    db = None
    session_ids: list[str] = []
    session_count = 0
    message_count = 0
    if db_path.is_file():
        try:
            from hermes_state import SessionDB

            db = SessionDB(db_path)
            session_count = db.session_count(include_archived=True)
            message_count = db.message_count()
            session_ids = _collect_session_ids(db)
            if len(session_ids) != session_count:
                raise RuntimeError(
                    "session count changed while reset was preparing; "
                    "stop all Hermes processes and retry"
                )
            if message_count and not session_ids:
                raise RuntimeError(
                    "state.db contains messages without sessions; refusing a partial reset"
                )
        except Exception as exc:
            _close_db(db)
            print(f"\n  ✗ Could not inspect conversation history: {exc}\n")
            return 1

    has_conversations = bool(session_count or message_count)
    if not existing_files and not has_conversations:
        _close_db(db)
        print("\n  Nothing to reset.\n")
        return 0

    print("\n  This will permanently erase:")
    for name, description in existing_files:
        size = (memories_dir / name).stat().st_size
        print(f"    ◆ {name} ({description}) — {size:,} bytes")
    if has_conversations:
        print(
            "    ◆ conversation history — "
            f"{session_count:,} sessions, {message_count:,} messages"
        )
        print("    Note: stop all other Hermes CLI/TUI/cron processes first.")

    if not getattr(args, "yes", False):
        try:
            answer = input("\n  Type 'yes' to confirm: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer != "yes":
            _close_db(db)
            print("  Cancelled.\n")
            return 0

    if has_conversations:
        try:
            deleted_sessions = _delete_conversations(
                db,
                sessions_dir,
                session_ids,
                expected_session_count=session_count,
            )
        except Exception as exc:
            print(f"  ✗ Failed to clear conversation history: {exc}")
            return 1
        finally:
            _close_db(db)
        print(
            "  ✓ Cleared conversation history "
            f"({deleted_sessions:,} sessions, {message_count:,} messages)"
        )
    else:
        _close_db(db)

    for name, description in existing_files:
        try:
            (memories_dir / name).unlink()
        except OSError as exc:
            print(f"  ✗ Failed to delete {name}: {exc}")
            return 1
        print(f"  ✓ Deleted {name} ({description})")

    print("\n  Memory reset complete.")
    print(f"  Hermes home: {display_hermes_home()}\n")
    return 0
