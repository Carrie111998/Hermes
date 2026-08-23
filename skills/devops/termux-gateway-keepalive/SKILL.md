---
name: termux-gateway-keepalive
description: Keep Hermes gateway alive on Android Termux via watchdog.
version: 0.1.0
author: Thamer (taljeri), Hermes Agent
license: MIT
platforms: [linux, android]
metadata:
  hermes:
    tags: [Termux, Android, Gateway, KeepAlive, Supervision, DevOps]
    category: devops
    related_skills: [sdlc-review, systematic-debugging]
---

# Termux Gateway Keep-Alive

Supervise, wake-lock, and maintain 24/7 uptime for the Hermes messaging gateway on Android Termux without rooting.

On Android, background processes inside Termux get reclaimed by battery management (Doze) or when the app is backgrounded. This skill provides a multi-layer keep-alive architecture to automatically recover dead gateway processes and send ambient offline recovery notifications.

## When to Use

- "My Hermes gateway stops receiving messages on Android after a few hours"
- "Set up auto-restart watchdog for Hermes gateway on Termux"
- "Keep Hermes running in background on Android without getting killed"
- "Check if Telegram gateway connection is healthy or stale"
- "Send ambient device vibration/notification alerts on Termux"

Don't use for:
- Standard Linux systemd servers (use `systemctl` / `hermes-s6-container-supervision`)
- Local desktop UI instances

## Prerequisites

- Android with Termux installed.
- Optional: `termux-api` package and Termux:API companion app for wake-locks and ambient notifications (`pkg install termux-api`).

## Architecture

```
Layer 1  gateway_watchdog.sh   Detached setsid supervisor + wake-lock + auto-restart
Layer 2  gateway_monitor.sh    Cron backstop: revives watchdog if Termux reclaims it
Layer 3  telegram_selfcheck.sh Delivery health: process + connection + state freshness
         termux_presence.py    Offline ambient alerts (vibrate + toast + notification)
```

## How to Run

Execute commands via the `terminal` tool or shell:

```bash
# 1. Install keep-alive scripts to ~/.hermes/scripts/
python3 skills/devops/termux-gateway-keepalive/scripts/keepalive_cli.py install

# 2. Start the detached watchdog supervisor
bash ~/.hermes/scripts/gateway_watchdog.sh start

# 3. Check gateway and watchdog liveness status
python3 skills/devops/termux-gateway-keepalive/scripts/keepalive_cli.py status

# 4. Run delivery and state freshness self-check
python3 skills/devops/termux-gateway-keepalive/scripts/keepalive_cli.py selfcheck

# 5. Send test ambient notification
python3 skills/devops/termux-gateway-keepalive/scripts/keepalive_cli.py notify "Test notification"
```

## Quick Reference

| Task | Command |
|---|---|
| Start watchdog | `bash ~/.hermes/scripts/gateway_watchdog.sh start` |
| Stop watchdog | `bash ~/.hermes/scripts/gateway_watchdog.sh stop` |
| Check status | `python3 skills/devops/termux-gateway-keepalive/scripts/keepalive_cli.py status [--json]` |
| Run selfcheck | `python3 skills/devops/termux-gateway-keepalive/scripts/keepalive_cli.py selfcheck [--json]` |
| Ambient alert | `python3 skills/devops/termux-gateway-keepalive/scripts/keepalive_cli.py notify "<msg>"` |

## Procedure

### 1. Install and Initialize Watchdog
1. Copy the keep-alive scripts to `~/.hermes/scripts/` via `keepalive_cli.py install`.
2. Launch the watchdog via `gateway_watchdog.sh start`.
3. The watchdog acquires a `termux-wake-lock`, starts `hermes gateway` detached via `setsid`, and logs to `~/.hermes/logs/gateway_watchdog.log`.

### 2. Configure Cron Monitor Backstop
1. Register `gateway_monitor.sh` in Hermes cron to run every 10 minutes (`*/10 * * * *`).
2. If both the gateway and watchdog ever get killed by extreme Android memory pressure, the cron trigger relaunches the watchdog.

### 3. Verify Health and Freshness
1. Run `keepalive_cli.py selfcheck`.
2. The self-check audits:
   - Gateway PID is alive.
   - `gateway_state.json` is updated and Telegram connection reports `connected`.
   - State file `mtime` is under 15 minutes old (detects zombie connection locks).

## Pitfalls

- **Battery Optimization:** Ensure Termux is exempted from Android battery optimizations in device settings (`Don't optimize`).
- **Wake-lock permission:** Requires `termux-api` package and Android app permissions enabled.

## Verification

Run CLI selfcheck to verify component availability:
```bash
python3 skills/devops/termux-gateway-keepalive/scripts/keepalive_cli.py status
```
