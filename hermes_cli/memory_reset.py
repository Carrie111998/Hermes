"""Safe conversation-history reset helpers for ``hermes memory reset``.

Only the new ``conversations`` and ``everything`` targets are handled here.
The legacy ``all`` / ``memory`` / ``user`` targets continue through the
existing ``cmd_memory`` implementation so there is one production path for
existing behavior.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

_SESSION_DELETE_BATCH = 250
_CONVERSATION_TARGETS = frozenset({"conversations", "everything"})


def _close_db(db: Any) -> None:
    if db is None:
        return
    try:
        db.close()
    except Exception:
        pass


def _get_running_gateway_pid() -> int | None:
    """Return the active gateway PID for the current Hermes home, if any."""
    from gateway.status import get_running_pid

    return get_running_pid()


def _memory_files_for_target(target: str) -> list[tuple[str, str]]:
    if target == "conversations":
        return []
    if target == "everything":
        return [
            ("MEMORY.md", "agent notes"),
            ("USER.md", "user profile"),
        ]
    raise ValueError(f"unsupported conversation reset target: {target}")


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
    """Delete the captured sessions through SessionDB transactions.

    ``SessionDB.delete_sessions`` owns the SQL contract: messages and sessions
    are removed with FTS triggers active, session-model usage cascades, orphaned
    child references are repaired, unreferenced system prompts are cleaned, and
    legacy transcript/request-dump files are removed. Unrelated tables are not
    touched.
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

    try:
        running_pid = _get_running_gateway_pid()
    except Exception as exc:
        print(f"\n  ✗ Could not verify gateway status: {exc}\n")
        return 1
    if running_pid is not None:
        print(
            "\n  ✗ The gateway is running "
            f"(PID {running_pid}). Stop it before clearing conversation history:\n"
            "      hermes gateway stop\n"
        )
        return 1

    hermes_home = Path(get_hermes_home())
    memories_dir = hermes_home / "memories"
    sessions_dir = hermes_home / "sessions"
    db_path = hermes_home / "state.db"

    selected_files = _memory_files_for_target(target)
    existing_files = [
        (name, description)
        for name, description in selected_files
        if (memories_dir / name).is_file()
    ]

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
