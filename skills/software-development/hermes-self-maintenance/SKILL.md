---
name: hermes-self-maintenance
description: "Set up nightly Hermes auto-update and health checks."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [hermes, maintenance, auto-update, health-check, cron, self-healing]
    related_skills: [hermes-agent]
---

# Hermes Self-Maintenance

Nightly health checks and update detection for Hermes Agent — silent when healthy, actionable alerts when something needs attention. Zero LLM cost, zero core changes, works on every platform the cron system supports.

## What it does

A single `no_agent` Python script (`scripts/maintenance_check.py`) runs on a cron schedule and checks three things:

1. **Update availability** — runs `hermes update --check` and reports how many commits behind upstream.
2. **Gateway health** — verifies the Hermes process is alive (cross-platform PID check via `psutil` if available, fallback to `tasklist`/`pgrep`).
3. **Cron job failures** — scans enabled cron jobs for `last_status: error` and lists the failed ones.

When everything is healthy, the script exits 0 with no output (cron stays silent). When something needs attention, it prints a structured report to stdout (cron delivers it to the configured channel).

## Setup

Tell your agent:

> "Set up nightly Hermes maintenance."

The agent should:

1. Verify the script exists at `skills/software-development/hermes-self-maintenance/scripts/maintenance_check.py`.
2. Create a cron job:

```
hermes cron add \
  --name "nightly-hermes-maintenance" \
  --schedule "0 3 * * *" \
  --script "maintenance_check.py" \
  --no-agent \
  --deliver telegram
```

Adjust `--schedule` and `--deliver` to the user's preference. Common delivery targets: `telegram`, `discord`, `origin` (back to the chat that created it).

3. Run the script once manually to confirm it works:

```bash
python3 skills/software-development/hermes-self-maintenance/scripts/maintenance_check.py
```

Silent exit = healthy. Report output = something needs attention.

## How the script works

The script uses only the Hermes CLI's existing commands and the cron state file — no imports from `hermes_cli` or the agent runtime. This makes it portable across versions.

### Update check

Calls `hermes update --check` and parses stdout for the "commits behind" count. If the command is unavailable or fails, the section is skipped (degraded, not broken).

### Gateway health

Checks for running Python processes that look like the Hermes gateway. On Windows: `tasklist` filtered to `python.exe`. On macOS/Linux: `pgrep -f hermes` or `ps aux | grep`. The `psutil` package is used if available for a cleaner cross-platform check, but is not required.

### Cron failure scan

Reads the cron state database (SQLite) directly via the standard library `sqlite3` module — no Hermes import needed. Queries enabled jobs with `last_status = 'error'` and lists their names. Falls back to `hermes cron list` if the DB path is not found.

## Customization

The script accepts optional environment variables:

- `MAINTENANCE_REPORT_ALL` — set to `1` to always print a report, even when healthy (useful for debugging).
- `MAINTENANCE_SKIP_UPDATE` — set to `1` to skip the update check.
- `MAINTENANCE_SKIP_GATEWAY` — set to `1` to skip the gateway health check.
- `MAINTENANCE_SKIP_CRON` — set to `1` to skip the cron failure scan.

## Report format

When action is needed, the script prints:

```
⚠ Hermes Maintenance Report — 2026-08-02 03:00:12

📦 Update available: 47 commits behind upstream
   Latest: a1b2c3d feat: add model catalog menu

✅ Gateway: 3 python processes running

⚠ Cron: 2 failed job(s)
   — Regime Scan (merged)
   — flow-trader-execute
```

Each section only appears if it has something to report. Healthy sections are omitted in the default alert-only mode (set `MAINTENANCE_REPORT_ALL=1` to see all sections).

## Why a skill, not a core feature

Three open PRs (#33514, #56787, #8062) propose adding `hermes update auto` as core CLI subcommands. They require per-platform scheduler implementations (LaunchAgent on macOS, systemd on Linux, Task Scheduler on Windows) and touch the update/gateway lifecycle — a high-risk area that has kept them stalled.

This skill takes a different approach: use the existing cross-platform cron system that Hermes already provides. The script is a `no_agent` cron job — no LLM cost, no daemon, no platform-specific scheduler code. The cron system already handles scheduling on all platforms. The script just checks and reports.

## Limitations

- The script does not auto-apply updates. It reports that updates are available; the user (or their agent) decides when to run `hermes update`. This is intentional — auto-updating a running gateway is the exact risk that has stalled the core PRs.
- Gateway health is a process-alive check, not a deep probe. For deeper health checking, use `hermes doctor` or `hermes monitoring status`.
- The cron failure scan reads the cron state database. If the database is locked (gateway is writing), the scan retries up to 3 times with backoff.