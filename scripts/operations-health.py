#!/usr/bin/env python3
"""Failure-only health check for Hermes cron and encrypted backups."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from foundation_cron_common import age_hours, load_json, local_now, newest_matching, parse_datetime


def _freshness_alert(label: str, path: Path, maximum_hours: float, now: datetime) -> str | None:
    if not path.exists():
        return f"{label} heartbeat is missing: {path}"
    age = age_hours(path, now)
    if age > maximum_hours:
        return f"{label} heartbeat is stale ({age:.1f}h; limit {maximum_hours:g}h)"
    return None


def inspect_health(
    *,
    hermes_home: Path,
    primary_backup_dir: Path,
    secondary_backup_dir: Path,
    now: datetime,
    backup_max_hours: float = 48,
    runtime_checks: bool = True,
) -> tuple[list[str], dict[str, object]]:
    alerts: list[str] = []
    facts: dict[str, object] = {}

    primary = newest_matching(primary_backup_dir, "hermes-backup-*.tar.age")
    secondary = newest_matching(secondary_backup_dir, "hermes-backup-*.tar.age")
    for label, archive in (("Primary encrypted backup", primary), ("Secondary encrypted backup", secondary)):
        if archive is None:
            alerts.append(f"{label} is missing")
            continue
        age = age_hours(archive, now)
        facts[f"{label.lower().replace(' ', '_')}_age_hours"] = round(age, 2)
        facts[f"{label.lower().replace(' ', '_')}_name"] = archive.name
        if age > backup_max_hours:
            alerts.append(f"{label} is stale ({age:.1f}h; limit {backup_max_hours:g}h)")

    if primary and secondary:
        if primary.name != secondary.name:
            alerts.append(
                "Secondary backup is behind primary "
                f"(primary {primary.name}; secondary {secondary.name})"
            )
        elif primary.stat().st_size != secondary.stat().st_size:
            alerts.append(f"Primary/secondary backup sizes differ for {primary.name}")

    if runtime_checks:
        heartbeat_checks = (
            ("Gateway", hermes_home / "state/gateway.heartbeat", 0.25),
            ("Cron ticker", hermes_home / "cron/ticker_last_success", 0.25),
            ("Upstream check", hermes_home / "scripts/.upstream-check-last-success", 48),
        )
        for label, path, maximum in heartbeat_checks:
            alert = _freshness_alert(label, path, maximum, now)
            if alert:
                alerts.append(alert)

        jobs_path = hermes_home / "cron/jobs.json"
        try:
            jobs = load_json(jobs_path).get("jobs", [])
        except (OSError, ValueError) as exc:
            alerts.append(f"Cron registry cannot be read: {exc}")
            jobs = []

        for job in jobs:
            if not job.get("enabled", True) or job.get("state") == "paused":
                continue
            name = job.get("name") or job.get("id") or "unknown job"
            last_status = job.get("last_status")
            if last_status not in (None, "ok"):
                alerts.append(f"Cron {name} last status is {last_status}: {job.get('last_error') or 'no detail'}")
            if job.get("last_delivery_error"):
                alerts.append(f"Cron {name} delivery failed: {job['last_delivery_error']}")

            next_run = job.get("next_run_at")
            if next_run:
                try:
                    overdue_hours = (now - parse_datetime(next_run)).total_seconds() / 3600
                    if overdue_hours > 0.25:
                        alerts.append(f"Cron {name} is overdue by {overdue_hours:.1f}h")
                except ValueError:
                    alerts.append(f"Cron {name} has an invalid next_run_at value")

            if not job.get("last_run_at") and job.get("created_at"):
                try:
                    never_run_age = (now - parse_datetime(job["created_at"])).total_seconds() / 3600
                    if never_run_age > 8 * 24:
                        alerts.append(f"Cron {name} has never run after {never_run_age / 24:.1f} days")
                except ValueError:
                    alerts.append(f"Cron {name} has an invalid created_at value")

    return alerts, facts


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hermes-home", type=Path, default=Path.home() / ".hermes")
    parser.add_argument("--primary-backup-dir", type=Path, default=Path.home() / "backups/hermes-agent")
    parser.add_argument("--secondary-backup-dir", type=Path, default=Path.home() / "hermes-backups-sync")
    parser.add_argument("--now", help="ISO timestamp override for deterministic tests")
    parser.add_argument("--max-backup-age-hours", type=float, default=48)
    parser.add_argument("--summary", action="store_true", help="Print a compact healthy summary instead of silence")
    parser.add_argument("--skip-runtime-checks", action="store_true", help="Only inspect backup fixtures")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    now = parse_datetime(args.now) if args.now else local_now()
    try:
        alerts, facts = inspect_health(
            hermes_home=args.hermes_home,
            primary_backup_dir=args.primary_backup_dir,
            secondary_backup_dir=args.secondary_backup_dir,
            now=now,
            backup_max_hours=args.max_backup_age_hours,
            runtime_checks=not args.skip_runtime_checks,
        )
    except Exception as exc:
        print(f"🩺 Operations Alert\n- health check crashed: {type(exc).__name__}: {exc}")
        return 1

    if alerts:
        print("🩺 Operations Alert")
        for alert in alerts:
            print(f"- {alert}")
    elif args.summary:
        primary_age = facts.get("primary_encrypted_backup_age_hours")
        secondary_age = facts.get("secondary_encrypted_backup_age_hours")
        print(f"healthy; encrypted backup age {primary_age:.1f}h primary / {secondary_age:.1f}h secondary")
    return 0


if __name__ == "__main__":
    sys.exit(main())
