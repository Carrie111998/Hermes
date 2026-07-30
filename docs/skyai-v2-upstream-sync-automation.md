# SkyAI v2 Upstream-Sync Automation

## Purpose

SkyAI follows `NousResearch/hermes-agent` while keeping every SkyAI
customization in the customer plugin/skills/docs/scripts/tests edge layer.

The automated rail runs every three hours. It is mechanical and fail-closed:
it may fetch Git refs, build and test an isolated candidate, and create or
update one fork-only pull request. It may not merge that pull request, change
the canonical SkyAI branch, deploy a runtime, or touch frontend/PBX state.

## Canonical Refs

- source branch: `codex/skyai-v2-hermes-plugin-bootstrap`
- upstream: `origin/main` (`NousResearch/hermes-agent`)
- fork: `fork` (`lomliev/hermes-agent`)
- rolling candidate:
  `codex/skyai-v2-upstream-sync-auto`

## Routine

The deterministic entry point is:

```bash
python3 scripts/skyai_v2_upstream_sync_routine.py
```

Default mode is a dry run. It fetches the source/upstream refs, reports
ahead/behind state, and says whether a candidate is required.

The scheduled candidate mode is:

```bash
python3 scripts/skyai_v2_upstream_sync_routine.py --execute --push-pr
```

Candidate mode:

1. Acquires a non-blocking local lock.
2. Refuses to run when the canonical worktree is dirty.
3. Fetches the canonical fork branch and public upstream main.
4. Creates an automation-owned isolated worktree.
5. Merges current canonical SkyAI and current upstream without force-push.
6. Stops on every unknown merge conflict.
7. Runs the boundary guard and the fixed SkyAI, voice, architecture, schema,
   bootstrap, comparison-helper, and automation tests.
8. Runs `git diff --check`.
9. Pushes only the rolling candidate branch.
10. Creates or reuses a fork-only pull request targeting the canonical SkyAI
    source branch.
11. Removes the isolated worktree and writes a private structured report.

Reports live under:

```text
~/.hermes/state/skyai-v2-upstream-sync/
```

They contain commit ids and verification outcomes, never credentials or
customer data. Repeated identical results are marked as duplicates.

## Daily Discord Summary

A separate daily reporting job reads the previous 24 hours of private
structured reports and sends one bounded Bulgarian summary to the explicitly
configured internal Discord target. It uses:

```bash
python3 scripts/skyai_v2_upstream_sync_daily_report.py
```

The formatter is deterministic and transport-agnostic. It never reads a
Discord token, customer data, logs, source files, or model output. Delivery is
performed by the existing `hermes send` CLI, so the bot credential remains in
the normal Hermes secret store and is never copied into source, scheduler
prompts, reports, or command output.

The daily summary includes the aggregate PASS/PARTIAL/BLOCKED state, run count,
latest source/upstream commits, ahead/behind counts, the latest complete
verification result, blockers, and a candidate PR URL when present. Missing
reports are shown as `NO DATA` instead of being misreported as a sync failure.

Daily reporting has no authority to run the sync, create a candidate, merge,
deploy, change runtime state, or retry failed delivery.

## PASS / PARTIAL / BLOCKED

- `PASS / up_to_date`: canonical SkyAI already contains current upstream.
- `PASS / candidate_verified`: a local candidate passed all checks.
- `PASS / candidate_pr_ready`: a verified fork-only candidate PR is ready.
- `PARTIAL / candidate_required`: dry-run found new upstream commits.
- `BLOCKED`: dirty source, missing refs/tools, merge conflicts, failing checks,
  concurrent run, or another unproven operational state.

`BLOCKED` must never be converted to a guessed merge resolution.

## Scheduler Contract

The scheduler runs on the always-on Muncho operational host, independently of
an operator workstation. Every three hours it invokes this exact deterministic
candidate rail. A separate daily reporting service reads only sanitized
structured results and sends one bounded report through the existing Hermes
transport to the configured internal Discord channel.

The sync service has no model/provider or Discord credential access. The
reporting service has no GitHub credential access. Neither service creates
Codex tasks/threads. Delivery is attempted once; a failed delivery is recorded
without a retry loop, and the next normal daily run remains eligible.

The scheduler is not authority for:

- merging into `codex/skyai-v2-hermes-plugin-bootstrap`;
- DEV or PROD deployment;
- GCP runtime changes;
- frontend changes;
- PBX/SIP/RTP/DID/queue/IVR changes;
- force-push;
- public-upstream PRs or pushes.

## Recovery And Rollback

- Stop or pause the scheduler.
- Remove only the automation-owned candidate worktree through
  `git worktree remove`.
- Close the automation-owned fork PR if it is no longer wanted.
- Delete the rolling remote candidate branch only after the PR is closed.
- The canonical SkyAI source branch and every runtime remain unchanged unless
  a separately reviewed integration or deployment gate is approved.

## Future Auto-Integration Gate

Auto-integration is disabled. Enabling it requires a separate owner decision
and must, at minimum, prove:

- no merge conflicts;
- canonical source unchanged since candidate creation;
- all fixed verification checks green;
- GitHub merge state clean;
- exact candidate SHA verification;
- no core/boundary drift;
- no deployment side effect.

Deployment remains a separate explicit gate even if auto-integration is ever
enabled.
