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
| No leased jobs | End with no user-facing message. |
| Exact source cwd is a saved project | Use that project. |
| Otherwise, exact git root is a saved project | Use that project. |
| Neither path is a saved project | Use the saved `Session Inbox` project rooted at the canonical local `.hermes` path. |
| An earlier create may have succeeded | Reconcile its authenticated marker before any create. |
| A native create outcome is ambiguous | Do not create again; settle the lease for later reconciliation. |

## Procedure

1. Call `session_sidebar_pending(limit=5)` exactly once. Never create a task without a lease from this batch. If `jobs` is empty, end immediately with no user-facing message.
2. List native Codex projects exactly once and index every saved project by its canonical local path. If listing fails, call `session_sidebar_fail` with `project_lookup_failed` for every unfinished lease, then end.
3. For each leased job, choose its sidebar project in this exact order:
   1. the saved project whose canonical path equals the job's exact cwd;
   2. the saved project whose canonical path equals the job's exact git root;
   3. the saved `Session Inbox` project whose canonical path is the local `.hermes` directory.
4. Treat project choice as sidebar grouping only. Sidebar grouping never changes command cwd, and registration never runs project commands. The job's exact source cwd remains authoritative for later command and file operations after continuation.
5. When `reconcile_required` is true or `recovered_thread_id` is returned, inspect native Codex tasks in the chosen project before creating anything. If `recovered_thread_id` is present, locate that exact native task and verify its authenticated marker; a missing or mismatched task maps to `marker_conflict` and never permits creation. Otherwise match the exact authenticated signed marker carried inside `registration_prompt`:
   - exactly one matching native task: reuse its thread ID;
   - no match: continue to creation only when no recovered thread was returned;
   - conflicting or multiple matches: call `session_sidebar_fail` with `marker_conflict`.
6. If no task was reconciled, create exactly one native local task in the chosen project using the job's `registration_prompt` verbatim. Do not replace it with the title, transcript text, or a summary.
7. Rename the reconciled or newly created task to the returned `[Claude]` or `[Hermes]` title whenever the returned flags require it. A rename failure maps to `rename_failed`; do not create a replacement task.
8. Call `session_sidebar_commit(lease_token=<exact token>, codex_thread_id=<native thread ID>)`. A job is complete only after commit succeeds.
9. Before exit, call `session_sidebar_fail(lease_token=<exact token>, error_code=<fixed code>)` once for every unfinished lease that can still be settled. The argument is named `error_code`, never `code`, `error`, or exception text.

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

## Hard Stops

- Never use app-server thread creation as a fallback, even under deadline pressure.
- Never copy or summarize a source transcript into the registration task.
- Never retry creation after an ambiguous outcome; a duplicate is worse than a delayed delivery.
- Never create without a current lease.
- Never infer projects from supplied prose or stale state; use the one native project listing.
- Never expose lease tokens, signed markers, or exception text in user-facing output.

## Continuation Contract

The registration task waits. On its first substantive continuation, call `session_continue` exactly as instructed by the authenticated registration prompt before doing project work. Do not call it during registration.

## Verification

Before ending, verify that pending was called once, projects were listed at most once, every created task used `registration_prompt` verbatim, every known native task was renamed as required, every completed lease was committed, and every other lease was failed/released with a fixed code.
