#!/usr/bin/env python3
"""Render a bounded Bulgarian daily summary of SkyAI upstream-sync reports.

This formatter is intentionally transport-agnostic. It reads only the private
structured reports produced by ``skyai_v2_upstream_sync_routine.py`` and writes
one Discord-sized Markdown message to stdout. Delivery remains the scheduler's
job through the existing ``hermes send`` CLI.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any, Iterable, Sequence
from zoneinfo import ZoneInfo


STATUS_ICON = {
    "PASS": "✅",
    "PARTIAL": "⚠️",
    "BLOCKED": "⛔",
    "NO DATA": "⚪",
}
STATUS_PRIORITY = {
    "PASS": 0,
    "PARTIAL": 1,
    "BLOCKED": 2,
}
DEFAULT_TIMEZONE = "Europe/Sofia"
DEFAULT_WINDOW_HOURS = 24
MAX_MESSAGE_LENGTH = 1900


def parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def load_reports(state_dir: Path) -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    for path in sorted(state_dir.glob("report-*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        run_at = parse_timestamp(payload.get("run_at"))
        if run_at is None:
            continue
        payload = dict(payload)
        payload["_run_at"] = run_at
        reports.append(payload)
    return sorted(reports, key=lambda report: report["_run_at"])


def reports_in_window(
    reports: Iterable[dict[str, Any]],
    *,
    now: datetime,
    window_hours: int,
) -> list[dict[str, Any]]:
    lower_bound = now.astimezone(timezone.utc) - timedelta(hours=window_hours)
    return [
        report
        for report in reports
        if lower_bound < report["_run_at"] <= now.astimezone(timezone.utc)
    ]


def aggregate_status(reports: Sequence[dict[str, Any]]) -> str:
    statuses = [
        str(report.get("status", "")).upper()
        for report in reports
        if str(report.get("status", "")).upper() in STATUS_PRIORITY
    ]
    if not statuses:
        return "NO DATA"
    return max(statuses, key=STATUS_PRIORITY.__getitem__)


def short_sha(value: object) -> str:
    text = str(value or "").strip()
    return text[:10] if text else "—"


def safe_int(value: object) -> str:
    return str(value) if isinstance(value, int) and not isinstance(value, bool) else "—"


def verification_summary(reports: Sequence[dict[str, Any]]) -> str | None:
    verified = next(
        (
            report
            for report in reversed(reports)
            if isinstance(report.get("checks"), list) and report["checks"]
        ),
        None,
    )
    if verified is None:
        return None
    parts: list[str] = []
    for check in verified["checks"]:
        if not isinstance(check, dict):
            continue
        name = str(check.get("name", "")).strip()
        if not name:
            continue
        parts.append(f"{name}={'PASS' if check.get('passed') is True else 'FAIL'}")
    return ", ".join(parts) or None


def format_report(
    reports: Sequence[dict[str, Any]],
    *,
    now: datetime,
    window_hours: int,
    timezone_name: str,
) -> str:
    local_tz = ZoneInfo(timezone_name)
    local_now = now.astimezone(local_tz)
    local_start = (now - timedelta(hours=window_hours)).astimezone(local_tz)
    status = aggregate_status(reports)
    counts = Counter(str(report.get("status", "")).upper() for report in reports)

    lines = [
        f"**SkyAI upstream sync — дневен отчет ({window_hours} ч.)**",
        f"**Статус:** {STATUS_ICON[status]} {status}",
        (
            f"**Период:** {local_start:%d.%m.%Y %H:%M} – "
            f"{local_now:%d.%m.%Y %H:%M} ({timezone_name})"
        ),
    ]

    if not reports:
        lines.extend(
            [
                "**Изпълнения:** 0",
                "⚠️ Няма структуриран sync отчет за периода. "
                "Провери 3-часовата автоматизация.",
            ]
        )
        return "\n".join(lines)

    latest = reports[-1]
    latest_local = latest["_run_at"].astimezone(local_tz)
    lines.extend(
        [
            (
                f"**Изпълнения:** {len(reports)} "
                f"(PASS {counts['PASS']} · PARTIAL {counts['PARTIAL']} · "
                f"BLOCKED {counts['BLOCKED']})"
            ),
            (
                f"**Последно:** {latest_local:%d.%m.%Y %H:%M} · "
                f"`{latest.get('outcome', 'unknown')}`"
            ),
            (
                f"**Source / upstream:** `{short_sha(latest.get('source_sha'))}` / "
                f"`{short_sha(latest.get('upstream_sha'))}`"
            ),
            (
                f"**Ahead / behind:** {safe_int(latest.get('head_ahead'))} / "
                f"{safe_int(latest.get('head_behind'))}"
            ),
        ]
    )

    checks = verification_summary(reports)
    if checks:
        lines.append(f"**Последна пълна проверка:** {checks}")

    blockers = sorted(
        {
            str(report["blocker"]).strip()
            for report in reports
            if report.get("blocker")
        }
    )
    if blockers:
        lines.append(f"**Blocker-и:** {', '.join(blockers)}")

    pr_url = next(
        (
            str(report.get("pr_url", "")).strip()
            for report in reversed(reports)
            if str(report.get("pr_url", "")).strip()
        ),
        "",
    )
    if pr_url:
        lines.append(f"**Candidate PR:** {pr_url}")

    if all(report.get("duplicate_of_previous") is True for report in reports):
        lines.append("Няма нова промяна спрямо предишния отчет.")

    lines.append("**Safety:** auto-merge, deploy и runtime промени са изключени.")
    message = "\n".join(lines)
    if len(message) > MAX_MESSAGE_LENGTH:
        return message[: MAX_MESSAGE_LENGTH - 1].rstrip() + "…"
    return message


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path.home() / ".hermes" / "state" / "skyai-v2-upstream-sync",
    )
    parser.add_argument(
        "--window-hours",
        type=int,
        default=DEFAULT_WINDOW_HOURS,
    )
    parser.add_argument("--timezone", default=DEFAULT_TIMEZONE)
    parser.add_argument(
        "--now",
        help="Optional ISO timestamp for deterministic testing.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.window_hours <= 0:
        raise SystemExit("--window-hours must be positive")
    try:
        ZoneInfo(args.timezone)
    except Exception as exc:
        raise SystemExit(f"unknown timezone: {args.timezone}") from exc

    now = parse_timestamp(args.now) if args.now else datetime.now(timezone.utc)
    if now is None:
        raise SystemExit("--now must be a valid ISO timestamp")
    reports = reports_in_window(
        load_reports(args.state_dir.expanduser().resolve()),
        now=now,
        window_hours=args.window_hours,
    )
    print(
        format_report(
            reports,
            now=now,
            window_hours=args.window_hours,
            timezone_name=args.timezone,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
