# Continuable Children v1 — Durable ID + Settlement Notice + Report

Status: implemented (safe subset) · Area: delegation (`delegate_task`)

## Problem

`delegate_task` children have no durable identity today: a result entry tells
the parent *what* happened (`summary`, `status`) but not *which* child it was.
The ephemeral `subagent_id` (`sa-<task>-<hex>`) lives only for the process
lifetime, the child's persisted session id is never surfaced, and nothing
names the child when its background completion re-enters the conversation.
The parent cannot re-open a finished child's transcript by id, and a child
that dies with the parent cannot be revisited at all.

## Goal (v1)

Give a delegated child a **durable id** the parent can name, receive a
**settlement notice** keyed by that id, and **re-read the child's final
report** by id — without changing the default delegation model in any way.

## Design

### Opt-in flag: `delegate_task(continuable=True)`

Everything is additive and opt-in. The `continuable` parameter (boolean,
default `False`, exposed in the tool schema) is the only surface change to
`delegate_task`. When `False` (the default), result payloads, dispatch
payloads, and completion events are byte-identical to previous behavior. The
flag rides on each built child as `child._continuable` so every entry-
construction site (`_run_single_child`) and event relay sees it without extra
plumbing.

### 1. Durable ID — `child_session_id`

Each child is already an `AIAgent` with its own **persisted session id**:
`_build_child_agent` constructs it with the parent's `session_db` and
`parent_session_id`, and `AIAgent._ensure_db_session` creates a
`source='subagent'` session row in the same SQLite state DB, linked to the
parent. That session id is durable (survives process death) — it *is* the
child's stable identity. The ephemeral `subagent_id` is exposed alongside it
as a convenience for TUI/tooling correlation.

When `continuable=True`:
- every result entry (success, timeout, error, crash) gains
  `child_session_id` + `subagent_id` (`_attach_continuable_ids`);
- the background dispatch payload (`status: dispatched`) gains a `children`
  list naming each child up-front (`task_index`, `goal`, `child_session_id`,
  `subagent_id`).

### 2. Settlement notice — "Child <id> finished"

Hermes already has an async context-injection channel: background delegation
completions are pushed onto `process_registry.completion_queue` and
re-injected into the parent's conversation as a `[ASYNC DELEGATION COMPLETE
...]` block on the next turn. v1 uses that channel, keyed by the durable id:

- `_push_completion_event` copies `child_session_id`/`subagent_id` from the
  child result onto the completion event (batch events already carry the full
  per-task `results` list).
- `_format_async_delegation` renders a settlement line per child:
  - `Child <id> finished (status=completed).`
  - `Child <id> was stopped before it finished (status=interrupted).`
  - `Child <id> failed before it finished (status=<status>).`

This mirrors the continuation-manager pattern from the TS subagent package
(durable childId + one-line settlement summary), without copying its
registry/disposal machinery.

For synchronous delegations the notice is the tool result itself — the entry
now carries the ids, so the parent sees "which child" in the same turn.

### 3. Report v1 — re-read by id via `session_search`

No new tool. The child's transcript is persisted in the same session DB, so
the existing `session_search` **READ** shape (`session_id=<id>` only) returns
the child's full transcript, including its final answer. Subagent sessions
are excluded from *browse* (`_HIDDEN_SESSION_SOURCES`) but remain readable by
id — exactly the property REPORT v1 needs. The `continuable` schema
description tells the model to use `session_search` with
`session_id=<child_session_id>`.

## Files changed

- `tools/delegate_tool.py` — `continuable` param + schema property;
  `child._continuable`; `_continuable_child_ids` /
  `_attach_continuable_ids`; dispatch payload `children`.
- `tools/async_delegation.py` — `_push_completion_event` carries child ids.
- `tools/process_registry.py` — `_settlement_line` + rendering in
  `_format_async_delegation` (single and batch shapes).
- `run_agent.py` — `_dispatch_delegate_task` forwards `continuable`.
- `tests/tools/test_delegate_continuable.py` — new mock-based tests.

## Testing

`tests/tools/test_delegate_continuable.py` (25 tests, no LLM calls):

- default path emits no id fields (byte-identical guarantee);
- continuable success/crash/timeout entries carry both ids;
- background dispatch payload names children only when continuable;
- completion event carries child ids; re-injection block renders
  `Child <id> finished/failed/stopped` lines (single + batch);
- report path: a `source='subagent'` session with `parent_session_id` is
  re-readable via `session_search` READ by id.

## Deferred (explicitly NOT in v1)

- **send_message to a child** — messaging a settled child requires a durable
  per-child inbox and delivery into a live turn; out of scope.
- **interrupt of a detached child** — `interrupt_subagent`/`steer_subagent`
  work only while the child is live in-process (`_active_subagents`); a
  durable interrupt path for resumed children is out of scope.
- **cold resume of a child by the parent** — resuming a finished child's
  session (re-attach to its persisted conversation) is the natural next step
  once ids are durable; the resume machinery in `run_agent.py` already
  supports `parent_session_id` lineage, but wiring it to a resumed
  delegation is not done here.
- **durable settlement registry** — the TS reference keeps a per-child
  settlement/disposal registry; v1 relies on the session DB (authoritative
  transcript) + delegation records (event replay) instead of a new table.

## Known limitations

- `child_session_id` is only as durable as the session DB row: sessions
  pruned by retention, or a child whose first turn never created its row
  (e.g. crash before any API call), lose the transcript. The id itself is
  still returned.
- The settlement line is rendered for *background* completions; a
  synchronous result's notice is the tool output itself (no separate line).
- Nested orchestrator children are continuable too (the flag is inherited
  through `_build_child_agent`), but their ids appear on the *parent's*
  result, not on the top-level aggregation.
