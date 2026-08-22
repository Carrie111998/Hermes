#!/usr/bin/env python3
"""Validate daily-brief save/delivery agreement, then advance its checkpoint."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from foundation_cron_common import load_json, local_now, normalized_text, parse_datetime

DAILY_JOB_NAME = "hermes-daily-brief"
RESPONSE_MARKER = "\n## Response\n\n"


def _find_job(hermes_home: Path) -> dict:
    jobs = load_json(hermes_home / "cron/jobs.json").get("jobs", [])
    matches = [job for job in jobs if job.get("name") == DAILY_JOB_NAME]
    if len(matches) != 1:
        raise RuntimeError(f"expected one {DAILY_JOB_NAME} job, found {len(matches)}")
    return matches[0]


def _latest_output(hermes_home: Path, job_id: str) -> Path | None:
    output_dir = hermes_home / "cron/output" / job_id
    if not output_dir.is_dir():
        return None
    files = [path for path in output_dir.glob("*.md") if path.is_file()]
    return max(files, key=lambda path: path.stat().st_mtime) if files else None


def _extract_final_response(audit_document: str) -> str:
    if RESPONSE_MARKER not in audit_document:
        raise ValueError("cron audit output has no final ## Response section")
    return audit_document.rsplit(RESPONSE_MARKER, 1)[1]


def _set_checkpoint(database: Path, job_id: str, value: str, now: datetime) -> None:
    with sqlite3.connect(database, timeout=5) as connection:
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute(
            """CREATE TABLE IF NOT EXISTS cron_notepad (
                 job_id TEXT NOT NULL,
                 key TEXT NOT NULL,
                 value TEXT NOT NULL,
                 updated_at TEXT NOT NULL,
                 PRIMARY KEY (job_id, key)
               )"""
        )
        connection.execute(
            """INSERT INTO cron_notepad (job_id, key, value, updated_at)
               VALUES (?, 'last_brief_at', ?, ?)
               ON CONFLICT(job_id, key)
               DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
            (job_id, value, now.isoformat()),
        )


def validate(
    *, hermes_home: Path, vault_root: Path, now: datetime, update_checkpoint: bool = True
) -> list[str]:
    problems: list[str] = []
    job = _find_job(hermes_home)
    job_id = job["id"]
    last_run_raw = job.get("last_run_at")
    last_run: datetime | None = None
    if not last_run_raw:
        problems.append("daily brief has no recorded last run")
    else:
        try:
            last_run = parse_datetime(last_run_raw)
            if last_run.date() != now.date():
                problems.append(f"latest daily-brief run is from {last_run.date()}, not {now.date()}")
        except ValueError:
            problems.append("daily brief has an invalid last_run_at value")

    if job.get("last_status") != "ok":
        problems.append(f"daily brief status is {job.get('last_status')!r}, not 'ok'")
    deliver = str(job.get("deliver") or "")
    if not deliver.startswith("telegram:"):
        problems.append(f"daily brief delivery target is {deliver!r}, not explicit Telegram")
    if job.get("last_delivery_error"):
        problems.append(f"daily brief delivery error: {job['last_delivery_error']}")

    brief_path = vault_root / "Hermes/Briefs" / f"{now.date().isoformat()}.md"
    output_path = _latest_output(hermes_home, job_id)
    if not brief_path.is_file():
        problems.append(f"saved brief is missing: {brief_path}")
    if output_path is None:
        problems.append("cron final-response output is missing")

    if brief_path.is_file() and output_path is not None:
        saved = normalized_text(brief_path.read_text(encoding="utf-8", errors="replace"))
        try:
            delivered = normalized_text(
                _extract_final_response(output_path.read_text(encoding="utf-8", errors="replace"))
            )
        except ValueError as exc:
            problems.append(str(exc))
            delivered = ""
        if not saved:
            problems.append("saved brief is empty")
        elif delivered and saved != delivered:
            problems.append(
                "saved brief does not match cron's final delivered response "
                f"({len(saved)} vs {len(delivered)} characters)"
            )
        if last_run and datetime.fromtimestamp(output_path.stat().st_mtime, tz=last_run.tzinfo) < last_run.replace(
            hour=0, minute=0, second=0, microsecond=0
        ):
            problems.append("latest cron output file predates today's run window")

    if not problems and update_checkpoint and last_run:
        _set_checkpoint(hermes_home / "cron/notepad.db", job_id, last_run.isoformat(), now)
    return problems


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hermes-home", type=Path, default=Path.home() / ".hermes")
    parser.add_argument("--vault-root", type=Path, default=Path.home() / "Obsidian Core")
    parser.add_argument("--now", help="ISO timestamp override for deterministic tests")
    parser.add_argument("--no-checkpoint-update", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    now = parse_datetime(args.now) if args.now else local_now()
    try:
        problems = validate(
            hermes_home=args.hermes_home,
            vault_root=args.vault_root,
            now=now,
            update_checkpoint=not args.no_checkpoint_update,
        )
    except Exception as exc:
        problems = [f"validator crashed: {type(exc).__name__}: {exc}"]
    if problems:
        print("🩺 Operations Alert — Daily brief validation failed")
        for problem in problems:
            print(f"- {problem}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
