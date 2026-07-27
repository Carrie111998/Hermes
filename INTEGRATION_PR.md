# Hermes core audit — integration

Reconciles both audit sessions, the upstream delta, and Codex's continuation
repair onto one branch. Full detail in `PATCH_LEDGER.md` (section
"Integration: `integration/hermes-core-audit-20260727`").

**Backup refs** (created before any history change):

```
refs/backup/pre-integration-audit-20260727       bdac397c3
refs/backup/pre-integration-main-20260727        f45bfab66
refs/backup/pre-integration-originmain-20260727  731aa0ccc
```

## What this contains

* All local audit commits from both sessions, preserved — nothing squashed or dropped.
* `origin/main` merged to tip `731aa0ccc` (0 behind).
* Codex's `stage_successors` continuation repair, committed for the first time.
* Step 8 of `PLAN_AUTO_DISPATCH.md` finished: all 19 XFAILs resolved.

## Topology note

The brief's starting numbers were stale. `audit..main` was **10** commits, not 61
(51 of main's 61 were already present); `main` was ~160 upstream commits behind
what the audit branch already carried, so merging in that direction would have
been a large regression. `origin/main` was merged first so the peer session's
work landed on a current base.

## Conflict resolution

**No textual conflict.** The only overlap is `agent/turn_finalizer.py` and
`tools/delegate_tool.py`, which both sessions independently fixed for H-01. The
auto-merge composed rather than chose, and neither side was a superset:

| File | Peer session | This session | Result |
|---|---|---|---|
| `agent/turn_finalizer.py` | `turn_crashed()` shared predicate | `EMPTY_TERMINAL_EXIT_REASON` (H-26) | both |
| `tools/delegate_tool.py` | `child_crashed`, `exit_reason="crashed"` | exit-reason-keyed `_empty_sentinel` | both |

`gateway/run.py` was **not touched by either commit range**, contrary to the
brief. Its only change is Codex's, applied hunk by hunk: two of their three
hunks (the Ultra mixin import and its MRO position) were already fixed
independently here in `b26927b2c` and were **not** reapplied; the third — widening
the `_start_worker_bridge_watchers` enable gate — was applied and fixes a live
defect where enabling only stage successors produced a watcher that never ran.

## Codex repair provenance

Not on any branch, ref, stash or note. It existed **only as uncommitted files in
the main worktree**. Recovered and committed in `8b8d73fbe`; originals left in
place. This also closed a clean-checkout breakage — `worker_bridge_watchers.py`
imported `gateway.stage_successors`, which did not exist in this tree and
resolved only because the venv's editable install served it from the *other*
worktree. Any fresh clone or CI runner would have failed at gateway import.

## XFAIL disposition — 19 of 19 resolved

Eighteen were implemented or corrected outright; see the ledger table for
per-test evidence. The
file's own header claimed four designs were "superseded during implementation" —
three of those claims were **false**, and checking them against the shipped code
is what resolved those tests.

**The final one is now resolved too.** `test_selects_only_created_and_queued`
was left open because it turned on a deployment decision: whether the guarded
watcher or the thin dispatcher owns `created` tasks. The operator chose the
guarded watcher, so ownership was handed over.

That closes a real guard bypass. `hermes_worker_bridge.dispatch.dispatch_pending`
honours only `spec.context.auto_dispatch is False` — every other guard (manual
hold, `depends_on`, pending permission, pending input, retry budget) lives in
the watcher and was never consulted, so `created` tasks were dispatched with
none of them applied. `claim_task_for_dispatch` now performs the created->queued
reservation inside its own `BEGIN IMMEDIATE`, so the reservation and the guard
checks cannot be interleaved, and the thin dispatcher stands down rather than
racing. Its previous body is retained, unreferenced, so reverting the handover
restores it on the next restart without touching the production plugin.

**All 19 XFAILs now pass. Zero remain.**

## Tests

```
tests/contract/                                36 passed
evals/behavioral/                              13 passed, 4 skipped
scripts/verify_protected_behavior.py           16/16 simulated regressions caught
scripts/check_patch_ledger.py                  ledger covers every local commit (74/74)
scripts/rehearse_rollback.py                   every rollback path restores and verifies
gateway watcher suites                         102 passed, 0 xfailed (was 58 passed, 19 xfailed)
tests/tools/ -k delegat                        352 passed, 3 skipped
```

Pre-existing failures, independently confirmed not caused by this work: a
read-only review extracted both merge parents with `git archive` and ran
`tests/gateway/` + `tests/tools/` on each — **228 failed on both, byte-identical
failure name sets**; the merge adds 74 net passing tests. A whole-repo
`pytest tests/` run aborts with 50 collection errors from cross-file import
pollution; every affected file collects cleanly alone.

## Rollback

```bash
git reset --hard refs/backup/pre-integration-audit-20260727
```

Database, config and history rollback procedures are in `ROLLBACK.md`, rehearsed
by `scripts/rehearse_rollback.py`.
