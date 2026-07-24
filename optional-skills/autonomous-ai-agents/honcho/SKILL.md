---
name: honcho
description: Configure and use Honcho memory with Hermes -- cross-session user modeling, multi-profile peer isolation, observation config, dialectic reasoning, session summaries, and context budget enforcement. Use when setting up Honcho, troubleshooting memory, managing profiles with Honcho peers, or tuning observation, recall, and dialectic settings.
version: 2.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Honcho, Memory, Profiles, Observation, Dialectic, User-Modeling, Session-Summary]
    homepage: https://docs.honcho.dev
    related_skills: [hermes-agent]
prerequisites:
  pip: [honcho-ai]
---

# Honcho Memory for Hermes

Honcho provides AI-native cross-session user modeling. It learns who the user is across conversations and gives every Hermes profile its own peer identity while sharing a unified view of the user.

## When to Use

- Setting up Honcho (cloud or self-hosted)
- Troubleshooting memory not working / peers not syncing
- Creating multi-profile setups where each agent has its own Honcho peer
- Tuning observation, recall, dialectic depth, or write frequency settings
- Understanding what the 5 Honcho tools do and when to use them
- Configuring context budgets and session summary injection

## Routing Table — read the reference for detail

| To do X | Read |
|:--------|:-----|
| Understand base-context injection, cold/warm start, peers, observation, sessions, recall modes; tune the three dialectic knobs (cadence/depth/level); set up multi-profile peers | `references/architecture.md` |
| Full per-tool detail for the 5 Honcho tools, bidirectional peer targeting, and agent usage patterns | `references/tools-and-usage.md` |
| Full config key reference, memory-context sanitization, troubleshooting, and all CLI commands | `references/config-and-cli.md` |

## Setup

### Cloud (app.honcho.dev)

```bash
hermes memory setup honcho
# select "cloud", paste API key from https://app.honcho.dev
```

### Self-hosted

```bash
hermes memory setup honcho
# select "local", enter base URL (e.g. http://localhost:8000)
```

See: https://docs.honcho.dev/v3/guides/integrations/hermes#running-honcho-locally-with-hermes

### Verify

```bash
hermes honcho status    # shows resolved config, connection test, peer info
```

## Core Model (at a glance)

Honcho models conversations between **peers** — a **user peer** (`peerName`, the human) and an **AI peer** (`aiPeer`, this Hermes profile). Each profile has its own AI peer but shares one workspace/user representation. In `hybrid` (default) and `context` recall modes, Honcho auto-injects a base context block (session summary → user representation → AI peer card) before every turn; in `tools` mode the agent fetches memory explicitly. Full detail: `references/architecture.md`.

## The 5 Tools (summary)

| Tool | LLM call? | Cost | Use when |
|------|-----------|------|----------|
| `honcho_profile` | No | minimal | Quick factual snapshot / name-role-pref lookups |
| `honcho_search` | No | low | Raw past-fact excerpts to reason over yourself |
| `honcho_context` | No | low | Full session snapshot: summary, representation, card, recent messages |
| `honcho_reasoning` | Yes | medium–high | Synthesized answer from the dialectic engine |
| `honcho_conclude` | No | minimal | Write/delete a persistent fact; `peer: "ai"` for self-knowledge |

All tools accept an optional `peer` (`user` default, `ai`, or explicit id). Per-tool detail and examples: `references/tools-and-usage.md`.

## Shortest End-to-End (conversation flow)

```
1. On start: honcho_profile                → fast warmup, no LLM cost
2. If context looks thin: honcho_context   → full snapshot, still no LLM
3. Only if deep synthesis needed: honcho_reasoning   → LLM call, use sparingly
4. When user shares a durable fact: honcho_conclude conclusion="<specific, actionable fact>"
5. To recall specifics later: honcho_search query="<topic>"  (escalate to honcho_reasoning if search isn't enough)
```

Do NOT call `honcho_reasoning` every turn — auto-injection already refreshes context. Do not re-fetch what was already injected. Full usage patterns (including `peer: "ai"` self-modeling and cadence awareness): `references/tools-and-usage.md`.

## Config & CLI

Config lives in `$HERMES_HOME/honcho.json` (profile-local) or `~/.honcho/config.json` (global). The full key reference (recall/observation/dialectic/context-budget), memory-context sanitization behavior, troubleshooting, and every `hermes honcho …` / `hermes memory …` command are in `references/config-and-cli.md`.
