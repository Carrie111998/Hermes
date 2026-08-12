---
name: fleet-ops
description: "Fleet operations and coordination — check fleet status, understand profile roles, manage cross-profile work, and coordinate multi-agent activity across the Hermes fleet."
version: 1.0.0
author: "SMF Works — Team Norddal (Lofoten Challenge)"
metadata:
  hermes:
    tags: [fleet, operations, coordination, multi-agent, monitoring]
    related_skills: [hermes-agent, gabriel-pm-operations]
---

# Fleet Operations Skill

Like the Lofoten fishing fleet during the Lofotfisket — the seasonal cod fishery where every boat coordinates to maximize the collective catch — this skill teaches agents to operate as part of a fleet, not just as isolated individuals.

## When to Use

Load this skill when you need to:
- Check what other Hermes profiles are doing or their status
- Coordinate work across multiple profiles
- Understand the fleet roster and role assignments
- Dispatch work to the right profile based on capabilities
- Monitor fleet health and activity
- Perform cross-profile operations

## Fleet Roster

The SMF Works fleet operates multiple Hermes profiles, each with a defined role:

| Profile | Role | Responsibilities |
|---------|------|-------------------|
| default (Dr J) | Hermes Infrastructure Lead | Hermes config, health, updates |
| gabriel | Chief of Staff & PM | Coordination, tracking, prioritization |
| aiona | Chief AI Research Scientist | Research, experiments, architecture |
| liam | Chief Development Officer | Engineering, code, development |
| pamela | Chief Marketing Officer | Brand, strategy, content direction |
| morgan | Social Media Manager | Social posts, engagement, scheduling |
| harry | Editor in Chief | Editorial quality, content review |
| nemo | LLM Engineer & Testing Lead | Model evaluation, testing |
| jasmine | Full Stack Dev | Remote PC — full-stack development |
| jeff | Developer/Microsoft AI | Remote PC — Microsoft AI integration |
| william | Book Writer/Ghostwriter | Remote PC — long-form writing |

**Always verify models via config.yaml** — profiles get reconfigured. Check `~/.hermes/profiles/<p>/config.yaml` and `~/.hermes/profiles/<p>/SOUL.md`.

## Fleet Status Check

### Quick status (all profiles)

```bash
hermes profile list
```

### Gateway status per profile

```bash
for p in default gabriel aiona liam pamela morgan harry nemo; do
  echo "=== $p ==="
  hermes --profile "$p" config 2>&1 | grep -E "Model:|provider:" | head -2
  systemctl --user status hermes-gateway${p:+-$p} 2>&1 | grep -E "Active:" | head -1
done
```

### Fleet activity (if fleet-pulse plugin installed)

Type `/fleet-pulse` in any session for a real-time overview of what every profile is doing — active sessions, tool usage, last activity timestamp.

### Per-profile health

```bash
hermes --profile <name> doctor
```

## Cross-Profile Operations

### Spawning work on another profile

```bash
# One-shot task
hermes --profile <name> chat -q "your task here"

# Background long task
hermes --profile <name> chat -q "long task" &
```

### Interactive session on another profile

```bash
# Via tmux for interactive sessions
tmux new-session -d -s <name> -x 120 -y 40 "hermes --profile <name>"

# Send a message
tmux send-keys -t <name> "your message" Enter

# Read output
tmux capture-pane -t <name> -p | tail -30
```

### Delegating to subagents

Use `delegate_task` for parallel subtasks. Each subagent gets its own isolated context. Batch multiple tasks for parallel execution.

## Fleet Coordination Patterns

### 1. Research → Build → Review

Like the stockfish trade route from Røst to Venice — a chain of value-adding steps:

1. **Research profile** (aiona) investigates and writes findings
2. **Build profile** (liam/jasmine) implements based on findings
3. **Review profile** (harry) checks quality and brand alignment

### 2. Parallel Sprint

Like the Lofoten fleet scattering to fishing grounds:

1. Define independent workstreams
2. Dispatch to multiple profiles simultaneously
3. Collect results and synthesize

### 3. Relay Handoff

Like the signal fires that guided boats through the Lofoten straits:

1. Profile A does its part and writes output to a shared file
2. Profile B reads the output and continues
3. Profile C finalizes and ships

## Common Fleet Tasks

### Check all profile models

```bash
for p in ~/.hermes/profiles/*/; do
  name=$(basename "$p")
  model=$(grep -E "^default:" "$p/config.yaml" 2>/dev/null | head -1)
  echo "$name: $model"
done
```

### Check gateway status across fleet

```bash
systemctl --user list-units 'hermes-gateway*' 2>&1 | grep -E "active|inactive|failed"
```

### Restart a profile's gateway

```bash
hermes --profile <name> gateway restart
```

### Install a skill on another profile

```bash
# Skills install to the active profile only.
# To install on another profile, copy the skill directory:
cp -r ~/.hermes/skills/<category>/<skill_name> \
      ~/.hermes/profiles/<target>/skills/<category>/
```

## Fleet Health Indicators

| Indicator | Healthy | Warning | Critical |
|-----------|---------|---------|----------|
| Gateway status | active (running) | activating | failed/stopped |
| Model config | matches SOUL.md role | stale model | missing config |
| Session count | reasonable for role | >1000 (prune needed) | DB >300MB |
| Cron jobs | firing on schedule | paused | never firing (gateway down) |
| Skills installed | covers role responsibilities | gaps in coverage | missing critical skill |

## Pitfalls

- **Profiles get reconfigured** — Always check config.yaml before assuming a profile's model or provider
- **Gateway crash loop** — If a profile's gateway won't start, check for missing messaging platforms (exit code 75 TEMPFAIL)
- **Port collisions after cloning** — .env overrides config.yaml for API_SERVER_PORT. Edit .env directly
- **Cross-profile skill install** — `hermes skills install` has no --profile flag. Copy directories manually
- **Concurrent repo editing** — When multiple agents push to the same repo, always `git pull --rebase` first
- **Cron jobs need running gateway** — If gateway is stopped, cron jobs exist but never fire

## Integration with fleet-pulse Plugin

If the `fleet-pulse` plugin is installed and enabled, you get real-time fleet awareness:

- `/fleet-pulse` — overview of all profiles
- `/fleet-pulse detail <name>` — detailed view of one profile
- `/fleet-pulse log` — recent activity log
- `/fleet-pulse reset` — clear all data

The plugin tracks sessions, tool calls, and activity timestamps automatically via hooks. No agent action required — just install and enable.