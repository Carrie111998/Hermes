# Issue #77881 / PR #77883 progress

Last updated: 2026-08-03 13:31 UTC

## Goal
Preserve safe continuation context when a dispatcher-owned Kanban worker yields at its iteration budget, while rejecting unsafe resume and retaining bounded retry/circuit-breaker behavior.

## Investigation
- [x] Confirm current finalizer generated a summary but closed the run before persisting it.
- [x] Confirm dispatcher launched every retry as a fresh `hermes ... chat -q` session.
- [x] Trace run schema, claim identity, worker argv, session resume support, and board/profile routing.
- [x] Confirm exact resume compatibility inputs and deterministic fallback behavior with tests.

## TDD slices
- [x] RED/GREEN: timeout closure atomically stores summary, worker session, run/task identity, workspace/profile/model/provider, git branch/head/content-sensitive dirty state, and cumulative counters.
- [x] RED/GREEN: compatible timed-out run adds `--resume <session> --no-restore-cwd` to the worker argv.
- [x] RED/GREEN: ownership/workspace/profile/model/provider/branch/head/dirty-state mismatch or competing run suppresses resume and leaves the durable prior-run handoff available.
- [x] RED/GREEN: root Kanban workers stop at a soft iteration threshold, use the reserved summary call, and record a checkpoint rather than consuming the hard limit.
- [x] RED/GREEN: timeout metadata tracks progress/no-progress and bounded cumulative retry/iteration observability without weakening the existing circuit breaker.
- [x] E2E: timeout -> re-claim -> spawn retains continuation context through the real board DB, session DB, Git state, and argv path.

## Implemented behavior
- Root Kanban workers reserve a bounded soft checkpoint budget; delegated children and pending user interrupts bypass it.
- Timeout summaries use an isolated message-list view, then persist the real transcript before the exact-run atomic handoff transition.
- Closed run metadata records session/workspace/profile/model/provider/reasoning identity, branch/head, worktree and staged-index fingerprints, retry counters, progress fingerprints, and cumulative iterations.
- Dispatcher resume is accepted only when the previous run, current task/workspace/route, one active run, and durable session row match exactly; otherwise the normal `kanban_show` prior-run summary is the deterministic fallback.
- Productive Git progress resets timeout stagnation. Repeated no-progress checkpoints trip the existing per-task circuit breaker, with a bounded cumulative timeout cap.
- Persistence, board-routing, or ownership conflicts fail closed without releasing a newer/current claim.

## Verification / publication
- [x] Focused CI-parity tests pass with `scripts/run_tests.sh`.
- [x] Broad affected suite passes: 49 files / 279 tests.
- [x] Ruff and `git diff --check` pass.
- [x] Local Codex review findings resolved: board pinning, persistence-before-release, delegated-child isolation, content-sensitive worktree/index fingerprints, and interrupt precedence.
- [x] Codex app-server applicability reviewed: it does not consume Hermes' per-tool iteration budget and owns native turn persistence/watchdog retirement, so this timeout behavior correctly remains scoped to the Hermes-managed loop.
- [ ] Coherent conventional commit pushed to `fork/resolve/issue-77881-kanban-resume`.
- [ ] Draft PR updated; GitHub `@codex review` loop reaches current-head all-clear.
- [ ] Exact Kanban review-required handoff written before blocking.
