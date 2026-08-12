# Memory Duo Production Provenance Fix

## Goal

Allow an explicit direct user request to add, correct, or forget built-in Hermes memory to promote the corresponding Memory Duo mutation, while keeping autonomous assistant, background-review, synthetic, quoted, and model-supplied content non-user-authoritative.

## Root cause

The real bridge already gates external mirroring on a successful built-in memory mutation and calls `AIAgent._build_memory_write_metadata()` from the tool-execution path. The metadata currently preserves only the mechanical `write_origin`, which is `assistant_tool` for a normal foreground agent. Obsidian Duo therefore correctly stages the candidate as agent/unverified. The missing signal is trusted causal provenance for the current direct user turn.

## Design

### Host provenance

Add a small generic Hermes-side provenance helper under `agent/` with the closed set `explicit_remember`, `explicit_update`, `explicit_forget`, and `none`. At turn setup, classify only the clean current-turn user input: the persistence override when present, otherwise the inbound message. Skill scaffolding is reduced through the existing `extract_user_instruction_from_skill_message()` helper. The classifier is deterministic, conservative, and fail-closed:

- positive forms must be a direct imperative at the beginning of the clean user instruction;
- quoted/explanatory questions, external-note descriptions, prompt-injection text embedded in a note, and negated “do not remember” requests do not match;
- synthetic/display-only turns and background-review agents are always `none`;
- no model tool argument participates in classification.

The host stores the result on the live agent for the duration of the turn. `build_memory_write_metadata()` emits both the existing mechanical `write_origin` and the host-owned `user_memory_intent` plus boolean `host_confirmed_user_memory`. The boolean is true only for an explicit classified intent on a non-background, non-synthetic foreground turn. Tool arguments cannot override these fields.

### Memory Duo authority

Obsidian Duo will require the host-owned confirmation boolean and an allowed explicit intent before assigning `Authority.USER` / `Verification.USER_CONFIRMED`. A bare `write_origin: user`, model-supplied `host_confirmed_user_memory`, or ordinary `assistant_tool` origin is insufficient. Unconfirmed writes retain the existing agent/unverified staging path.

### Add, replace, and remove

- `add`: preserve the current candidate promotion path when trusted explicit intent is present.
- `replace`: use the built-in `old_text` as an exact match against one active Memory Duo record of the target type. If exactly one match exists, attach `contradicts` and `user_correction` provenance so the new record is promoted and the old record is superseded with history preserved. Zero or multiple matches fail closed and remain auditable as staged pending correction.
- `remove`: use the same exact, unique active-record match. If found, archive the existing record and rewrite only its managed note metadata to `archived` while preserving its content/history; no empty record is created. If no unique match exists, stage an auditable pending correction and change nothing else.

The broker’s existing SQLite versions, relationships, journal, managed-note, and retrieval status contracts remain the source of truth. Retrieval already excludes superseded/archived records.

## Data flow

```text
clean direct user turn
  -> host classifier at turn setup
  -> agent-owned current-turn provenance
  -> successful builtin memory mutation
  -> MemoryManager.notify_memory_tool_write
  -> build_memory_write_metadata
  -> ObsidianDuoMemoryProvider
  -> broker promote/supersede/archive or safe staging
  -> SQLite + managed Markdown
```

## Testing

Tests will cover direct remember, autonomous assistant write, quoted phrase, external/prompt-injection wording, background review, synthetic turn, explicit update, explicit forget, and model spoof attempts. An integration-style test will exercise the actual MemoryManager successful-write bridge through Obsidian Duo into SQLite and Markdown. Existing provider, broker, memory-tool, turn-context, background-review, routing, whole-vault, security, and other-provider tests will be rerun. Production re-smoke will use a new phrase and will credit deep recall only after checking both SQLite and managed Markdown.

## Safety boundaries

The protected dirty checkout and existing live state remain untouched during development. No model, API key, vector database, sync daemon, or unrelated refactor is introduced. The existing rollback bundle remains in place. The PR is not merged/tagged, and Autopilot Orchestrator work is out of scope.
