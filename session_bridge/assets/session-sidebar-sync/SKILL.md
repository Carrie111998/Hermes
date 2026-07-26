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
| `create_reserved` is true but marker search returns zero | Never create; settle once with `native_create_ambiguous`. |
| `create_thread` was invoked but did not return one exact thread ID | Treat the outcome as `native_create_ambiguous`, even if the tool reports desktop offline. |
| A native create outcome is ambiguous | Do not create again; settle the lease for later reconciliation. |
| A returned thread ID is not readable or not quiescent | Poll the exact ID until it is an authenticated quiescent registration before rename or commit. |

## Authenticated Quiescent Registration

A readable task is an authenticated quiescent registration only after its exact thread ID, normalized local host, chosen project/cwd identity, and exact signed marker have passed the applicable checks below, and either:

- its literal `idle` top-level status is present; or
- its top-level status is `notLoaded`, at least one returned turn is present, every returned turn has status `completed`, and the response contains no active turn, approval request, user-input request, or system error.

Never treat `notLoaded` as globally equivalent to `idle`. Missing turns, an incomplete turn, an active turn, an approval or user-input request, a system error, or any identity or marker mismatch is not quiescent and must continue polling or fail closed under the fixed mapping.

## Authenticated Local Transport Fallback

Use the native `session_bridge` MCP tools when they are callable in the current task. If, and only if, those MCP tools are absent from the task's tool schema, use this authenticated loopback command prefix for the corresponding bridge operation:

```powershell
uv run --project "C:\\Users\\diego\\.hermes\\worktrees\\session-bridge-ship" --no-sync python -m session_bridge.broker_client status|pending|reserve|bind|commit|fail
```

The subcommands map one-for-one to `session_status`, `session_sidebar_pending`, `session_sidebar_reserve`, `session_sidebar_bind`, `session_sidebar_commit`, and `session_sidebar_fail`. Supply `pending --limit 1`, `reserve --lease-token=<exact token>`, `bind --lease-token=<exact token> --thread-id=<threadId>`, `commit --lease-token=<exact token> --thread-id=<threadId>`, or `fail --lease-token=<exact token> --error-code=<fixed code> --thread-id=<threadId>` when an exact returned native ID is known. Omit `--thread-id` from fail only when no exact native ID was returned. The equals-sign form is mandatory because opaque tokens and IDs may begin with `-` and must remain one argument. Parse the command's JSON stdout as the bridge tool result.

Each fallback invocation counts as the exact single bridge call required by the procedure. Select one bridge transport for each step and never call both transports for the same bridge step. Do not retry a fallback invocation whose result is ambiguous, and do not mutate the bridge database directly. If neither transport is available, stop before leasing. Native Codex project and task operations still use the native app tools; this fallback applies only to the six bridge operations above.

## Procedure

1. Call `session_status` exactly once before any native project call or lease. Require `health.running` and the watcher to be running, require every reported provider `degraded_reason` to be null, and read `sidebar.counts` without broadening the request. If status is unavailable or malformed, a health requirement fails, or both `sidebar_pending` and `sidebar_retry` are zero, end immediately with no user-facing message. Do not call `session_sidebar_pending`, and no job attempt is consumed.
2. Call the native tool `list_projects({})` exactly once before leasing. Read every returned saved project's canonical local path, and index that canonical path to its returned `projectId` as (`projectId`, original returned `hostId`, normalized host). Normalize a missing or null `hostId` and the explicit string `local` to the current-local sentinel `local`. Reject every other explicit host value from this local-sidebar run; never infer or coerce an arbitrary host string. Do not call the tool again for another job. If the call or project-map construction fails, do not call `session_sidebar_pending`; end without a lease, do not call `session_sidebar_fail`, and no job attempt is consumed. Only after the native project map is valid, call `session_sidebar_pending(limit=1)` exactly once. Never create a task without that lease; process at most one job per wake. If `jobs` is empty, end immediately with no user-facing message. Process that one lease sequentially to completion: reconcile or create, bind, read until indexed and idle, rename, and commit or fail/release before exit. Never run `create_thread` concurrently, and never run native delivery operations concurrently. Do not claim or process another lease in the same wake. Never let one job's result authorize a replacement for another job.
3. For the leased job, choose its sidebar project in this exact order:
   1. the saved project whose canonical path equals the job's exact cwd;
   2. the saved project whose canonical path equals the job's exact git root;
   3. the saved `Session Inbox` project whose canonical path is the local `.hermes` directory.
4. Treat project choice as sidebar grouping only. Sidebar grouping never changes command cwd, and registration never runs project commands. The job's exact source cwd remains authoritative for later command and file operations after continuation.
5. When `reconcile_required` is true, `recovered_thread_id` is returned, or `create_reserved` is true, reconcile before creating anything. Extract the exact authenticated signed marker from `registration_prompt`.
   - When `recovered_thread_id` is present, it is the bridge-authenticated candidate. Do not call `list_threads` before this recovered-ID read. Call `read_thread({"threadId":"<recovered_thread_id>","turnLimit":10,"includeOutputs":false})` directly and pass no other fields. An unavailable, missing, or not-yet-indexed recovered-ID read maps to `native_task_not_indexed`; include `codex_thread_id=<recovered_thread_id>` in fail/release and never permit creation. On success, inspect the response's nested `thread` object; the absence of a top-level thread ID is expected. Use `thread.id`, `thread.hostId`, and `thread.cwd` as the returned identity fields: require the ID to equal `recovered_thread_id`; missing or null `thread.hostId` and explicit `local` normalize only to `local`; every other explicit `thread.hostId` maps to `codex_thread_conflict`; and the normalized task host must equal the chosen project's normalized host. Require the cwd or any supplied project identity to match the chosen project's canonical path or identity. The native `read_thread` response does not return an explicit environment field for an ordinary local task; that omission must not be treated as unavailable or ambiguous because the native read tool is itself the task surface. If a recovered-ID read returns successfully but `thread.id` or the signed marker mismatches, map to `marker_conflict`, include `codex_thread_id=<recovered_thread_id>` in fail/release, and never permit creation. A supplied host, project, environment, or task-kind field that explicitly contradicts local native execution maps to `codex_thread_conflict`, includes that same exact recovered ID in fail/release, and never permits creation. After identity and marker checks pass, apply the authenticated quiescent registration rule before rename or commit. Continue condition-based polling while the task is active or not yet quiescent. If it does not become quiescent within 60 seconds, fail/release once with `native_task_not_indexed`, include that same exact recovered ID, and never permit creation.
   - Only when `recovered_thread_id` is absent, call `list_threads({"query":"<exact signed marker>","limit":20})`. Normalize and filter each `list_threads` candidate summary before any `read_thread` call. Apply the same host normalization to every thread candidate: missing, null, and explicit `local` normalize only to `local`. Require that normalized host is `local` and equals the chosen project's normalized host; an explicit non-`local` host maps to `codex_thread_conflict` without a read and is never reused or replaced. Compare the candidate summary's project with the chosen project only when that summary supplies project identity; do not invent a missing project field. For each surviving candidate, call `read_thread({"threadId":"<candidate threadId>","hostId":"<candidate hostId>","turnLimit":10,"includeOutputs":false})`. Pass the original candidate `hostId` unchanged when it is non-null; Omit `hostId` only when it was absent or null. Pass no other fields. Ten is the bounded reconciliation and read limit; never paginate or broaden the query during this batch. Inspect the read response's nested `thread` object using the same schema rules above, substituting the exact candidate thread ID for `recovered_thread_id`. Before matching the signed marker, require the read result to belong to the chosen local project and host identity; omission of an explicit environment or task-kind field is acceptable, while an explicit contradiction maps to `codex_thread_conflict`. A remote-host candidate or other remote marker collision, wrong-project candidate, explicitly non-native task, or explicitly non-local environment maps to `codex_thread_conflict`, is never reused, and never permits replacement creation. Never infer or coerce an arbitrary candidate host string. Then verify the exact signed marker:
   - exactly one matching native task: reuse its thread ID;
   - zero candidate summaries from the exact-marker search: continue to the guarded creation decision only when `create_reserved` is false;
   - if a candidate read is unavailable or not yet indexed within the ten-turn read, map to `native_task_not_indexed`; never continue to creation after a candidate summary was returned;
   - if a candidate read returns successfully but its exact ID or signed marker mismatches, map to `marker_conflict`; never continue to creation after a candidate summary was returned;
   - an explicit host, project, environment, or task-kind contradiction maps to `codex_thread_conflict`; never continue to creation after a candidate summary was returned;
   - conflicting or multiple authenticated exact-marker matches: call `session_sidebar_fail` with `marker_conflict`.
   Creation is permitted only when the exact-marker search returns zero candidate summaries and `create_reserved` is false. When `create_reserved` is true, zero marker-search results never authorize creation: settle once with `native_create_ambiguous` and end this wake.
6. If no task was reconciled and `create_reserved` is false, call `session_sidebar_reserve(lease_token=<exact token>)` immediately before `create_thread`. Do not create unless reserve succeeds with `state=sidebar_leased` and `create_reserved=true`; a definite or ambiguous reserve failure maps to `bridge_temporarily_unavailable`, must be settled once, and never authorizes creation. After successful reservation, create exactly one native local task with `create_thread({"prompt":"<registration_prompt verbatim>","target":{"type":"project","projectId":"<chosen projectId>","environment":{"type":"local"}}})`. Do not replace the prompt with the title, transcript text, or a summary. Only the returned `threadId` is a successful create result. After `session_sidebar_reserve` succeeds and `create_thread` is invoked, every raised error or missing or uncertain response, including an explicit desktop-offline tool error, is `native_create_ambiguous`. `desktop_offline` applies only before native-create dispatch. `worktreeId`, `clientThreadId`, any other ID, a missing response, or an uncertain call outcome is an ambiguous create: settle it once with `native_create_ambiguous`, never accept it, and never create again for that lease. This fatal quarantine requires an operator audit before the failed job may be requeued. Immediately after a successful create returns a `threadId`, call `session_sidebar_bind(lease_token=<exact token>, codex_thread_id=<threadId>)` before any native read, rename, or commit. A definite or ambiguous bind failure must be settled once with `session_sidebar_fail(lease_token=<exact token>, error_code=bridge_temporarily_unavailable, codex_thread_id=<threadId>)`; never poll, rename, commit, or create a replacement for that lease. That fail/release call durably retains the exact returned native ID whenever bind's response was lost. Only that exact same thread ID may be rebound idempotently on a later leased retry; zero marker results never permit a replacement. Only after the bind succeeds, for up to 60 seconds call `read_thread` only for that returned `threadId` with the same bounded native-read schema used above. Continue condition-based polling while the ID is not yet readable, active, or not yet quiescent. Proceed only when the nested thread has the same thread ID, local host and project identity, exact signed marker, and is an authenticated quiescent registration. If those conditions are not all true within 60 seconds, fail/release once with `native_task_not_indexed` and include `codex_thread_id=<threadId>`; never create a replacement.
7. Before any rename, durably bind every reconciled task and every newly created task to its exact native thread ID with `session_sidebar_bind(lease_token=<exact token>, codex_thread_id=<threadId>)`. The bind is idempotent for the same lease and ID; newly created tasks were already bound in step 6, while reconciled tasks must be bound here. On a definite or ambiguous bind failure, call `session_sidebar_fail(lease_token=<exact token>, error_code=bridge_temporarily_unavailable, codex_thread_id=<threadId>)` once; do not rename, commit, or create a replacement. Rename a bound task only after every applicable exact-ID read, identity, marker, and authenticated-quiescence check has passed. Rename it to the returned `[Claude]` or `[Hermes]` title before commit. Use `set_thread_title({"threadId":"<threadId>","title":"<exact title>"})`. On rename failure, call `session_sidebar_fail` with `rename_failed` and `codex_thread_id=<threadId>`; do not commit and do not create a replacement task.
8. Call `session_sidebar_commit(lease_token=<exact token>, codex_thread_id=<threadId>)`. A job is complete only after commit succeeds. On a definite or ambiguous commit failure, never create a replacement or repeat create; try fail/release once with `bridge_temporarily_unavailable` and `codex_thread_id=<threadId>`, then end this wake.
9. Before exit, call `session_sidebar_fail(lease_token=<exact token>, error_code=<fixed code>)` once for the unfinished lease when no prior fail/release attempt was made in this wake. Whenever an exact native ID is known, include `codex_thread_id=<threadId>` in that same call; omit it only when no exact native ID was ever returned or authenticated. The argument is named `error_code`, never `code`, `error`, or exception text.

## Fixed Failure Mapping

| Failure | Fixed code |
|---|---|
| Native Codex task/project operation unavailable before native-create dispatch, or during a non-create native operation | `codex_tool_unavailable` |
| Desktop offline before native-create dispatch | `desktop_offline` |
| Bridge temporarily unavailable | `bridge_temporarily_unavailable` |
| SQLite busy | `sqlite_busy` |
| Project listing or canonical lookup failed | `project_lookup_failed` |
| Rename failed | `rename_failed` |
| Create response lost or otherwise ambiguous | `native_create_ambiguous` |
| Bound task not yet indexed | `native_task_not_indexed` |
| Lease/time budget cannot safely finish | `broker_time_budget` |
| Authenticated marker conflict | `marker_conflict` |
| Source identity mismatch | `source_identity_mismatch` |
| Native thread conflicts with source | `codex_thread_conflict` |
| Provider mismatch | `provider_mismatch` |
| Source cwd is invalid or missing from the job | `source_cwd_missing` |
| Permission preflight failed | `permission_preflight_failed` |

Calling `session_sidebar_fail` is the fail/release operation for the unfinished lease; end this wake after that single settlement attempt.

## Deterministic Call-Failure Rules

Classify failures without copying exception text. Apply the first matching rule:

- Unavailable native tool before native-create dispatch, or during a non-create native operation -> `codex_tool_unavailable`. This rule never classifies an error or uncertain response from an invoked `create_thread` call.
- Desktop explicitly offline before native-create dispatch -> `desktop_offline`.
- Bridge call temporarily unavailable -> `bridge_temporarily_unavailable`; an explicit SQLite busy result -> `sqlite_busy`.
- Project listing or project-map construction fails before leasing -> end without calling `session_sidebar_pending`; no job attempt is consumed. A source-to-project mapping failure discovered only after a lease -> `project_lookup_failed`.
- A native-create rejection proven before invoking `create_thread` -> `native_task_not_indexed`. After `session_sidebar_reserve` succeeds and `create_thread` is invoked, every raised error or missing or uncertain response, including an explicit desktop-offline tool error, is `native_create_ambiguous`. `desktop_offline` applies only before native-create dispatch. Never retry create after any ambiguous create outcome. The fatal quarantine requires an operator audit before the failed job may be requeued. Try fail/release once and end this wake.
- Definite or ambiguous create reservation failure -> `bridge_temporarily_unavailable`. Never create unless reservation definitely succeeded; try fail/release once and end this wake.
- Definite or ambiguous native-ID bind failure -> `bridge_temporarily_unavailable`. Pass the exact returned `codex_thread_id` to fail/release. Never read, rename, commit, or create a replacement after bind ambiguity; try fail/release once and end this wake.
- Unavailable, not-yet-indexed, or not-quiescent reconciliation read -> `native_task_not_indexed`.
- Successfully returned thread-ID or marker mismatch, or multiple exact marker matches -> `marker_conflict`.
- Explicit host, project, environment, or task-kind contradiction -> `codex_thread_conflict`.
- Failed rename -> `rename_failed`; never commit or create a replacement.
- Definite or ambiguous commit failure -> `bridge_temporarily_unavailable`. Never create a replacement after commit ambiguity; try fail/release once and end this wake.
- If the fail/release call itself fails, do not substitute a new error code and do not retry create or commit. A fail/release attempt, whether successful, failed, or ambiguous, exhausts settlement for that lease in this batch: record it as attempted and never call `session_sidebar_fail` for that lease again. End this wake, let the broker lease expire or recover later, and never expose raw exception text.

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

Before ending, verify that status was called once, projects were listed at most once and only before pending, and pending was called at most once and only after both preflights passed. If a task was created, verify reserve definitely succeeded first, `registration_prompt` was used verbatim, and the returned exact ID was supplied to bind and to any later fail/release. If a task was reconciled, verify only the authenticated recovered ID was reused and no create was attempted. If a newly created task reached native-read polling, verify its exact ID was durably bound before that polling. If a reconciled candidate was read before binding, verify that read was used only to authenticate it and no rename or commit preceded a successful bind. If exact-ID polling reached an authenticated quiescent registration, verify rename was attempted only afterward. If exact-ID polling failed or timed out, verify no rename or commit was attempted and the exact ID was supplied to fail/release. If rename succeeded, verify the exact returned title was used before commit. If bind or rename failed, verify no commit was attempted and the exact ID was supplied to fail/release. If commit succeeded, verify that exact lease and thread ID became visible. If commit did not succeed, verify the noncommitted lease had exactly one fail/release attempt with a fixed code, whether successful, failed, or ambiguous.
