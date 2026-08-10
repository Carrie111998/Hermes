# ARCH-001-MAINLINE-001: Mainline integration reconciliation

**Status:** Codex self-review READY; owner-approved AGY cross-review waiver;
ready for commit. Merge and deployment remain separate gates.

## Objective

Port only the ARCH-001 runtime-state integration onto Hermes `origin/main`.
Do not merge the release branch history or the old ARCH branch wholesale, and
do not modify the live DGX checkout.

## Scope

- Add profile-scoped runtime-state configuration and database boundary.
- Run runtime-state preflight before gateway adapters are constructed.
- Track session, task, approval and compression lifecycle state through the
  existing gateway paths.
- Preserve direct `GatewayRunner(config)` construction for tests and embedders.
- Add focused startup and runtime-state coverage.

## Non-goals

- No merge to `main` in this worktree.
- No push, release rebuild, service restart or deployment.
- No edits to `/home/cwliao/.hermes/hermes-agent` or Claude-owned worktrees.
- No import of unrelated commits from the release or ARCH branches.

## Evidence so far

- Base: `origin/main` at `7fa1865f7`.
- Focused runtime/startup tests: `34 passed`.
- Config/approval/compression regression tests: `138 passed`.
- Ruff, compileall and `git diff --check`: passed.
- Codex self-review: READY after 387 relevant tests passed, Ruff, compileall
  and `git diff --check` passed. Two unrelated Windows-only approval tests
  remain environment-limited: `/tmp` path classification and symlink privilege.
- AGY independent review: waived by the owner for this ticket. The headless
  runner insisted on a bare `git status` outside the approved worktree-scoped
  command shape; no further AGY work is required for this gate.

## Acceptance gates

1. The diff contains only the main-based ARCH-001 port and current roadmap
   documents.
2. Full relevant tests pass, including failure-path cleanup and approval
   behavior.
3. Codex review finds no unresolved high-severity issue. **Met: READY.**
4. AGY independently returns `READY`, or the owner explicitly waives the
   independent cross-review and the exception is recorded. **Current result:
   OWNER-WAIVED.**
5. Only after the above: commit and push the branch for merge review.
