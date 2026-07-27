# Writer policy — concurrent audit sessions

Two Opus 5 sessions are auditing this repository at the same time. This file
records who writes where, so neither absorbs the other's changes.

## Worktrees

| Worktree | Base | Owner | Scope |
|---|---|---|---|
| `hermes-agent/` (main) | `main` @ `10ba6cc4f` | **shared — treat as read-mostly** | live gateway runs from here |
| `hermes-worktrees/audit-items-2-4` | detached @ `10ba6cc4f` | **this session** | ledger items 2, 3, 4 + updater/CI/evals |
| `hermes-worktrees/lead-runtime` | detached @ `10ba6cc4f` | other session | unknown — do not edit |
| `_worktrees/broker-deny-hardening` | `fix/broker-dangerous-deny-no-approval-path` | pre-existing | do not edit |

Both audit worktrees are **detached HEAD**. That is deliberate: the profile's
`reference-transaction` hook gates every `refs/heads/*` update through
pipeline-gate (`--require-run`), so creating a branch aborts the worktree.
Detached commits move no branch ref, so they neither trip the gate nor bypass
its intent — the operator still reviews before anything lands on a branch.

## Rules in force

* One integrator per repository; this session integrates only its own hunks.
* No `git add -A` / `git add .` — every commit stages explicit paths, and
  `git status --porcelain` is inspected before each commit.
* No history rewriting and no force-moving refs while another writer is active.
* The main tree is not edited by this session except for files it owns
  outright (`AUDIT_*.md`, and the profile plugins it fixed), staged by path.
* Anything found dirty in the main tree that this session did not write is left
  strictly alone.

## Entangled work preserved, not staged

`profiles/aletheon/plugins/worker-alert-gate/` carries pre-existing WIP
(+392/−17 in `alert_core.py`, +239 in its tests) interleaved with ~65 lines of
this session's lock fix. It is **not** committed by this session. Recoverable
snapshot:

```
backups/hermes-audit-safety-20260727T070301Z/entangled-wip/
  worker-alert-gate.patch          816 lines, full working diff
  worker-alert-gate.sha256.json    sha256 of all 24 files
```

## Handover

Final integration is the operator's: they merge both sessions' work. This
session's commits are reachable from the detached HEAD of its worktree; the SHA
is recorded in `profiles/aletheon/AUDIT_STATUS.md` at session end.
