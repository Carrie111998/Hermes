---
sidebar_position: 4
---

# Running Many Gateways at Once

Operate multiple [profiles](./profiles.md) — each with its own bot tokens,
sessions, and memory — as managed services on a single machine. This page
covers the operational concerns: starting them all together, viewing logs
across profiles, preventing the host from sleeping, and recovering from common
launchd/systemd quirks.

If you only run one Hermes agent, you don't need this page — see
[Profiles](./profiles.md) for the basics.

## When to use this

You want this setup when you have two or more Hermes agents that should all
be online at the same time. Common reasons:

- A personal assistant on one Telegram bot and a coding agent on another
- One agent per family member or one per Slack workspace
- Sandbox + production instances of the same configuration
- A research agent + a writing agent + a cron-driven bot — each with isolated
  memory and skills

Every profile already gets its own per-platform LaunchAgent
(`ai.hermes.gateway-<name>.plist`) or systemd user service
(`hermes-gateway-<name>.service`). This guide adds the patterns for managing
them collectively.

## Quick start

```bash
# Create profiles (once)
hermes profile create coder
hermes profile create personal-bot
hermes profile create research

# Configure each
coder setup
personal-bot setup
research setup

# Install each gateway as a managed service
coder gateway install
personal-bot gateway install
research gateway install

# Start them all
coder gateway start
personal-bot gateway start
research gateway start
```

That's it — three independent agents, each on its own process, restarting
automatically on crash and on user login.

## Start, stop, or restart all gateways at once

The CLI ships with single-profile lifecycle commands. To act across every
profile, wrap them in a shell loop. Put the snippet below in
`~/.local/bin/hermes-gateways` and `chmod +x` it:

```sh
#!/bin/sh
set -eu

# Add or remove profile names here as you create / delete profiles.
profiles="default coder personal-bot research"

usage() {
  echo "Usage: hermes-gateways {start|stop|restart|status|list}"
}

run_for_profile() {
  profile="$1"
  action="$2"
  if [ "$profile" = "default" ]; then
    hermes gateway "$action"
  else
    hermes -p "$profile" gateway "$action"
  fi
}

action="${1:-}"
case "$action" in
  start|stop|restart|status)
    for profile in $profiles; do
      echo "==> $action $profile"
      run_for_profile "$profile" "$action"
    done
    ;;
  list)
    hermes gateway list
    ;;
  *)
    usage
    exit 2
    ;;
esac
```

Then:

```bash
hermes-gateways start      # start every configured profile
hermes-gateways stop       # stop every configured profile
hermes-gateways restart    # restart all
hermes-gateways status     # status across all
hermes-gateways list       # delegates to `hermes gateway list`
```

:::tip
The `default` profile is targeted with `hermes gateway <action>` (no `-p`),
not `hermes -p default gateway <action>`. The wrapper above handles both forms.
:::

## Manage one profile

The shortcut commands every profile installs:

```bash
coder gateway run        # foreground (Ctrl-C to stop)
coder gateway start      # start the managed service
coder gateway stop       # stop the managed service
coder gateway restart    # restart
coder gateway status     # status
coder gateway install    # create the LaunchAgent / systemd unit
coder gateway uninstall  # remove the service file
```

These are equivalent to `hermes -p coder gateway <action>` — useful if a
profile alias is not on `PATH` or if you target profiles dynamically from a
script.

## Service files

Each profile installs its own service with a unique name, so installations
never clash:

| Platform | Path                                                              |
| -------- | ----------------------------------------------------------------- |
| macOS    | `~/Library/LaunchAgents/ai.hermes.gateway-<profile>.plist`        |
| Linux    | `~/.config/systemd/user/hermes-gateway-<profile>.service`         |

The default profile keeps the historical names: `ai.hermes.gateway.plist` /
`hermes-gateway.service`.

## Linux shutdown stability

If a Linux gateway appears to crash while stopping, first distinguish a
Python failure from a service-manager timeout. A common failure mode is that
the gateway receives `SIGTERM` and finishes its own shutdown, but a language
server or another descendant remains in the systemd cgroup. With
`KillMode=mixed`, systemd waits for the cgroup until `TimeoutStopSec` and then
escalates to `SIGKILL`. This can look like a gateway crash even when the
original process exited normally.

Use a user-service drop-in rather than editing the generated unit:

```ini
[Service]
KillMode=control-group
TimeoutStopSec=240s
Restart=on-failure
RestartSec=10s
```

Apply it and inspect the effective settings:

```bash
mkdir -p ~/.config/systemd/user/hermes-gateway.service.d
editor ~/.config/systemd/user/hermes-gateway.service.d/override.conf
systemctl --user daemon-reload
systemd-analyze --user verify ~/.config/systemd/user/hermes-gateway.service
systemctl --user show hermes-gateway.service \
  -p KillMode -p TimeoutStopUSec -p Restart -p RestartUSec
```

The gateway shutdown path also needs to clean up resources that are not
registered as terminal-tool processes:

- LSP clients start each server in its own POSIX process group. Shutdown sends
  `SIGTERM` to the group and uses a short `SIGKILL` fallback, which also
  reaps wrappers such as `pyright` and `typescript-language-server` children.
- Gateway shutdown explicitly stops the process-wide LSP service after tool,
  terminal-environment, and browser cleanup.
- The Feishu adapter disables reconnect, invokes the SDK's bounded `stop`,
  `close`, or `disconnect` primitive when available, then cancels the
  websocket loop and waits a bounded interval for its worker thread. A stuck
  SDK worker is logged rather than allowed to block the whole gateway forever.

For diagnosis, compare the service journal with the process tree and cgroup
state:

```bash
journalctl --user -u hermes-gateway.service -b --no-pager
systemctl --user status hermes-gateway.service
systemctl --user show hermes-gateway.service -p ControlGroup
systemd-cgls --user
```

Other checks from the incident that are worth keeping in the runbook:

- A tool binary must match the host OS and architecture. A macOS Mach-O
  `tirith` binary cannot execute on Linux/aarch64; replace it with a matching
  Linux build or disable `security.tirith_enabled` until one is available.
- Back up the session database before maintenance. Stop the gateway, run a
  SQLite integrity check, and checkpoint the WAL; do not delete the database
  or WAL files while the gateway is active. Enable session pruning and set a
  retention period after the backup.
- `hermes doctor` may still report unrelated environment issues (for example,
  a stale virtualenv entry point or missing `pytest`). Record those separately
  from service shutdown verification.

## Viewing logs

Each profile writes to its own log files:

```bash
# Default profile
tail -f ~/.hermes/logs/gateway.log
tail -f ~/.hermes/logs/gateway.error.log

# Named profile
tail -f ~/.hermes/profiles/<name>/logs/gateway.log
tail -f ~/.hermes/profiles/<name>/logs/gateway.error.log
```

Stream every profile's log simultaneously:

```bash
tail -f ~/.hermes/logs/gateway.log ~/.hermes/profiles/*/logs/gateway.log
```

The CLI also has a structured log viewer:

```bash
hermes logs --tail              # follow default profile
hermes -p coder logs --tail     # follow one profile
hermes logs --help              # filters, levels, JSON output
```

## Identify what's actually running

```bash
hermes profile list             # profiles + model + gateway state
hermes-gateways status          # full status across every profile
launchctl list | grep hermes    # macOS — PIDs and labels
systemctl --user list-units 'hermes-gateway-*'   # Linux — units
```

## Editing configuration

Every profile keeps its config inside its own directory:

```
~/.hermes/profiles/<name>/
├── .env              # API keys, bot tokens (chmod 600)
├── config.yaml       # model, provider, toolsets, gateway settings
└── SOUL.md           # personality / system prompt
```

The default profile uses `~/.hermes/` directly with the same three files.

Edit them with any editor or via the CLI:

```bash
hermes config set model.model anthropic/claude-sonnet-4    # default profile
coder config set model.model openai/gpt-5                  # named profile
```

After editing `.env` or `config.yaml`, restart the affected gateway:

```bash
coder gateway restart
# or, for everything:
hermes-gateways restart
```

## Keeping the host awake

The gateway process can run all day, but the operating system will still try
to sleep when idle. Two patterns:

### macOS — `caffeinate`

`caffeinate` is built into macOS and prevents sleep while it runs. No install.

```bash
caffeinate -dis                    # block display, idle, and system sleep
caffeinate -dis -t 28800           # same, auto-exit after 8 hours
caffeinate -i -w $(cat ~/.hermes/gateway.pid) &   # awake while default gateway runs

# Persistent: run in background and forget
nohup caffeinate -dis >/dev/null 2>&1 &
disown

# Inspect / stop
pmset -g assertions | grep -iE 'caffeinate|prevent|user is active'
pkill caffeinate
```

| Flag   | Effect                                            |
| ------ | ------------------------------------------------- |
| `-d`   | block display sleep                               |
| `-i`   | block idle system sleep (default)                 |
| `-m`   | block disk sleep                                  |
| `-s`   | block system sleep (AC-powered Macs only)         |
| `-u`   | simulate user activity (prevents screen lock)     |
| `-t N` | auto-exit after `N` seconds                       |
| `-w P` | exit when PID `P` exits                           |

:::warning Lid-close still sleeps the Mac
`caffeinate` cannot override the hardware-driven lid-close sleep on MacBooks.
For lid-closed operation, change your Energy Saver / Battery preferences or
use a third-party tool.
:::

### Linux — `systemd-inhibit` or `loginctl`

```bash
# Inhibit suspend while a command runs
systemd-inhibit --what=idle:sleep --who=hermes --why="gateways running" \
  sleep infinity &

# Allow user services to keep running after logout (recommended)
sudo loginctl enable-linger "$USER"
```

After enabling lingering, your systemd user units (including
`hermes-gateway-<profile>.service`) continue running across SSH disconnects
and reboots.

## Token-conflict safety

Each profile must use unique bot tokens for each platform. If two profiles
share a Telegram, Discord, Slack, WhatsApp, or Signal token, the second
gateway refuses to start with an error naming the conflicting profile.

To audit:

```bash
grep -H 'TELEGRAM_BOT_TOKEN\|DISCORD_BOT_TOKEN' \
     ~/.hermes/.env ~/.hermes/profiles/*/.env
```

## Updating the code

`hermes update` pulls the latest code once and syncs new bundled skills into
every profile:

```bash
hermes update
hermes-gateways restart
```

User-modified skills are never overwritten.

## Troubleshooting

### "Could not find service in domain for user gui: 501"

You ran `hermes gateway start` after a previous `hermes gateway stop`. The
CLI's `stop` does a full `launchctl unload`, which removes the service from
launchd's registry. The CLI catches this specific error on `start` and
automatically re-loads the plist (`↻ launchd job was unloaded; reloading
service definition`). The service starts normally. Nothing to fix.

### Stale PID after a crash

If a profile's gateway shows `not running` but a process is still alive:

```bash
ps -ef | grep "hermes_cli.*-p <profile>"
cat ~/.hermes/profiles/<profile>/gateway.pid
kill -TERM <pid>          # graceful
kill -KILL <pid>          # if that fails after a few seconds
<profile> gateway start
```

### Forcing a hard reset of one service

```bash
# macOS
launchctl unload ~/Library/LaunchAgents/ai.hermes.gateway-<profile>.plist
launchctl load   ~/Library/LaunchAgents/ai.hermes.gateway-<profile>.plist

# Linux
systemctl --user restart hermes-gateway-<profile>.service
```

### Health check

```bash
hermes doctor                  # default profile
hermes -p <profile> doctor     # one profile
```
