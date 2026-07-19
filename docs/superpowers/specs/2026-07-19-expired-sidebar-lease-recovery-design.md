# Expired Sidebar Lease Recovery Design

Date: 2026-07-19

Status: approved by the user after live diagnosis

## Objective

Eliminate the deadlock where expired `sidebar_leased` rows remain invisible to the
Session Sidebar Sync worker even though their exact native Codex thread IDs are
durably bound. Recovery must never create a replacement native task.

## Confirmed failure

Two production rows remained `sidebar_leased` more than eight minutes after their
five-minute leases expired. Both rows had exact `codex_thread_id` values, and both
native Codex tasks were local, readable, correctly titled, authenticated by their
signed markers, and idle. `session_status` nevertheless reported
`sidebar_pending=0`, `sidebar_retry=0`, and `sidebar_leased=2`.

`SessionBridgeStore.claim_sidebar_jobs` currently selects pending and retry rows
before converting expired leased rows to retry. A first claim call therefore
repairs the rows but returns no jobs; a second call is required to claim them. The
installed worker contract forbids the first call when status reports no pending or
retry work, so the repair call never occurs.

## Considered approaches

### 1. Transactional recovery before selection (selected)

Within the existing `BEGIN IMMEDIATE` claim transaction, convert every expired
lease to retry before querying due pending/retry work. The same claim call can then
return an expired job with its durable `codex_thread_id`. Status classifies expired
leases as actionable retry work so the status-first worker is authorized to call
the claim endpoint.

This fixes the source of the deadlock, preserves atomicity, and uses the existing
exact-ID reconciliation path.

### 2. Worker calls the pending endpoint twice

This would depend on undocumented store ordering and would still contradict the
status-first safety contract. It is rejected as a fragile workaround.

### 3. Add a separate lease-reaper service

A background sweeper would add another writer and operational dependency for a
transition already owned by the claim transaction. It is rejected as unnecessary.

## Selected behavior

### Atomic claim

`claim_sidebar_jobs(now, limit)` performs these steps in one write transaction:

1. Reclassify every `sidebar_leased` row with `lease_expires_at <= now` as
   `sidebar_retry`, clear its lease digest and expiry, preserve its exact
   `codex_thread_id`, clear any transient error, and set `next_attempt_at=now`.
2. Select due retry and pending rows, retaining retry priority and existing stable
   ordering.
3. Lease up to the requested bounded limit.

An expired job can therefore be reclaimed by the first claim call. Its old lease
token remains invalid, and its durable thread ID is returned for exact-ID
reconciliation.

### Actionable status

`sidebar_delivery_status(now)` reports expired leased rows as actionable retry work
for broker gating while leaving unexpired leases counted as leased. Counts remain
mutually exclusive and preserve the total number of jobs. This is a read-only
classification; the durable state transition still occurs atomically in the claim
transaction.

### Native identity safety

Recovered rows with `codex_thread_id` must use the existing recovered-ID path:
direct bounded read, host/project/marker verification, idempotent bind, rename,
commit. Marker search and task creation are forbidden for those rows.

## Testing

Regression coverage must prove:

- a database containing only one expired leased row returns that row from the first
  claim call;
- its exact `codex_thread_id` is preserved and its old token cannot commit;
- status reports the expired row as retry/actionable and does not count it as an
  active lease;
- an unexpired lease remains leased and is not claimable;
- pending/retry ordering and concurrent claim uniqueness remain unchanged.

The focused store, MCP, coordinator, and sidebar reconciliation suites must pass
before deployment.

## Deployment and recovery

1. Deploy through the canonical Session Bridge launcher.
2. Verify health and provider status.
3. Reclaim the two expired production rows.
4. Reconcile their exact bound Codex IDs and commit them without replacement
   creation.
5. Verify pending, leased, retry, and failed counts are zero and visible identities
   remain unique.
6. Resume the one-minute Session Sidebar Sync heartbeat and verify one empty cycle.

## Rollback

Pause the heartbeat and restore the previous deployed build. Do not clear bound
thread IDs, delete native tasks, or manually edit the production database. Existing
expired rows remain safely recoverable after the corrected build is restored.

## Acceptance criteria

- The first claim call reclaims expired leases.
- Status exposes expired leases as actionable work.
- Exact bound native task IDs survive recovery.
- No replacement task is created after lease, bind, or commit ambiguity.
- The two production rows commit visibly and uniquely.
- The resumed worker completes an empty cycle without leaving stale leases.
