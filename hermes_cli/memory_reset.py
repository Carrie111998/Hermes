"""Safe reset helpers for ``hermes memory reset``.

The legacy reset command only removes MEMORY.md and USER.md.  Conversation
history lives in state.db and must be removed through SessionDB so unrelated
routing, metadata, and platform tables remain intact.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

_SESSION_DELETE_BATCH = 250


def _memory_files_for_target(target: str) -> list[tuple[str, str]]:
    files: list[tuple[str, str]] = []
    if target in {"all", "memory", "everything"}:
        files.append(("MEMORY.md", "agent notes"))
    if target in {"all", "user", "everything"}:
        files.append(("USER.md", "user profile"))
    return files


def _iter_session_id_batches(db: Any) -> Iterable[list[str]]:
    """Yield current session IDs in bounded batches.

    Always page from offset zero: each yielded batch is deleted before the next
    query, so advancing an offset would skip rows.
    """
    while True:
        rows = db.list_sessions_rich(
            limit=_SESSION_DELETE_BATCH,
            offset=0,
            include_children=True,
            project_compression_tips=False,
            include_archived=True,
            compact_rows=True,
        )
        ids = [
            row["id"]
            for row in rows
            if isinstance(row, dict) and isinstance(row.get("id"), str)
        ]
        if not ids:
            return
        yield ids


def _delete_conversations(db: Any, sessions_dir: Path) -> int:
    deleted = 0
    for session_ids in _iter_session_id_batches(db):
        removed = db.delete_sessions(session_ids, sessions_dir=sessions_dir)
        if removed <= 0:
            raise RuntimeError(
                "conversation reset made no progress; stop running Hermes "
                "processes and retry"
            )
        deleted += removed

    remaining = db.session_count(include_archived=True)
    if remaining:
        raise RuntimeError(
            f"{remaining} session(s) remained; stop the gateway and retry"
        )
    return deleted


def cmd_memory_reset(args: Any) -> int:
    """Reset built-in memory files and/or persisted conversation history."""
    from hermes_constants import display_hermes_home, get_hermes_home

    target = getattr(args, "target", "all")
    reset_conversations = target in {"conversations", "everything"}

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
    session_count = 0
    message_count = 0
    if reset_conversations and db_path.is_file():
        try:
            from hermes_state import SessionDB

            db = SessionDB(db_path)
            session_count = db.session_count(include_archived=True)
            message_count = db.message_count()
        except Exception as exc:
            if db is not None:
                db.close()
            print(f"\n  ✗ Could not inspect conversation history: {exc}\n")
            return 1

    has_conversations = bool(db is not None and (session_count or message_count))
    if not existing_files and not has_conversations:
        if db is not None:
            db.close()
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
        print(
            "    Note: stop the gateway first; active processes may create "
            "new sessions while reset is running."
        )

    if not getattr(args, "yes", False):
        try:
            answer = input("\n  Type 'yes' to confirm: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer != "yes":
            if db is not None:
                db.close()
            print("  Cancelled.\n")
            return 0

    if has_conversations:
        try:
            deleted_sessions = _delete_conversations(db, sessions_dir)
        except Exception as exc:
            print(f"  ✗ Failed to clear conversation history: {exc}")
            return 1
        finally:
            db.close()
        print(
            "  ✓ Cleared conversation history "
            f"({deleted_sessions:,} sessions, {message_count:,} messages)"
        )
    elif db is not None:
        db.close()

    for name, description in existing_files:
        (memories_dir / name).unlink()
        print(f"  ✓ Deleted {name} ({description})")

    print("\n  Memory reset complete.")
    print(f"  Hermes home: {display_hermes_home()}\n")
    return 0
