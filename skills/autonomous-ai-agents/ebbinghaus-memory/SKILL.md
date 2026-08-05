---
name: ebbinghaus-memory
description: "Use Ebbinghaus memory sleep, recall, dream, and decay."
version: 1.2.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [memory, ebbinghaus, sleep, recall, forgetting, dream, archive]
    related_skills: [hermes-agent]
    plugin: plugins/memory/ebbinghaus
    tools: [ebbinghaus_memory]
---

# Ebbinghaus Memory Skill

Use this skill to operate the bundled Ebbinghaus memory plugin as an agent memory routine. It covers durable recall, limited rehearsal, archive-first idle sleep, and provenance-backed dream consolidation.

This skill does not replace other memory providers. Use it when the active memory provider is `ebbinghaus` or when you are helping a user configure that provider.

## When to Use

- Use when the user asks about agent sleep, memory consolidation, Ebbinghaus forgetting, recall, rehearsal, decay, archive, or dream lessons.
- Use when durable user preferences or operational facts should be stored through the local `ebbinghaus_memory` tool.
- Use when idle memory maintenance should run through `memory.sleep` and the bundled `plugins/memory/ebbinghaus` provider.
- Do not use for unrelated knowledge-base providers such as Hindsight, Supermemory, ByteRover, OpenViking, RetainDB, or Holographic memory unless the task compares providers.

## Prerequisites

- The memory provider is `ebbinghaus`.
- The `ebbinghaus_memory` tool is available from the active memory provider.
- For idle sleep, `memory.sleep.enabled` is true and `memory.sleep.idle_after_seconds` is greater than zero.
- Persistent state is stored by the plugin under `HERMES_HOME`, using the provider's configured SQLite database path.

## How to Run

Use `ebbinghaus_memory` directly when the user asks for explicit memory work.

For manual memory writes, call:

```json
{"action":"remember","content":"User prefers Japanese status updates.","tags":"user-preference,communication","salience":0.9}
```

For recall before answering from memory, call:

```json
{"action":"recall","query":"Japanese status updates","limit":5}
```

For consolidation, call:

```json
{"action":"rehearse","query":"Japanese status updates","limit":1}
```

For sleep maintenance, call:

```json
{
  "action": "sleep",
  "limit": 1000,
  "prune_mode": "archive",
  "rehearse_threshold": 0.35,
  "forget_threshold": 0.10,
  "salience_keep_threshold": 0.80
}
```

For dream consolidation (no plugin-side LLM call):

```json
{"action":"dream","mode":"preview"}
```

Then synthesize a reusable lesson with the agent LLM and apply:

```json
{
  "action": "dream",
  "mode": "apply",
  "dreams": [
    {
      "cluster_id": "dream_20260720_001",
      "source_memory_ids": [12, 18],
      "summary": "Confirm consent before body manipulation in VRChat.",
      "tags": ["dream-summary", "semantic", "consent", "safety"],
      "salience": 0.85,
      "valence": -0.10
    }
  ]
}
```

## Quick Reference

| Action | Use |
|---|---|
| `remember` | Store a durable fact with cue tags and salience. |
| `recall` | Retrieve matching active memories and reinforce retrieval. |
| `rehearse` | Consolidate a known memory by id or query. |
| `decay` | Inspect low-retention traces and optionally prune. |
| `sleep` | Limited rehearse + archive/forget low-value traces. `limit` is a review batch, not total capacity. |
| `dream` | `preview` clusters candidates; `apply` stores semantic lessons with provenance. |
| `forget` | Delete one memory by `memory_id`. |
| `list` | Inspect stored memories. |
| `stats` | Inspect active/archived counts, capacity, and valence summary. |
| `revise` | Correct an existing belief; prefer this over remember for corrections. |
| `retract` | Retract a belief while preserving history. |
| `history` | Show belief versions when the user explicitly asks for history. |
| `events` | Inspect experiential events (miss, reactivation, revision). |
| `correction_check` | Replay a pending correction rehearsal without reinforcing. |
| `insight_propose` | Propose an insight candidate from an association preview. |
| `insight_validate` | Promote a candidate to semantic memory with non-empty evidence. |
| `insight_reject` | Keep a false insight on the ledger without deleting it. |

## Experiential memory rules

- When the user corrects an existing memory, use `revise` rather than `remember`.
- `history` is for explicit requests only; do not dump version chains by default.
- A rescued / reactivated memory is recalled context, not verified truth.
- Run association preview, then propose one hypothesis as an insight candidate.
- Never present an insight candidate as a settled fact.
- Do not call `insight_validate` with empty evidence.
- Do not delete rejected insights; keep them as false-insight history.
- When a validated insight's source is revised, treat the insight as contested.

## Important semantics

- `limit` on sleep is **not** total memory capacity. Capacity is `plugins.ebbinghaus.capacity.max_active_memories`.
- Lowering `forget_threshold` makes forgetting **slower** (wait for lower retention).
- `archive` keeps rows out of normal recall/prefetch; `delete` physically removes them.
- High salience still has `max_sleep_rehearsals`; strongly negative valence has a stricter cap.
- Safety-critical lessons should be dream-summarized; do not delete them merely for negative valence.
- Sleep is lazy maintenance before the next turn after idle — not a background thread.
- Do not hardcode `~/.hermes`; use `HERMES_HOME` / profile paths.

## Procedure

1. Check whether the task is about memory behavior, not ordinary file or session state.
2. If the answer depends on existing memory, call `ebbinghaus_memory` with `action="recall"` before relying on memory.
3. If the user gives a durable preference, fact, or operating constraint, call `action="remember"` with short tags and an appropriate salience value. If they correct an earlier memory, call `action="revise"` with a non-empty reason and optional evidence.
4. If a memory is important but retention is low, call `action="rehearse"` or include it in a sleep pass.
5. If the user asks for agent sleep or maintenance, call `action="sleep"` with `prune_mode="archive"` unless they explicitly demand delete.
6. For dream work: preview → LLM synthesis → apply. Never invent source ids.
7. For insight work: association preview → propose one candidate → validate only with evidence. Rejected candidates stay as false insights.
8. If the user asks to remove a memory, prefer `action="forget"` for a known `memory_id` (protected rows need explicit force). For belief retraction that keeps history, use `action="retract"`.

## Pitfalls

- Do not treat `sleep` as a background thread. The built-in idle path is lazy and runs before the next turn after the idle threshold.
- Do not permanently auto-rehearse high-salience memories every sleep forever.
- Do not assume a recalled memory is current truth. Use it as context, then verify live state when the fact can drift. Recovered latent/archived traces are especially provisional.
- Do not hardcode `~/.hermes` in instructions or code. The plugin is profile-aware through `HERMES_HOME`.
- Do not use this skill when `memory.provider` is set to another provider unless the user is switching to `ebbinghaus`.
- Do not validate insight candidates without evidence, and do not speak candidates as facts.

## Verification

- `ebbinghaus_memory` appears in the active tool list.
- `{"action":"stats"}` returns active/archived/capacity fields plus experiential counters when enabled.
- `remember` followed by `recall` returns the stored content.
- A sleep pass returns `mode: "sleep_cycle"` with `rehearsed`, `forgotten`, `archived`, and `pruned` arrays (and `latent` when experiential forgetting is enabled).
- Idle sleep is configured under `memory.sleep` if the user expects automatic agent sleep.
