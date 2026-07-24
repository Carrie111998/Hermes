# Honcho Tools & Agent Usage Patterns

## Tools

The agent has 5 bidirectional Honcho tools (hidden in `context` recall mode):

| Tool | LLM call? | Cost | Use when |
|------|-----------|------|----------|
| `honcho_profile` | No | minimal | Quick factual snapshot at conversation start or for fast name/role/pref lookups |
| `honcho_search` | No | low | Fetch specific past facts to reason over yourself — raw excerpts, no synthesis |
| `honcho_context` | No | low | Full session context snapshot: summary, representation, card, recent messages |
| `honcho_reasoning` | Yes | medium–high | Natural language question synthesized by Honcho's dialectic engine |
| `honcho_conclude` | No | minimal | Write or delete a persistent fact; pass `peer: "ai"` for AI self-knowledge |

### `honcho_profile`
Read or update a peer card — curated key facts (name, role, preferences, communication style). Pass `card: [...]` to update; omit to read. No LLM call.

### `honcho_search`
Semantic search over stored context for a specific peer. Returns raw excerpts ranked by relevance, no synthesis. Default 800 tokens, max 2000. Good when you need specific past facts to reason over yourself rather than a synthesized answer.

### `honcho_context`
Full session context snapshot from Honcho — session summary, peer representation, peer card, and recent messages. No LLM call. Use when you want to see everything Honcho knows about the current session and peer in one shot.

### `honcho_reasoning`
Natural language question answered by Honcho's dialectic reasoning engine (LLM call on Honcho's backend). Higher cost, higher quality. Pass `reasoning_level` to control depth: `minimal` (fast/cheap) → `low` → `medium` → `high` → `max` (thorough). Omit to use the configured default (`low`). Use for synthesized understanding of the user's patterns, goals, or current state.

### `honcho_conclude`
Write or delete a persistent conclusion about a peer. Pass `conclusion: "..."` to create. Pass `delete_id: "..."` to remove a conclusion (for PII removal — Honcho self-heals incorrect conclusions over time, so deletion is only needed for PII). You MUST pass exactly one of the two.

### Bidirectional peer targeting

All 5 tools accept an optional `peer` parameter:
- `peer: "user"` (default) — operates on the user peer
- `peer: "ai"` — operates on this profile's AI peer
- `peer: "<explicit-id>"` — any peer ID in the workspace

Examples:
```
honcho_profile                        # read user's card
honcho_profile peer="ai"              # read AI peer's card
honcho_reasoning query="What does this user care about most?"
honcho_reasoning query="What are my interaction patterns?" peer="ai" reasoning_level="medium"
honcho_conclude conclusion="Prefers terse answers"
honcho_conclude conclusion="I tend to over-explain code" peer="ai"
honcho_conclude delete_id="abc123"    # PII removal
```

## Agent Usage Patterns

Guidelines for Hermes when Honcho memory is active.

### On conversation start

```
1. honcho_profile                  → fast warmup, no LLM cost
2. If context looks thin → honcho_context  (full snapshot, still no LLM)
3. If deep synthesis needed → honcho_reasoning  (LLM call, use sparingly)
```

Do NOT call `honcho_reasoning` on every turn. Auto-injection already handles ongoing context refresh. Use the reasoning tool only when you genuinely need synthesized insight the base context doesn't provide.

### When the user shares something to remember

```
honcho_conclude conclusion="<specific, actionable fact>"
```

Good conclusions: "Prefers code examples over prose explanations", "Working on a Rust async project through April 2026"
Bad conclusions: "User said something about Rust" (too vague), "User seems technical" (already in representation)

### When the user asks about past context / you need to recall specifics

```
honcho_search query="<topic>"       → fast, no LLM, good for specific facts
honcho_context                       → full snapshot with summary + messages
honcho_reasoning query="<question>"  → synthesized answer, use when search isn't enough
```

### When to use `peer: "ai"`

Use AI peer targeting to build and query the agent's own self-knowledge:
- `honcho_conclude conclusion="I tend to be verbose when explaining architecture" peer="ai"` — self-correction
- `honcho_reasoning query="How do I typically handle ambiguous requests?" peer="ai"` — self-audit
- `honcho_profile peer="ai"` — review own identity card

### When NOT to call tools

In `hybrid` and `context` modes, base context (user representation + card + session summary) is auto-injected before every turn. Do not re-fetch what was already injected. Call tools only when:
- You need something the injected context doesn't have
- The user explicitly asks you to recall or check memory
- You're writing a conclusion about something new

### Cadence awareness

`honcho_reasoning` on the tool side shares the same cost as auto-injection dialectic. After an explicit tool call, the auto-injection cadence resets — avoiding double-charging the same turn.
