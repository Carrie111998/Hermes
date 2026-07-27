# Session Sidebar Backlog Recovery Design

**Date:** 2026-07-26
**Status:** Approved behavior; implementation pending written-spec review

## Problem

The readable-session rollout proved one new-task canary but did not satisfy the
user-visible requirement across the real population.

Live evidence at the start of this recovery:

- 144 eligible Claude sessions were in `sidebar_pending` without Codex task IDs.
- The oldest pending session had waited about 85 hours.
- The worker claimed pending registrations oldest-first and completed roughly one
  task every 172 seconds at the observed median rate.
- The newest manually recognizable Claude Desktop sessions were indexed and queued,
  but were waiting behind the historical backlog.
- In a sample of 38 recent `[Claude]` Codex tasks, 10 had a readable initial prompt
  and 28 still had the legacy placeholder prompt.
- Only two legacy tasks had been hydrated during the earlier canary rollout.

The original prompt and sidebar preview of an existing Codex task are immutable.
Legacy tasks therefore cannot be made to look like newly created readable tasks
without replacing them. Replacement is forbidden because it would break exact
source/task identity and create duplicates.

## Approved Outcome

1. Every eligible native Claude session from the last 30 days gets one exact Codex
   task.
2. Newly active sessions become visible promptly instead of waiting behind the
   historical queue.
3. Every legacy placeholder-only task in that same eligible population receives one
   readable in-place append containing:
   - the bounded continuation brief;
   - the chronological last five user/assistant messages;
   - the authenticated hydration marker and continuation instruction.
4. Existing task IDs, titles, project grouping, and source links are preserved.
5. No replacement or duplicate task is created.
6. The deleted `session-sidebar-sync-worker` Codex automation is not recreated.

## Considered Approaches

### 1. Durable in-process recovery worker — selected

Extend the existing Session Bridge service with a single serialized recovery loop.
It uses the durable registration and hydration queues, authenticates exact task and
source identity, and performs one native Codex operation at a time. It continues
after restarts from durable reservations and marker reconciliation.

This is selected because it is resumable, observable, does not interrupt the
current Codex task with heartbeat messages, and preserves the existing duplicate
prevention model.

### 2. Recreate the Codex heartbeat automation — rejected

The existing skill can process one lease per heartbeat, but the prior automation
repeatedly interrupted active work. Recreating it violates an explicit user
requirement.

### 3. One-off bulk script — rejected

A one-off loop could process the current rows but would not be safely resumable
after ambiguous sends, process loss, or app restarts. It would also leave the same
backlog failure mode for future bursts.

## Architecture

### Queue scheduling

Retries remain highest priority because they may own an exact native task or an
ambiguous-send reservation.

Pending registrations use a freshness lane plus a backlog lane:

- the freshness lane selects the newest eligible pending session;
- the backlog lane selects the oldest eligible pending session;
- the durable scheduler alternates bounded groups of fresh and backlog work.

The initial policy is three fresh claims followed by one backlog claim. This makes
new Desktop sessions visible quickly while guaranteeing that old work cannot
starve. Ordering inside each lane is deterministic by `eligible_at` and job ID.

The lane position is persisted in `session_bridge_state` in the same transaction as
the claim, so restarts do not reset fairness.

### Continuous drain

Sidebar delivery moves out of the provider-scan critical path into one dedicated
local worker owned by the Session Bridge service. The worker:

1. runs only when sidebar continuous mode is enabled;
2. owns its own native Codex client;
3. processes exactly one lease at a time;
4. immediately continues while actionable work remains;
5. uses a bounded idle wait when both queues are empty;
6. stops cleanly during service shutdown.

Existing process and durable database locks remain authoritative. No concurrent
native creation or hydration send is allowed.

### Bulk hydration seeding

A guarded bulk seed operation selects only rows that satisfy all of these rules:

- source provider is Claude;
- source is an eligible native session within the configured 30-day window;
- the sidebar job is `sidebar_visible`;
- the exact Codex task ID is present;
- no hydration job already exists for the source/task pair;
- the registration became visible before the readable-registration cutover, or the
  exact task read proves the first prompt is a legacy placeholder.

The command supports dry-run output before mutation and requires the literal
confirmation:

`HYDRATE_ALL_EXACT_EXISTING_TASKS`

Seeding is idempotent. Re-running it creates no duplicate hydration rows.

### In-place hydration execution

The service claims hydration before registration work. For each hydration lease it:

1. reads the exact existing Codex task;
2. authenticates the original signed source marker, local host, source/task link,
   and allowed cwd/project identity;
3. searches that exact task for the exact hydration marker;
4. commits immediately if the marker is already present in a completed turn;
5. refuses a second send after any ambiguous reserved send;
6. otherwise reserves the send and starts one turn on that exact task with the
   hydration message verbatim;
7. polls only that task until the authenticated marker is present and the turn is
   complete;
8. commits the hydration row.

It never creates, renames, archives, or replaces a task in hydration mode.

## Failure Handling

- Retryable native indexing and service failures retain the exact task ID.
- Ambiguous creation and ambiguous hydration sends remain reconciliation-only.
- Marker, source, host, or task conflicts fail closed.
- A failed job cannot globally block unrelated pending work once it has a durable
  terminal resolution.
- Public status reports counts, oldest age, drain rate, and fixed error codes only;
  it never exposes prompts, messages, markers, tokens, or raw exception text.

## Rollout

1. Add failing tests for fresh/backlog scheduling, starvation prevention,
   idempotent bulk seeding, exact-task hydration, ambiguity reconciliation, and
   dedicated-worker serialization.
2. Implement the smallest changes required by those tests.
3. Run the focused store/executor/CLI/end-to-end suites, then the full Session
   Bridge suite and Ruff.
4. Deploy from local `agent-src` main at a zero-lease boundary.
5. Run bulk-seed dry-run and report the exact count.
6. Apply the guarded seed with the approved confirmation.
7. Verify at least:
   - three newest pending Claude sessions become readable Codex tasks;
   - three pre-cutover placeholder tasks receive one readable append in place;
   - the originally reported task remains the same ID with no duplicate;
   - pending and hydration queues decrease across successive observations.
8. Leave the in-process worker running until both queues drain. Do not create a
   Codex automation.

## Acceptance Criteria

- Recent Claude Desktop sessions appear in Codex within the bounded worker drain
  interval, not after the entire historical backlog.
- The 30-day eligible registration backlog reaches zero.
- The eligible legacy hydration backlog reaches zero.
- Every hydrated source/task pair is unique.
- No task replacement or duplicate is created.
- New tasks contain the readable brief and last five messages in their initial
  prompt.
- Legacy tasks contain exactly one readable hydration append; their immutable
  original placeholder remains historical evidence.
- Session Bridge health remains non-degraded with zero blocking failures.
- `session-sidebar-sync-worker` remains absent.
