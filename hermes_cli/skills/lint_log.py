"""Durable audit operations for write-time skill lint findings."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from hermes_cli.skills import schema
from hermes_cli.skills.lint import LintFinding
from hermes_cli.sqlite_util import retrying_write_txn


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def record_lint(
    *,
    skill_name: str,
    skill_path: str | Path,
    write_source: str,
    findings: Iterable[LintFinding],
    db_path: str | Path | None = None,
) -> None:
    rows = list(findings)
    if not rows:
        return
    schema.ensure_migrated(db_path)
    timestamp = utc_now()
    conn = schema.connect(db_path)
    try:
        with retrying_write_txn(conn):
            conn.executemany(
                """
                INSERT INTO skill_lint_log (
                    ts, skill_name, skill_path, write_source, category,
                    pattern_label, matched_text, line_number, replacement
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        timestamp,
                        str(skill_name),
                        str(skill_path),
                        str(write_source),
                        finding.category,
                        finding.pattern_label,
                        finding.matched_text,
                        finding.line_number,
                        finding.replacement,
                    )
                    for finding in rows
                ],
            )
    finally:
        conn.close()


# A descriptive alias retained for direct internal callers.
record_findings = record_lint


def list_findings(
    *,
    skill_name: str | None = None,
    category: str | None = None,
    since: str | None = None,
    limit: int = 50,
    db_path: str | Path | None = None,
) -> list[dict]:
    schema.ensure_migrated(db_path)
    clauses: list[str] = []
    params: list[object] = []
    if skill_name:
        clauses.append("skill_name = ?")
        params.append(skill_name)
    if category:
        clauses.append("category = ?")
        params.append(category)
    if since:
        clauses.append("ts >= ?")
        params.append(since)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, int(limit)))
    conn = schema.connect(db_path)
    try:
        rows = conn.execute(
            f"""
            SELECT id, ts, skill_name, skill_path, write_source, category,
                   pattern_label, matched_text, line_number, replacement
              FROM skill_lint_log
              {where}
             ORDER BY id DESC
             LIMIT ?
            """,
            params,
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def category_stats(
    *,
    days: int = 7,
    now: datetime | None = None,
    db_path: str | Path | None = None,
) -> list[dict]:
    schema.ensure_migrated(db_path)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    cutoff = (current.astimezone(timezone.utc) - timedelta(days=days)).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")
    conn = schema.connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT category, COUNT(*) AS count
              FROM skill_lint_log
             WHERE ts >= ?
             GROUP BY category
             ORDER BY count DESC, category ASC
            """,
            (cutoff,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


__all__ = [
    "category_stats",
    "list_findings",
    "record_findings",
    "record_lint",
    "utc_now",
]
