"""Audit-event slicer — per-agent stats over a sliding window.

Reads `events/audit.jsonl` (and any rotated archives `audit.jsonl.1`,
`audit.jsonl.2`, ...), filters by source-name patterns belonging to a
named agent, and aggregates run counts, durations, and event-type tallies.

Pure function: takes a Path + agent + window, returns a dict. No side
effects, no I/O outside reading the audit files.

Spec: `docs/superpowers/plans/2026-04-26-curator-backfill-and-nightly.md`
Task 2.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Dict, Iterable, List, Optional

# Per-agent possible `source` values in audit.jsonl (cron names, etc.).
# Mirrors the constants in profiles/curator/workspace/memory_bootstrap.py
# so the slicer stays in sync with the legacy bootstrap.
AGENT_SOURCES: Dict[str, List[str]] = {
    "scout": ["scout", "jobflow-scout"],
    "sentinel": [
        "sentinel",
        "sentinel-vip-morning",
        "sentinel-vip-midday",
        "sentinel-vip-evening",
    ],
    "matcher": ["matcher", "jobflow-matcher"],
    "tailor": ["tailor", "jobflow-tailor"],
    "applier": ["applier", "jobflow-applier"],
    "tracker": [
        "tracker",
        "jobflow-tracker-cycle",
        "jobflow-tracker-followup",
        "jobflow-tracker-weekly",
    ],
    "notifier": ["notifier", "jobflow-notifier"],
    "cv-handler": ["cv-handler"],
    "devflow": ["devflow", "jobflow-devflow", "devflow-standup"],
    "main": ["main", "jaum-inbox-sweeper", "jaum-skill-evolution"],
}

# Event types treated as successful runs.
_OK_EVENT_TYPES = {
    "cron_completed",
    "application_submitted",
    "digest_generated",
}
# Event types treated as failed runs.
_FAIL_EVENT_TYPES = {
    "cron_failed",
    "application_failed",
    "agent_error",
}


def _iter_audit_paths(audit_path: Path) -> Iterable[Path]:
    """Yield the main audit file plus any rotated archives in numeric order.

    Rotated archives use Hermes's standard suffix `.1`, `.2`, ... .
    Missing files are skipped silently.
    """
    if audit_path.exists():
        yield audit_path
    parent = audit_path.parent
    base = audit_path.name
    i = 1
    while True:
        candidate = parent / f"{base}.{i}"
        if not candidate.exists():
            break
        yield candidate
        i += 1


def slice_agent_events(
    audit_path: Path,
    agent: str,
    window_days: int = 30,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Return aggregated stats + raw event list for one agent.

    Args:
        audit_path: Path to `events/audit.jsonl`. Rotated archives are
            picked up automatically.
        agent: Agent name (e.g. "scout"). Used to look up source patterns
            in AGENT_SOURCES; falls back to the agent name itself if the
            agent is not in the registry.
        window_days: Number of days back from `now` to include.
        now: Reference timestamp; defaults to UTC now.

    Returns:
        {
            "agent": str,
            "window_start": datetime,
            "window_end": datetime,
            "runs_total": int,
            "runs_ok": int,
            "runs_fail": int,
            "avg_duration_s": float | None,
            "event_type_counts": dict[str, int],
            "events": list[dict],  # raw, sorted by timestamp asc
        }
    """
    now = now or datetime.now(timezone.utc)
    window_end = now
    window_start = now - timedelta(days=window_days)

    sources = set(AGENT_SOURCES.get(agent, [agent]))

    runs_total = 0
    runs_ok = 0
    runs_fail = 0
    event_type_counts: Dict[str, int] = {}
    durations: List[float] = []
    events: List[Dict[str, Any]] = []

    for path in _iter_audit_paths(audit_path):
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if ev.get("source") not in sources:
                        continue
                    ts_raw = ev.get("timestamp", "")
                    try:
                        ts = datetime.fromisoformat(ts_raw)
                    except (TypeError, ValueError):
                        continue
                    if ts < window_start or ts > window_end:
                        continue

                    et = ev.get("event_type", "?")
                    event_type_counts[et] = event_type_counts.get(et, 0) + 1
                    if et in _OK_EVENT_TYPES:
                        runs_total += 1
                        runs_ok += 1
                    elif et in _FAIL_EVENT_TYPES:
                        runs_total += 1
                        runs_fail += 1

                    payload = ev.get("payload") or {}
                    dur = payload.get("duration")
                    if isinstance(dur, (int, float)) and et in _OK_EVENT_TYPES:
                        durations.append(float(dur))

                    events.append(ev)
        except OSError:
            # If a rotated archive becomes unreadable mid-iteration, skip it
            # and continue with whatever we already gathered.
            continue

    events.sort(key=lambda e: e.get("timestamp", ""))

    return {
        "agent": agent,
        "window_start": window_start,
        "window_end": window_end,
        "runs_total": runs_total,
        "runs_ok": runs_ok,
        "runs_fail": runs_fail,
        "avg_duration_s": float(mean(durations)) if durations else None,
        "event_type_counts": event_type_counts,
        "events": events,
    }


# ---------------------------------------------------------------------------
# CLI shim — for sanity-runs against real audit.jsonl
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("usage: python -m curator.audit_slicer <agent> [audit_path]", file=sys.stderr)
        sys.exit(1)
    agent = sys.argv[1]
    audit = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(r"C:/Users/diego/.hermes/events/audit.jsonl")
    res = slice_agent_events(audit, agent, window_days=30)
    print(f"agent={res['agent']}")
    print(f"window: {res['window_start'].isoformat()} -> {res['window_end'].isoformat()}")
    print(f"runs_total={res['runs_total']} ok={res['runs_ok']} fail={res['runs_fail']}")
    print(f"avg_duration_s={res['avg_duration_s']}")
    print("event_type_counts:")
    for et, c in sorted(res["event_type_counts"].items(), key=lambda kv: -kv[1]):
        print(f"  {et}: {c}")
