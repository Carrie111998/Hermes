#!/usr/bin/env python3
"""Simplify the jobflow-notifier cron job per Communication Layer spec (Section 7).

Before EventBus:
    jobflow-notifier generated a 7am digest AND wrote a NOTIFICATION message
    to Jaum's inbox, which Jaum then relayed to WhatsApp.  This coupled
    notification delivery to Jaum's session state.

After EventBus:
    DigestComposer runs inside the gateway process at 8am/1pm/6pm ET,
    queries the event bus directly, and delivers to Telegram + (morning
    only) WhatsApp.  Delivery is no longer Jaum's responsibility.

This script rewrites the jobflow-notifier prompt to focus on generating
pipeline snapshot data for DigestComposer to consume — no inbox writes,
no delivery, no WhatsApp.  Idempotent: running twice is a no-op.

Honors HERMES_HOME (picks up the profile-specific jobs.json at
~/.hermes/profiles/<name>/cron/jobs.json when set).
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from hermes_constants import get_default_hermes_root, get_hermes_home

JOBS_PATH = get_hermes_home() / "cron" / "jobs.json"

# Use the native root for path references in the prompt, so paths are
# relative to ~/.hermes (not the active profile dir, which lives under it).
_ROOT = str(get_default_hermes_root()).replace("\\", "/")

# The simplified prompt — data generation only, no delivery side effects.
SIMPLIFIED_PROMPT = (
    "Act as the Notifier agent for JobFlow on Hermes. "
    f"Root: {_ROOT}. "
    f"Read {_ROOT}/profiles/notifier/workspace/AGENTS.md and SOUL.md first. "
    "Generate a pipeline snapshot from tracker state and write it to "
    "workspace/digest-data.json as a JSON object with this schema: "
    '{"generated_at": "<ISO8601 UTC timestamp>", '
    '"pipeline_snapshot": {"total_active": <int>, '
    '"by_stage": {"discovered": <int>, "scored": <int>, "tailoring": <int>, '
    '"submitted": <int>, "interviewing": <int>, "offer": <int>, '
    '"rejected": <int>, "archived": <int>}}, '
    '"action_items": [{"company": "<name>", "type": "<approve_dry_run|followup_due|blocked>", "detail": "<short text>"}], '
    '"system_health": {"errors_24h": <int>}}. '
    "DigestComposer reads this file at 8am/1pm/6pm ET to include the "
    "snapshot in the digest. "
    "Do NOT write to any inbox. Do NOT send WhatsApp. Do NOT format "
    "user-facing text — DigestComposer handles delivery across Telegram "
    "and WhatsApp via the event bus. "
    "On failure, exit non-zero so the event bus emits cron_failed."
)


def simplify() -> int:
    if not JOBS_PATH.exists():
        print(f"jobs.json not found at {JOBS_PATH}")
        return 1

    try:
        raw = json.loads(JOBS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"ERROR: could not parse {JOBS_PATH}: {exc}")
        return 2

    if isinstance(raw, dict):
        jobs = raw.get("jobs", [])
        wrapper = True
    elif isinstance(raw, list):
        jobs = raw
        wrapper = False
    else:
        print(f"ERROR: unexpected format, got {type(raw).__name__}")
        return 2

    target = None
    for job in jobs:
        if job.get("name") == "jobflow-notifier":
            target = job
            break

    if target is None:
        print("jobflow-notifier not found — nothing to simplify")
        return 0

    if target.get("prompt") == SIMPLIFIED_PROMPT:
        print("jobflow-notifier already simplified (idempotent no-op)")
        return 0

    old = target.get("prompt", "")
    target["prompt"] = SIMPLIFIED_PROMPT
    if target.get("deliver") != "local":
        target["deliver"] = "local"

    print("Simplifying jobflow-notifier prompt:")
    print(f"  OLD length: {len(old)} chars")
    print(f"  NEW length: {len(SIMPLIFIED_PROMPT)} chars")
    print(f"  diff: {len(SIMPLIFIED_PROMPT) - len(old):+d} chars")

    if wrapper:
        out = {"jobs": jobs, "updated_at": datetime.now(timezone.utc).isoformat()}
    else:
        out = jobs

    JOBS_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nUpdated {JOBS_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(simplify())
