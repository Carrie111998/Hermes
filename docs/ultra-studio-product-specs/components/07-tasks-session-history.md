# Tasks / Session History

Status: partial — session storage, a sessions browser with message search,
and session-context prompts are implemented; the Tasks product surface (task
rows with jobs/outputs, full-context restore) is spec-only.
Date: 2026-06-11

Sources:

- Docs: `docs/ultra-studio-product-specs/05-memory-marketplace-files.md`
  (§Tasks, §Search, §Access Control), `02-agent-runtime-contract.md`
  (§Session Lifecycle, §Event Stream), `01-product-surface.md` (§Main Jobs:
  "Continue work"), `06-delivery-plan.md` (P0 gates, P2 gate on job
  survival)
- Code (verified this session): `gateway/session.py` (`SessionStore`,
  `SessionEntry`, `SessionContext`, `SessionSource`, `build_session_key`,
  `build_session_context_prompt`, hashed ids via `_hash_sender_id` /
  `_hash_chat_id`), `web/src/pages/SessionsPage.tsx` (`SessionRow`,
  `MessageList`, `SessionsPagination`, search highlighting),
  `tools/session_search_tool.py`, `hermes_state.py`, `agent/insights.py`
  (`InsightsEngine._get_sessions`)

## Purpose & Scope

Tasks represent work history and running jobs
(`05-memory-marketplace-files.md` §Tasks). The product promise is
"Continue work": clicking a task restores the transcript, active/complete
jobs, task files, selected model, active skill profile, and relevant memory
(`01-product-surface.md` §Main Jobs; `05-memory-marketplace-files.md`
§Tasks).

This spec covers the task list surface, the task row contract, restore
semantics, and the relationship between "session" (runtime conversation
state, owned by the gateway) and "task" (the product object users browse).
Media job durability itself is owned by `10-media-job-service.md`; the chat
surface that a restored task opens into is `02-creative-chat-ui.md`.

## Implementation Status

| Status | Item | Citation |
|---|---|---|
| Implemented | Durable session store keyed by platform/source, with session entries and context | `gateway/session.py` (`SessionStore`, `SessionEntry`, `build_session_key`) |
| Implemented | Privacy-hashed identifiers in session records | `gateway/session.py` (`_hash_sender_id`, `_hash_chat_id`) |
| Implemented | Session-context prompt assembly for resumed/multi-user contexts | `gateway/session.py` (`build_session_context_prompt`, `build_session_context`, `is_shared_multi_user_session`) |
| Implemented | Sessions browser: list, paginate, open transcript, message search with hit highlighting and auto-scroll | `web/src/pages/SessionsPage.tsx` (`SessionRow`, `MessageList`, `SnippetHighlight`, `SessionsPagination`) |
| Implemented | Tool calls rendered inside historical transcripts | `web/src/pages/SessionsPage.tsx` (`ToolCallBlock`) |
| Implemented | Agent-facing search over past sessions | `tools/session_search_tool.py` |
| Implemented | Session aggregates for analytics (count, duration, model/platform breakdowns) | `agent/insights.py` (`InsightsEngine._get_sessions`, `_compute_overview`) |
| Implemented | Process-level state persistence helpers | `hermes_state.py` |
| Specified, not built | `Tasks` nav entry distinct from the admin Sessions page | `01-product-surface.md` §Left Nav Shell |
| Specified, not built | Task row fields: title, last user request, status, active jobs, output count, source | `05-memory-marketplace-files.md` §Tasks; `SessionsPage` rows show session metadata, not job/output counts |
| Specified, not built | Full restore: transcript + jobs + task files + model + skill profile + memory | `05-memory-marketplace-files.md` §Tasks; `02-agent-runtime-contract.md` `session.resume` |
| Specified, not built | Session state carrying active media jobs, selected assets, task file root, skill profile | `02-agent-runtime-contract.md` §Session Lifecycle |
| Specified, not built | "Refreshing the browser during a media job does not lose the job" | `02-agent-runtime-contract.md` §Acceptance; depends on `10-media-job-service.md` |

## User Entry Points

- `Tasks` entry in the left nav (planned); today the closest surface is the
  dashboard Sessions page (`web/src/pages/SessionsPage.tsx`).
- "Open the previous … task" phrasing in chat — the agent finds prior work
  via session search (implemented: `tools/session_search_tool.py`) and the
  router/user restores it (restore flow planned).
- Cross-surface Search returning task results (planned,
  `05-memory-marketplace-files.md` §Search).
- Deep link from a media job or asset back to its originating task
  (planned; lineage carries `user/session/run` per
  `03-media-asset-contract.md` §Lineage).

## Feature List

| Feature | Status |
|---|---|
| List past sessions with pagination | Implemented (`SessionsPage`) |
| Search messages across sessions with highlighted hits | Implemented (`SessionsPage` search + `SnippetHighlight`) |
| Open a historical transcript incl. tool calls | Implemented (`MessageList`, `ToolCallBlock`) |
| Agent-side recall of past sessions | Implemented (`tools/session_search_tool.py`) |
| Task rows with status, active jobs, output count, date, source | Planned |
| Source labels `web / tui / cli / panel` on task rows | Planned (`SessionSource` exists in `gateway/session.py`; not surfaced as the spec's four product values) |
| One-click restore into a live chat session | Planned (`session.resume` contract) |
| Restore selected model + active skill profile | Planned; session state fields not yet in `SessionEntry` |
| Restore active/complete media jobs into the task view | Planned; depends on durable MediaJob records |
| Restore task files and relevant memory | Planned |
| Running-job indicator on the task list | Planned |
| Rename / archive / delete a task | Planned; not in spec pack — see Open Questions |

## State Machine

Task status (product-level, derived — not stored as a single enum today):

```text
active      (live session; gateway holds runtime state)
  -> idle   (no live connection; transcript + state durable)
  -> resumed -> active
idle | active
  -> archived (explicit user action; read-only)
```

Derived display status on a task row combines session liveness with job
state from the Media Job Service:

| Display | Condition |
|---|---|
| Running | ≥1 active media job, regardless of websocket liveness |
| Waiting | Pending approval or ask-user question (`approval.requested` outstanding) |
| Idle | No live connection, no active jobs |
| Failed | Last job/turn ended in a typed error |

Rule: a refresh or disconnect must never transition a task to a terminal
state; only explicit archive does (`02-agent-runtime-contract.md`
§Acceptance).

## APIs & Events

Implemented:

- Gateway session lifecycle and storage — `gateway/session.py`
  (`SessionStore` create/lookup; session keys via `build_session_key`).
- Sessions listing/read APIs consumed by `SessionsPage` (served by the
  dashboard web server; page verified, route shape not re-derived here).
- Session search tool for the agent — `tools/session_search_tool.py`.

Planned (per `02-agent-runtime-contract.md` §Session Lifecycle):

- `session.create`, `session.resume` (restore messages, active jobs,
  selected assets, task files), `prompt.submit`, `slash.exec`.
- Task list API:

```http
GET /api/tasks?project_id=&status=&source=&q=&cursor=
GET /api/tasks/{task_id}            # row + restore manifest
POST /api/tasks/{task_id}/archive
```

Events: task rows update from the existing gateway event stream
(`media_job.created/updated`, `asset.ready`, `approval.requested` — see
`02-agent-runtime-contract.md` §Event Stream); no separate task event
channel.

## Data Model

Implemented: session entries and context in the gateway store
(`gateway/session.py`: `SessionEntry` with hashed sender/chat ids,
`SessionContext`), plus analytics projections in the insights DB
(`agent/insights.py`).

Planned task projection (a view over sessions + jobs, not a second source of
truth):

```text
task_row
- task_id            (= session id)
- title              (generated; agent/title_generator.py exists)
- last_user_request
- status             (derived; see State Machine)
- active_job_ids[]   (from Media Job Service)
- output_count       (ready assets linked to this session)
- source: web | tui | cli | panel
- project_id, workspace_id
- created_at, last_activity_at

restore_manifest
- transcript ref
- active/complete job ids
- task_files root
- selected model
- active skill profile
- memory scope refs
```

Restore must read each element from its owning component (jobs from Media
Job Service, files from task root, memory from Memory) — the manifest is
pointers, not copies.

## UI Behavior

- Tasks page lists rows: title, status chip, last request snippet, active
  job spinner, output count, source badge, date. Default sort:
  last activity desc.
- Clicking a row opens the task view: restored transcript in the center,
  task file browser tab, and the Inspector showing the most recent job/asset
  (per `01-product-surface.md` §Required States).
- A running task opened mid-job shows live job state from resumed events,
  not a frozen snapshot.
- Search hits deep-link into the transcript position (the existing
  auto-scroll-to-hit behavior in `SessionsPage` `MessageList` is the
  reference implementation).
- Archived tasks render read-only with a visible archived banner.
- Empty state is blank; no demo tasks.

## Permissions & Error Handling

Permissions: tasks are scoped like their sessions (user/workspace/project).
Shared conversations do not imply shared sandbox or credentials
(`05-memory-marketplace-files.md` §Access Control); a shared task view must
exclude task files and credentials unless explicitly shared.

Error contract:

| Error | Trigger |
|---|---|
| `session_not_found` | Stale task id or cross-project access. |
| `resume_state_incomplete` | Restore manifest references missing pieces (e.g. job record gone). Must list what failed to restore; never silently render a partial state as complete. |
| `sandbox_unavailable` | Resume requires a sandbox that cannot be attached (typed in `02-agent-runtime-contract.md` §Error Contract). |
| `archive_failed` | Archive action failed; task stays in prior state. |

Partial restore is allowed only with an explicit per-element failure notice
(e.g. "2 task files missing"); claiming a full restore without all elements
verified violates the no-fake rule of the pack.

## Acceptance Criteria

- Tasks page lists real sessions with derived status; a session with an
  active media job shows Running even after browser refresh
  (`02-agent-runtime-contract.md` §Acceptance).
- Clicking a task restores transcript and shows active jobs and their
  current states ("A resumed session shows active media jobs and their
  current states").
- Restored task shows the same selected model and skill profile the session
  last used (once those fields land in session state).
- Message search finds a phrase from a prior session and deep-links to it.
- Source badges reflect the real origin surface.
- Archiving is explicit, reversible state is preserved, and archived tasks
  remain searchable.

## Non-Goals

- Project management features (assignees, due dates, kanban) — the existing
  `plugins/kanban` is a separate plugin, not this surface.
- Cross-user task assignment or shared editing.
- Storing job/asset state in the task projection (owned by Media Job /
  Asset services).
- Synthetic task titles that misrepresent content (titles come from the
  existing title generation path, `agent/title_generator.py`).

## Open Questions

1. Is `task_id` literally the session id, or can one task span multiple
   sessions (e.g. resumed across surfaces)?
2. Rename/archive/delete semantics are not in the spec pack — which verbs
   ship at P1?
3. How do non-web sessions (TUI/CLI/gateway platforms) map to the four
   product source values, given `gateway/session.py` `SessionSource` carries
   richer platform info?
4. Does restore re-attach the previous sandbox (`sandbox.attach`) or start
   cold with `restore_artifacts` (`02-agent-runtime-contract.md` §Sandbox
   Lifecycle)?
5. Output count definition: ready assets only, or all finalized job outputs
   including failed-but-inspectable?
6. Retention/TTL for idle tasks and their transcripts; relationship to the
   insights DB which already aggregates session stats.
