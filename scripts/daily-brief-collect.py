#!/usr/bin/env python3
"""Collect bounded, read-only inputs for JID's daily brief in one pass."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

from foundation_cron_common import TORONTO, age_hours, load_json, local_now, newest_matching, parse_datetime

DAILY_JOB_NAME = "hermes-daily-brief"
MAX_CHANGED_FILES = 40
MAX_ITEMS_PER_QUEUE = 20
MAX_PREVIEW_CHARS = 1600
MAX_EVENT_LINES = 80


def _job_id(hermes_home: Path) -> str:
    jobs = load_json(hermes_home / "cron/jobs.json").get("jobs", [])
    matches = [job["id"] for job in jobs if job.get("name") == DAILY_JOB_NAME]
    if len(matches) != 1:
        raise RuntimeError(f"expected one {DAILY_JOB_NAME} job, found {len(matches)}")
    return matches[0]


def _last_brief_at(hermes_home: Path, job_id: str, now: datetime) -> datetime:
    database = hermes_home / "cron/notepad.db"
    try:
        with sqlite3.connect(database) as connection:
            row = connection.execute(
                "SELECT value FROM cron_notepad WHERE job_id = ? AND key = 'last_brief_at'",
                (job_id,),
            ).fetchone()
        if row:
            return parse_datetime(row[0])
    except (sqlite3.Error, OSError, ValueError):
        pass
    return now - timedelta(hours=24)


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _changed_vault_files(vault_root: Path, since: datetime) -> dict[str, object]:
    excluded_prefixes = (".obsidian/", "Hermes/Briefs/", "Hermes/Reports/")
    changed: list[tuple[float, str]] = []
    for path in vault_root.rglob("*.md"):
        if not path.is_file():
            continue
        relative = _relative(path, vault_root)
        if relative.startswith(excluded_prefixes):
            continue
        modified = datetime.fromtimestamp(path.stat().st_mtime, tz=TORONTO)
        if modified > since:
            changed.append((path.stat().st_mtime, relative))
    changed.sort(reverse=True)
    return {
        "count": len(changed),
        "files": [name for _, name in changed[:MAX_CHANGED_FILES]],
        "truncated": len(changed) > MAX_CHANGED_FILES,
    }


def _queue_files(directory: Path, placeholder: str) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(
        (path for path in directory.iterdir() if path.is_file() and path.name != placeholder),
        key=lambda path: path.name.casefold(),
    )


def _preview(path: Path) -> str:
    try:
        value = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"[unreadable: {exc}]"
    return value[:MAX_PREVIEW_CHARS] + ("\n[preview truncated]" if len(value) > MAX_PREVIEW_CHARS else "")


def _mailbox(vault_root: Path) -> dict[str, object]:
    root = vault_root / "Hermes/Mailbox"
    inbox = _queue_files(root / "Inbox", "Inbox.md")
    outbox = _queue_files(root / "Outbox", "Outbox.md")
    return {
        "inbox_count": len(inbox),
        "inbox_items": [path.name for path in inbox[:MAX_ITEMS_PER_QUEUE]],
        "inbox_truncated": len(inbox) > MAX_ITEMS_PER_QUEUE,
        "outbox_count": len(outbox),
        "outbox_items": [
            {"file": path.name, "preview": _preview(path)} for path in outbox[:MAX_ITEMS_PER_QUEUE]
        ],
        "outbox_truncated": len(outbox) > MAX_ITEMS_PER_QUEUE,
    }


def _family_events(vault_root: Path, since: datetime) -> dict[str, list[str]]:
    log_path = vault_root / "Hermes/Reports/Family Updates/family-updates.md"
    folder_activity: list[str] = []
    handoffs: list[str] = []
    if not log_path.is_file():
        return {"folder_activity": [], "handoff_ends": []}
    timestamp_pattern = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\s*\|")
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = timestamp_pattern.match(line)
        if not match:
            continue
        try:
            timestamp = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M").replace(tzinfo=TORONTO)
        except ValueError:
            continue
        if timestamp <= since:
            continue
        if "| type:handoff |" in line:
            if "| end |" in line:
                handoffs.append(line)
        elif "| type:folder-activity |" in line or "| type:" not in line:
            folder_activity.append(line)
    return {
        "folder_activity": folder_activity[-MAX_EVENT_LINES:],
        "handoff_ends": handoffs[-MAX_EVENT_LINES:],
    }


def _family_outboxes(vault_root: Path) -> list[dict[str, object]]:
    family_root = vault_root / "Hermes/Profile/Family"
    if not family_root.is_dir():
        return []
    results: list[dict[str, object]] = []
    for directory in sorted(family_root.rglob("Outbox")):
        if not directory.is_dir():
            continue
        items = _queue_files(directory, "Outbox.md")
        if not items:
            continue
        results.append(
            {
                "location": _relative(directory, vault_root),
                "count": len(items),
                "items": [
                    {"file": path.name, "preview": _preview(path)}
                    for path in items[:MAX_ITEMS_PER_QUEUE]
                ],
                "truncated": len(items) > MAX_ITEMS_PER_QUEUE,
            }
        )
    return results


def _journal(vault_root: Path, since: datetime) -> list[dict[str, str]]:
    root = vault_root / "Journal"
    if not root.is_dir():
        return []
    candidates: list[tuple[float, Path]] = []
    for path in root.glob("*.md"):
        if path.name == "Journal.md" or not path.is_file():
            continue
        modified = datetime.fromtimestamp(path.stat().st_mtime, tz=TORONTO)
        if modified > since:
            candidates.append((path.stat().st_mtime, path))
    candidates.sort(reverse=True)
    return [
        {"file": path.name, "preview": _preview(path)} for _, path in candidates[:10]
    ]


def _system(hermes_home: Path, primary_backup_dir: Path, now: datetime) -> dict[str, object]:
    archive = newest_matching(primary_backup_dir, "hermes-backup-*.tar.age")
    pending_dir = hermes_home / "pending/upstream_fix"
    pending = sorted(path.name for path in pending_dir.glob("*.json")) if pending_dir.is_dir() else []
    heartbeat = hermes_home / "scripts/.upstream-check-last-success"
    return {
        "backup": {
            "archive": archive.name if archive else None,
            "age_hours": round(age_hours(archive, now), 2) if archive else None,
            "under_48_hours": bool(archive and age_hours(archive, now) < 48),
        },
        "upstream_check_heartbeat_age_hours": round(age_hours(heartbeat, now), 2) if heartbeat.exists() else None,
        "pending_upstream_fix_count": len(pending),
        "pending_upstream_fix_records": pending[:10],
    }


def collect(
    hermes_home: Path, vault_root: Path, primary_backup_dir: Path, now: datetime
) -> dict[str, object]:
    job_id = _job_id(hermes_home)
    since = _last_brief_at(hermes_home, job_id, now)
    return {
        "collector_contract": (
            "Bounded read-only inputs. Calendar and email are intentionally absent and must be queried once each. "
            "Do not treat Inbox filenames as weekly ranking recommendations."
        ),
        "generated_at": now.isoformat(),
        "since": since.isoformat(),
        "daily_brief_job_id": job_id,
        "vault_changes": _changed_vault_files(vault_root, since),
        "mailbox": _mailbox(vault_root),
        "family_events": _family_events(vault_root, since),
        "family_outboxes": _family_outboxes(vault_root),
        "journal_candidates": _journal(vault_root, since),
        "system": _system(hermes_home, primary_backup_dir, now),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hermes-home", type=Path, default=Path.home() / ".hermes")
    parser.add_argument("--vault-root", type=Path, default=Path.home() / "Obsidian Core")
    parser.add_argument(
        "--primary-backup-dir", type=Path, default=Path.home() / "backups/hermes-agent"
    )
    parser.add_argument("--now", help="ISO timestamp override for deterministic tests")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    now = parse_datetime(args.now) if args.now else local_now()
    try:
        payload = collect(args.hermes_home, args.vault_root, args.primary_backup_dir, now)
    except Exception as exc:
        print(json.dumps({"collector_error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 1
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
