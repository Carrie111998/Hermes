#!/usr/bin/env python3
"""Migrate cron job delivery targets and disable OpenClaw legacy jobs.

Changes:
  1. All Hermes cron jobs: deliver → "local" (EventBus handles delivery)
  2. Delete jaum-daytime-relay job
  3. Disable all OpenClaw cron jobs
  4. Create jobflow-archiver job
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

HERMES_CRON = Path.home() / ".hermes" / "cron" / "jobs.json"
OPENCLAW_CRON = Path.home() / ".openclaw" / "cron" / "jobs.json"


def migrate_hermes_jobs():
    """Update Hermes cron jobs: deliver=local, remove daytime-relay."""
    if not HERMES_CRON.exists():
        print("Hermes cron/jobs.json not found, skipping")
        return

    jobs = json.loads(HERMES_CRON.read_text(encoding="utf-8"))
    modified = False

    # Remove jaum-daytime-relay
    original_count = len(jobs)
    jobs = [j for j in jobs if j.get("name") != "jaum-daytime-relay"]
    if len(jobs) < original_count:
        print("Removed jaum-daytime-relay")
        modified = True

    # Set all deliver fields to "local"
    for job in jobs:
        if job.get("deliver") not in ("local", None):
            old = job.get("deliver", "unset")
            job["deliver"] = "local"
            print(f"  {job.get('name', job['id'])}: deliver {old} → local")
            modified = True

    # Add jobflow-archiver if not exists
    if not any(j.get("name") == "jobflow-archiver" for j in jobs):
        import uuid
        archiver = {
            "id": uuid.uuid4().hex[:12],
            "name": "jobflow-archiver",
            "prompt": (
                "Review pipeline.json. Archive any job that has been stale "
                "(no status change) for 30+ days. NEVER archive jobs in "
                "interviewing, offer, or negotiation stages. Log archived "
                "jobs to workspace/archived.jsonl with reason and date."
            ),
            "skills": [],
            "skill": None,
            "model": None,
            "provider": None,
            "base_url": None,
            "script": None,
            "schedule": {"kind": "cron", "expr": "0 2 * * 0", "display": "Sunday 2am ET"},
            "schedule_display": "0 2 * * 0",
            "repeat": {"times": None, "completed": 0},
            "enabled": True,
            "state": "scheduled",
            "paused_at": None,
            "paused_reason": None,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "next_run_at": None,  # Will be computed on first tick
            "last_run_at": None,
            "last_status": None,
            "last_error": None,
            "deliver": "local",
            "origin": None,
            "last_delivery_error": None,
            "consecutive_errors": 0,
        }
        jobs.append(archiver)
        print("Added jobflow-archiver (Sunday 2am)")
        modified = True

    if modified:
        HERMES_CRON.write_text(json.dumps(jobs, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nHermes jobs updated: {len(jobs)} jobs")
    else:
        print("No Hermes changes needed")


def disable_openclaw_jobs():
    """Disable all OpenClaw cron jobs."""
    if not OPENCLAW_CRON.exists():
        print("\nOpenClaw cron/jobs.json not found, skipping")
        return

    jobs = json.loads(OPENCLAW_CRON.read_text(encoding="utf-8"))
    count = 0
    for job in jobs:
        if job.get("enabled", False):
            job["enabled"] = False
            job["paused_at"] = datetime.now(timezone.utc).isoformat()
            job["paused_reason"] = "Migrated to Hermes EventBus — disabled by migration script"
            count += 1

    OPENCLAW_CRON.write_text(json.dumps(jobs, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nOpenClaw: disabled {count} jobs")


if __name__ == "__main__":
    print("=== Hermes Communication Layer Migration ===\n")
    migrate_hermes_jobs()
    disable_openclaw_jobs()
    print("\nMigration complete. Restart the gateway to activate the EventBus.")
