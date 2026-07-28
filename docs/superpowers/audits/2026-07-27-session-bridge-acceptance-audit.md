# Session Bridge completion and acceptance audit

Date: 2026-07-27

## Scope

This audit reopens and completes:

- the 2026-07-13 cross-harness Session Bridge implementation plan; and
- the 2026-07-17 Claude native-session visibility design.

It treats committed code as implementation evidence, but treats only current
tests and live state as acceptance evidence.

## Recovered implementation

The July 13 tasks are present in history and in the current source tree:

| Area | Current implementation |
| --- | --- |
| Signed models, schema, store | `session_bridge/models.py`, `schema.py`, `store.py` |
| Claude and Codex discovery | `claude_adapter.py`, `codex_adapter.py` |
| Context and lineage | `context_pack.py`, `mirror.py`, `coordinator.py` |
| Authenticated MCP and CLI | `mcp_server.py`, `cli.py` |
| Desktop catalog metadata | Hermes desktop session source, search, and row components |
| Native Claude visibility | `claude_visibility.py`, `claude_registrar.py`, `claude_skill.py` |
| Claude desktop mirror surfacing | `mirror_float.py` |
| Guarded deployment | `scripts/install_session_bridge.ps1` and smoke test |

The MCP retains the original five user-facing catalog/continuation tools.
Approved follow-up recovery designs added authenticated operational tools for
native sidebar and Claude visibility workers; the original “exactly five”
check therefore applies to the user-facing catalog surface, not to the full
administrative tool list.

The July 17 design document was recovered from commit
`7f501ba713c11b46f5e07d2ab0a53d34e72bdba1`; its previous worktree had been
retired without carrying the document forward.

## Defects found in the completion audit

1. The long-running catalog kept a dead Codex app-server subprocess forever.
   The catalog remained stale even though a fresh native protocol probe was
   healthy.
2. A reviewed, exact-UUID Claude recovery could be requeued, but the configured
   attempt ceiling immediately failed it again before the single authorized
   recovery launch.
3. One real Claude visibility job remained `max_attempts_exhausted`; its exact
   reserved UUID was absent from both Claude JSONL discovery and the Claude
   Desktop registry.
4. The acceptance documents were missing or still marked pending even though
   most implementation commits had landed.
5. The Windows worktree JavaScript bootstrap currently fails with
   `spawnSync npm.cmd EINVAL`; the documented direct
   `npm ci --ignore-scripts` command succeeds and was used for UI verification.
6. The Claude registrar did not recognize Claude Code's current
   `You've hit your session limit` wording. A provider-limit response therefore
   occupied the full PTY timeout instead of returning a typed provider limit.
7. Continuous native Codex registration still depended on a successful,
   timely provider scan. Newly indexed sessions could remain `catalog_only`
   indefinitely even while the delivery executor itself was healthy.
8. The deployed 30-day sidebar inventory contained 150 eligible sessions with
   no durable sidebar job. This was a discovery/scheduling backlog, not missing
   Claude transcript data.
9. On Windows, `CodexAppServerClient.close()` terminated only the wrapper
   process. Its app-server descendants and their MCP children survived, so the
   long-running service accumulated process trees and eventually stalled
   provider refresh.
10. The public bounded-retry CLI formatter accepted any recognized backend
    error code instead of proving that the backend returned the exact
    operator-confirmed code.
11. The installer readiness failure path could spend longer in process-tree
    cleanup than the harness's bounded-startup contract allowed.
12. The MCP status sanitizer recognized only the bound and pre-create terminal
    resolution codes. It dropped the valid unbound
    `native_create_unrecoverable` bucket, then correctly failed its own count
    invariant and falsely blocked execution even though the store ledger and
    standalone CLI were valid.
13. A persisted Claude scan entry could remain pending forever when discovery
    no longer returned its transcript. The first recovery used an exact
    record-ID lookup, but that lookup probed every unrelated transcript and
    turned a bounded scan into a multi-minute operation.
14. Claude and Codex periodic scans shared one serial loop. A slow Codex
    inventory therefore prevented every later Claude scan even though the
    Claude provider itself was healthy.
15. Both scan loops and the filesystem watcher waited for the entire initial
    mirror reconciliation. One slow native read could hold that startup gate
    indefinitely.
16. The remaining Claude source was not actually deleted. Its transcript had
    reappeared with two native `sessionId` values, so the strict parser rejected
    it on every scan. The filename was one of the two exact IDs and the other
    ID also had its own canonical transcript.
17. Continuous Codex discovery paged the complete active and archived
    app-server inventory every three seconds. That monopolized the recovering
    client lock, delayed refreshes, and made routine incremental discovery look
    degraded.
18. The bounded newest-first sidebar probe jumped directly from its first page
    to the older durable backlog cursor. When more than 30 stable ineligible
    rows arrived ahead of that cursor, an eligible recovered session ranked
    behind them was skipped forever. The exact recovered Claude session above
    reproduced this defect: it was readable in the catalog but had neither a
    sidebar job nor a durable exclusion.
19. Continuous Codex discovery used the recent-only state database inventory
    even when the durable Codex seen-set was empty. After a provider outage (or
    on first bootstrap), the continuous watermark could be newer than every
    existing task, causing recovery to report success without indexing the
    inventory.
20. Periodic continuation reconciliation refreshed one exact Codex target by
    paging the complete active inventory before reading the task. With roughly
    1,400 Codex tasks, most refreshes took about 50 seconds and one exceeded the
    60-second refresh deadline, producing `codex_refresh_failed` during the
    acceptance soak.
21. Initial continuation reconciliation attempted every persisted continuation
    inside one 60-second startup deadline. Even after exact Codex reads were
    reduced to roughly 1-4 seconds, the aggregate 38-link pass exceeded the
    startup deadline and produced `mirror_reconcile_failed`.

## Corrections

- Use a replaceable Codex app-server client for provider inventory. A transport
  failure recycles the child and retries only read-only methods once.
- Never replay a mutation with an unknown outcome. The replacement is merely
  prepared for the next request.
- Preserve all Claude registration attempt history and the deterministic UUID,
  while allowing exactly one operator-authorized recovery launch after a fresh
  exact-UUID absence reconciliation.
- Recognize the current Claude Code session-limit wording as
  `provider_limit`, so a quota stop fails promptly and does not masquerade as a
  transport timeout.
- Run bounded catalog-to-sidebar registration inside the independent recovery
  worker whenever delivery is idle. Register no more than the configured batch,
  immediately deliver one job, and drain existing work before discovering the
  next batch.
- Reap the exact Codex app-server process tree on Windows with
  `taskkill /PID <exact-pid> /T /F`, then reap the root process. Retain the
  existing terminate/kill fallback for other platforms and failed tree cleanup.
- Require the retry result's backend error code to equal the exact
  operator-confirmed code before exposing a successful requeue result.
- Bound installer readiness cleanup so a failed cold start remains within the
  documented startup deadline.
- Preserve and validate all three fixed terminal-resolution codes in the MCP
  status surface, matching the store and CLI contract exactly.
- Retire an exactly absent persisted Claude queue entry without deleting any
  catalog or native history, and use exact filename-stem recovery rather than
  probing the contents of every transcript.
- Run Claude and Codex periodic scans in independent provider loops.
- Bound the initial reconciliation gate with the existing provider timeout so
  catalog discovery and the watcher always start.
- When a non-subagent transcript contains multiple session IDs and its filename
  is one of those exact IDs, project only records carrying the filename ID.
  Continue to reject mixed identities when the filename cannot resolve the
  conflict.
- Use the Codex app server's state-database-only, newest-first inventory for
  continuous discovery. Preserve complete active/archived pagination for
  explicit full-history backfill.
- Preserve the historical sidebar cursor while draining a separately persisted
  newest-range catch-up lane. Record the probe frontier and the catch-up
  cursor, target, and head atomically so a restart cannot skip the gap, repeat
  it forever, or discard older backlog progress.
- When the durable Codex seen-set is empty, perform one complete bootstrap
  inventory even in continuous mode. Use bounded state-database-only recent
  inventory after that durable baseline exists.
- Refresh a Codex continuation target with one exact `thread/read`; do not page
  the full inventory first. If Codex returns a lean read without lifecycle
  metadata, fall back to authoritative inventory so archive/delete semantics
  remain correct.
- Reconcile continuations in restart-safe batches of five and persist the
  bridge cursor after every full batch. Startup performs one bounded batch;
  later cycles continue from the cursor and wrap only after the set is drained.
- Restore this spec and maintain this evidence record in the active tree.

## Verification evidence

### Code and regression gates

- Fresh post-merge full Session Bridge suite:
  **2,413 passed, 10 skipped, 0 failed** in 24m16s.
- An earlier final isolated Session Bridge candidate completed
  **2,446 passed, 0 failed** across 35 files in 5m34s. The exact final commit
  `0966bae64`, including the two later live-discovered scheduling corrections,
  then completed **2,454 passed, 10 skipped, 0 failed** in 14m04s. The earlier
  load-sensitive coordinator wall-clock assertion also passed 10/10 in fresh
  processes.
- The final accepted source commit `c557537af`, including exact Codex
  continuation reads, lean-response lifecycle fallback, and batched startup
  reconciliation, completed **2,456 passed, 10 skipped, 0 failed** in 15m14s
  with an empty stderr log.
- Post-change focused gates:
  - recovering Codex client and adapter: **111 passed**;
  - merged critical recovery slice: **113 passed**;
  - Claude registrar after provider-limit parsing: **155 passed**;
  - CLI after independent registration scheduling: **178 passed**;
  - Codex app-server runtime/shutdown file: **30 passed**;
  - final MCP sanitizer file: **75 passed, 1 skipped**;
  - final merged narrow regressions: **2 passed**;
  - final Claude/Codex adapter and coordinator gate after the live-discovered
    fixes: **322 passed**.
  - complete coordinator file after the sidebar-gap correction:
    **120 passed**;
  - the new restart-safe starvation regression reproduced the failure before
    the correction and passed afterward;
  - the full final candidate suite exposed the empty-seen-set Codex recovery
    regression (**2,453 passed, 10 skipped, 1 failed**); the exact failure then
    reproduced alone, passed after the bootstrap correction, and the combined
    coordinator/end-to-end gate passed **144 tests**.
- Ruff passed on every touched Python/test file.
- Desktop verification used a direct `npm ci --ignore-scripts` because the
  repository bootstrap helper currently raises `spawnSync npm.cmd EINVAL`.
  Typecheck passed; targeted source/search/row tests passed **26 tests**.
- The isolated PowerShell installer harness passed its idempotency,
  reversibility, preservation, and secret-redaction checks.
- The bounded-readiness installer harness passed again after the cleanup
  deadline correction.
- Desktop final verification passed **1,715 tests, 1 skipped, 0 failed** across
  208 test files.
- A repository-wide isolated Python run was attempted from restored locked
  dependencies. It established pre-existing failures outside Session Bridge
  before being stopped to avoid delaying and contaminating the live soak:
  **13 failed assertions in 7 non-bridge files** covering ACP
  resource-link/ping/edit-approval cases, Windows tilde and sandbox-mirror path
  cases, context-reference path safety, and a Codex persistence test whose
  functional assertions complete before Windows teardown cannot delete an open
  temporary SQLite file. The exact retained log is
  `%TEMP%\hermes-full-suite-final.out`.
- The one Session Bridge load-sensitive result was repeated after isolation:
  `test_refresh_waiting_behind_hung_read_has_its_own_bounded_timeout` passed
  **10/10** in fresh processes.

### Native readable-content proof

- Native Codex task `019fa58a-5ec3-7bf2-b092-223035332e1d`, sourced from a
  211-message Claude Code session, was read through Codex app-server
  `thread/read`, not through bridge catalog metadata.
- Its native name is
  `[Claude] Land credential-pool fix b685528df and restart gateway`.
- Its initial preview starts with `# Imported Claude Code Session`, contains a
  deterministic Continuation Brief and `## Last 5 Messages`, has exactly five
  rendered User/Claude message blocks, and contains exactly one original signed
  bridge marker.
- A second native task,
  `019fa58a-227a-7ac2-b359-1bf90f6ba021`, independently proved that a newly
  imported Claude task is readable and named rather than marker-only.
- The exact mixed-ID recovery source
  `claude:afe1d37a-281c-4909-973e-45211bb36493` was then discovered through the
  corrected live catch-up lane, delivered as native Codex task
  `019fa83f-7e2b-71b3-99ce-37dccb7c2df7`, and read back through native
  `thread/read`. Its `[Claude] Investigate recurring unclean reboots (fork)`
  task starts with `# Imported Claude Code Session`, includes the continuation
  brief and the actual `## Last 5 Messages`, and retains the signed registration
  block only as the final provenance section.

### Deployment and live gates

- Deployed commits:
  - `d80174178` — recover stale native inventories and permit one reviewed
    exact-UUID recovery beyond the historical ceiling;
  - `fa235b36f` — recognize current Claude session-limit responses;
  - `0e43647d0` — register catalog sessions independently of provider scans.
- The final service process started at **2026-07-27 17:51:15 EDT** from
  `0e43647d0`; `:7484/health` returned `ok`.
- After deployment, Claude and Codex scans remained current and non-degraded.
  The internal worker began draining the 30-day discovery gap without the
  retired `session-sidebar-sync-worker` automation: the read-only missing count
  fell from **150** to **115** while native visible tasks increased.
- MemPalace and GBrain were probed independently and healthy. Session Bridge
  smoke passed health, MCP, and executor-help checks.
- Before the final restart, the old service owned **126** descendants, including
  leaked Codex app-server MCP trees. It was identity-validated and stopped as
  one exact PID tree.
- The first clean-restart candidate started at **2026-07-28 04:13:00 EDT** from
  `65177dac9`. Initial health, MCP, and executor smoke passed; the sidebar
  store/CLI terminal-resolution ledger was valid; both native delivery lanes
  were quiescent; and its descendant tree remained **14** through the initial
  repeated empty cycles. A live MCP probe then exposed the sanitizer defect
  above, so this candidate was not accepted.
- The final candidate service started at **2026-07-28 04:25:37 EDT** from
  `3f87d7c5d`. Its first live MCP status preserved all three resolution
  buckets, reported the ledger valid with no execution blockers, and completed
  fresh Claude and Codex provider scans.
- A later acceptance restart exposed a persisted Claude source
  `afe1d37a-281c-4909-973e-45211bb36493`. Its native file contains records for
  that ID and for `d9ca1174-a62b-4fb4-a281-fef844f6e9bc`; the latter also has
  its own canonical transcript. The preservation-safe parser recovery indexed
  the filename identity as a distinct 35-message session titled
  `Investigate recurring unclean reboots (fork)` and excluded the other
  session's records.
- The authoritative incremental scan then completed in **2.875 seconds** with
  `indexed=1`, `failed=0`, and Claude progress advancing from
  `indexed_total=41,410, remaining=1` to
  `indexed_total=41,411, remaining=0`.
- Provider scans now run independently, startup reconciliation is bounded, and
  continuous Codex inventory uses state-database-only paging. These corrections
  eliminate the three separate paths that had made healthy live services look
  stuck.
- No active `automation.toml` and no matching Windows scheduled task remained
  for `session-sidebar-sync-worker`. Two retired canary directories retain only
  historical `memory.md` records and cannot wake a task. Backlog scheduling and
  delivery are owned by the long-running Session Bridge recovery worker, so
  ordinary Codex work is no longer interrupted by broker heartbeats.
- The final inventory accounts exactly for every native-sidebar-eligible
  source: **1,118 Claude + 10 Hermes = 1,128 eligible**, and
  **1,122 visible + 6 terminally resolved = 1,128 accounted for**, with zero
  pending, leased, or retry rows. Readable legacy hydration is also quiescent
  at **93 visible**, zero pending/leased/retry/failed.
- A final newest-first catalog sample showed current substantive Claude Code
  sessions already linked to native Codex tasks with `mirror_state=mirrored`,
  including sessions active on 2026-07-28. This closes the original
  recent-session visibility complaint as delivery accounting rather than
  transcript discovery.

The accepted production candidate ran from commit `c557537af` as PID **88812**,
starting at **2026-07-28 12:29:23 UTC**. It completed the required 30-minute
clean soak through **12:59:44 UTC** with empty recent error codes, current
provider scans, no degraded reasons, and empty delivery and hydration queues.
The Windows descendant tree remained bounded at **14**.

The exact final Session Bridge suite completed **2,456 tests with 10 skips and
zero failures**. Read-only database verification found zero duplicate visible
source, bridge, idempotency, or native-thread identities; zero visible rows
without links; and zero open sidebar or hydration jobs. The final endpoint
accounted for all eligible sources: **1,118 Claude + 10 Hermes = 1,128
eligible**, and **1,122 visible + 6 terminally resolved = 1,128 accounted
for**. Readable legacy hydration was quiescent at **93 visible** with no open
or failed jobs.

Native `thread/read` for the exact formerly broken task
`019fa83f-7e2b-71b3-99ce-37dccb7c2df7` proved that its first turn contains the
Continuation Brief and actual last five Claude messages before the signed
provenance block.

## Acceptance disposition

Status: **implemented and accepted**

Every required live queue is quiescent, provider scans are current, the
Windows descendant count is bounded, the repeat stress gate is clean, and the
required fresh-service soak completed successfully.
