# Native Kanban review and scheduled handoffs

Status: review-only implementation; production activation is not authorized.
Base commit: `2eaa863112d2980bbe6f15ea409a6a29e50964fe`.

## Architecture decision

The same card retains `assignee` as the implementation owner. Native review routing uses `review_assignee`; the dispatcher resolves that independent profile only while the card is in Review. The writer must present its active run id and authenticated profile to request review. The transition atomically closes the writer run, stores canonical frozen commit/tree/artifact digests, clears the claim, parks the card in Review, and emits `review_requested`.

A reviewer claim creates a separately attributable run whose `profile` is the effective reviewer and whose claim event records `source_status=review`. PASS and REQUEST_CHANGES require the bound reviewer profile and active run id. PASS emits `review_passed` and moves to Done. REQUEST_CHANGES emits `changes_requested`, clears the current frozen artifact set, and returns to Ready/Todo for the preserved writer. Generic completion cannot approve a native review run.

Native reviewer claims require a nonempty authenticated actor exactly matching `review_assignee`; argument omission is not authority. Autonomous review dispatch is fail closed: the default is false and only the exact managed Boolean `kanban.review_dispatch=true` activates it. Missing keys, nulls, integers, strings, malformed containers, and configuration-loader failures all leave Review parked. Gateway stuck detection calls the same gate as the dispatcher.

Review and writer lanes share the existing global and per-profile concurrency accounting. Queue order remains priority descending, then creation time ascending. Existing crash, timeout, stale-claim, restart, spawn-failure, and orphan reconciliation use claim provenance to restore interrupted reviewer runs to Review rather than Ready.

The existing `kanban_notify_subs` cursor protocol remains the single notification mechanism. It already provides ordered event ids, transactional cursor claim, CAS rewind, deduplication, per-subscription failure isolation, retry, and dead-route removal. Native review verdict and scheduling event kinds are added to the notifier allowlist; no second outbox or dispatcher is introduced.

Native schedules use UTC Unix epochs. `scheduled_for` controls promotion; `due_at` is a visible deadline. A single dispatcher tick atomically emits at most one `scheduled_pre_notice` inside T-15 and at most one `scheduled_promoted` at/after the scheduled epoch. Open parents land in Todo; otherwise cards land in Ready. Durable markers and status-CAS make restart, missed tick, and duplicate tick safe.

## Compatibility and migration

The migration is additive and idempotent:

- `tasks.review_assignee TEXT`
- `tasks.review_artifacts TEXT`
- `tasks.review_protocol TEXT NOT NULL DEFAULT 'native_v2'` with an allow-list
  constraint for `native_v2|legacy`
- `tasks.scheduled_for INTEGER`
- `tasks.due_at INTEGER`
- `tasks.pre_notice_sent_at INTEGER`
- `idx_tasks_schedule(status, scheduled_for, created_at)`

When `review_protocol` is first added, rows already present in that transaction are explicitly marked `legacy`; tasks created after the migration inherit `native_v2`. Reopen/reapply does not relabel either class. Legacy compatibility is selected only by that durable one-time marker, never by omitted function arguments or nullable artifact metadata. Missing, malformed, or future protocol values fail closed as native. New authenticated work does not depend on `review-required` comment text.

SQLite ignores additional columns when an older binary reads named legacy fields. Historical review/schedule fields and events therefore remain preserved if code is rolled back. Cards already in Review remain in the pre-existing Review status. Cards in Scheduled remain parked and can be manually unblocked by the legacy control surface.

## Activation plan

1. Freeze the reviewed commit/tree and capture a production-shaped database backup plus SQLite integrity result.
2. Stage the image without changing the running digest.
3. Provision a distinct reviewer profile and verify the sole dispatcher resolves it; do not share BB8, R2D2, human, or admin credentials.
4. On a production-shaped copy, run migration twice, validate all row counts/links/comments/runs/events/subscriptions, and run the full Kanban regression suite.
5. Deploy with `kanban.review_dispatch=false` and `kanban.native_scheduling=false`; restart only in the approved maintenance step.
6. Exercise one non-production board through writer -> reviewer -> changes -> writer -> reviewer -> pass, including controller wake delivery.
7. Enable `review_dispatch` for the staged board only. Observe ordering, global/per-profile caps, reviewer attribution, cursor advancement, and abnormal-run return to Review.
8. Enable `native_scheduling` only after T-15 and due-time route proof.
9. Migrate legacy review-required cards through the authenticated API in a separately reviewed operation; never infer reviewer authority from free text.
10. Expand board scope only after R2D2 accepts the staged evidence.

## Rollback

1. Set `kanban.review_dispatch=false` and `kanban.native_scheduling=false`; verify no new review/schedule claims occur.
2. Stop only the approved dispatcher/gateway unit in the maintenance window.
3. Verify there are no active or parked `native_v2` Review cycles before restoring the previously pinned image/runtime atomically; the old runtime does not enforce the native verdict gate. Do not downgrade or rewrite the database.
4. Restart the prior runtime and verify card/comment/link/run/event/subscription counts and SQLite integrity.
5. Leave native columns/events intact as historical evidence. Do not delete them.
6. For any active reviewer run, first allow the approved reclaim path to restore it to Review; do not directly edit SQLite.
7. Scheduled cards remain parked until manually unblocked or until the reviewed runtime is restored.

Rollback proof is a feature-disable plus prior-runtime restoration, not a destructive down migration. This avoids loss or misassignment of in-flight cards.

## Threat model

- Self-approval: reviewer canonical identity must differ from the stable writer identity.
- Identity spoofing: worker tools derive profile and run id from dispatcher-owned environment; DB transitions compare them with the active run.
- Stale/replayed verdict: verdicts require current_run_id, an open matching run, and a review-origin claim event.
- Authority downgrade: review authority is bound to `review_protocol`; omitted actor/run/artifact arguments cannot select legacy behavior for new tasks.
- Generic completion bypass: every `native_v2` review rejects generic completion, including malformed rows with missing frozen metadata.
- Artifact substitution: canonical commit/tree ids and relative artifact paths with SHA-256 digests are frozen in the request transaction; reassignment is refused during Review.
- Queue starvation: review lane reserves an opportunity within the shared bounded budget.
- Crash/restart: review-origin provenance restores the Review phase.
- Notification replay/loss: event-id ordering, atomic cursor claim, CAS rewind, retry, and dead-route handling are reused.
- Schedule replay: durable pre-notice marker and status-CAS make ticks idempotent.
- Shared credentials: prohibited; reviewer and writer profiles remain separately attributable.

## Residual risk and approval gates

- Real controller-session wake and external route delivery must be proven in the staged deployment. This branch does not activate notification fan-out, by work-order exclusion.
- Reviewer profile provisioning and authority are platform-state changes requiring exact-tree review.
- No live database migration, dispatcher replacement, gateway restart, or production activation was performed.
- The deployment’s accepted approximately-one-month-behind source target still needs R2D2 to compare this backport base with the managed image ancestry before rollout.
- Legacy compatibility paths remain intentionally available; removal requires a later deprecation cycle after every board is migrated.
