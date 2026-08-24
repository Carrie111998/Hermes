#!/usr/bin/env python3
"""
Fix cron jobs blocked by the provider/base_url safety gate.

These jobs were created with base_url='https://api.ollama.com' which the
scheduler rejects because it cannot host-match 'api.ollama.com' to the ollama
provider's registered inference endpoint.  The fix is to point them at the
local LiteLLM proxy (provider='custom', base_url='http://localhost:4000/v1'),
which:
  - passes the safety gate (custom providers are BYOK — no stored credential)
  - routes to whatever backend LiteLLM is configured to use on this host

Usage:
    python3 scripts/fix-cron-ollama-base-url.py [--dry-run]

Requires: a live Hermes environment (HERMES_HOME / ~/.hermes must exist and
contain cron/jobs.json).
"""

import argparse
import sys
import os

# Allow running from the repo root without installing the package.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TARGET_JOB_IDS = {
    "122eda6f83b1",  # dla-sanford-confirmation-monitor
    "0060360d35b4",  # Signal Scanner Unified — Advocacy + Cadenazzi (3x daily)
    "3672e891da08",  # LinkedIn Content Scan — Post Opportunity Digest (3x daily)
    "aa9098c66058",  # UST Legal Matter Tracker
    "141615d9bbfb",  # UST Accounting Wind-down Weekly Scan
}

BAD_BASE_URL = "https://api.ollama.com"
GOOD_PROVIDER = "custom"
GOOD_BASE_URL = "http://localhost:4000/v1"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="Print what would change without writing.")
    args = parser.parse_args()

    from cron.jobs import load_jobs, save_jobs, _normalize_job_optional_text

    jobs = load_jobs()
    changed = []

    for job in jobs:
        job_id = job.get("id", "")
        if job_id not in TARGET_JOB_IDS:
            continue

        current_base_url = _normalize_job_optional_text(job.get("base_url"), strip_trailing_slash=True)
        current_provider = _normalize_job_optional_text(job.get("provider"))
        name = job.get("name", job_id)

        print(f"  Job {job_id} ({name!r})")
        print(f"    provider: {current_provider!r} → {GOOD_PROVIDER!r}")
        print(f"    base_url: {current_base_url!r} → {GOOD_BASE_URL!r}")

        if not args.dry_run:
            job["provider"] = GOOD_PROVIDER
            job["base_url"] = GOOD_BASE_URL
            # Clear any stale provider snapshot — it captured 'ollama' at create
            # time; after the update the job uses 'custom' which carries no
            # stored credential and needs no snapshot.
            job["provider_snapshot"] = None
            # If the job was auto-paused due to the base_url error, re-enable it.
            if job.get("state") in {"paused", "error"} and (
                "unsafe provider/base_url" in (job.get("last_error") or "")
                or "refusing to run" in (job.get("last_error") or "")
                or "not allowed" in (job.get("last_error") or "")
            ):
                from cron.jobs import compute_next_run
                job["enabled"] = True
                job["state"] = "scheduled"
                job["paused_at"] = None
                job["paused_reason"] = None
                job["consecutive_failures"] = 0
                job["last_error"] = None
                job["next_run_at"] = compute_next_run(job.get("schedule", {}), job.get("last_run_at"))
                print(f"    state: auto-resumed (was {job.get('state', '?')})")

        changed.append(job_id)

    if not changed:
        print("No matching jobs found — nothing to fix.")
        return 0

    if args.dry_run:
        print(f"\nDry run: would update {len(changed)} job(s). Re-run without --dry-run to apply.")
        return 0

    save_jobs(jobs)
    print(f"\nUpdated {len(changed)} job(s) and saved jobs.json.")
    print("Run 'hermes cron list' to confirm state=scheduled for each job.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
