#!/usr/bin/env python3
"""
Fix cron jobs blocked by the provider/base_url safety gate.

These jobs were created with base_url='https://api.ollama.com' which the
scheduler rejects because it cannot host-match 'api.ollama.com' to the ollama
provider's registered inference endpoint.

On this fleet, Hermes already has a named custom provider ``litellm_proxy``
(Tailscale LiteLLM, stored ``LITELLM_VIRTUAL_KEY``). Pointing jobs at that
named provider + its configured host passes the safety-gate host-match and
actually authenticates. Bare ``custom`` + ``http://localhost:4000/v1`` is the
generic fallback for hosts with no named proxy — and on mfc1 nothing listens
on :4000.

Usage:
    python3 scripts/fix-cron-ollama-base-url.py [--dry-run]

Requires: a live Hermes environment (HERMES_HOME / ~/.hermes must exist and
contain cron/jobs.json).
"""
from __future__ import annotations

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
FALLBACK_PROVIDER = "custom"
FALLBACK_BASE_URL = "http://localhost:4000/v1"
NAMED_PROXY = "litellm_proxy"


def resolve_litellm_target(config: dict | None) -> tuple[str, str]:
    """Return (provider, base_url) for this host's LiteLLM proxy."""
    if isinstance(config, dict):
        for entry in config.get("custom_providers") or []:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("name") or "").strip() != NAMED_PROXY:
                continue
            url = str(entry.get("base_url") or "").strip().rstrip("/")
            if url:
                return NAMED_PROXY, url
    return FALLBACK_PROVIDER, FALLBACK_BASE_URL


def _load_hermes_config() -> dict | None:
    try:
        from hermes_cli.config import load_config_readonly
        cfg = load_config_readonly()
        return cfg if isinstance(cfg, dict) else None
    except Exception:
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="Print what would change without writing.")
    args = parser.parse_args()

    from cron.jobs import load_jobs, save_jobs, _normalize_job_optional_text

    good_provider, good_base_url = resolve_litellm_target(_load_hermes_config())
    print(f"Target: provider={good_provider!r} base_url={good_base_url!r}")

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
        print(f"    provider: {current_provider!r} → {good_provider!r}")
        print(f"    base_url: {current_base_url!r} → {good_base_url!r}")

        if current_provider == good_provider and current_base_url == good_base_url:
            print("    already at target — skip")
            continue

        if not args.dry_run:
            job["provider"] = good_provider
            job["base_url"] = good_base_url
            # Drop a stale snapshot from the old ollama/openrouter pairing.
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
