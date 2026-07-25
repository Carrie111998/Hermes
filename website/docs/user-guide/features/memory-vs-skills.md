---
sidebar_position: 3.5
title: "Memory vs. Skills"
description: "When to use Persistent Memory vs. Skills — the decision framework for what to persist and where"
---

# Memory vs. Skills

Hermes Agent has **two persistence mechanisms** — and knowing which one to use for a given piece of information is the difference between an agent that works smoothly and one that wastes tokens, re-learns the same thing repeatedly, or fills its memory with procedures that belong in a skill.

| | **Persistent Memory** | **Skills** |
|---|---|---|
| What it stores | Facts (who, what, where) | Procedures (how) |
| Loaded | Always — injected every session | On demand — progressive disclosure |
| Size limit | 2,200 chars (MEMORY) + 1,375 chars (USER) | No practical limit |
| Managed by | Agent via `memory` tool | Agent via `skill_manage` tool |
| Changes take effect | Next session (frozen snapshot) | Immediately on `/reload-skills` |
| Versioning | None — replace/remove only | Full patch/edit/delete lifecycle |

## The One-Question Test

**Is this a fact or a procedure?**

Ask yourself: does the information tell you *something about the world* or does it tell you *how to do something*?

| If it answers… | It belongs in… |
|---|---|
| "Who is the user?" | **Memory** |
| "What port does the service run on?" | **Memory** |
| "What's the project structure?" | **Memory** |
| "How do I deploy this service?" | **Skill** |
| "How do I debug this stack?" | **Skill** |
| "How do I generate a daily report?" | **Skill** |

## Decision Table

Answer each question with SÍ or NO:

| Question | If YES → |
|---|---|
| Does it contain steps or instructions for *doing* something? | **SKILL** |
| Is it a reproducible workflow (step 1, step 2, step 3)? | **SKILL** |
| Does it require exact commands or terminal sequences? | **SKILL** |
| Does it describe *how* to solve a recurring problem type? | **SKILL** |
| Is it a fact about the user (name, role, preference, correction)? | **MEMORY** |
| Is it an environment detail (path, version, IP, installed command)? | **MEMORY** |
| Is it contextual info that will still be true in weeks or months? | **MEMORY** |
| Is it an infrastructure observation (port, service, config)? | **MEMORY** |

## Edge Cases

Sometimes information has **both** natures — it describes a fact AND a procedure. Example:

> "The MySQL server runs on port 3306 in the `db-prod` container, and to connect you need to run `docker exec -it db-prod mysql -u root -p`."

**Rule for mixed cases:**
- The descriptive part (fact) → **Memory**
- The instructive part (how-to) → **Skill**

Split them:

```text
# Memory entry:
MySQL server runs on port 3306 in container db-prod

# Skill entry (how-to):
How to connect to MySQL: docker exec -it db-prod mysql -u root -p
```

## Lifecycle

```text
One-off fact (today is Tuesday, user said X) → DON'T persist
                                                    ↓
Durable fact (preference, path, version)         → MEMORY
                                                    ↓
Procedure used 1-2 times                          → MEMORY (temporary)
                                                    ↓
Procedure used 3+ times or clearly reusable       → SKILL
                                                    ↓
Procedure that evolves (gets corrected, improved) → PATCH on existing SKILL
```

## Practical Examples

| Situation | Type | Action |
|---|---|---|
| "User prefers concise responses" | User fact | `memory(target='user')` |
| "OpenClaw runs on port 18789" | Infrastructure fact | `memory(target='memory')` |
| "How to debug Node.js: 1) start with --inspect 2) connect DevTools 3) set breakpoints" | Reusable procedure | `skill_manage(action='create')` |
| "The daily report cron runs at 06:00" | Configuration fact | `memory(target='memory')` |
| "How to generate a daily report with SearXNG data" | Procedure | `skill_manage(action='create')` |

## Common Mistakes

### ❌ Putting procedures in memory

```python
# BAD — this is a procedure, not a fact
memory(action="add", target="memory",
       content="To deploy: git pull, docker compose up -d --build, check logs")
```

Memory fills up fast (2,200 chars) and you cannot version or patch it. A procedure belongs in a skill where it can be edited, versioned, and loaded on demand.

### ❌ Putting facts in skills

```yaml
# BAD — this skill hardcodes infrastructure facts
name: deploy-config
description: Server configuration
---
The server is at 192.168.1.100
DB password is xyz
Redis runs on port 6379
```

Skills are for methodology, not data. Hardcoded facts make skills fragile and hard to share. Facts change; keep them in memory where they are easy to update.

### ❌ Not persisting at all

If the agent solves the same problem three times from scratch, that is a signal it should have created a skill (for the procedure) or saved a memory (for the fact). The agent now handles this proactively via periodic nudges and the [Curator](/features/core/curator), but you can always prompt it explicitly.

## Quick Reference

```python
# Memory — for facts
memory(action="add", target="memory", content="Project uses FastAPI + PostgreSQL")
memory(action="add", target="user", content="User prefers concise answers")

# Skills — for procedures
skill_manage(action="create", name="deploy-api",
             content="How to deploy the FastAPI service...")
```

## See Also

- [Persistent Memory](/features/core/persistent-memory)
- [Skills System](/features/core/skills)
- [Curator](/features/core/curator) — automatic skill lifecycle management
- [Personality & SOUL.md](/features/core/personality) — identity, not storage
