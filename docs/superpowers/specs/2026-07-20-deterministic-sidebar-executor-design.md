# Deterministic Session Sidebar Executor Design

Date: 2026-07-20

## Decision

Replace the conversational Codex automation as the delivery worker with a local,
deterministic Session Bridge executor. The executor uses the supported Codex
app-server transport already required by the cross-harness plan. The temporary
native-tool-only rollout restriction is retired after migration; direct mutation of
Codex SQLite or rollout JSONL remains prohibited.

## Why the rollout stalled

The current broker owns durable leases, but native creation is performed by a
short-lived conversational task. Every wake repeats provider status, project lookup,
lease acquisition, marker search, creation, indexing polls, rename, and commit.
Tool timeouts can outlive a lease, the task has no durable program counter, and the
next wake must reconstruct state from prose. This consumes tokens and repeatedly
strands pending or retry rows even though exact thread-ID reconciliation is correct.

## Executor boundary

Create a focused `SidebarDeliveryExecutor` that depends on:

- `SessionBridgeStore` for atomic lease, bind, fail/release, and commit operations;
- `CodexSourceAdapter` for exact-ID and authenticated-marker reads;
- a narrow Codex app-server delivery port for thread start, registration turn,
  read-until-idle, and rename;
- an injected clock/sleep implementation for deterministic tests.

The executor processes at most one job per call and serializes all native mutations.
It never runs project commands and uses the source cwd only as Codex thread metadata.
The service invokes it after successful scans when continuous delivery is enabled;
the CLI exposes `sidebar-run-once` for deterministic supervision and diagnostics.

## Durable state machine

1. Preflight provider health and claim exactly one due job.
2. If `codex_thread_id` is already durable, read only that ID.
3. Otherwise search the exact signed marker. One authenticated match is bound.
4. Only a never-created job with zero authenticated candidates may call
   `thread/start` once.
5. Bind the returned native ID before any registration turn, poll, rename, or commit.
6. Run the fixed registration turn, read the exact ID until indexed and idle, verify
   source/bridge/marker/cwd identity, rename, and commit lineage atomically.
7. Map every failure to the fixed error-code allowlist. Any ambiguous create, bind,
   or commit preserves the exact known ID and never authorizes replacement creation.

No step relies on conversational memory. Restart recovery derives its next action
only from the durable row and the exact native ID.

## Safety

- One process-wide executor lock and one claimed job prevent concurrent creation.
- No direct writes to Claude or Codex transcript files or provider databases.
- Exact native IDs are persisted immediately after successful creation.
- A create timeout without an ID becomes fatal `native_create_ambiguous`; operator
  audit is required and a zero-result search alone cannot create a replacement.
- Known IDs reconcile by exact `thread/read` before any inventory pagination.
- The old Codex automation remains paused during migration and is deleted only after
  an empty autonomous cycle is proven.

## Claude-native visibility and full-scope closure

The existing pseudoterminal Claude registrar remains the single Claude delivery
worker. Closure is evidence-driven:

- zero open Codex-sidebar and Claude-visibility jobs;
- reviewed 30-day dry-runs with zero candidates or documented exclusions;
- exact uniqueness of source, bridge, idempotency, Codex thread, and Claude UUID;
- native Codex sidebar and Claude `/resume` visibility;
- installed `/session-bridge` unified catalog command;
- continuation from Codex to Claude and Claude/Hermes to Codex with immutable packs;
- continuous registration of one new meaningful source within one minute;
- full Python/desktop regression, independent MCP health, and a 30-minute clean soak.

Both prior documents receive an acceptance appendix with exact evidence. No item is
marked complete from historical output alone.

## Rollback

Disable continuous delivery, stop the executor, and leave all visible native tasks
and catalog links intact. The paused conversational automation is not re-enabled.
Rollback never deletes provider sessions.
