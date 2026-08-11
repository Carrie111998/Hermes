#!/usr/bin/env python3
"""Re-route existing Hermes Kanban notification subscriptions safely.

Dry-run is the default. ``--apply`` creates integrity-checked online SQLite
backups before updating subscriptions. Collisions keep the destination row,
advance its cursor to the newest consumed event, then remove the source row.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sqlite3
from typing import Any


def board_databases(home: pathlib.Path) -> list[pathlib.Path]:
    candidates = [home / "kanban" / "kanban.db"]
    candidates.extend(sorted((home / "kanban" / "boards").glob("*/kanban.db")))
    return [path for path in candidates if path.is_file()]


def online_backup(source: pathlib.Path, target: pathlib.Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(source)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
        check = dst.execute("PRAGMA integrity_check").fetchone()[0]
        if check != "ok":
            raise RuntimeError(f"backup integrity_check={check}")
    finally:
        dst.close()
        src.close()


def migrate_database(
    path: pathlib.Path,
    *,
    platform: str,
    source_chat: str,
    destination_chat: str,
    apply: bool,
    backup_root: pathlib.Path,
) -> dict[str, Any]:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM kanban_notify_subs WHERE platform=? AND chat_id=? "
            "ORDER BY task_id,thread_id",
            (platform, source_chat),
        ).fetchall()
        result: dict[str, Any] = {
            "database": str(path),
            "matched": len(rows),
            "moved": 0,
            "merged": 0,
        }
        if not apply or not rows:
            return result

        backup_name = "default.db" if path.parent.name == "kanban" else f"{path.parent.name}.db"
        online_backup(path, backup_root / backup_name)

        conn.execute("BEGIN IMMEDIATE")
        try:
            for row in rows:
                existing = conn.execute(
                    "SELECT * FROM kanban_notify_subs WHERE task_id=? AND platform=? "
                    "AND chat_id=? AND thread_id=?",
                    (row["task_id"], platform, destination_chat, row["thread_id"] or ""),
                ).fetchone()
                if existing is None:
                    conn.execute(
                        "UPDATE kanban_notify_subs SET chat_id=? WHERE task_id=? "
                        "AND platform=? AND chat_id=? AND thread_id=?",
                        (
                            destination_chat,
                            row["task_id"],
                            platform,
                            source_chat,
                            row["thread_id"] or "",
                        ),
                    )
                    result["moved"] += 1
                    continue

                conn.execute(
                    "UPDATE kanban_notify_subs SET last_event_id=MAX(last_event_id,?), "
                    "failure_count=MIN(failure_count,?) WHERE task_id=? AND platform=? "
                    "AND chat_id=? AND thread_id=?",
                    (
                        int(row["last_event_id"] or 0),
                        int(row["failure_count"] or 0),
                        row["task_id"],
                        platform,
                        destination_chat,
                        row["thread_id"] or "",
                    ),
                )
                conn.execute(
                    "DELETE FROM kanban_notify_subs WHERE task_id=? AND platform=? "
                    "AND chat_id=? AND thread_id=?",
                    (row["task_id"], platform, source_chat, row["thread_id"] or ""),
                )
                result["merged"] += 1
            check = conn.execute("PRAGMA integrity_check").fetchone()[0]
            if check != "ok":
                raise RuntimeError(f"post-migration integrity_check={check}")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return result
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", type=pathlib.Path, default=pathlib.Path.home() / ".hermes")
    parser.add_argument("--platform", default="discord")
    parser.add_argument("--source-chat", required=True)
    parser.add_argument("--destination-chat", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.source_chat == args.destination_chat:
        parser.error("source and destination chats must differ")

    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_root = args.home / "backups" / "kanban-notify-route" / stamp
    results = [
        migrate_database(
            path,
            platform=args.platform,
            source_chat=args.source_chat,
            destination_chat=args.destination_chat,
            apply=args.apply,
            backup_root=backup_root,
        )
        for path in board_databases(args.home)
    ]
    print(json.dumps({
        "apply": args.apply,
        "backup_root": str(backup_root) if args.apply else None,
        "matched": sum(item["matched"] for item in results),
        "moved": sum(item["moved"] for item in results),
        "merged": sum(item["merged"] for item in results),
        "databases": results,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
