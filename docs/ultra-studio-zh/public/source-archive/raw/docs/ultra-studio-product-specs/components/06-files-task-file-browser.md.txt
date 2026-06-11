# Files / Task File Browser

Status: partial — agent-side file tooling, chat attachment upload, sandbox
file sync, and tool-result persistence are implemented; the Files navigation
surface, task file browser UI, and promote-to-asset flow are spec-only.
Date: 2026-06-11

Sources:

- Docs: `docs/ultra-studio-product-specs/05-memory-marketplace-files.md`
  (§Files, §Search, §Access Control), `02-agent-runtime-contract.md`
  (§Task Files, §Session Lifecycle), `01-product-surface.md`
  (§Left Nav Shell), `03-media-asset-contract.md` (`task_file` asset type),
  `06-delivery-plan.md` (P0 item 3, P2 items 2-3)
- Code (verified this session): `tools/file_tools.py`,
  `tools/file_operations.py`, `tools/file_state.py`,
  `tools/path_security.py`, `tools/tool_result_storage.py`,
  `tools/environments/file_sync.py`,
  `web/src/components/chat/ChatComposer.tsx` (`uploadAttachment`)

## Purpose & Scope

Files are task/workspace objects, not necessarily reusable assets
(`05-memory-marketplace-files.md` §Files): uploaded originals, downloaded web
artifacts, generated task files, logs, prompt plans, storyboard sheets, and
rendered outputs. Task files become asset library entries only through
explicit registration or promotion (`02-agent-runtime-contract.md`
§Task Files).

This spec covers: the per-session task file root, file categories, the Files
nav surface and task file browser, upload intake, promotion to assets, and
the safety rules on file access. Asset semantics after promotion are owned by
`09-asset-service.md`; sandbox lifecycle by `14-sandbox-lifecycle.md`.

## Implementation Status

| Status | Item | Citation |
|---|---|---|
| Implemented | Agent file tools with per-task path resolution (`task_id`-scoped cwd) | `tools/file_tools.py` (`_resolve_path`, `_resolve_path_for_task`, `read_file_tool`) |
| Implemented | Path safety: blocked device paths, sensitive-path checks, cross-profile checks | `tools/file_tools.py` (`_is_blocked_device_path`, `_check_sensitive_path`, `_check_cross_profile_path`), `tools/path_security.py` |
| Implemented | Shell-backed file operations layer behind the tools | `tools/file_operations.py` (`ShellFileOperations`, via `_get_file_ops`), `tools/file_state.py` |
| Implemented | Read budget + dedup guards (capped read chars, repeated-read tracking, patch-failure tracking) | `tools/file_tools.py` (`_get_max_read_chars`, `_cap_read_tracker_data`, `_record_patch_failure`) |
| Implemented | Oversized tool results persisted to files with preview stubs | `tools/tool_result_storage.py` (`maybe_persist_tool_result`, `generate_preview`, `enforce_turn_budget`) |
| Implemented | Host/sandbox file sync for execution environments | `tools/environments/file_sync.py` |
| Implemented | Chat attachment upload from the composer (one file per action, disabled while uploading) | `web/src/components/chat/ChatComposer.tsx` (`uploadAttachment`, `uploadingAttachment`) |
| Specified, not built | Files nav entry and browsable Files page | `01-product-surface.md` §Left Nav Shell; no files page in `web/src/pages/` (listing verified this session) |
| Specified, not built | Task file browser (per-session tree of task files) | `06-delivery-plan.md` P2 item 2 |
| Specified, not built | Upload becomes a typed `media_input` asset record | `06-delivery-plan.md` P0 item 3, `03-media-asset-contract.md`; upload today lands as a chat attachment, not an asset row |
| Specified, not built | Promote file -> asset action | `05-memory-marketplace-files.md` §Files ("Files can be promoted into assets") |
| Specified, not built | File category labels (log, prompt plan, storyboard, rendered output, …) | `05-memory-marketplace-files.md` §Files |
| Specified, not built | Files in unified Search with typed cards | `05-memory-marketplace-files.md` §Search |
| Specified, not built | Artifact bundle export | `06-delivery-plan.md` P2 item 3 |

## User Entry Points

- Composer attachment button in chat (implemented:
  `web/src/components/chat/ChatComposer.tsx`).
- `Files` entry in the left nav opening the Files page (planned).
- Task detail view -> task file browser for that session (planned; task
  restore must include task files per `05-memory-marketplace-files.md`
  §Tasks).
- Inspector download/export actions referencing task files (see
  `03-inspector-live-panel.md`).
- Agent-initiated file creation during tool runs (implemented: file tools
  write under the task root; oversized tool results auto-persist via
  `tools/tool_result_storage.py`).

## Feature List

| Feature | Status |
|---|---|
| Per-session task file root tracked in session state | Partial — task-scoped path resolution exists (`_resolve_path_for_task`); "active task files root" as session state field is planned (`02-agent-runtime-contract.md` §Session Lifecycle) |
| Upload original files from chat | Implemented (composer) — typed `media_input` registration planned |
| Agent read/write/edit files with safety checks | Implemented (`tools/file_tools.py`, `tools/path_security.py`) |
| Persist oversized tool output as files | Implemented (`tools/tool_result_storage.py`) |
| Sync files into/out of sandbox environments | Implemented (`tools/environments/file_sync.py`) |
| Browse task files per session (tree + preview) | Planned |
| File categories (uploaded, downloaded, generated, log, prompt plan, storyboard, rendered) | Planned |
| Promote file to asset (explicit action) | Planned |
| Download a file from the UI | Planned (downloads must use real storage URLs or local materialization, `03-media-asset-contract.md` §Acceptance) |
| Search files with typed result cards | Planned |
| Artifact bundle export | Planned (P2) |
| Auto-promotion of files into assets | Excluded by spec ("should not automatically become reusable project assets") |

## State Machine

A task file's lifecycle:

```text
created (upload | tool write | result persistence | download capture)
  -> available           (readable in task root)
  -> promoted            (explicit promotion -> asset id minted; file remains)
  -> archived            (session archived; file kept per retention policy)
  -> deleted             (explicit delete or retention expiry)
```

- `created -> available` is immediate for tool writes; uploads pass through
  the upload-in-progress state visible in the composer
  (`uploadingAttachment`).
- `promoted` is one-way and explicit; the resulting asset's lifecycle is then
  owned by the Asset Service (`uploading -> processing -> ready`, see
  `09-asset-service.md`). The task file does not change identity.
- No state may be faked: a file row must correspond to a real file in the
  task root or object storage.

## APIs & Events

Implemented (agent tool surface): file read/write/edit tools registered in
the tool registry (`tools/file_tools.py`, dispatched through
`model_tools.py`); chat attachment upload via the web app's session API
(consumed by `ChatComposer`).

Planned (HTTP, for Files page and browser; no code exists):

```http
GET  /api/files?session_id=&category=&q=&cursor=     # list/filter
GET  /api/files/{file_id}                            # metadata + preview
GET  /api/files/{file_id}/download
POST /api/files/{file_id}/promote                    # -> asset id
```

Planned events (gateway event naming per `02-agent-runtime-contract.md`):

- `task_file.created`
- `task_file.promoted` (carries the new `asset_id`)

Promotion must call the Asset Service registration path
(`docs/hermes-asset-library-backend-design.md` §上传入库) rather than
duplicating asset creation logic.

## Data Model

Implemented: files live on disk under task-scoped roots; read/patch state is
tracked in-memory per task (`tools/file_tools.py` trackers). No file metadata
table exists.

Planned entity:

```text
task_files
- id
- session_id
- path                  (relative to task root)
- category: uploaded | downloaded | generated | log
            | prompt_plan | storyboard | rendered_output
- mime, size
- source_tool_call_id   (nullable)
- promoted_asset_id     (nullable)
- created_at
```

`task_file` is also a declared asset type in `03-media-asset-contract.md`
§Asset Types — meaning a not-yet-promoted file. The browser should read from
the task root as ground truth and treat the table as an index, so files
created directly by tools are never invisible.

## UI Behavior

- Files page lists files across sessions with category filter chips and a
  session filter; default scope is the current project.
- Task file browser inside a task view shows a tree of the session's task
  root with file preview (text, image thumbnails) and download.
- Each file row: name, category chip, size, source (upload / tool / result),
  created time, and a `Promote to asset` action where eligible.
- Promotion opens a confirm dialog stating the target asset type
  (`media_input` for originals) and never auto-selects bulk promotion.
- Internal filesystem paths are not exposed by default
  (`03-media-asset-contract.md` §Asset Card UI rule applies to file cards
  too); show task-root-relative paths.
- Empty state is blank, no sample files.

## Permissions & Error Handling

Permissions: files are scoped by session/project
(`05-memory-marketplace-files.md` §Access Control). Shared conversations do
not imply shared task files ("Shared conversations do not imply shared
sandbox or credentials"). Promotion requires `use` on the file and create
rights in the target project's asset scope.

Error contract:

| Error | Trigger | Today |
|---|---|---|
| `file_path_blocked` | Device/sensitive/cross-profile path access | Implemented as tool-level rejections (`tools/file_tools.py`, `tools/path_security.py`) |
| `file_read_budget_exceeded` | Read exceeds char cap | Implemented (`_get_max_read_chars` cap behavior) |
| `upload_mime_not_allowed` | Disallowed upload type | Planned (shared with Asset Service error table) |
| `file_not_found` | Stale id or deleted file | Planned |
| `promotion_failed` | Asset Service registration error | Planned; must surface the Asset Service error, not a generic apology |
| `asset_upload_failed` | Storage write failure during promotion | Typed in `02-agent-runtime-contract.md` §Error Contract |

Failures must be loud: a failed promotion leaves the file un-promoted with a
visible error; it must never mint a fake asset id.

## Acceptance Criteria

- An uploaded image appears in the session's task files and (post-P0) as a
  typed `media_input` asset usable as a generation reference
  (`06-delivery-plan.md` P0 gates).
- Files created during a work session are browsable from the task view
  (`06-delivery-plan.md` P2 gate: "Files created during work are
  browseable").
- Promoting a file produces a real asset id traceable in the Asset Service,
  and the file row links to it.
- A blocked path access from the agent produces a visible typed error in the
  tool stream.
- Search returns file results typed as files, distinct from assets.
- No file row exists without a real backing file.

## Non-Goals

- Auto-promotion of any file into the asset library.
- A general-purpose cloud drive (sharing, collaborative editing, versioning
  beyond what sessions need).
- Exposing absolute host filesystem paths in the UI.
- Replacing object storage for promoted assets (Asset Service owns that).
- Indexing file contents for semantic search in P0/P1 (filename/category
  filter first).

## Open Questions

1. Retention: how long do task files of archived sessions live, and where
   (host disk vs object storage)?
2. Is the Files page scoped to project or workspace by default, and how do
   CLI/TUI sessions' files appear (`source: web | tui | cli | panel` exists
   for tasks in `05-memory-marketplace-files.md` §Tasks)?
3. Does promotion copy the binary into object storage immediately or
   lazily on first use?
4. Are downloaded web artifacts (browser tool captures) auto-categorized as
   `downloaded`, and do they carry provenance (source URL) for the ledger
   (`16-observation-provenance-ledger.md`)?
5. Multi-file upload: the composer currently takes one file per action;
   is batch upload a P0 need?
6. Where does the task-files-root session field live in gateway session
   state (`gateway/session.py` `SessionEntry`) vs sandbox state?
