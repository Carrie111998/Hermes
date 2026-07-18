---
name: session-sidebar-sync
description: Use when leased native Claude or Hermes sessions are pending delivery to the Codex sidebar.
---

# Session Sidebar Sync

## Overview

Deliver one bounded broker batch as native local Codex tasks. Preserve the broker lease, authenticated registration, native project grouping, and source identity; registration is not a transcript migration.

## Quick Reference

| Situation | Required action |
|---|---|
| No actionable pending or retry jobs | End with no user-facing message. |
| Bridge or native project preflight is unhealthy | End without leasing; no job attempt is consumed. |
| Exact source cwd is a saved project | Use that project. |
| Otherwise, exact git root is a saved project | Use that project. |
| Neither path is a saved project | Use the saved `Session Inbox` project rooted at the canonical local `.hermes` path. |
| An earlier create may have succeeded | Reconcile its authenticated marker before any create. |
| A native create outcome is ambiguous | Do not create again; settle the lease for later reconciliation. |
| A returned thread ID is not readable or still active | Poll the exact ID until it is indexed and idle before rename or commit. |

## Authenticated Local Transport Fallback

Use the native `session_bridge` MCP tools when they are callable in the current task. If, and only if, those MCP tools are absent from the task's tool schema, use this authenticated loopback command prefix for the corresponding bridge operation:

```powershell
uv run --project "C:\\Users\\diego\\.hermes\\worktrees\\session-bridge-ship" --no-sync python -m session_bridge.broker_client status|pending|bind|commit|fail
```

The subcommands map one-for-one to `session_status`, `session_sidebar_pending`, `session_sidebar_bind`, `session_sidebar_commit`, and `session_sidebar_fail`. Supply `pending --limit 5`, `bind --lease-token <exact token> --thread-id <threadId>`, `commit --lease-token <exact token> --thread-id <threadId>`, or `fail --lease-token <exact token> --error-code <fixed code>` as required. Parse the command's JSON stdout as the bridge tool result.

Each fallback invocation counts as the exact single bridge call required by the procedure. Select one bridge transport for each step and never call both transports for the same bridge step. Do not retry a fallback invocation whose result is ambiguous, and do not mutate the bridge database directly. If neither transport is available, stop before leasing. Native Codex project and task operations still use the native app tools; this fallback applies only to the five bridge operations above.

## Procedure

1. Call `session_status` exactly once before any native project call or lease. Require `health.running` and the watcher to be running, require every reported provider `degraded_reason` to be null, and read `sidebar.counts` without broadening the request. If status is unavailable or malformed, a health requirement fails, or both `sidebar_pending` and `sidebar_retry` are zero, end immediately with no user-facing message. Do not call `session_sidebar_pending`, and no job attempt is consumed.
2. Call the native tool `list_projects({})` exactly once before leasing. Read every returned saved project's canonical local path, and index that canonical path to its returned `projectId` as (`projectId`, original returned `hostId`, normalized host). Normalize a missing or null `hostId` and the explicit string `local` to the current-local sentinel `local`. Reject every other explicit host value from this local-sidebar run; never infer or coerce an arbitrary host string. Do not call the tool again for another job. If the call or project-map construction fails, do not call `session_sidebar_pending`; end without a lease, do not call `session_sidebar_fail`, and no job attempt is consumed. Only after the native project map is valid, call `session_sidebar_pending(limit=5)` exactly once. Never create a task without a lease from this batch; this batch contains at most five jobs. If `jobs` is empty, end immediately with no user-facing message. Process independent native searches, creates, indexing polls, renames, and commits concurrently across leases, but preserve the procedure order within each lease. Settle every lease even when another lease fails, and never let one job's result authorize a replacement for another job.
3. For each leased job, choose its sidebar project in this exact order:
   1. the saved project whose canonical path equals the job's exact cwd;
   2. the saved project whose canonical path equals the job's exact git root;
   3. the saved `Session Inbox` project whose canonical path is the local `.hermes` directory.
4. Treat project choice as sidebar grouping only. Sidebar grouping never changes command cwd, and registration never runs project commands. The job's exact source cwd remains authoritative for later command and file operations after continuation.
5. When `reconcile_required` is true or `recovered_thread_id` is returned, reconcile before creating anything. Extract the exact authenticated signed marker from `registration_prompt`.
   - When `recovered_thread_id` is present, it is the bridge-authenticated candidate. Do not call `list_threads` before this recovered-ID read. Call `read_thread({"threadId":"<recovered_thread_id>","turnLimit":10,"includeOutputs":false})` directly and pass no other fields. On success, inspect the response's nested `thread` object; the absence of a top-level thread ID is expected. Use `thread.id`, `thread.hostId`, and `thread.cwd` as the returned identity fields: require the ID to equal `recovered_thread_id`; missing or null `thread.hostId` and explicit `local` normalize only to `local`; every other explicit `thread.hostId` maps to `codex_thread_conflict`; and the normalized task host must equal the chosen project's normalized host. Require the cwd or any supplied project identity to match the chosen project's canonical path or identity. The native `read_thread` response does not return an explicit environment field for an ordinary local task; that omission must not be treated as unavailable or ambiguous because the native read tool is itself the task surface. Reject the task only when a supplied project, environment, or task-kind field explicitly contradicts local native execution. Then verify the exact signed marker in the read result's bounded turns. Only that exact recovered ID may be accepted; a missing or mismatched task maps to `marker_conflict` and never permits creation. A remote host, wrong project, explicitly non-native task, or explicitly non-local environment maps to `codex_thread_conflict` and never permits creation.
   - Only when `recovered_thread_id` is absent, call `list_threads({"query":"<exact signed marker>","limit":20})`. Normalize and filter each `list_threads` candidate summary before any `read_thread` call. Apply the same host normalization to every thread candidate: missing, null, and explicit `local` normalize only to `local`. Require that normalized host is `local` and equals the chosen project's normalized host; an explicit non-`local` host maps to `codex_thread_conflict` without a read and is never reused or replaced. Compare the candidate summary's project with the chosen project only when that summary supplies project identity; do not invent a missing project field. For each surviving candidate, call `read_thread({"threadId":"<candidate threadId>","hostId":"<candidate hostId>","turnLimit":10,"includeOutputs":false})`. Pass the original candidate `hostId` unchanged when it is non-null; Omit `hostId` only when it was absent or null. Pass no other fields. Ten is the bounded reconciliation and read limit; never paginate or broaden the query during this batch. Inspect the read response's nested `thread` object using the same schema rules above, substituting the exact candidate thread ID for `recovered_thread_id`. Before matching the signed marker, require the read result to belong to the chosen local project and host identity; omission of an explicit environment or task-kind field is acceptable, while an explicit contradiction maps to `codex_thread_conflict`. A remote-host candidate or other remote marker collision, wrong-project candidate, explicitly non-native task, or explicitly non-local environment maps to `codex_thread_conflict`, is never reused, and never permits replacement creation. Never infer or coerce an arbitrary candidate host string. Then verify the exact signed marker:
   - exactly one matching native task: reuse its thread ID;
   - zero candidate summaries from the exact-marker search: continue to creation;
   - any returned candidate that cannot be authenticated within the ten-turn read maps to `native_task_not_indexed`; never continue to creation after a candidate summary was returned;
   - conflicting or multiple authenticated matches: call `session_sidebar_fail` with `marker_conflict`.
   Creation is permitted only when the exact-marker search returns zero candidate summaries.
6. If no task was reconciled, create exactly one native local task with `create_thread({"prompt":"<registration_prompt verbatim>","target":{"type":"project","projectId":"<chosen projectId>","environment":{"type":"local"}}})`. Do not replace the prompt with the title, transcript text, or a summary. Only the returned `threadId` is a successful create result. `worktreeId`, `clientThreadId`, any other ID, a missing response, or an uncertain call outcome is an ambiguous create: never accept it and never create again for that lease. Immediately after a successful create returns a `threadId`, call `session_sidebar_bind(lease_token=<exact token>, codex_thread_id=<threadId>)` before any native read, rename, or commit. A definite or ambiguous bind failure must be settled once with `bridge_temporarily_unavailable`; never poll, rename, commit, or create a replacement for that lease. Only after the bind succeeds, for up to 60 seconds call `read_thread` only for that returned `threadId` with the same bounded native-read schema used above. Continue condition-based polling while the ID is not yet readable or its status is active. Proceed only when the nested thread has the same thread ID, local host and project identity, exact signed marker, and its status is `idle`. If those conditions are not all true within 60 seconds, fail/release once with `native_task_not_indexed`; never create a replacement.
7. Before any rename, durably bind every reconciled task and every newly created task to its exact native thread ID with `session_sidebar_bind(lease_token=<exact token>, codex_thread_id=<threadId>)`. The bind is idempotent for the same lease and ID; newly created tasks were already bound in step 6, while reconciled tasks must be bound here. On a definite or ambiguous bind failure, call `session_sidebar_fail` once with `bridge_temporarily_unavailable`; do not rename, commit, or create a replacement. Rename every bound task to the returned `[Claude]` or `[Hermes]` title before commit. Use `set_thread_title({"threadId":"<threadId>","title":"<exact title>"})`. On rename failure, call `session_sidebar_fail` with `rename_failed`; do not commit and do not create a replacement task.
8. Call `session_sidebar_commit(lease_token=<exact token>, codex_thread_id=<threadId>)`. A job is complete only after commit succeeds. On a definite or ambiguous commit failure, never create a replacement or repeat create; try fail/release once with `bridge_temporarily_unavailable`, then continue to the next lease.
9. Before exit, call `session_sidebar_fail(lease_token=<exact token>, error_code=<fixed code>)` once for every unfinished lease with no prior fail/release attempt in this batch. The argument is named `error_code`, never `code`, `error`, or exception text.

## Fixed Failure Mapping

| Failure | Fixed code |
|---|---|
| Native Codex task/project operation unavailable | `codex_tool_unavailable` |
| Desktop offline | `desktop_offline` |
| Bridge temporarily unavailable | `bridge_temporarily_unavailable` |
| SQLite busy | `sqlite_busy` |
| Project listing or canonical lookup failed | `project_lookup_failed` |
| Rename failed | `rename_failed` |
| Create response lost or task not yet indexed | `native_task_not_indexed` |
| Lease/time budget cannot safely finish | `broker_time_budget` |
| Authenticated marker conflict | `marker_conflict` |
| Source identity mismatch | `source_identity_mismatch` |
| Native thread conflicts with source | `codex_thread_conflict` |
| Provider mismatch | `provider_mismatch` |
| Source cwd is invalid or missing from the job | `source_cwd_missing` |
| Permission preflight failed | `permission_preflight_failed` |

Calling `session_sidebar_fail` is the fail/release operation for an unfinished lease. Continue settling the other leases even when one job fails.

## Deterministic Call-Failure Rules

Classify failures without copying exception text. Apply the first matching rule:

- Unavailable native tool -> `codex_tool_unavailable`.
- Desktop explicitly offline -> `desktop_offline`.
- Bridge call temporarily unavailable -> `bridge_temporarily_unavailable`; an explicit SQLite busy result -> `sqlite_busy`.
- Project listing or project-map construction fails before leasing -> end without calling `session_sidebar_pending`; no job attempt is consumed. A source-to-project mapping failure discovered only after a lease -> `project_lookup_failed`.
- Definite or ambiguous create failure -> `native_task_not_indexed`. Never retry create after any ambiguous create outcome; try fail/release once and continue with the next leased job.
- Definite or ambiguous native-ID bind failure -> `bridge_temporarily_unavailable`. Never read, rename, commit, or create a replacement after bind ambiguity; try fail/release once and continue with the next leased job.
- Failed or ambiguous reconciliation -> `native_task_not_indexed`. Multiple authenticated matches or a recovered-ID mismatch remain `marker_conflict`.
- Failed rename -> `rename_failed`; never commit or create a replacement.
- Definite or ambiguous commit failure -> `bridge_temporarily_unavailable`. Never create a replacement after commit ambiguity; try fail/release once and continue with the next leased job.
- If the fail/release call itself fails, do not substitute a new error code, do not retry create or commit, and continue with the next leased job. A fail/release attempt, whether successful, failed, or ambiguous, exhausts settlement for that lease in this batch: record it as attempted and never call `session_sidebar_fail` for that lease again. Let the broker lease expire or recover later, and never expose raw exception text.

## Hard Stops

- Never use app-server thread creation as a fallback, even under deadline pressure.
- Never copy or summarize a source transcript into the registration task.
- Never retry creation after an ambiguous outcome; a duplicate is worse than a delayed delivery.
- Never create without a current lease.
- Never poll, rename, or commit a known native task before its exact thread ID is durably bound to the lease.
- Never infer projects from supplied prose or stale state; use the one native project listing.
- Never expose lease tokens, signed markers, or exception text in user-facing output.

## Continuation Contract

The registration task waits. On its first substantive continuation, call `session_continue` exactly as instructed by the authenticated registration prompt before doing project work. Do not call it during registration.

## Verification

Before ending, verify that status was called once, projects were listed at most once and only before pending, pending was called at most once and only after both preflights passed, every created task used `registration_prompt` verbatim, every known native task ID was durably bound before native read or rename, every newly created task became readable and idle before rename, every bound task was renamed as required, every completed lease was committed, and every noncommitted lease had exactly one fail/release attempt with a fixed code, whether successful, failed, or ambiguous.
