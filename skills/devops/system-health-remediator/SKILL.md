---
name: system-health-remediator
description: "Triage and fix blocked or misbehaving Hermes cron jobs; filter known log noise."
version: 1.1.0
author: dizhaky
platforms: [linux, macos]
metadata:
  hermes:
    tags: [devops, cron, health, monitoring, ops, remediation]
    related_skills: [ugw-health-check, config-integrity-watchdog]
---

# System Health Remediator

Operational skill for diagnosing and auto-fixing common Hermes health issues surfaced by the gateway logs or the `hermes cron status` / `hermes cron audit` commands. Run this skill whenever the gateway reports blocked cron jobs, scheduler errors, or persistent warning floods.

## Scope

- Detect and fix cron jobs blocked by the provider/base_url safety gate
- Detect and fix cron jobs auto-paused after consecutive failures
- Filter known log noise patterns that do not represent real errors
- Escalate only genuine anomalies

## Known noise patterns — do NOT escalate

The following log lines are expected operational noise. Suppress them in reports and do not open tickets for them:

| Log fragment | Root cause | Action |
|---|---|---|
| `tool_call to 'mcp__unified_gateway__dispatch' is missing required argument` | Model probed the dispatch tool without a required param; the gateway rejected it safely | Ignore |
| `tool_call requires a 'name' argument` | Same as above — the `name` field was omitted from a dispatch call | Ignore |
| `skill_view returned error` + "skill not found" | Model requested a skill that isn't installed in this profile | Ignore; optionally note which skill is missing |
| `Tool tool_call returned error` + "missing required argument" | A tool call was malformed; the tool layer rejected it | Ignore |

If a single session produces more than 20 occurrences of any of the above, surface it once as a low-priority note (possible prompt-loop or bad tool schema) but do not page.

## Blocked cron jobs — provider/base_url safety gate

### Symptom

```
Job '<id>': refusing to run — unsafe provider/base_url pair could
exfiltrate a stored credential: base_url '<URL>' is not allowed
```

### Root cause

A cron job has an explicit `base_url` override that does not match the named provider's own known endpoint. The scheduler fails closed to prevent credential exfiltration.

**Common case:** jobs that were created with `provider=ollama` (or `ollama-cloud`) and `base_url=https://api.ollama.com`. The provider alias `ollama` resolves to the `custom` BYOK path internally but is not listed in the provider registry with `api.ollama.com` as its inference host, so the host-match guard rejects the pair.

### Fix

Change the job to use `provider=custom` and point `base_url` at the local LiteLLM proxy (`http://localhost:4000/v1`). This passes the security guard (custom providers are BYOK — no stored credential to exfiltrate) and routes through the local proxy to whatever backend is configured there.

```
cronjob(action="update", job_id="<ID>", provider="custom", base_url="http://localhost:4000/v1")
```

Repeat for every blocked job. After updating, verify with:

```
hermes cron list
```

The `state` field should return to `scheduled` and `last_error` should be cleared on the next tick.

### Currently known blocked jobs (as of 2026-08-24)

| Job ID | Name |
|---|---|
| `122eda6f83b1` | dla-sanford-confirmation-monitor |
| `0060360d35b4` | Signal Scanner Unified — Advocacy + Cadenazzi (3x daily) |
| `3672e891da08` | LinkedIn Content Scan — Post Opportunity Digest (3x daily) |
| `aa9098c66058` | UST Legal Matter Tracker |
| `141615d9bbfb` | UST Accounting Wind-down Weekly Scan |

Apply the fix to all five:

```python
job_ids = [
    "122eda6f83b1",
    "0060360d35b4",
    "3672e891da08",
    "aa9098c66058",
    "141615d9bbfb",
]
for jid in job_ids:
    cronjob(action="update", job_id=jid, provider="custom", base_url="http://localhost:4000/v1")
```

After applying, resume any that are in `paused` state:

```python
for jid in job_ids:
    cronjob(action="resume", job_id=jid)
```

## Auto-paused jobs (consecutive failures)

Jobs auto-pause after 3 consecutive failures (configurable via `failure_limit`). After fixing the root cause:

```
hermes cron resume <job_id>
```

Or via tool:

```python
cronjob(action="resume", job_id="<ID>")
```

## Runbook

1. Run `hermes cron status` — note any jobs in `paused` or `error` state.
2. For each blocked job, check `last_error` for the provider/base_url safety-gate message.
3. Apply the fix above (update to `provider=custom`, `base_url=http://localhost:4000/v1`).
4. Resume auto-paused jobs.
5. Tail the gateway log for 60 s to confirm no new blocking errors:
   ```
   hermes logs --tail 60
   ```
6. Cross-check with `hermes cron list` — all target jobs should show `state: scheduled`.
7. If any job is still blocked after the update, inspect `last_error` for a different cause and escalate to the on-call channel.

## Escalation criteria

Escalate (do NOT suppress) if:

- A job is blocked by a cause other than the known base_url issue
- The same job fails more than 3 ticks after being resumed
- The `ticker_heartbeat` file has not been updated in > 5 minutes (ticker thread dead)
- `hermes cron status` shows `ticker_last_error` with an unexpected message
