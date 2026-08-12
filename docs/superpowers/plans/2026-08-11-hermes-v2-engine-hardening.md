# Hermes v2 Engine Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the reviewed Hermes v2 workflow, repository, intake, Epic-integration, and human-release corrections as independently testable changes.

**Architecture:** This is the sequencing plan for four subsystem plans. The first production slice changes only outcome interpretation and immutable Test/Review authority; repository pinning, intake recovery, and Epic integration remain separate review gates so a failure can be bisected to one contract.

**Tech Stack:** Python 3, SQLite/WAL transactions, Git CLI, FastAPI, dashboard JavaScript, GitHub Actions read-only APIs, pytest through `scripts/run_tests.sh`.

## Global Constraints

- Engine source execution requires a Hermes named assignment or Ole's explicit direct-execution override; this document grants neither.
- Never call `git push` from engine code, tests, fixtures, migrations, or workers.
- Use `scripts/run_tests.sh`; never invoke `pytest` directly.
- Tests assert behavior through imports and public calls; no test may read source text.
- Canonical `metadata.workflow_outcome` is the only lifecycle authority.
- An integration intent is a request, not evidence; claim-time authority is independently re-derived from ended runs.
- Development keeps its existing commit-first handoff gate.
- `_release_run_evidence` remains an independent release backstop.
- Configuration and infrastructure failures never consume product rework budget.
- Epic-member commits and integrations require no human approval; only the final pinned Epic merge/push is Ole's action.
- The first CI-failure behavior is observation plus a passing rerun for the same SHA; automated revert and forward repair are excluded.
- If CI remains failed, Hermes retains the snapshot and takes no Git or work-item action; recovery is a human-operated external procedure until Ole chooses and separately authorizes a revert or forward-repair design.
- Spec 5 external-boundary assurance is excluded until the workflow contracts produced by Specs 1, 2, and 4 exist in production code.

---

## Plan Set

| Order | Plan | Independent result | Dependency |
|---|---|---|---|
| 1 | [Workflow authority](2026-08-11-hermes-v2-workflow-authority.md) | Fail-closed outcome envelope, latest-run authority, no-op guard, directives, narrow recovery | None |
| 2 | [Repository correctness](2026-08-11-hermes-v2-repository-correctness.md) | Board-owned Git refs/commands, refresh, pinned evidence phases | Workflow authority types |
| 2 (parallel) | [Intake reliability](2026-08-11-hermes-v2-intake-reliability.md) | Typed verifier paths, safe reporting, bounded retry/dedup | None |
| 3 | [Epic integration and release](2026-08-11-hermes-v2-epic-integration-release.md) | Automatic member integration, snapshots, human final release, CI observation, guarded migration | Workflow authority + repository correctness |

## Delivery Gates

### Gate A — Critical correctness slice

- [ ] Land only Tasks 1–4 of the workflow-authority plan: production fixtures 304/354/369/407/410, canonical outcome envelope, Resolver-path preservation, latest terminal Test/Review authority, and candidate eligibility.
- [ ] Run `scripts/run_tests.sh tests/hermes_cli/test_kanban_product_outcomes.py tests/hermes_cli/test_kanban_db.py -q`.
- [ ] Confirm runs 304/407 reject without mutation, runs 354/369 retain the privileged non-verdict `preflight_repaired` behavior, and run 410 records `serialized_parameter_leak` while advancing normally.
- [ ] Commit this slice before the recovery verb or `_commit_worker_diff` change.

### Gate B — Repository and intake contracts

- [ ] Execute the repository and intake plans as separate branches or serialized commits; neither may share a schema migration commit with the other.
- [ ] Keep the Test/Review `_commit_worker_diff` removal in its own commit after pinning, generated-path policy, worker guidance, and migration notes are green.
- [ ] Run the exact per-plan suites and inspect the diff for any remote-write primitive.

### Gate C — Epic coordinator

- [ ] Execute the Epic plan only after the authority and repository interfaces are on the branch.
- [ ] Run migration against a scratch copy of a production-shaped database and scratch Git remotes before any live board is considered.
- [ ] Route affected `release_measure` Epic members through refresh → Test → Review; never use historical approval to enqueue integration.
- [ ] Review every latest-run canonical-outcome gap. Nonterminal members require fresh evidence; done members may retain only an exact, Git-validated pre-existing integration fact under the documented grandfather rule. Redundant-only approval never creates authority or a fact.
- [ ] Verify member integration completes without an approval action and final release still stops at a pinned, human-operated handoff.

### Gate D — Full verification

- [ ] Run:

```bash
scripts/run_tests.sh \
  tests/hermes_cli/test_kanban_product_outcomes.py \
  tests/hermes_cli/test_kanban_repository.py \
  tests/hermes_cli/test_kanban_intake.py \
  tests/hermes_cli/test_kanban_story_integration.py \
  tests/hermes_cli/test_kanban_epic_release.py \
  tests/hermes_cli/test_kanban_db.py \
  tests/hermes_cli/test_kanban_release_evidence.py \
  tests/hermes_cli/test_kanban_release_cli.py \
  tests/hermes_cli/test_kanban_epics.py \
  tests/plugins/test_kanban_dashboard_plugin.py \
  tests/e2e/test_kanban_product_recovery_flow.py -q
```

- [ ] Run `scripts/run_tests.sh` for CI parity.
- [ ] Search the implementation diff with `git diff --check` and `rg -n "git push|push --force|update-ref.*refs/remotes" hermes_cli plugins/kanban tests/hermes_cli tests/e2e`; every match must be a refusal assertion or test fixture, never an executable engine path.
- [ ] Review the scratch migration report, retained histories, repository-contract digests, and immutable release snapshot before proposing live enablement.

### Gate E — Board compatibility before enabling fail-closed

- [ ] Verify the Resolver exemption is structural: ordinary `complete_task`
  rejects missing canonical authority even when metadata claims
  `preflight_repaired`, `preflight_resolved`, or `preflight_escalated`. No
  persisted-outcome allowlist exists in the completion kernel.
- [ ] On the target board, measure the 50 most recent verdict-bearing Test/Review runs with the exact persisted-outcome classification used by the migration:

```sql
WITH recent AS (
  SELECT id, json_type(metadata, '$.workflow_outcome') AS canonical
    FROM task_runs
   WHERE status = 'completed'
     AND ended_at IS NOT NULL
     AND step_key IN ('test', 'review')
     AND outcome IN ('advanced', 'rework_requested')
   ORDER BY id DESC
   LIMIT 50
)
SELECT COUNT(*) AS verdict_bearing_runs,
       SUM(canonical IS NULL) AS canonical_absent,
       ROUND(100.0 * SUM(canonical IS NULL) / COUNT(*), 1) AS absent_percent,
       MIN(id) AS oldest_run_id,
       MAX(id) AS newest_run_id
  FROM recent;
```

- [ ] If `canonical_absent / verdict_bearing_runs` exceeds 5%, stop enablement and investigate every absent run. Do not broaden the Resolver exemption or synthesize authority from redundant fields to make the percentage pass.
- [ ] Record the baseline in the migration report. The verified TTC baseline is 89 completed Test/Review runs overall with 19 canonical-absent rows; six are privileged non-verdict preflight finalizations. For run IDs ≥300, four of 46 are absent: runs 354/369 are legitimate Resolver repairs and runs 304/407 are the two malformed verdict-bearing completions, so the applicable rate is 2/44 (4.5%). The rolling 50 verdict-bearing query above reports 2/50 (4.0%).

## Commit Order

1. `test: capture production workflow outcome envelopes`
2. `fix: validate canonical product outcomes before mutation`
3. `fix: derive latest immutable workflow authority`
4. `fix: reject empty integration candidates`
5. `feat: persist product rework directives`
6. `feat: add narrow terminal-state recovery`
7. Repository-plan commits in their documented order, with evidence-phase commit removal isolated.
8. Intake-plan commits in their documented order.
9. Epic-plan commits in their documented order.

Do not squash across these numbered boundaries: each boundary represents a distinct rollback and review decision.
