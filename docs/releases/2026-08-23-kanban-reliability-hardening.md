# Kanban reliability hardening — 2026-08-23

## Status

- **Engineering state:** integration complete; QA-approved candidate frozen.
- **Release state:** not released.
- **Deployment state:** not deployed.
- **CI / merge state:** pending. PR #93021 is open as a draft and has no completed CI status checks at the time of this note.
- **Candidate commit:** `bcff6c7627bd9b826fb00ee850766a21c2fa0a3a`
- **Candidate tree:** `74d86da103ac23575ed6cb2d5136ccc5ba242376`
- **PR:** https://github.com/NousResearch/hermes-agent/pull/93021

This note records the reviewed candidate; it is not a release announcement and does not indicate that production contains these changes.

## Operator impact

The hardening changes make Kanban dispatch and sensitive execution more conservative when the required immutable identity, workspace, or containment conditions are not proven. Operators should expect unsafe or ambiguous work to be held rather than started. Review and dispatch decisions must continue to use the frozen candidate identity; do not rebase, squash, or replace it without a new remediation and fresh combined QA.

## What changed

The candidate hardens these reliability boundaries:

- **Atomic containment and preflight:** containment and dispatch holds are applied atomically so a partial state cannot advertise a task as safe to run.
- **Immutable workspace identity:** dispatch is gated on the immutable workspace identity required by the review candidate.
- **Immutable review candidates:** review lifecycle handling preserves the exact candidate under review instead of silently accepting a moving or substituted tree.
- **Secret-sensitive execution:** sensitive execution is explicitly gated and runs with an isolated runner environment.
- **Sensitive artifacts and tool taint:** the focused QA coverage validates the sensitive-artifact and sensitive-tool paths alongside the dispatcher and worker behavior.

## Compatibility and migration

No user-facing migration was identified in the reviewed evidence. Existing Kanban tasks that satisfy the new preflight and identity requirements should continue through the normal lifecycle. Tasks that cannot prove those requirements may remain held and require operator or reviewer follow-up; this is intentional containment behavior, not evidence that the task should be force-started.

The candidate is six commits ahead of the planning base `f293e7206b4ddd66042329442c6afebc19a8808d`. `origin/main` advanced independently to `739bc555b1932e66c169b20edec3a48368e2ddf3`; it is 31 commits ahead of the candidate branch, so the PR/CI gate must decide whether a rebase is required. A rebase or replacement must not be performed silently because it would invalidate the reviewed tree.

## Security and privacy

Sensitive execution is fail-closed at the dispatch boundary and uses an isolated runner environment. The hardening is intended to prevent secret-sensitive work from running without the required authorization and containment conditions. This note intentionally omits credentials, tokens, and raw sensitive data.

## Verification evidence

QA and integration verification reported the following focused command as passing:

```text
scripts/run_tests.sh tests/hermes_cli/test_kanban_atomic_containment.py tests/hermes_cli/test_kanban_block_kinds.py tests/hermes_cli/test_kanban_dispatch_preflight.py tests/hermes_cli/test_kanban_host_cap.py tests/hermes_cli/test_kanban_review_lifecycle.py tests/hermes_cli/test_kanban_sensitive_execution.py tests/hermes_cli/test_kanban_worker_spawn_toolsets.py tests/tools/test_kanban_sensitive_artifacts.py tests/agent/test_kanban_sensitive_tool_taint.py -q
```

Result: **84 tests passed across 9 files, 0 failures.**

Additional candidate checks passed:

- `git diff --check f293e7206b4ddd66042329442c6afebc19a8808d..HEAD && git diff --check`
- clean worktree verification;
- local candidate SHA/tree matched the fork remote branch;
- all six expected lane commits were present in reviewed order.

The broader/full suite remained environment-limited by unrelated missing optional Anthropic/OpenAI provider dependencies, as documented by QA. PR CI had not completed when this note was written.

## Rollback

Because the candidate is not released or deployed, no production rollback is currently required. If the candidate must be removed before merge, restore the reviewed branch to the approved pre-hardening base `f293e7206b4ddd66042329442c6afebc19a8808d` through the repository's normal PR/review process. Do not rewrite the approved candidate in place; any changed candidate requires remediation and fresh combined QA.

After deployment, rollback should use the repository's standard versioned deployment procedure to restore the last known-good build, followed by verification that the running code identity matches the intended rollback target. No deployment-specific rollback command was supplied in the reviewed evidence.

## Limitations and follow-up

- CI, mergeability, release, and deployment remain pending.
- The candidate branch is behind the current upstream main line; the existing PR identity/CI gate must determine whether a rebase is required.
- Full-suite validation is not represented by the focused 84-test result because optional provider dependencies were unavailable in the QA environment.
- The next gate is PR identity/CI validation against the relevant immutable head. If rebase is required, create a remediation and run fresh combined QA rather than replacing this candidate silently.

## Exact next action

Run the existing PR identity/CI gate task `t_7a647365` against the immutable candidate and this documentation commit. Keep release and deployment blocked until CI/rebase/merge gates pass and the resulting released SHA is explicitly identified.
