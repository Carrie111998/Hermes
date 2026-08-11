# Hermes v2 Epic Engine Hardening — Final Design for Review

**Date:** 2026-08-11

**Status:** Proposed; verified against current source and production evidence; implementation not authorized by this document

**Decision owner:** Ole

**Source baseline:** `e6052da58` (`main == origin/main`)

**Production evidence:** The Trading Company Epic `t_a0c4e8c9`

## Decision

Hermes v2 will make Epic-member work autonomous through verified local
integration while preserving Ole's exclusive authority over the final Epic
merge and push.

The member lifecycle becomes:

```text
Architecture → Development → Test → Review → integration_pending
             ↖──────────── structured rework ────────────┘
integration_pending → verified Epic integration → Done
```

The Epic lifecycle becomes:

```text
collecting_members
  → aggregate_verification
  → awaiting_final_release
  → Ole merges and pushes the pinned candidate
  → ci_pending
  → Done | ci_failed
```

`release_measure` remains unchanged for standalone product cards. It is
removed only from Epic-member stories. Hermes never pushes a Git remote.

The work is divided into five independently reviewable specifications. The
required landing order is:

```text
Spec 1 → Spec 2 → Spec 4 → Spec 5
Spec 3 is independent and may land after Spec 1 or Spec 2.
```

Spec 4 must not ship before Spec 1. Today the human story gate accidentally
stops a missing Review verdict. Removing that gate first would turn the
current fail-open deadlock into silent integration.

## Authority and scope

This is a design document only. It authorizes no engine edit, board mutation,
process restart, release, merge, push, or Work Inbox submission.

In scope:

- Hermes v2 product boards using Epic membership.
- Test/Review outcome integrity and durable rework instructions.
- repository base selection, verification, and branch refresh;
- Work Contract failure diagnostics and intake recovery;
- automatic local story-to-Epic integration;
- explicit final-Epic release and CI observation;
- external-boundary evidence produced by Test and inspected by Review;
- migration of existing v2 boards and in-flight Epic-member cards.

Out of scope:

- changing standalone-card release policy;
- allowing Hermes or a worker to push;
- automatic PreProd or production deployment;
- weakening provider separation, rework ceilings, resolver refusal, Work
  Contract authority, or release evidence;
- creating a reconcile/deploy product card for a completed Epic;
- inferring a missing verdict from prose or redundant metadata;
- deleting or rewriting historical runs, events, approvals, or integration
  facts.

## Verified current-state facts

The statements in this section are observations, not design assumptions.

| Fact | Current evidence |
|---|---|
| Review normally advances to story-level `release_measure`. | `PRODUCT_WORKFLOW_TRANSITIONS` maps `review → release_measure` in `hermes_cli/kanban_db.py:171-178`. |
| Test/Review outcome is optional at the tool boundary. | `KANBAN_COMPLETE_SCHEMA` defines `workflow_outcome`, but the request's required list does not require it, in `tools/kanban_tools.py:2790-2895`. `_handle_complete` copies it only when supplied at `tools/kanban_tools.py:1483-1509`. |
| Missing outcome fails open. | `_route_product_rework_if_requested` returns `None` when `metadata.workflow_outcome` is absent at `hermes_cli/kanban_db.py:10062-10074`; `complete_task` then proceeds to `handoff` at `:10195-10368`. |
| Run 407 is the observed malformed call, not a hypothetical shape. | The read-only production row has `step_key=review`, `outcome=advanced`, no `metadata.workflow_outcome`, root and reviewer verdicts `changes_requested`, and the literal marker `<parameter name="workflow_outcome">` embedded in `summary`. |
| Release defense-in-depth would have refused run 407. | `_release_run_evidence` requires canonical approved Review evidence at `hermes_cli/kanban_db.py:13993-14066`. The failure would have been a misleading deadlock, not a bad merge. |
| An old approval can survive a later rejection of the same SHA. | `_latest_approved_review_candidate` scans backward until it finds any approval at `hermes_cli/kanban_db.py:13972-13990`; it does not stop at the latest terminal Review. |
| Development already has a no-commit gate on current `main`. | `handoff` calls `_commit_worker_diff` and refuses Development when no commit SHA exists at `hermes_cli/kanban_db.py:14884-14978`. Tests cover the no-diff return at `tests/hermes_cli/test_kanban_db.py:4002`. This protection must be preserved, not reimplemented. |
| Integration can still record a no-op/ancestor result. | `integrate_story_to_epic` records `already_integrated` before candidate verification when the story branch is an ancestor of the Epic branch, and `_record_story_integration` inserts the row in `hermes_cli/kanban_db.py:13482-13769`. Production contains the original row for `t_438ed30d` with identical source and candidate prefix `2d9dc9a28adc`; its later repaired integration is a second row. |
| Integration idempotency already has a useful key. | `epic_story_integrations` has primary key `(epic_id, story_id, source_sha)` at `hermes_cli/kanban_db.py:4326-4333`. It has no surrogate row ID. |
| Story integration currently occurs after `done`. | `reconcile` scans `status='done'` stories and then calls `integrate_story_to_epic` at `hermes_cli/kanban_db.py:15443-15655`. |
| Verification is repository-specific but currently hard-coded. | `_build_verified_merge_candidate` and `_default_epic_verify` require `scripts/run_tests.sh` at `hermes_cli/kanban_db.py:12963-13183`; absence becomes generic verification failure. The Trading Company has no such file and declares `python -m unittest discover -s tests -v` plus a cockpit build in CI. |
| A fresh Epic base can come from ambient checkout state. | `_epic_base_start_point` falls back to `_git_head_sha(repo_root)` at `hermes_cli/kanban_db.py:12548-12570`. |
| Existing story worktrees are reused without refresh. | `_resolve_worktree_workspace` provisions dependencies and returns an existing same-branch worktree at `hermes_cli/kanban_db.py:12721-12822`; it does not bring in the current Epic tip. |
| Review SHA pinning already exists. | `_prepare_review_target` records `review_base_sha` and `review_head_sha` on the active run at `hermes_cli/kanban_db.py:15130-15198`. |
| Evidence phases can currently create a source commit. | `handoff` calls `_commit_worker_diff` before checking whether the phase is source-producing at `hermes_cli/kanban_db.py:14950-14978`. Development requires a SHA, but Test/Review can still commit a dirty worktree. |
| Rework findings are durable but not first-class worker input. | Rework stores findings in the ended run and event at `hermes_cli/kanban_db.py:10142-10183`; `build_worker_context` shows them only inside bounded prior-attempt metadata/comments at `:18550-18830`, while resolver work gets a prominent `Required resolver action` block. |
| Work Contract verification collapses distinct failures. | `verify_work_contract` returns `bool` and catches `OSError`, `TypeError`, `ValueError`, and `WorkContractError` together at `hermes_cli/kanban_intake.py:288-321`; the caller reports only `Work Contract signature is invalid` at `:343-347`. |
| Intake recovery and retry already exist, but the affordance is incomplete. | `recover_stale_qualification_intakes` scans only running attempts at `hermes_cli/kanban_db.py:7030-7144`. `retry_qualification_intake` exists at `:7273-7301`, and the Work Inbox API accepts `kind=retry` at `plugins/kanban/dashboard/plugin_api.py:579-719`; the status response at `:821-877` does not advertise that action. |
| Requalification active-state deduplication is incomplete. | `existing_requalification_intake` queries only `pending` and `rejected` at `hermes_cli/kanban_intake.py:444-467`; it ignores `running`, `needs_clarification`, and `attention_required`. |
| Epics are intentionally unclaimable but can appear `ready`. | `claim_task` refuses `work_item_kind='epic'` at `hermes_cli/kanban_db.py:9290-9360`. Production Epic `t_a0c4e8c9` remains `status=ready`, with no phase or assignee. |
| Current Epic readiness trusts member status and re-runs an unpinned suite. | `epic_ready` requires every child `status == 'done'` and returns a fresh boolean verification result at `hermes_cli/kanban_db.py:13205-13252`. |
| Current Epic progress is not an integration/release ledger. | `epic_progress` counts member `done` statuses and reports only `pending`, `merged`, or `released` at `hermes_cli/kanban_db.py:7587-7615`. |
| Current Epic release incorrectly starts through story evidence. | `release_product_task` derives `_latest_approved_review_candidate` and `_release_run_evidence` before its Epic branch at `hermes_cli/kanban_db.py:14236-14378`; a legacy Epic has neither. |
| Hermes's local-only boundary is already explicit. | `merge_epic_to_main` states and implements no remote push at `hermes_cli/kanban_db.py:13283-13406`. That boundary remains load-bearing. |
| The Trading Company push boundary is real. | `.github/workflows/ci.yml` and `.github/workflows/deploy-test.yml` both trigger on `main` push. Deploy Test runs tests/build, publishes a commit-tagged image, deploys Test, and checks health plus the exact `${{ github.sha }}`. `deploy-preprod.yml` remains manual. |

## Cross-cutting invariants

These invariants apply to all five specifications.

1. **A missing terminal-phase result is an error, never success.** Test and
   Review advance only from a positively present canonical outcome.
2. **The latest terminal Review is authoritative.** A candidate is approved
   only when the latest ended Review run approved that exact branch and SHA.
   Older approvals remain history but confer no authority.
   The analogous Test authority is the latest ended Test run for that source
   lineage, and it must be `passed`.
3. **Board position is not evidence.** Phase/status selects the next action;
   immutable runs, integration rows, verification runs, and release snapshots
   authorize it.
4. **No empty contribution becomes an integration fact.** A normal Epic
   member must have a non-empty diff between its pinned Review base and source
   SHA. Work already satisfied elsewhere requires an explicit governed
   no-change disposition; it does not fabricate an integration row.
5. **Git and SQLite use a recoverable two-phase boundary.** A DB transaction
   records intent before Git changes. A retained candidate and pinned SHAs let
   replay finish the DB record after a crash.
   An integration intent is a request, not evidence; the integrator derives
   authority again from terminal run data when it claims the intent.
6. **Story `done` means integrated.** An Epic member reaches `done` only after
   its exact `(epic_id, story_id, source_sha)` integration row is durable, or
   after a separate governance-authored no-change disposition.
7. **Epic readiness reads facts, not claims.** It reads the complete member
   integration set and a green aggregate verification bound to the exact Epic
   and release-candidate SHAs.
8. **Evidence follows the candidate.** Refresh, rework, conflict resolution,
   or any source-SHA change requires new Test and Review evidence.
   Test and Review are evidence-only phases; they cannot change the source
   branch they attest to.
9. **Human authority is at the Epic boundary.** No Epic member waits for Ole
   to approve a commit or local story integration. Ole alone mutates local
   `main` for the final candidate and pushes it.
10. **Hermes never pushes.** Neither workers nor engine adapters receive a
    remote-write capability.
11. **History is append-only.** Rejections, superseded approvals, failed
    integration attempts, and failed CI runs remain visible.
12. **Configuration failure is not test failure.** Missing commands, refs, CI
    policies, or adapters produce typed configuration errors.

## Module boundaries

The design uses four deep modules. Their interfaces are narrow; parsing,
normalization, Git orchestration, leases, and recovery remain hidden behind
them.

### 1. Product outcome kernel

Interface:

```python
validate_terminal_outcome(
    *, task_id, run_id, phase, summary, result, metadata
) -> PositiveOutcome | ReworkOutcome

latest_review_authority(*, task_id) -> ApprovedCandidate | None

latest_test_authority(*, task_id, source_sha) -> PassedTest | None

active_rework_directive(*, task_id) -> ReworkDirective | None
```

This module owns canonical outcome presence, exact schema, malformed tool
serialization detection, redundant-verdict consistency, latest-Review
selection, and first-class rework directives. Callers do not inspect raw
metadata to decide lifecycle transitions.

### 2. Repository policy and candidate service

Interface:

```python
load_repository_contract(board_metadata) -> RepositoryContract
refresh_story_branch(refresh_request) -> RefreshResult
prepare_integration_candidate(integration_request) -> PreparedCandidate
run_verification(profile, candidate_path, candidate_sha) -> VerificationRun
```

This module owns configured refs and commands, isolated worktrees, clean-tree
checks, candidate refs, command execution, output bounds, and Git error
classification. It never changes task lifecycle state and never pushes.

### 3. Story integration coordinator

Interface:

```python
enqueue_approved_story(conn, approved_candidate) -> IntegrationIntent
claim_next_intent(conn, owner, lease) -> IntegrationIntent | None
finish_intent(conn, intent, prepared_candidate) -> IntegrationFact
recover_expired_intents(conn) -> RecoveryCounts
```

This module owns durable intent, claim/lease, idempotency, crash recovery, and
the transition from `integration_pending` to `done`. It calls the repository
service outside DB write transactions.

### 4. Epic release coordinator

Interface:

```python
evaluate_epic_readiness(conn, epic_id) -> ReadinessResult
prepare_release_snapshot(conn, epic_id) -> EpicReleaseSnapshot
observe_release(snapshot, ci_adapter) -> ReleaseObservation
```

This module owns the pinned member set, aggregate verification, final release
candidate, human handoff, remote/CI observation, and Epic terminal state. Its
CI adapter is read-only.

The existing `_release_run_evidence` remains an independent defense for
standalone cards and any story path that still reaches release. It must not be
simplified into the outcome kernel or removed as “redundant.” It is the guard
that refused run 407 after routing had already failed.

## Data model

### Product rework directives

Add `product_rework_directives`:

```text
id                    INTEGER PRIMARY KEY
task_id               TEXT NOT NULL
origin_kind           test | review | integration | refresh
origin_run_id         INTEGER
origin_intent_key     TEXT
origin_phase          TEXT NOT NULL
target_phase          TEXT NOT NULL  -- architecture | development
rejected_branch       TEXT
rejected_sha          TEXT
epic_tip_sha          TEXT
findings_json         TEXT NOT NULL
status                active | resolved | superseded
created_at            INTEGER NOT NULL
resolved_by_run_id    INTEGER
```

At most one directive is active per task. Creating a new directive supersedes
the old one in the same transaction but never deletes it. `build_worker_context`
renders the active directive as `## Required rework directive` before prior
attempts, including the origin, rejected SHA, target phase, and findings.

A Development handoff resolves the directive only if its new SHA differs from
the directive's rejected SHA. Architecture work may refine the plan, but an
`architecture_invalid` directive remains active until a subsequent
Development handoff produces a new candidate.

### Story integration intents

Add `story_integration_intents` with the same durable identity as the existing
integration fact:

```text
epic_id               TEXT NOT NULL
story_id              TEXT NOT NULL
source_sha            TEXT NOT NULL
source_branch         TEXT NOT NULL
review_run_id         INTEGER NOT NULL
review_base_sha       TEXT NOT NULL
status                pending | running | prepared | rework_required |
                      attention_required | integrated | superseded
claim_lock            TEXT
claim_expires         INTEGER
attempt_count         INTEGER NOT NULL DEFAULT 0
target_pre_sha        TEXT
candidate_sha         TEXT
candidate_ref         TEXT
verification_run_id   INTEGER
last_failure_code     TEXT
created_at            INTEGER NOT NULL
updated_at            INTEGER NOT NULL
PRIMARY KEY (epic_id, story_id, source_sha)
```

The existing `epic_story_integrations` table remains the successful-fact
ledger and keeps primary key `(epic_id, story_id, source_sha)`. It gains no
surrogate ID. Wherever a release snapshot refers to an integration “row ID,”
it stores that composite key plus `candidate_sha`.

Before changing the Epic ref, the integrator persists `target_pre_sha`,
`candidate_sha`, `candidate_ref`, and the green `verification_run_id` with
status `prepared`. It then fast-forwards the Epic ref with a compare-and-swap.
Afterward it inserts the integration fact and marks the intent integrated in
one DB transaction.

Crash recovery is deterministic:

- If the Epic ref is still `target_pre_sha`, apply the prepared candidate.
- If the Epic ref equals `candidate_sha`, finish the missing DB record.
- If `candidate_sha` is an ancestor of a later Epic tip, finish the DB record
  using the pinned candidate SHA, not the later tip.
- Otherwise mark target movement and rebuild from the current Epic tip while
  retaining the same reviewed source SHA.

### Verification runs

Add `repository_verification_runs`:

```text
id                    INTEGER PRIMARY KEY
scope                 story_integration | epic_release
subject_id            TEXT NOT NULL
source_sha            TEXT NOT NULL
candidate_sha         TEXT NOT NULL
contract_digest       TEXT NOT NULL
profile               TEXT NOT NULL
status                running | passed | failed | configuration_error |
                      infrastructure_error
started_at            INTEGER NOT NULL
ended_at              INTEGER
step_results_json     TEXT
```

Output stored in `step_results_json` is redacted and capped. Exit codes,
durations, and bounded tails are retained; environment values and credentials
are not.

### Epic release snapshots

Add `epic_release_snapshots` and `epic_release_members`:

```text
epic_release_snapshots
  id                       INTEGER PRIMARY KEY
  epic_id                  TEXT NOT NULL
  epic_tip_sha             TEXT NOT NULL
  target_branch            TEXT NOT NULL
  target_pre_sha           TEXT NOT NULL
  release_candidate_sha    TEXT NOT NULL
  candidate_ref            TEXT NOT NULL
  aggregate_verification_id INTEGER NOT NULL
  repository_contract_digest TEXT NOT NULL
  status                   awaiting_push | ci_pending | ci_failed |
                           released | invalidated
  pushed_sha               TEXT
  created_at               INTEGER NOT NULL
  updated_at               INTEGER NOT NULL

epic_release_members
  snapshot_id              INTEGER NOT NULL
  epic_id                  TEXT NOT NULL
  story_id                 TEXT NOT NULL
  source_sha               TEXT NOT NULL
  candidate_sha            TEXT NOT NULL
  integrated_at            INTEGER NOT NULL
  PRIMARY KEY (snapshot_id, story_id)
```

Only one non-invalidated snapshot may exist per Epic. Any change to the member
set, an integration key, Epic tip, target pre-SHA, repository contract digest,
release candidate, or a member leaving terminal status invalidates the
snapshot and requires aggregate verification again.

### Epic workflow state

Introduce a typed `product_epic` template rather than storing ad hoc phases on
a task whose workflow template is null:

```text
collecting_members       generic status: todo
aggregate_verification  generic status: review
awaiting_final_release  generic status: review
ci_pending              generic status: review
ci_failed               generic status: blocked
done                    generic status: done
```

Epics remain unclaimable through the ordinary worker claim path. The
dashboard uses `current_step_key` and `work_item_kind` to show the precise
state instead of presenting all unassigned Epics as dispatchable `ready`.

Epic-member `integration_pending` uses generic status `review`, has no worker
assignee, and is consumed only by the integration coordinator. `claim_task`,
manual `complete`, Work Inbox delivery, generic unblock/promote, and generic
move APIs must refuse engine-owned member and Epic phases. Only their owning
coordinator may transition them.

## Repository contract

Board metadata gains a validated, operator-owned repository contract:

```json
{
  "repository": {
    "base_ref": "refs/remotes/origin/main",
    "target_branch": "main",
    "verification_profiles": {
      "story_integration": [
        {
          "id": "python-tests",
          "argv": ["python", "-m", "unittest", "discover", "-s", "tests", "-v"],
          "workdir": ".",
          "timeout_seconds": 1800
        }
      ],
      "epic_release": [
        {
          "id": "python-tests",
          "argv": ["python", "-m", "unittest", "discover", "-s", "tests", "-v"],
          "workdir": ".",
          "timeout_seconds": 1800
        },
        {
          "id": "cockpit-install",
          "argv": ["npm", "ci"],
          "workdir": "cockpit",
          "timeout_seconds": 1800
        },
        {
          "id": "cockpit-build",
          "argv": ["npm", "run", "build"],
          "workdir": "cockpit",
          "timeout_seconds": 1800
        }
      ]
    },
    "ci_observation": {
      "provider": "github_actions",
      "required_workflows": ["CI", "Deploy Test"]
    },
    "boundary_evidence": {
      "test_globs": ["tests/**"],
      "fixture_globs": ["tests/fixtures/**", "**/*fake*.py", "**/*stub*.py"],
      "generated_paths": ["dashboard/index.html", "dashboard/data.json"]
    }
  }
}
```

The example matches the commands currently declared by The Trading Company;
it is not a universal default. Every board must declare its own commands.

Rules:

- Commands are argument arrays, never shell strings.
- `workdir` must resolve inside the candidate worktree.
- The engine passes a minimal environment and no unrelated credentials.
- `base_ref` and `target_branch` are explicit. The engine resolves and pins a
  full SHA; it never substitutes ambient `HEAD`.
- A missing ref, profile, command, executable, or CI adapter is
  `configuration_error`, not `failed`.
- There is no implicit fallback to `scripts/run_tests.sh` after migration.
- The normalized contract is hashed. Every verification run and release
  snapshot stores that digest.
- `boundary_evidence.generated_paths` is an explicit allowlist of tracked
  files that verification may regenerate. At Test completion the engine may
  restore only those declared paths to the pinned source SHA after recording
  their mutation; any other tracked mutation fails closed. The allowlist is
  validated to resolve inside the repository and to match tracked files.
- Network fetch is not implicit. If a board needs a read-only fetch before
  base resolution, that must be a separately configured read capability. A
  locally stale remote-tracking ref is visible in the pinned base evidence.

## Spec 1 — Workflow correctness (P0)

### Outcome envelope

For ordinary verdict-bearing Test and Review completion through
`complete_task`, `workflow_outcome` is required at the kernel interface even
if a caller bypasses the model-facing JSON schema.

Privileged Resolver finalization is a separate, non-verdict boundary.
`resolve_product_preflight` remains outside the kernel and may close a Test or
Review run as `preflight_repaired`, `preflight_resolved`, or
`preflight_escalated` without
`workflow_outcome`. Resolver is pinned to `resolver_readonly`, exposes
`kanban_resolve`, and cannot call `kanban_complete`; the implementation must
not move outcome validation into `_end_run`, `handoff`, or another generic run
finalizer that would collapse these entrypoints.

The separation is structural, not a persisted-outcome allowlist.
`complete_task` never accepts or inspects a caller-claimed run outcome to skip
the kernel; only `resolve_product_preflight` writes the three privileged
outcomes through its separate path. Therefore an ordinary completion carrying
`preflight_repaired`, `preflight_resolved`, or `preflight_escalated` in
free-form metadata still requires canonical `workflow_outcome`.

Accepted canonical shapes remain:

```text
Test positive:       {"verdict":"passed"}
Review positive:     {"verdict":"approved"}
Test/Review rework:  {"verdict":"changes_requested",
                      "target_step":"development",
                      "findings":[non-empty strings...]}
Review architecture: {"verdict":"architecture_invalid",
                      "target_step":"architecture",
                      "findings":[non-empty strings...]}
```

For ordinary `complete_task`, the validator runs before provenance mutation,
run closure, handoff, rework routing, or task-state mutation. Resolver repair
continues through its existing independently-authorized CAS path.

It rejects:

- absence or non-object values;
- fields outside the exact phase-specific schema;
- verdicts invalid for the active phase;
- empty findings on rework;
- contradictory redundant verdicts.

Serialized parameter markup is an observation, not an independent verdict.
When canonical `workflow_outcome` is present, the exact marker
`<parameter name="workflow_outcome">` or paired
`</summary>...<parameter name=` shape emits `serialized_parameter_leak` and
completion continues. When the canonical outcome is absent and the marker is
present, completion fails for `missing` qualified by
`serialized_parameter`; the engine never parses the leaked JSON as authority.

Canonical `workflow_outcome` is the only authority. Redundant fields may
remain for compatibility but cannot disagree:

- root `metadata.verdict` and `ai_provenance.reviewer.verdict` must equal the
  canonical Review verdict when present;
- Test `ai_provenance.tester.result` must be `passed` for canonical `passed`,
  and may be `failed` or `changes_requested` for canonical
  `changes_requested`;
- absence of an optional redundant field is allowed; it is never used to
  synthesize the canonical outcome.

Rejected completion emits `completion_rejected_outcome` with task ID, run ID,
phase, a safe reason code, and the optional `serialized_parameter` qualifier.
The active run and task remain in the current phase so the worker can retry
the tool call. If the worker exits without a valid retry, the existing worker
watcher closes the run through its normal protocol-failure path. The engine
does not advance, route, or infer intent.

### Latest Review authority

`latest_review_authority` examines exactly the latest ended Review run:

- If none exists, return no authority.
- If its canonical outcome is not `approved`, return no authority.
- If reviewer identity, independent-provider evidence, branch, or full SHA is
  absent, return no authority.
- Otherwise return that exact branch/SHA/run tuple.

It never scans backward for an older approval. `_release_run_evidence`
independently checks the same exact candidate on the release path.

The dispatcher also stamps `review_branch` beside the existing
`review_base_sha` and `review_head_sha`; worker-authored branch aliases are not
release authority. `latest_test_authority` applies the same latest-terminal
rule to Test for the exact source SHA, so a later Test rejection cannot fall
back to an older pass.

Before Test starts, the dispatcher stamps `test_branch` and `test_head_sha` on
the run. Test completion requires the branch to remain at that SHA and the
tracked worktree/index to remain clean. Review must start at the same tested
SHA. Test and Review no longer call the source-commit helper; if either phase
needs a source or fixture edit, it returns a Development rework directive.

### Rework delivery

Test/Review rejection atomically closes the run, creates the durable rework
directive, routes to its declared phase, and preserves the rework ceiling.
The next Architecture/Development worker receives the directive before prior
attempt history. This replaces reliance on a comment or a prior-run metadata
entry surviving prompt bounds.

### Candidate eligibility

Before any new integration row can be written:

- use the Review run's pinned `review_base_sha` and `review_head_sha`;
- require the reviewed source SHA to equal `review_head_sha`;
- run `git diff --quiet review_base_sha review_head_sha`;
- reject a quiet diff as `empty_contribution`;
- require the latest terminal Test to have passed and the latest terminal
  Review to have approved the exact same source SHA.

An already-integrated replay is allowed only when the exact composite
integration row already exists, or when a prepared intent proves a crash
occurred after applying its non-empty candidate. A bare ancestor relation is
not sufficient to invent a new integration fact.

### Narrow recovery verb

Add an operator-only `clear-terminal-state` verb. It may only:

- compare-and-swap an exact task ID whose expected status is `done`;
- require expected `completed_at`, current phase, latest event ID, actor, and
  non-empty reason;
- set `completed_at = NULL` and derive the non-terminal generic status from
  the task's already-stored non-terminal phase;
- append an audit event with the expected snapshot and operation.

It refuses a task whose stored phase is already terminal, because repairing
that would require changing history rather than clearing an erroneous status.
It cannot set an arbitrary phase/status, edit an integration row, remove an
event/run, change an assignee, or delete evidence.

### Acceptance tests

- Production run-304 fixture: a second card in an earlier Epic reproduces the
  missing-canonical plus serialized-marker defect and fails closed without a
  task-specific exception.
- Production run-407 fixture: missing outcome plus serialized parameter marker
  leaves the task in Review and emits a safe rejection tied to that run.
- Production run-410 fixture: canonical `approved` plus the same marker emits
  `serialized_parameter_leak` and completes normally.
- Production runs 354 and 369 remain valid non-verdict
  `preflight_repaired` Resolver finalizations; the kernel is not invoked.
- The existing `preflight_resolved` and `preflight_escalated` Resolver routes
  are likewise unchanged.
- Missing outcome without marker also fails closed.
- Canonical/redundant verdict disagreement fails closed.
- A valid rejection routes backward and the next worker sees the first-class
  directive.
- A later rejected Review of the same SHA invalidates an older approval.
- A later rejected Test of the same lineage invalidates an older pass.
- A new SHA invalidates Test and Review evidence.
- Test/Review source edits cannot be committed from an evidence phase and
  route through Development instead.
- Development with no commit remains in Development, preserving the existing
  gate.
- A new no-op integration row cannot be written.
- `_release_run_evidence` still refuses missing, non-independent, stale, or
  non-approved evidence.

The removal of `_commit_worker_diff` from Test/Review is a separate,
independently bisectable implementation commit. Production measurement before
planning found six Test handoffs with a SHA and zero Review handoffs; the Test
cases include both source/generated-file changes and artifact-only additions.
That change must therefore land with Test/Review pinning, declared generated
paths, worker guidance, and migration notes—not as part of the outcome-envelope
commit.

## Spec 2 — Repository correctness

### Deterministic Epic base

At first Epic materialization, resolve `repository.base_ref` to a full SHA and
persist it in the existing Epic base-pin event. Refuse materialization if the
configured ref is missing or ambiguous. Remove the ambient-`HEAD` fallback
from the governed Epic path; keep legacy behavior only for non-v2 workspaces.

### Dispatcher-owned refresh

Before the first Architecture dispatch and before every Development dispatch
for an Epic member:

1. Resolve and pin the current Epic tip and story head.
2. Refuse if the story worktree is dirty; preserve it and emit
   `story_refresh_attention_required` with its path and pinned SHAs.
3. If the Epic tip is already an ancestor of the story head, dispatch without
   mutation.
4. Otherwise build an isolated refresh candidate, merging the current Epic
   tip into the story head.
5. If conflict-free, compare-and-swap the story branch to the refreshed SHA,
   record lineage, invalidate prior Test/Review authority, and dispatch.
6. If conflicted, preserve a dispatcher-owned detached conflict worktree and
   route a Development rework directive containing the original story SHA,
   Epic tip SHA, conflict paths, and workspace. The worker resolves files;
   the engine, not the worker, owns the branch update and applies it by CAS.

The original story worktree is never overwritten while dirty. A refresh after
Review is not silently applied. Integration instead tests the reviewed source
against the current Epic tip in an isolated candidate. Conflict there becomes
Development rework and therefore requires new Test and Review.

### Verification behavior

All candidate verification uses the repository contract. Results distinguish:

- `passed`;
- `failed` (commands ran and returned nonzero);
- `configuration_error` (profile/command/ref/executable absent or invalid);
- `infrastructure_error` (timeout, process/IO failure, provisioning failure).

Only `failed` becomes candidate rework. Configuration/infrastructure failures
park the engine-owned intent in `attention_required` without consuming the
story's product rework budget.

### Acceptance tests

Use real temporary Git repositories and executable fixture commands:

- fresh Epic base resolves the configured ref even when another branch is
  checked out;
- missing base ref is a typed configuration error;
- existing clean story branch refreshes to the current Epic tip;
- dirty worktree is preserved and not refreshed;
- conflict worktree is retained and a Development directive is produced;
- refresh changes the exact source SHA and invalidates old Test/Review evidence;
- configured commands run at the declared workdirs without a wrapper;
- missing command is configuration error, not test failure;
- target/source movement loses CAS without mutating the target;
- no Git remote write command is reachable.

## Spec 3 — Intake reliability

### Typed Work Contract verification

Replace `bool` with:

```python
@dataclass(frozen=True)
class WorkContractVerification:
    valid: bool
    failure: Literal[
        "shape",
        "canonical_mismatch",
        "digest_mismatch",
        "signature_mismatch",
        "key_unreadable",
        "io_error",
    ] | None
```

Exception mapping is explicit:

- contract/type/schema/canonicalization problems → `shape`;
- canonical, digest, and signature comparisons → their named codes;
- missing, unsafe, invalid, or unreadable key → `key_unreadable`;
- other filesystem/process I/O → `io_error`.

`OSError` no longer shares a branch with `TypeError`. The caller reports only
the safe path name and intake/run identity. It never includes the payload,
canonical JSON, key material, signature, or digest.

This specification does not retain failed signed envelopes. The required
typed failure plus existing intake/run/event identity is sufficient for the
first implementation and avoids creating a new sensitive-data store. Bounded
payload retention would require a separate retention/access-control decision.

### Bounded recovery and deduplication

- Work Inbox status includes an authenticated `actions` entry for
  `attention_required` when the original submitter may call the existing
  retry endpoint.
- Dashboard renders that action. CLI gains equivalent inspect/retry support.
- Board policy defines a total qualification-attempt ceiling. Manual retry
  cannot reset the historical count; reaching the ceiling requires a new
  intake or explicit operator override.
- Requalification deduplication treats `pending`, `running`,
  `needs_clarification`, and `attention_required` as active for the same task.
- A partial unique index on the extracted requalification target enforces one
  active intake, not just an application-level query.
- Existing evidence digest, qualifier revision, and Work Contract
  compare-and-swap protections remain unchanged.

### Acceptance tests

- Each verifier path returns its exact safe code.
- Read-only/unreadable key cannot appear as `signature_mismatch`.
- Messages contain no payload, key, signature, digest, or canonical JSON.
- `attention_required` status advertises retry only to its owning submitter.
- Retry obeys total attempt budget.
- Concurrent submissions for one requalification target create one active
  intake.
- A stale older intake cannot replace a newer contract.

## Spec 4 — Epic member integration and final release

### Review-to-integration transaction

For an Epic member, approved Review no longer transitions to
`release_measure`. In one DB transaction it:

1. closes the approved Review run;
2. verifies latest-Review authority and candidate eligibility;
3. inserts or reuses the composite integration intent;
4. sets phase `integration_pending`, generic status `review`, and assignee
   null;
5. appends `story_integration_enqueued` with safe pinned identifiers.

No Git operation occurs in that transaction.

The bounded integrator claims at most the configured number of intents under
leases. It uses the current Epic tip plus the reviewed source SHA to build and
verify an isolated candidate. Successful CAS + durable integration fact moves
the story to `done`. Conflict or candidate verification failure creates a
Development directive and moves the story there. Configuration/infrastructure
failure leaves the intent retryable/attention-required and does not ask Ole to
approve an ordinary member commit.

At claim time the integrator independently re-runs latest-Test authority,
latest-Review authority, exact-SHA equality, provider separation, and
candidate eligibility from terminal run data. It does not treat cached fields
on the intent as evidence. A disagreement supersedes/refuses the intent before
any Git operation. This independent derivation is the Epic-member replacement
for the release-path `_release_run_evidence` backstop.

### Epic readiness

The Epic enters aggregate verification only when:

- it has at least one member;
- every current member has a successful integration fact matching its exact
  terminal source SHA; older integration rows remain history and do not
  participate in readiness;
- no member has an active Review, rework directive, integration intent, or
  non-terminal status;
- every integration fact represents a non-empty contribution or the member
  has a separate governed no-change disposition;
- the current Epic tip includes every pinned integration candidate.

Member `status='done'` alone is never sufficient.

Aggregate verification runs on an isolated final-merge candidate built from:

- the exact current Epic tip;
- the configured target branch at `target_pre_sha`;
- the `epic_release` verification profile.

On green, persist the release snapshot and candidate ref before moving the
Epic to `awaiting_final_release`. The snapshot is the complete evidence Ole is
asked to act on.

### Human final merge and push

Hermes prepares but does not apply the final candidate to local `main`.
Dashboard and CLI show:

- Epic ID and outcome;
- member integration composite keys;
- Epic tip, target pre-SHA, release candidate SHA, and verification run;
- exact local ref to merge;
- whether the local/remote target has moved;
- CI workflows that will be observed after push.

Immediately before the release surface presents an actionable merge/push
handoff, it re-checks local and remote target heads against `target_pre_sha`.
Movement invalidates the snapshot and refuses the action; it is not first
discovered after Ole pushes.

Ole performs the local fast-forward/merge to the pinned candidate and the
remote push. No ordinary worker tool can invoke this action, and no Hermes
code path receives a push primitive.

The read-only release observer then behaves as follows:

- remote `main` at the pinned candidate SHA → `ci_pending`;
- required workflows pass for that exact SHA → Epic `done`, snapshot
  `released`;
- any required workflow fails/cancels/times out → Epic `ci_failed`, snapshot
  retained;
- a later rerun passing for the same SHA may close the Epic;
- a different pushed SHA, changed target, changed Epic tip/member set, or
  changed repository contract invalidates the snapshot and requires a new
  aggregate verification.

The first implementation does not automatically revert a pushed failure or
open forward-repair member work. `ci_failed` retains the exact snapshot and
supports only observation plus a passing rerun of the same SHA. Choosing
forward repair versus revert after a persistent failure remains an explicit
Ole decision and requires a separate recovery design; the engine takes no Git
or work-item action meanwhile. The operator surface reports
`manual_recovery_required`; a human handles the repository outside Hermes
until that separate decision and authorization exist.

For The Trading Company, required observation is both `CI` and `Deploy Test`.
The latter already verifies the deployed runtime reports the exact pushed SHA.
PreProd remains manual and is not an Epic completion condition.

### Dashboard/API behavior

- Member cards show `Integrating`, integration attempt, and safe failure code;
  no Release/Measure action appears for them.
- Epics show `Collecting members`, `Aggregate verification`, `Awaiting final
  release`, `CI pending`, `CI failed`, or `Done`.
- `awaiting_final_release` is visually and operationally distinct from Ready.
- Final release surface prepares/displays the immutable snapshot; it never
  pushes.
- CI failure shows workflow, run URL/identity, exact SHA, and rerun status.
- No reconcile/deploy sub-card is created.

### In-flight migration

Migration is a guarded operation and refuses to run while an affected
Epic-member task has an active worker/run. Deployment must first drain those
runs; the migration lists exact blockers rather than reclaiming them.

Then, in one migration transaction per board:

- add `integration_pending` and Epic lifecycle columns/configuration;
- keep standalone `release_measure` cards unchanged;
- for every nonterminal Epic-member card currently at `release_measure`, first
  run the dispatcher-owned refresh preflight, then route it to Test for the
  refreshed/current exact SHA, followed by fresh Review; record a
  `story_release_gate_migrated` event;
- never auto-integrate an in-flight card from historical approval;
- preserve all prior runs, approvals, events, comments, and integration rows;
- leave already-done members unchanged, but validate their integration facts
  before Epic readiness;
- assign existing Epics the `product_epic` template and
  `collecting_members`; no existing Epic enters `awaiting_final_release`
  without a fresh aggregate verification and snapshot;
- update strict-board phase mappings so engine-owned phases are explicitly
  unassigned and cannot be worker-unblocked/claimed.

Migration is idempotent and records schema version plus per-task events.

The dry-run lists every current member whose latest Test or Review run lacks a
canonical outcome. A nonterminal member receives disposition
`fresh_test_review`. A done member is never authorized by a redundant-only
historical approval: migration may grandfather only its exact pre-existing
`epic_story_integrations` fact when membership matches, the fact's
`source_sha` equals the latest Development handoff SHA, its full
`candidate_sha` exists, and that candidate is contained in the current Epic
tip. Migration records `legacy_integration_fact_grandfathered`; it does not
synthesize a canonical outcome or new fact. Missing or mismatched facts are
explicit blockers and are not repaired automatically.

### Acceptance tests

- Epic-member approval creates an intent and never enters `release_measure`.
- Standalone approval still enters `release_measure` unchanged.
- A missing/malformed Review outcome cannot enqueue integration.
- Duplicate completion/reconcile calls create one composite intent/fact.
- Crash after candidate preparation, after Git CAS, and before DB commit each
  recover without duplicate merge or false candidate SHA.
- Story cannot become `done` before integration fact is durable.
- Integration conflict routes to Development with pinned paths/SHAs and
  supersedes—not deletes—approval.
- Verification/config/infrastructure failures take their distinct paths.
- Epic readiness ignores member status without matching integration facts.
- Snapshot pins every member key, Epic tip, target pre-SHA, candidate SHA,
  contract digest, and aggregate verification run.
- Same-SHA CI rerun can recover `ci_failed`; different SHA invalidates.
- No engine test double or real path can issue `git push`.
- Migration sends release-gate members through refresh → Test → Review and
  leaves standalone cards untouched.
- Migration lists latest-run canonical-outcome gaps, retains only validated
  pre-existing facts under the grandfather rule, and refuses redundant-only
  approval as authority for any new fact.

## Spec 5 — External-boundary assurance

### Structured contract and Test evidence

Work Contract v2 adds explicit external boundaries:

```json
{
  "external_boundaries": [
    {
      "id": "paperclip-agent-api",
      "kind": "http_api",
      "required_assurance": "real_or_explained"
    }
  ]
}
```

Test completion for a contract with boundaries requires:

```json
{
  "boundary_manifest": [
    {
      "boundary_id": "paperclip-agent-api",
      "mode": "real|mocked|unverified",
      "checks": ["bounded evidence identifiers"],
      "fixtures": ["paths actually added or changed"],
      "reason": "required for mocked or unverified"
    }
  ]
}
```

Every declared boundary has exactly one entry. An empty list is an explicit
claim that the contract declares no external boundary; silence is invalid.
Real checks execute only when credentials, authorization, environment safety,
and contract scope permit them. Otherwise the entry must name what was faked
and why the real instance was unavailable.

### Review enforcement

Review receives:

- the Test manifest;
- the candidate's changed-file list derived by the engine;
- the subset of changed paths classified as tests, fixtures, fakes, stubs, or
  adapters;
- real-check evidence identifiers and recorded limitations.

The board's repository contract used with Work Contract v2 also declares
repository-specific test and fixture path selectors. The engine applies those
selectors to the actual candidate diff; it does not guess from filenames or
trust a worker-provided list. The selectors must at minimum cover the
repository's test roots and fixture/fake/stub directories, and configuration
validation reports uncovered declared paths before v2 boundary assurance is
enabled.

Review outcome metadata must attest that changed fixtures were inspected and
list the fixture paths. The engine checks that every engine-detected changed
fixture path is either listed or explicitly marked not applicable with a
reason. A worker-authored manifest alone is never release evidence.

Unverified boundaries remain visible on the story integration fact and Epic
release snapshot. Board policy may make a named boundary blocking; otherwise
Ole sees the limitation before final release. Internal green tests are never
presented as proof of a real external boundary.

Existing Work Contract v1 cards finish under v1 rules. New intake switches to
v2 only after qualifier, API, dashboard, and worker prompts support the field;
there is no silent synthesis from prose.

### Acceptance tests

- Declared boundary without a manifest fails Test completion.
- Mocked/unverified entry without reason fails.
- Review cannot approve while omitting a changed fixture detected by the
  engine.
- Manifest claiming `real` without a configured evidence adapter fails.
- Unsafe or unavailable real boundary records a limitation without making an
  automatic live call.
- Release snapshot carries unresolved limitations.
- Contract v1 behavior remains compatible during migration.

## End-to-end state behavior

| Event | Story state | Epic state | Authority/evidence |
|---|---|---|---|
| Architecture completes | Development | collecting_members | architecture run |
| Development commits | Test | collecting_members | new source SHA |
| Test requests changes | Development | collecting_members | rework directive |
| Test passes | Review | collecting_members | Test run bound to SHA |
| Review requests changes | Development/Architecture | collecting_members | latest Review rejects; older approval void |
| Review approves Epic member | integration_pending | collecting_members | integration intent for exact SHA |
| Integration passes | Done | collecting_members or aggregate_verification | integration composite key |
| Integration conflicts | Development | collecting_members | integration directive; approval superseded |
| All member facts complete | Done members | aggregate_verification | full integration set |
| Aggregate candidate green | Done members | awaiting_final_release | immutable release snapshot |
| Ole pushes exact candidate | Done members | ci_pending | remote exact SHA |
| Required CI passes | Done members | Done | exact-SHA CI/deploy evidence |
| Required CI fails | Done members | ci_failed | pinned failing SHA/run |

## Failure taxonomy

Every failure exposed by these specs uses a stable code and a safe message.

| Domain | Codes |
|---|---|
| Outcome | `missing` (optionally qualified by `serialized_parameter`), `shape`, `phase_mismatch`, `contradictory_verdict`; observation `serialized_parameter_leak` |
| Candidate | `empty_contribution`, `stale_review`, `source_moved`, `target_moved`, `merge_conflict` |
| Repository | `missing_contract`, `missing_ref`, `missing_profile`, `missing_executable`, `dirty_worktree`, `timeout`, `io_error` |
| Intake | `shape`, `canonical_mismatch`, `digest_mismatch`, `signature_mismatch`, `key_unreadable`, `io_error` |
| Release | `member_fact_missing`, `snapshot_invalidated`, `pushed_sha_mismatch`, `ci_failed`, `ci_timeout` |

Messages may include task/run/intent IDs, branch names, safe paths, phase, and
SHAs when those are already board-visible release evidence. Work Contract
errors never include payloads or cryptographic material.

## Verification and rollout gates

Each specification is test-driven and lands only with behavior-level tests.
Tests must use `scripts/run_tests.sh`; they must not inspect source text to
assert implementation.

Rollout gates:

1. **Spec 1:** run-407 regression, latest-Review invariant, no-op integration
   guard, Resolver repair preservation, and existing release-evidence suite
   green. Run 304 proves the defect is not task-specific; runs 354/369 prove
   non-verdict Resolver finalization remains outside the kernel. A
   behavior-level impersonation test proves ordinary `complete_task` rejects
   all three privileged outcome strings in metadata when canonical
   `workflow_outcome` is absent, without mutating the task or run.
2. **Spec 2:** real temporary-repository tests green; board repository contract
   validated for The Trading Company before enabling it.
3. **Spec 3:** verifier fault-injection, retry ownership/budget, and concurrent
   deduplication green.
4. **Spec 4:** full Epic E2E with injected crash points, migration dry-run,
   dashboard/API contract tests, no-push tests, and exact-SHA CI observer tests
   green. Spec 1 and Spec 2 must already be deployed.
5. **Spec 5:** Work Contract v2 compatibility, manifest/fixture cross-check,
   and safe external-boundary tests green.

Before enabling automatic integration on a live board:

- repository contract validates;
- no affected worker run is active;
- migration dry-run lists every state change;
- the 50 most recent completed verdict-bearing Test/Review runs
  (`outcome IN ('advanced', 'rework_requested')`) are measured for missing
  canonical outcomes; an absence rate above 5% stops enablement for
  investigation;
- every latest-run authority gap and every grandfathered/invalid historical
  integration fact is listed in the migration report;
- every `release_measure` Epic member is routed through refresh → Test →
  Review;
- integrator concurrency starts at one per repository;
- `merge_after_green` is disabled for the old reconcile path;
- operator surfaces show the pinned final release state;
- read-only CI credentials/adapters are validated;
- a rollback disables new intent claiming without deleting intents or facts.

Rollback never rewrites completed integrations. It stops claiming new intents,
preserves prepared candidate refs and leases for recovery, and leaves stories
in visible engine-owned states.

## Protected behavior

The implementation must preserve:

- independent writer/tester/reviewer provider checks;
- exact Review branch/SHA pinning;
- `_release_run_evidence` as a separate backstop;
- for Epic members, independently-derived Test/Review/candidate authority at
  integration claim time; an intent is never evidence;
- Development's existing commit-first handoff;
- budgets, rework ceilings, and block-loop limits;
- resolver preflight/refusal and prominent resolver instructions;
- Resolver's separate non-verdict `preflight_repaired`,
  `preflight_resolved`, and `preflight_escalated` finalization path;
- immutable Work Contracts and qualification CAS;
- auditable break-glass history;
- candidate verification before any target branch update;
- local-only Git operations and the absolute no-push boundary;
- existing standalone-card `release_measure` behavior;
- The Trading Company CI/CD ownership of post-push Test deployment and runtime
  SHA verification.

## Review checklist

Reviewers should reject this design if any answer is no:

1. Does every Test/Review terminal path require a canonical outcome before
   mutation?
2. Can any older approval survive a later terminal rejection as authority?
3. Can a no-op or bare ancestor relation create a new integration fact?
4. Can a crash move an Epic ref without a recoverable prepared intent?
5. Can an Epic member become Done before its integration fact is durable?
6. Is every refresh or repair forced through new Test/Review evidence?
7. Is final release bound to one immutable member set and exact candidate SHA?
8. Can Hermes or an ordinary worker reach a push primitive?
9. Can a CI result for a different SHA close the Epic?
10. Can a worker-authored boundary manifest pass without Review checking the
    fixtures actually changed?
11. Are standalone cards unchanged?
12. Are current provider separation, resolver refusal, rework limits, and
    release defense-in-depth preserved?

## Review outcome requested

Approve, approve with changes, or reject the following as one lifecycle
design:

- the five-spec decomposition and hard ordering;
- the four module interfaces;
- the durable intent/fact/snapshot data model;
- the member and Epic state machines;
- the repository and CI authority boundaries;
- the in-flight migration rules;
- the failure taxonomy and acceptance-test gates.

Implementation planning starts only after this document is reviewed and
approved.
