# Hermes Current-Upstream Session Ownership Port Plan

**Goal:** Produce one clean, current-`origin/main` commit that preserves v7 as immutable evidence, ports the smallest universal SQLite lease/fencing solution, removes installation-specific core coupling, verifies every mutating surface, and leaves a complete implementation/report/deployment manifest.

**Authoritative controller:** The current Hermes Agent session. Claude/Codex workers may investigate or implement isolated pathsets, but cannot stage, commit, push, deploy, or change task status.

**Timebox:** 8–12 focused hours. Stop expansion after the stated acceptance criteria. This is a one-long-day repair, not a new multi-day product program.

## Scope discipline

### Must ship in this commit

1. Immutable v7 historical bundle and evidence ledger.
2. Clean worktree and branch from freshly fetched `origin/main`.
3. Current-upstream mutating-surface inventory and ownership table.
4. Small universal SQLite lease/fencing kernel with canonical conversation identity.
5. RED→GREEN coverage for all current mutating entry points.
6. API 429 fast-path derived from, but never authoritative over, SQLite ownership.
7. Removal of private `orchestration-sessions.json` knowledge from Hermes core.
8. Installation-specific delegation defaults moved to supported profile/config/provider contracts.
9. Canonical route identities; no `claude-*` prefix inference.
10. Focused, broad, Desktop, dashboard, and private Workspace acceptance evidence.
11. Frozen immutable candidate, independent review, one coherent Git commit, completion report, and versioned temporary-patch manifest.

### Explicitly not part of this repair

- Rebuilding the comprehensive private Workspace.
- Porting every Workspace screen into official clients.
- Redesigning unrelated delegation UX.
- Broad cleanup, formatting, EOL normalization, generated website changes, or opportunistic refactors.
- Deploying before immutable review passes.
- Pushing unless separately authorized after the local commit is verified.

## Time budget

| Phase | Budget | Exit condition |
|---|---:|---|
| Preserve v7 and create clean branch/worktree | 30 min | Bundle hashes verify; new worktree is clean at fetched `origin/main` |
| Re-inventory upstream and write ownership table | 60 min | Every current mutating surface has exact source/test paths and an owner |
| RED tests and minimal kernel port | 3–4 hr | Each vertical behavior was observed RED then GREEN |
| Remove private coupling and normalize routing/config | 1–1.5 hr | No private schema/prefix inference remains in core; E2E config tests pass |
| Focused + broad + client acceptance | 1.5–2 hr | No new normalized failures; Desktop/dashboard/Workspace smoke evidence recorded |
| Freeze, parallel immutable reviews, remediation | 1–2 hr | Current artifact hash has no unresolved P0/P1 |
| Commit, report, manifest, post-commit verification | 45 min | One verified commit; clean worktree; committed diff equals approved bytes |

Expected total: 8–12 hours. If current upstream already implements part of v7, the lower bound is realistic. If broad tests reveal unrelated upstream failures, compare exact baseline signatures rather than turning this into a repository-wide cleanup.

## Phase 0 — Preserve v7 permanently

1. Treat the existing v7 patch as read-only historical evidence:
   - expected size: `288739` bytes;
   - expected SHA-256: `e7d378fd4c3b0cc55171d63920cfc38013c68433336cbe627675d93557e7b2e7`;
   - expected changed paths: `39`.
2. Create a repository-independent historical bundle containing:
   - patch;
   - path manifest;
   - JSON manifest;
   - evidence ledger;
   - test logs and review transport failures;
   - architecture review;
   - README stating that v7 is historical, unapproved, undeployed, and must not be rebased mechanically.
3. Compute a bundle manifest with individual SHA-256 values. Do not alter or delete the old hardening/verification worktrees.

## Phase 1 — Clean current-upstream worktree

1. Fetch `origin` from the authoritative Hermes repository.
2. Record fetched `origin/main` commit and remote URL.
3. Create a new branch such as `fix/current-upstream-session-ownership` in a new native Windows-path worktree.
4. Verify:
   - `HEAD == origin/main` at creation;
   - empty index;
   - no tracked or untracked files;
   - no shared writable worker processes.
5. Copy this plan byte-for-byte into the clean repository as `plan.md` before any production-code edit.
6. Add `IMPLEMENTATION-REPORT.md`, `OWNERSHIP-TABLE.md`, and the deployment manifest only as the work proceeds; keep the ledger current after every slice.

## Phase 2 — Re-inventory current upstream

Do not use the v7 caller list as authority. Search current source and trace every path that can mutate a conversation, transcript, lineage, checkpoint, replacement, publication, or lease.

The ownership table must include:

| Surface | Required operations |
|---|---|
| CLI/core agent | create/resume/send/save/model-tool publication |
| Gateway | send, queued send, shutdown flush, platform callbacks |
| API | send/stream, retry, rewind, reset, compress, resume |
| Desktop | backend calls for send/stream/retry/rewind/reset/compress/resume |
| Dashboard/TUI | send/model switch/retry/reset/compress/resume |
| ACP | prompt, cancel, resume, release |
| Cron/background | scheduled prompt, wake/replay, shutdown/recovery |
| Rewrite operations | retry, rewind, reset, compress, replacement, resume |

For each row record exact entry point, canonical identity source, lease acquisition point, fenced write/publication point, release/cancellation behavior, conflict projection, current tests, and missing RED test.

Run two read-only inventory workers in parallel (core/storage and clients/adapters), then have the controller reconcile against direct source searches. Workers cannot edit.

## Phase 3 — Vertical RED→GREEN implementation

Implement in dependency order; one behavior per cycle.

### Slice A — Canonical identity and SQLite kernel

1. Add a failing multiprocess test proving two processes cannot own one canonical conversation simultaneously.
2. Observe the expected RED failure on untouched current upstream.
3. Implement only the minimal schema/API for:
   - canonical conversation/root identity;
   - lease holder identity;
   - fencing generation/token;
   - acquisition/refresh/release;
   - stale-holder takeover using process identity/create-time and expiry;
   - transactional fenced writes.
4. Run focused GREEN and crash/cancellation tests.

### Slice B — Core agent lifecycle

Add RED tests for admission-before-history-load, holder lifetime through model execution and durable publication, cancellation-safe release, and stale writer rejection. Wire the core agent to the kernel and run GREEN.

### Slice C — Gateway/API/TUI/ACP/cron callers

For each ownership-table row:

1. Add or adapt one current-upstream test that fails because the path bypasses authority.
2. Observe RED for the intended reason.
3. Route the path through the same kernel; do not add a second lock authority.
4. Project typed conflicts consistently.
5. Run the focused caller suite before moving to the next row.

API-side in-memory/429 handling may reject obvious conflicts early, but it must derive canonical identity and truth from the SQLite authority. Its absence or process restart must not weaken correctness.

### Slice D — Destructive rewrites and resume

RED→GREEN each of retry, rewind, reset, compress, replacement, and resume. Assert that stale fencing tokens cannot publish, replace, or delete newer history and that lineage changes preserve the canonical authority contract.

## Phase 4 — Remove installation-specific core coupling

1. Add a failing test proving core does not read or parse `$HERMES_HOME/workspace/orchestration-sessions.json`.
2. Remove private Workspace filename/schema knowledge from core.
3. Move installation defaults—nested delegation, context handoff, shared memory, account choices, and fallback preferences—to supported profile/config contracts with conservative upstream defaults retained.
4. Replace `claude-*` prefix inference with canonical provider/route identities resolved from supported provider/profile metadata.
5. Add real-import E2E tests using a temporary `HERMES_HOME`; do not rely only on mocks.
6. Keep the comprehensive Workspace separate. Expose only the backend contracts needed by authenticated standalone dashboard/Desktop plugin halves; do not port the product during this repair.

## Phase 5 — Verification gates

Run in this order, recording exact commands, commit/base, environment, duration, pass/fail counts, and normalized failure signatures:

1. Kernel and multiprocess tests.
2. Core/session/storage tests.
3. Gateway/API/platform tests.
4. TUI/dashboard/ACP/cron tests.
5. Retry/rewind/reset/compress/resume tests.
6. Delegation/config/provider-route E2E tests with temporary `HERMES_HOME`.
7. Repository lint/type/static gates relevant to changed paths.
8. Broad canonical suite.
9. Clean-base comparison for any pre-existing broad failures.
10. Official dashboard smoke as a backend acceptance client.
11. Official Desktop smoke as a backend acceptance client.
12. Private Workspace smoke against supported contracts, without modifying Workspace.
13. Secret/privacy/prohibited-path scan.
14. `git diff --check` and exact path-manifest reconciliation.

Do not claim a globally red gate is green. Accept only zero new normalized failure signatures.

## Phase 6 — Freeze and immutable review

1. Stop/isolate all writers and prove bounded quiescence.
2. Stage only the exact owned manifest—never `git add .` or `git add -A`.
3. Export the complete binary candidate directly from Git to disk.
4. Record byte count, path count/list, base commit, and SHA-256.
5. Apply it in a fresh detached worktree at the recorded base and rerun focused and broad acceptance gates.
6. Dispatch independent parallel read-only reviews against the same immutable hash:
   - lease/fencing/transaction durability;
   - caller completeness and lifecycle;
   - architecture/privacy/config/provider boundaries;
   - Desktop/dashboard/Workspace compatibility.
7. Reproduce every concrete P0/P1 with a RED test before remediation. Any edit invalidates all approvals and requires a new hash/review cycle.
8. Timebox to two remediation/review cycles. If unresolved P0/P1 remains, stop fail-closed with the clean implementation preserved but uncommitted.

## Phase 7 — Commit and report

1. Commit one coherent vertical fix after all gates pass. Suggested message:
   - `fix(session): enforce durable cross-process conversation ownership`
2. Do not include installed-tree noise, caches, credentials, runtime state, generated website churn, or unrelated Workspace files.
3. Verify post-commit:
   - committed parent is the recorded current `origin/main` base;
   - committed diff hash equals the approved candidate hash;
   - worktree is clean;
   - focused and broad evidence is fresh after the final change.
4. Complete `IMPLEMENTATION-REPORT.md` with:
   - prior state and root cause;
   - current-upstream inventory;
   - ownership table summary;
   - implementation and architectural reasoning;
   - RED→GREEN evidence;
   - exact gates/results and baseline differences;
   - immutable review ledger and dispositions;
   - commit hash and path manifest;
   - exclusions and known limitations;
   - final state.
5. Create a versioned temporary-patch deployment manifest containing:
   - upstream base;
   - accepted commit and patch hash;
   - source-file hashes;
   - deployed-file hashes;
   - running-process/source hashes;
   - installation target;
   - retirement condition when equivalent upstream code lands.
6. Keep the final local commit unpushed unless explicit push authorization is given.

## Stop conditions

Stop rather than widening scope when:

- a mutating surface cannot be mapped to canonical identity;
- a worker or stale process can still mutate the candidate;
- current-upstream behavior cannot be reproduced deterministically;
- a focused test passes before implementation and therefore is not a valid RED test;
- any new broad failure signature appears;
- Desktop/dashboard acceptance exposes a backend contract break;
- immutable review has unresolved P0/P1;
- the approved, committed, deployed, or running hashes differ.

## Definition of done

- v7 exists as a hash-verified historical evidence bundle.
- New worktree started clean from freshly fetched `origin/main`.
- `plan.md` preceded production edits.
- Every current mutating surface appears in the ownership table.
- One SQLite lease/fencing authority covers all mutation paths.
- API 429 is only an optimization.
- No private Workspace schema or route-prefix inference remains in Hermes core.
- Installation preferences use supported config/profile/provider contracts.
- Focused, broad, Desktop, dashboard, and Workspace acceptance evidence is recorded with no new failures.
- One immutable candidate hash has independent approval.
- One coherent local Git commit contains all owned improvements and reports.
- Worktree is clean and the committed diff matches the approved bytes.
- Deployment, if separately authorized, is proven by exact source/deployed/running hashes.
