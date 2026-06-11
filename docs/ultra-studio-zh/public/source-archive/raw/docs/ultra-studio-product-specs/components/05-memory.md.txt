# Memory

Status: partial — agent-side memory storage, a provider plugin layer, and
prompt injection are implemented; the Memory product surface (visible,
editable, source-attributed entries in the UI) is spec-only.
Date: 2026-06-11

Sources:

- Docs: `docs/ultra-studio-product-specs/05-memory-marketplace-files.md`
  (§Memory, §Search, §Access Control, §Acceptance),
  `01-product-surface.md` (§Left Nav Shell), `02-agent-runtime-contract.md`
  (§Session Lifecycle), `06-delivery-plan.md` (P1 item 9),
  `docs/hermes-references-knowledge-model.md`
- Code (verified this session): `tools/memory_tool.py` (`MemoryStore`,
  `memory_tool`, `get_memory_dir`), `agent/memory_manager.py`
  (`MemoryManager`, `build_memory_context_block`, `sanitize_context`,
  `StreamingContextScrubber`), `agent/memory_provider.py` (`MemoryProvider`
  ABC), `plugins/memory/` (`byterover`, `hindsight`, `holographic`, `honcho`,
  `mem0`, `openviking`, `retaindb`, `supermemory`)

## Purpose & Scope

Memory stores durable facts that should influence future work: user
preferences, brand rules, project facts, reusable prompt decisions, model
preferences, rejected styles, and safety/policy notes
(`05-memory-marketplace-files.md` §Memory). The governing rule is "Memory
must be visible and editable. Hidden memory creates trust problems."

This spec covers the memory data layer, the provider plugin layer, prompt
injection, and the planned Memory page. It does not cover session transcripts
(owned by `07-tasks-session-history.md`) or RAM monitoring
(`gateway/memory_monitor.py` is process RSS logging, unrelated to this
component).

## Implementation Status

| Status | Item | Citation |
|---|---|---|
| Implemented | File-backed memory store with per-target files, char budgets (memory 2200 / user 1375 by default), file locking, and drift detection against external edits | `tools/memory_tool.py` (`MemoryStore.__init__`, `_file_lock`, `_drift_error`, `load_from_disk`) |
| Implemented | Agent-facing memory tool: add / replace / remove entries | `tools/memory_tool.py` (`MemoryStore.add`, `.replace`, `.remove`, `memory_tool`) |
| Implemented | Memory injection into the system prompt | `tools/memory_tool.py` (`format_for_system_prompt`), `agent/memory_manager.py` (`build_memory_context_block`) |
| Implemented | Context sanitization before injection (prompt-injection scrubbing) | `agent/memory_manager.py` (`sanitize_context`, `StreamingContextScrubber`), `tools/memory_tool.py` (`_scan_memory_content`) |
| Implemented | Pluggable external memory providers behind a `MemoryProvider` ABC | `agent/memory_provider.py`; `plugins/memory/mem0/__init__.py` (Mem0 Platform API, circuit breaker), plus `byterover`, `hindsight`, `holographic`, `honcho`, `openviking`, `retaindb`, `supermemory` |
| Specified, not built | Memory nav entry and Memory page (list, inspect, delete/revoke) | `01-product-surface.md` §Left Nav Shell; no memory page exists in `web/src/pages/` (listing verified this session) |
| Specified, not built | Source attribution per entry ("show source session or user action") | `05-memory-marketplace-files.md` §Memory; `MemoryStore` entries are plain strings without provenance fields |
| Specified, not built | User-authored vs inferred memory distinction | `05-memory-marketplace-files.md` §Memory |
| Specified, not built | Workspace/project scoping ("Memory is scoped by user/workspace/project") | `05-memory-marketplace-files.md` §Access Control; current store is per-Hermes-home, not per-project |
| Specified, not built | Memory in unified Search with typed result cards | `05-memory-marketplace-files.md` §Search |
| Specified, not built | "Memory can influence a follow-up request and can be inspected" P1 gate | `06-delivery-plan.md` P1 |

## User Entry Points

- `Memory` entry in the left nav opening the Memory page (planned).
- Agent-initiated writes during conversation via the memory tool
  (implemented: `tools/memory_tool.py` `memory_tool`).
- Implicit read path: every agent turn can receive the memory block in its
  system prompt (implemented: `format_for_system_prompt`,
  `build_memory_context_block`).
- Search results of type `memory` (planned).
- Task restore: reopening a task should bring "relevant memory" back into
  context (`05-memory-marketplace-files.md` §Tasks; planned).

## Feature List

| Feature | Status |
|---|---|
| Add / replace / remove memory entries from agent turns | Implemented (`MemoryStore.add/replace/remove`) |
| Char-budgeted storage with explicit limits per target | Implemented (`MemoryStore._char_limit`) |
| Concurrent-write safety and external-edit drift detection | Implemented (`_file_lock`, `_drift_error`) |
| Injection scrubbing of memory content before prompt use | Implemented (`sanitize_context`, `_scan_memory_content`) |
| External memory backends (Mem0, Supermemory, etc.) with failure circuit breakers | Implemented (`plugins/memory/*`; circuit breaker in `plugins/memory/mem0/__init__.py`) |
| Memory page: list entries for current workspace/project | Planned |
| Inspect entry: content, category, source session, created time | Planned |
| Delete / revoke from UI | Planned (delete exists only as agent tool `remove`) |
| User-authored vs inferred badge | Planned |
| Category taxonomy (preferences, brand rules, project facts, …) | Planned; current store has targets, not categories |
| Per user/workspace/project scoping | Planned |
| Never store provider secrets | Planned as a validation rule; scrubbing exists but no secret-pattern rejection on write is specified |
| Memory entries in unified Search | Planned |

## State Machine

Memory entries are not stateful objects today; a stored entry is either
present or absent (`MemoryStore` keeps lists of strings per target). The
planned product model adds a lifecycle:

```text
proposed (inferred by agent)
  -> active            (auto, or user confirms)
active
  -> revoked           (user delete/revoke from Memory page)
active
  -> superseded        (replace writes a new active entry)
```

- `proposed -> active` policy is an open question (auto-apply vs confirm).
- `revoked` entries must stop influencing prompts immediately and should
  remain auditable (who revoked, when).
- `superseded` preserves history for "why did the agent think this" digs.

Until that model exists, the implemented behavior is immediate-active on
`add` and hard-delete on `remove`.

## APIs & Events

Implemented (agent tool surface, not HTTP):

- `memory_tool(action=add|replace|remove, target, content, …)` —
  `tools/memory_tool.py` (`memory_tool`, dispatching into `MemoryStore`).
- `MemoryProvider` ABC for external backends — `agent/memory_provider.py`;
  providers configured via env/`$HERMES_HOME` config (e.g. `MEM0_API_KEY`,
  per `plugins/memory/mem0/__init__.py` docstring).

Planned (HTTP, for the Memory page; no code exists):

```http
GET    /api/memory?scope=workspace|project|user&category=&q=&cursor=
GET    /api/memory/{entry_id}
DELETE /api/memory/{entry_id}        # revoke
```

Planned events, following `02-agent-runtime-contract.md` naming:

- `memory.entry.created` (with `source: user | inferred`, session id)
- `memory.entry.revoked`

## Data Model

Implemented: plain-text entry lists per target file under the memory dir
(`tools/memory_tool.py` `get_memory_dir`, `_path_for`), with char budgets and
a rendered block format for prompts (`_render_block`).

Planned entity for the product surface:

```text
memory_entries
- id
- scope: user | workspace | project
- scope_id
- category: preference | brand_rule | project_fact | prompt_decision
            | model_preference | rejected_style | policy_note
- content
- source: user_authored | inferred
- source_session_id
- created_by, created_at
- status: active | revoked | superseded
```

Migration note: existing file-store entries map to
`scope=user, source=inferred, category=null` and must not be silently
dropped.

## UI Behavior

- Memory page lists entries grouped by category, scoped to the current
  workspace/project, with a scope switcher.
- Each row: content, category chip, source badge (user/inferred), source
  session link, created date, revoke button.
- Revoke asks for confirmation and takes effect on the next agent turn.
- Empty state is blank ("no memory yet"), never seeded with fake examples.
- A memory search result card must look like memory, not like a file
  (`05-memory-marketplace-files.md` §Search).
- The page never renders provider secrets; if scrubbing flags an entry, it
  renders with a warning state instead of raw content.

## Permissions & Error Handling

Permissions (`05-memory-marketplace-files.md` §Access Control): memory is
scoped by user/workspace/project; minimum verbs are read, update, delete,
revoke. Shared conversations must not leak another user's memory scope.

Error contract:

| Error | Trigger | Today |
|---|---|---|
| `memory_drift_detected` | Store file changed outside Hermes between load and write | Implemented as a structured error (`tools/memory_tool.py` `_drift_error`) |
| `memory_budget_exceeded` | Add exceeds target char limit | Implemented (limits in `MemoryStore`; add returns failure response) |
| `memory_entry_not_found` | Revoke/inspect of unknown id | Planned with the HTTP API |
| `memory_scope_denied` | Cross-workspace read/revoke | Planned |
| Provider outage | External provider (e.g. Mem0) unreachable | Implemented as circuit breaker pause (`plugins/memory/mem0/__init__.py`); UI surfacing planned |

Provider failures must degrade visibly (memory marked unavailable), not
silently produce a turn that ignores known facts.

## Acceptance Criteria

- The left nav exposes Memory; the page lists real entries from the store
  (blank when none — no sample data).
- A user can revoke an entry and a subsequent agent turn provably stops
  using it (verify via prompt block diff).
- New inferred entries appear with source session attribution.
- User-authored and inferred entries are visually distinct.
- A memory write containing a provider key pattern is rejected and surfaced
  as an error (never stored).
- `06-delivery-plan.md` P1 gate holds: memory influences a follow-up request
  and can be inspected.

## Non-Goals

- Embedding-based semantic memory retrieval as a P0/P1 requirement
  (external providers may offer it, but the product surface only requires
  list/inspect/revoke).
- Memory as a chat transcript archive (Tasks owns transcripts).
- Cross-tenant or cross-workspace memory sharing.
- Storing provider credentials or tokens of any kind.
- Automatic memory harvesting from every turn without an inferred-source
  trail.

## Open Questions

1. Do inferred entries auto-activate or require user confirmation
   (`proposed -> active` policy)?
2. Which store is authoritative when an external provider (Mem0 et al.) and
   the local file store disagree?
3. Per-project scoping: keyed how — project id from session state
   (`02-agent-runtime-contract.md` §Session Lifecycle), or directory-based?
4. Category taxonomy enforcement: free-form tags vs the fixed seven
   categories in `05-memory-marketplace-files.md`.
5. Retention: do revoked/superseded entries expire, and is there an export?
6. Does the Memory page edit content in place (`update` verb) or only
   revoke-and-recreate, given drift detection on the file store?
