---
name: session-sidebar-sync
description: Use when leased native Claude or Hermes sessions are pending delivery to the Codex sidebar.
---

# Session Sidebar Sync

## Overview

Deliver one bounded broker batch as native local Codex tasks. Preserve the broker lease, authenticated registration, canonical Session Inbox placement, and exact source identity; registration is not a transcript migration.

## Quick Reference

| Situation | Required action |
|---|---|
| `session_status` counts show no pending, leased, or retry work | Not a stop condition. Still run the full preflight and both persisted-heartbeat pending calls (`session_sidebar_hydration_pending`, then `session_sidebar_pending`); these advance the broker heartbeat. End with no user-facing message only after registration pending returns no job. |
| Bridge or native project preflight is unhealthy | End without leasing; no job attempt is consumed. |
| Hydration and registration are both actionable | Claim one hydration job; do not call registration pending unless hydration returned no job. |
| Hydration targets an existing exact task | Read and authenticate that exact task before any send; never create or replace it. |
| A hydration send may already have happened | Reconcile the exact hydration marker; never send again after ambiguity. |
| Any Claude or Hermes registration delivery | Use the one saved local `.hermes` project returned by `list_projects({})`; a valid project-scoped create never requires optional cwd, runtime roots, or idempotency fields. |
| Any in-place hydration delivery | Authenticate the exact linked task; a projectless legacy task is valid and remains valid. |
| An earlier create may have succeeded | Trust the bridge-authoritative recovered or blocked reconciliation result; never discover tasks from the broker. |
| `create_reserved` is true on a new lease | Never create; settle once with `native_create_ambiguous`. |
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
uv run --project "C:\\Users\\diego\\.hermes\\agent-src" --no-sync python -m session_bridge.broker_client status|pending|reserve|bind|commit|fail
```

The registration subcommands map one-for-one to `session_status`, `session_sidebar_pending`, `session_sidebar_reserve`, `session_sidebar_bind`, `session_sidebar_commit`, and `session_sidebar_fail`. Supply `pending --limit 1`, `reserve --lease-token=<exact token> --reconciliation-proof-digest=<exact digest> --reconciliation-generation=<exact generation>`, `bind --lease-token=<exact token> --thread-id=<threadId>`, `commit --lease-token=<exact token> --thread-id=<threadId>`, or `fail --lease-token=<exact token> --error-code=<fixed code> --thread-id=<threadId>` when an exact returned native ID is known. Omit `--thread-id` from registration fail only when no exact native ID was returned.

The hydration subcommands map one-for-one to `session_sidebar_hydration_pending`, `session_sidebar_hydration_reserve`, `session_sidebar_hydration_commit`, and `session_sidebar_hydration_fail`. Supply `hydration-pending --limit 1`, `hydration-reserve --lease-token=<exact token>`, `hydration-commit --lease-token=<exact token> --thread-id=<exact id> --hydration-marker=<exact marker>`, or `hydration-fail --lease-token=<exact token> --error-code=<fixed code> --thread-id=<exact id>`. The equals-sign form is mandatory because opaque tokens, markers, and IDs may begin with `-` and must remain one argument. Parse the command's JSON stdout as the bridge tool result.

Each fallback invocation counts as the exact single bridge call required by the procedure. Select one bridge transport for each step and never call both transports for the same bridge step. Do not retry a fallback invocation whose result is ambiguous, and do not mutate the bridge database directly. If neither transport is available, stop before leasing. Native Codex project and task operations still use the native app tools; this fallback applies only to the bridge operations above.

## Queue Selection

Call `session_status` exactly once. Validate scanner health and the configured broker/inbox identity. Read the configured broker thread ID from that same `session_status` response at `sidebar.broker.thread_id`; require an exact non-empty string and never substitute, infer, default, or carry over a previously seen value. A missing, empty, or non-string value stops preflight before lease. Then call `read_thread({"threadId":"<exact sidebar.broker.thread_id>","turnLimit":10,"includeOutputs":false})` exactly once and require its local host, title `Fix Claude session translation`, and cwd `C:\Users\diego\Developer\session-sidebar-broker`. Then call the native tool `list_projects({})` exactly once. Require exactly one local saved project whose canonical path equals `C:\Users\diego\Developer\session-sidebar-broker`, whose returned ID equals configured `broker_project_id`, and whose production ID is `local-453ac85f86839c6d001817cb8480b8ca`. Separately require exactly one local saved project whose canonical path equals `C:\Users\diego\.hermes`; retain its returned ID separately as `inbox_project_id`, whose production value is `local-e59c279a6cdda9313cf111e46a80b027`. If either ID differs, preflight stops before lease. Preflight failure ends before leasing and no job attempt is consumed.

One provider's non-null `degraded_reason` must not globally block healthy queued delivery from another provider.

After successful preflight, always call hydration pending once; if it is empty, always call registration pending once regardless of status counts. Status counts never authorize skipping either persisted-heartbeat call. Call `session_sidebar_hydration_pending(limit=1)`. If it returns a job, process that one hydration lease and do not call registration pending. If it returns no job, call `session_sidebar_pending(limit=1)`. If registration is empty, end silently. Claim and process at most one total lease per wake.

## In-place Hydration Procedure

- **Read.** Required lease fields: `lease_token`, `codex_thread_id`, `hydration_message`, `hydration_marker`, `cwd`, `git_root`, `send_reserved`. Require exact non-empty token, task ID, message, and marker; `git_root` may be null. Do not require or invent any field not returned by the lease. The returned `codex_thread_id` is the only permitted target. Call `read_thread({"threadId":"<exact codex_thread_id>","turnLimit":10,"includeOutputs":false})` before any reservation or send. Pass no other fields.
- **Authenticate.** The coordinator already authenticated source, bridge, and preview before issuing the lease. The broker validates only the exact returned fields and exact task content available through `read_thread`: the exact linked task ID, the legacy signed registration marker when present, no substantive work, quiescence, and the hydration-marker flow below. A projectless legacy task is valid and remains valid. Exact authenticated projectless legacy hydration is exempt and valid. A missing or unreadable task maps to `native_task_not_indexed`; a legacy registration marker mismatch maps to `marker_conflict`; an explicit host, environment, or task-kind contradiction maps to `codex_thread_conflict`. Settle once with `session_sidebar_hydration_fail`, always including the exact `codex_thread_id`.
- **Reconcile.** Every resumed `send_reserved=true` lease reconciles the exact marker before any reserve or send decision. Search only the returned turns of that exact task. If the marker is present in an authenticated completed turn and the task is quiescent, call `session_sidebar_hydration_commit(lease_token=<exact token>, codex_thread_id=<exact id>, hydration_marker=<exact marker>)` and end. Do not send again.
- **Stop after ambiguity.** If the exact hydration marker is absent and `send_reserved=true`, call `session_sidebar_hydration_fail` once with `hydration_send_ambiguous` and the exact task ID. This state is reconciliation-only and never authorizes another send.
- **Reserve and send.** Otherwise call `session_sidebar_hydration_reserve(lease_token=<exact token>)` immediately before `send_message_to_thread({"threadId":"<exact id>","message":"<hydration_message verbatim>"})`. Send only after a definite response with exact `state=hydration_leased` and `send_reserved=true`; also require matching exact `codex_thread_id` and `hydration_marker` when supplied. A missing, malformed, stale, or ambiguous reserve response maps to `bridge_temporarily_unavailable`: fail/settle once and do not call `send_message_to_thread`. After the definite guard passes, send the returned `hydration_message` verbatim to the exact linked task ID. Do not add commentary or instructions of your own.
- **Classify send uncertainty.** After send invocation, every raised error, missing response, timeout, desktop-offline result, or otherwise uncertain outcome maps to `hydration_send_ambiguous`. Call hydration fail at most once with the exact task ID and end. Never invoke send again for that lease.
- **Verify.** After a definite send result, poll only the same exact task for up to 60 seconds with the same bounded read schema. Commit only when the exact hydration marker is present in a completed turn and the task has no active turn, approval request, user-input request, or system error. If the task remains unreadable or incomplete, use `native_task_not_indexed`; if the remaining safe time cannot finish verification, use `broker_time_budget`.
- **Settle.** A bridge reserve or commit failure maps to `bridge_temporarily_unavailable`; an unavailable native read or send tool before send invocation maps to `codex_tool_unavailable`. Never copy exception text, hydration content, tokens, or markers into failure output. Make exactly one hydration fail/release attempt for any unfinished lease.

Never create, rename, archive, move, fork, or replace a task in hydration mode.

## Registration Procedure

1. Use the one already-validated `session_status` result only for health and identity. Its pending and retry counts never skip the Queue Selection persisted-heartbeat calls.
2. Use the native project map already returned by `list_projects({})` exactly once before leasing. Read every returned saved project's canonical local path, and index that canonical path to its returned `projectId` as (`projectId`, original returned `hostId`, normalized host). Normalize a missing or null `hostId` and the explicit string `local` to the current-local sentinel `local`. Reject every other explicit host value from this local-sidebar run; never infer or coerce an arbitrary host string. Do not call the tool again for another job. Use the lease already selected in Queue Selection. Never create a task without that lease; process at most one job per wake. If the selected lease is absent, end immediately with no user-facing message. Process that one lease sequentially to completion: reconcile or create, bind, read until indexed and idle, rename, and commit or fail/release before exit. Never run `create_thread` concurrently, and never run native delivery operations concurrently. Do not claim or process another lease in the same wake. Never let one job's result authorize a replacement for another job.
3. For the leased job, select only the saved `Session Inbox` project whose canonical path equals the resolved canonical local `.hermes` inbox cwd. The exact source cwd and exact git root never select placement or project identity. If that exact inbox project is unavailable after leasing, fail/release once with `inbox_unavailable`; never create in the source cwd, git root, an arbitrary saved project, or a projectless context.
4. The saved local `.hermes` project is the registration target. Its returned project ID is required, but optional cwd, `runtimeWorkspaceRoots`, and `idempotencyKey` are not. Do not refuse a valid project-scoped create because those optional fields are unavailable.
5. Trust only the authoritative reconciliation object returned by `session_sidebar_pending`. Never search by title or tag, paginate Codex tasks, or infer absence from Recents. Require exact non-empty `reconciliation_state`, `reconciliation_proof_digest`, and `reconciliation_generation` fields plus a boolean `create_eligible`. Extract the exact authenticated signed marker from `registration_prompt`. Bounded pre-bind reads are allowed solely to authenticate the one exact recovered ID; do not poll, rename, or commit during candidate authentication. Never bind an unauthenticated candidate.
   - When `reconciliation_state` is `recovered`, require exact `recovered_thread_id`, require `create_eligible=false`, and call `read_thread({"threadId":"<recovered_thread_id>","turnLimit":10,"includeOutputs":false})` directly. Pass no other fields. An unavailable, missing, or not-yet-indexed recovered-ID read maps to `native_task_not_indexed`; include `codex_thread_id=<recovered_thread_id>` in fail/release and never permits creation. On success, inspect the response's nested `thread` object; the absence of a top-level thread ID is expected. Use `thread.id`, `thread.hostId`, and `thread.cwd` as the returned identity fields: require the ID to equal `recovered_thread_id`; missing or null `thread.hostId` and explicit `local` normalize only to `local`; every other explicit `thread.hostId` maps to `codex_thread_conflict`; and the normalized task host must equal the Session Inbox project's normalized host. Require `thread.cwd` to match the resolved Session Inbox cwd. A returned cwd outside the inbox, or a supplied project identity outside the selected inbox project, maps to `placement_mismatch`, includes `codex_thread_id=<recovered_thread_id>` in fail/release, and never permits creation or replacement. The exact source cwd remains authenticated only from the registration metadata and signed marker; it never satisfies native placement. The native `read_thread` response does not return an explicit environment field for an ordinary local task; that omission must not be treated as unavailable or ambiguous because the native read tool is itself the task surface. If a recovered-ID read returns successfully but `thread.id` or the signed marker mismatches, map to `marker_conflict`, include `codex_thread_id=<recovered_thread_id>` in fail/release, and never permits creation. A supplied host, environment, or task-kind field that explicitly contradicts local native execution maps to `codex_thread_conflict`, includes that same exact recovered ID in fail/release, and never permits creation. After the bounded read authenticates it, call `session_sidebar_bind(lease_token=<exact token>, codex_thread_id=<threadId>)` exactly once for that exact ID. On any bind failure, fail/settle once with `bridge_temporarily_unavailable` and that exact ID; do not poll, rename, commit, or create a replacement.
   - When `reconciliation_state` is `absence_proven`, require no `recovered_thread_id`, require `create_eligible=true`, require `create_reserved=false`, and do not inspect any other native task. Only this state may proceed to the guarded reserve decision.
   - A missing or unsupported reconciliation state, generation, or proof digest maps to `bridge_temporarily_unavailable` and never permits creation. `blocked`, malformed, contradictory, or expired reconciliation data also fails closed.
6. For `absence_proven`, call `session_sidebar_reserve(lease_token=<exact token>, reconciliation_proof_digest=<exact digest>, reconciliation_generation=<exact generation>)` immediately before native create. Pass the exact digest and generation from the current lease. Reserve failure maps to `bridge_temporarily_unavailable` and never authorizes creation. Do not create unless reserve succeeds with an exact recognized response. If reserve returns `state=recovered`, require one exact `codex_thread_id` and `create_reserved=false`; read and authenticate only that exact task under Step 5, call `session_sidebar_bind` for that same exact ID, and do not create. Create exactly once only when reserve returns `state=sidebar_leased` and `create_reserved=true`. Every other response fails closed.

   Create exactly one native local task:
   `create_thread({"prompt":"<registration_prompt verbatim>","target":{"type":"project","projectId":"local-e59c279a6cdda9313cf111e46a80b027","environment":{"type":"local"}}})`

   This example illustrates the validated returned production ID; substitute only the exact preflight-validated returned `inbox_project_id`. The environment MUST be the object exactly `"environment":{"type":"local"}`; the string `"environment":"local"` is invalid. Do not include cwd, `runtimeWorkspaceRoots`, or `idempotencyKey`; do not replace the prompt with title, transcript text, or a summary. Only the returned `threadId` is a successful create result. For a newly returned create ID, immediately call `session_sidebar_bind(lease_token=<exact token>, codex_thread_id=<threadId>)` before the first `read_thread`, rename, or commit. If binding fails, call `session_sidebar_fail(lease_token=<exact token>, error_code=bridge_temporarily_unavailable, codex_thread_id=<threadId>)` once and never create a replacement. Only that exact same thread ID may be rebound idempotently; never bind or create a substitute. After invocation, every raised, missing, or uncertain response is `native_create_ambiguous`: mark needs attention, never retry create, and never create a replacement. After binding, poll `read_thread` only for the same thread ID for up to 60 seconds; verify local host, the `.hermes` project when available, `.hermes` cwd, signed marker, source cwd metadata, digest, readable sections, and authenticated quiescent registration; otherwise settle once with `native_task_not_indexed`. Only then set the exact returned `[Claude]` title. If verification or binding is uncertain, retain the exact ID and settle once without replacement.
7. Reconciled and newly created tasks are already bound exactly once in their respective branches. This shared verification step must not bind again: Do not call `session_sidebar_bind` again. Rename a bound task only after every applicable exact-ID read, identity, marker, and authenticated-quiescence check has passed. Rename it to the returned `[Claude]` or `[Hermes]` title before commit. Use `set_thread_title({"threadId":"<threadId>","title":"<exact title>"})`. On rename failure, call `session_sidebar_fail` with `rename_failed` and `codex_thread_id=<threadId>`; do not commit and do not create a replacement task.
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
| Session Inbox unavailable | `inbox_unavailable` |
| Native task outside Session Inbox placement (registration/new mirror only) | `placement_mismatch` |
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
- Project listing or project-map construction fails before leasing -> end without calling `session_sidebar_pending`; no job attempt is consumed. A missing, ambiguous, invalid, or changed Session Inbox discovered after a lease -> `inbox_unavailable`; never fall back to the source cwd, git root, another project, or projectless creation.
- A native-create rejection proven before invoking `create_thread` -> `native_task_not_indexed`. After `session_sidebar_reserve` succeeds and `create_thread` is invoked, every raised error or missing or uncertain response, including an explicit desktop-offline tool error, is `native_create_ambiguous`. `desktop_offline` applies only before native-create dispatch. Never retry create after any ambiguous create outcome. The fatal quarantine requires an operator audit before the failed job may be requeued. Try fail/release once and end this wake.
- Definite or ambiguous create reservation failure -> `bridge_temporarily_unavailable`. Never create unless reservation definitely succeeded; try fail/release once and end this wake.
- Definite or ambiguous native-ID bind failure -> `bridge_temporarily_unavailable`. Pass the exact returned `codex_thread_id` to fail/release. Never read, rename, commit, or create a replacement after bind ambiguity; try fail/release once and end this wake.
- Unavailable, not-yet-indexed, or not-quiescent reconciliation read -> `native_task_not_indexed`.
- Successfully returned thread-ID or marker mismatch, or multiple exact marker matches -> `marker_conflict`.
- A registration candidate, recovered task, or newly created task whose cwd or supplied project identity is outside the resolved Session Inbox -> `placement_mismatch`; retain any exact known thread ID and never create a replacement.
- Explicit host, environment, or task-kind contradiction -> `codex_thread_conflict`.
- Failed rename -> `rename_failed`; never commit or create a replacement.
- Definite or ambiguous commit failure -> `bridge_temporarily_unavailable`. Never create a replacement after commit ambiguity; try fail/release once and end this wake.
- If the fail/release call itself fails, do not substitute a new error code and do not retry create or commit. A fail/release attempt, whether successful, failed, or ambiguous, exhausts settlement for that lease in this batch: record it as attempted and never call `session_sidebar_fail` for that lease again. End this wake, let the broker lease expire or recover later, and never expose raw exception text.

## Hard Stops

- Never use app-server thread creation as a fallback, even under deadline pressure.
- Never copy or summarize a source transcript into the registration task.
- Never retry creation after an ambiguous outcome; a duplicate is worse than a delayed delivery.
- Never create without a lease; the lease must be current.
- Bounded pre-bind candidate-authentication reads are permitted. Never poll, rename, or commit a selected task before binding its exact authenticated thread ID to the lease; never bind an unauthenticated candidate.
- Never infer projects from supplied prose or stale state; use the one native project listing.
- Never use source cwd or git root as native placement or project selection. Source cwd is authenticated metadata only; only `.hermes` is an attached root.
- Never expose lease tokens, signed markers, or exception text in user-facing output.

## Continuation Contract

The registration task waits. On its first substantive continuation, call `session_continue` in the same authenticated task context before doing project work. Only attached `.hermes` roots are mirrored. If a command or file mutation would leave verified attached roots, stop and offer an explicit source-project handoff; never claim that the source cwd is attached. Do not call it during registration.

## Verification

Before ending, verify status, broker-thread read, and project listing each ran once before pending; hydration pending ran once and registration pending ran once only when hydration was empty. If a task was created, verify reserve definitely succeeded first, `registration_prompt` was used verbatim, the exact project target was used without cwd, runtime roots, or idempotency, and the returned exact ID was bound before read, title, or commit. If a hydration task was processed, verify its exact marker was reconciled before send when reserved and send was reserved immediately before the one exact message. If commit succeeded, verify the exact lease and bound thread ID became visible. If commit did not succeed, verify the noncommitted lease had exactly one fail/release attempt with a fixed code, whether successful, failed, or ambiguous.
