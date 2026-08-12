# Hermes v2 Epic Integration and Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automatically integrate approved Epic-member stories without human commit approval, then prepare one pinned Epic release for Ole's final merge/push and observe CI read-only.

**Architecture:** A durable story-integration coordinator uses SQLite intent leases around an isolated Git candidate and compare-and-swap ref update. A separate Epic-release coordinator derives readiness from integration facts, persists an immutable aggregate-verified snapshot, exposes a human handoff without push capability, and observes GitHub Actions for the exact pushed SHA.

**Tech Stack:** Python dataclasses/protocols, SQLite/WAL and CAS, Git CLI, FastAPI, dashboard JavaScript, GitHub Actions REST read-only observation, pytest with real temporary repositories through `scripts/run_tests.sh`.

## Global Constraints

- This plan depends on the workflow-authority and repository-correctness plans.
- An integration intent is a request, not evidence; every claim re-derives latest Test/Review authority, provider independence, exact SHA equality, and candidate eligibility from ended runs.
- Story `done` requires a durable exact composite integration fact.
- Epic-member Review approval never enters `release_measure` and requires no human approval action.
- Standalone cards retain the existing `release_measure` behavior.
- Git operations occur outside SQLite write transactions.
- Prepared candidate metadata is durable before the Epic ref CAS.
- No engine, worker, API, dashboard, adapter, migration, or test can issue `git push`.
- Ole alone applies the final pinned local merge/fast-forward and remote push.
- `ci_failed` supports observation and a later passing workflow rerun for the same SHA only; no automated revert or forward-repair work is created.
- A persistent `ci_failed` snapshot remains visible while Hermes takes no Git or work-item action; a human handles the repository externally until Ole separately chooses and authorizes revert or forward repair.
- Member set/state, Epic tip, target head, candidate SHA, integration keys, verification run, or contract digest drift invalidates the release snapshot.
- Migration runs first against a scratch database and scratch repository; a live Epic is never the migration test.
- Use `scripts/run_tests.sh`; never invoke `pytest` directly.

---

### Task 1: Add integration and release schema with typed records

**Files:**
- Create: `hermes_cli/kanban_story_integration.py`
- Create: `hermes_cli/kanban_epic_release.py`
- Modify: `hermes_cli/kanban_db.py` (schema and additive migration)
- Create: `tests/hermes_cli/test_kanban_story_integration.py`
- Create: `tests/hermes_cli/test_kanban_epic_release.py`
- Modify: `tests/hermes_cli/test_kanban_db.py`

**Interfaces:**
- Consumes: existing `tasks`, `task_runs`, `epic_memberships`, and `epic_story_integrations`.
- Produces: data classes matching `story_integration_intents`, `repository_verification_runs`, `epic_release_snapshots`, and `epic_release_members` rows.

- [ ] **Step 1: Write failing fresh/migrated-schema tests**

Assert exact columns, composite primary key `(epic_id, story_id, source_sha)`, legal status checks, one non-invalidated snapshot per Epic, and idempotent initialization on both a fresh DB and a pre-feature DB copy.

- [ ] **Step 2: Run tests to verify failure**

Run: `scripts/run_tests.sh tests/hermes_cli/test_kanban_story_integration.py tests/hermes_cli/test_kanban_epic_release.py tests/hermes_cli/test_kanban_db.py -k 'schema or migration' -q`

Expected: FAIL because the tables do not exist.

- [ ] **Step 3: Add the final-design tables and indexes**

Use the final-design columns/statuses exactly. Add:

```sql
CREATE INDEX IF NOT EXISTS idx_story_integration_intents_claim
ON story_integration_intents(status, claim_expires, created_at);

CREATE UNIQUE INDEX IF NOT EXISTS idx_epic_release_one_active
ON epic_release_snapshots(epic_id)
WHERE status IN ('awaiting_push', 'ci_pending', 'ci_failed');
```

Retain `epic_story_integrations` as the fact ledger with its existing composite key; do not add a surrogate ID.

- [ ] **Step 4: Add immutable row types and parsers**

```python
@dataclass(frozen=True)
class IntegrationKey:
    epic_id: str
    story_id: str
    source_sha: str


@dataclass(frozen=True)
class IntegrationIntent:
    key: IntegrationKey
    source_branch: str
    review_run_id: int
    review_base_sha: str
    status: str
    claim_lock: str | None
    claim_expires: int | None
    attempt_count: int
    target_pre_sha: str | None
    candidate_sha: str | None
    candidate_ref: str | None
    verification_run_id: int | None
```

Add equivalent `EpicReleaseSnapshot`, `EpicReleaseMember`, and `RepositoryVerificationRun` types. Row parsers reject invalid full SHAs/status values rather than silently normalizing.

- [ ] **Step 5: Run schema tests**

Run: `scripts/run_tests.sh tests/hermes_cli/test_kanban_story_integration.py tests/hermes_cli/test_kanban_epic_release.py tests/hermes_cli/test_kanban_db.py -q`

Expected: PASS.

- [ ] **Step 6: Commit schema and types**

```bash
git add hermes_cli/kanban_story_integration.py hermes_cli/kanban_epic_release.py hermes_cli/kanban_db.py tests/hermes_cli/test_kanban_story_integration.py tests/hermes_cli/test_kanban_epic_release.py tests/hermes_cli/test_kanban_db.py
git commit -m "feat: add epic integration and release records"
```

### Task 2: Enqueue Epic-member approval atomically

**Files:**
- Modify: `hermes_cli/kanban_story_integration.py`
- Modify: `hermes_cli/kanban_db.py` (`handoff`, `complete_task`, claim/move/promote/unblock guards)
- Test: `tests/hermes_cli/test_kanban_story_integration.py`
- Test: `tests/hermes_cli/test_kanban_db.py`

**Interfaces:**
- Consumes: latest `ApprovedCandidate`, latest `PassedTest`, `CandidateEligibility`, and explicit Epic membership.
- Produces: `enqueue_approved_story(conn, approved_candidate) -> IntegrationIntent` and member phase `integration_pending`.

- [ ] **Step 1: Write failing member-versus-standalone tests**

Assert approved Epic-member Review closes its run, inserts one composite intent, sets `current_step_key='integration_pending'`, generic status `review`, `assignee=NULL`, and appends `story_integration_enqueued`. Assert an identical standalone card still enters `release_measure`.

Add refusal cases for missing/malformed outcome, later Test/Review rejection, same-provider writer/reviewer, empty contribution, active rework directive, and stale run ownership.

- [ ] **Step 2: Run tests to verify failure**

Run: `scripts/run_tests.sh tests/hermes_cli/test_kanban_story_integration.py tests/hermes_cli/test_kanban_db.py -k 'integration_pending or integration_enqueued or standalone_release_measure' -q`

Expected: FAIL because all approvals currently follow the human release transition.

- [ ] **Step 3: Implement the one-transaction enqueue**

```python
def enqueue_approved_story(
    conn: sqlite3.Connection,
    *,
    epic_id: str,
    story_id: str,
    approved: ApprovedCandidate,
    passed: PassedTest,
    eligibility: CandidateEligibility,
    expected_run_id: int,
) -> IntegrationIntent:
    # caller already holds authorized write transaction
    # re-read membership/run/task, insert-or-read exact key, CAS task phase,
    # close run, append event, return durable row
```

No Git command runs inside this function/transaction. Replays return the same intent; a different source SHA supersedes the older pending/attention intent without deleting it.

- [ ] **Step 4: Guard engine-owned phases**

Make ordinary `claim_task`, manual complete, generic move, promote, unblock, Work Inbox delivery, and release-measure helpers refuse `integration_pending` and `product_epic` phases. Coordinator functions enter `authorized_governance_write` explicitly.

- [ ] **Step 5: Run DB/integration tests**

Run: `scripts/run_tests.sh tests/hermes_cli/test_kanban_story_integration.py tests/hermes_cli/test_kanban_db.py -q`

Expected: PASS.

- [ ] **Step 6: Commit enqueue behavior**

```bash
git add hermes_cli/kanban_story_integration.py hermes_cli/kanban_db.py tests/hermes_cli/test_kanban_story_integration.py tests/hermes_cli/test_kanban_db.py
git commit -m "feat: enqueue approved epic stories for integration"
```

### Task 3: Claim intents and independently re-derive authority

**Files:**
- Modify: `hermes_cli/kanban_story_integration.py`
- Modify: `hermes_cli/kanban_db.py` (dispatcher tick hook)
- Test: `tests/hermes_cli/test_kanban_story_integration.py`

**Interfaces:**
- Consumes: owner, lease seconds, stored intent key, ended task runs, membership, active directive, and repository contract.
- Produces: `claim_next_intent(conn, owner, lease) -> IntegrationIntent | None` and `ClaimAuthority` derived fresh at claim time.

- [ ] **Step 1: Write failing lease/concurrency/authority tests**

With two SQLite connections, assert one claimant wins. After enqueue but before claim, append a later Review rejection, later Test rejection, change membership, or move the source branch; each case must supersede/refuse the intent before any injected repository-service call occurs.

- [ ] **Step 2: Run tests to verify failure**

Run: `scripts/run_tests.sh tests/hermes_cli/test_kanban_story_integration.py -k 'claim or authority' -q`

Expected: FAIL because no integrator claim path exists.

- [ ] **Step 3: Implement bounded claim CAS**

Select one `pending` or expired `running` intent ordered by creation, set a random `claim_lock`, expiry, `status='running'`, and increment `attempt_count` in one immediate transaction. Limit the dispatcher to the board-configured concurrency, initially one per repository.

- [ ] **Step 4: Re-derive every authority fact after claim**

```python
authority = derive_claim_authority(
    runs=list_runs(conn, intent.key.story_id, include_active=False),
    expected_source_sha=intent.key.source_sha,
)
if (
    authority.review.run_id != intent.review_run_id
    or authority.review.source_sha != intent.key.source_sha
    or authority.test.source_sha != intent.key.source_sha
):
    supersede_intent(conn, intent, code="stale_authority")
    return None
```

Also re-run provider separation, candidate non-empty check from pinned Review base/head, membership, task phase, and active-directive absence. Cached intent fields are comparison inputs only, never proof.

- [ ] **Step 5: Run claim tests**

Run: `scripts/run_tests.sh tests/hermes_cli/test_kanban_story_integration.py -q`

Expected: PASS.

- [ ] **Step 6: Commit independent claim authority**

```bash
git add hermes_cli/kanban_story_integration.py hermes_cli/kanban_db.py tests/hermes_cli/test_kanban_story_integration.py
git commit -m "fix: rederive story authority at integration claim"
```

### Task 4: Prepare, verify, CAS, and recover story integrations

**Files:**
- Modify: `hermes_cli/kanban_story_integration.py`
- Modify: `hermes_cli/kanban_repository.py`
- Test: `tests/hermes_cli/test_kanban_story_integration.py`
- Modify: `tests/hermes_cli/test_kanban_repository.py`

**Interfaces:**
- Consumes: claimed intent, current Epic tip, reviewed exact source SHA, repository contract/profile.
- Produces: `prepare_integration_candidate`, durable `prepared` intent, Epic-ref CAS, `finish_intent`, and `recover_expired_intents`.

- [ ] **Step 1: Write failing crash-boundary tests**

Inject crashes after candidate preparation, after persisting prepared metadata, after Epic ref CAS, and before fact DB commit. Assert replay either applies the prepared candidate or finishes the DB record, never duplicates merge/fact, and records the pinned `candidate_sha` rather than a later Epic tip.

- [ ] **Step 2: Run tests to verify failure**

Run: `scripts/run_tests.sh tests/hermes_cli/test_kanban_story_integration.py -k 'prepared or crash or recover' -q`

Expected: FAIL because existing reconciliation does not have durable prepared intent state.

- [ ] **Step 3: Build and verify outside DB transactions**

Call `prepare_integration_candidate` using current Epic tip as `target_pre_sha` and reviewed source SHA. Persist `repository_verification_runs` around the configured `story_integration` profile. A passed result yields retained `candidate_ref` and full candidate SHA.

- [ ] **Step 4: Persist prepared state before Git CAS**

In one short transaction, CAS the still-owned running intent to `prepared` with `target_pre_sha`, `candidate_sha`, `candidate_ref`, and passing verification ID. Then outside the transaction run:

```bash
git update-ref refs/heads/<epic-branch> <candidate-sha> <target-pre-sha>
```

- [ ] **Step 5: Finish the fact and task atomically**

After successful/reflected CAS, insert-or-verify exact `epic_story_integrations`, mark intent `integrated`, set story `done`/terminal phase, append `story_integrated`, and invalidate affected release snapshot in one transaction. Task `done` cannot be written before the fact exists.

- [ ] **Step 6: Implement deterministic expired-intent recovery**

Handle exactly:

- Epic ref equals `target_pre_sha`: apply prepared CAS;
- Epic ref equals `candidate_sha`: finish DB record;
- `candidate_sha` is ancestor of later Epic tip: finish using pinned candidate SHA;
- otherwise: clear prepared fields, record `target_moved`, and rebuild from current tip with the same reviewed source SHA after re-deriving authority.

- [ ] **Step 7: Run integration/repository tests**

Run: `scripts/run_tests.sh tests/hermes_cli/test_kanban_story_integration.py tests/hermes_cli/test_kanban_repository.py -q`

Expected: PASS.

- [ ] **Step 8: Commit two-phase integration**

```bash
git add hermes_cli/kanban_story_integration.py hermes_cli/kanban_repository.py tests/hermes_cli/test_kanban_story_integration.py tests/hermes_cli/test_kanban_repository.py
git commit -m "feat: integrate epic stories with recoverable git cas"
```

### Task 5: Route integration failures without human commit approval

**Files:**
- Modify: `hermes_cli/kanban_story_integration.py`
- Modify: `hermes_cli/kanban_db.py`
- Test: `tests/hermes_cli/test_kanban_story_integration.py`
- Modify: `tests/e2e/test_kanban_product_recovery_flow.py`

**Interfaces:**
- Consumes: repository result/error code for a claimed intent.
- Produces: `rework_required`, `attention_required`, retry, or superseded intent plus appropriate task/directive state.

- [ ] **Step 1: Write failing failure-taxonomy tests**

Assert merge conflict and verification `failed` create Development directives and consume one product rework cycle. Assert missing command/ref/profile, timeout, provisioning/IO, and target/source movement park/retry without consuming rework. No case creates an approval request or `release_measure` member phase.

- [ ] **Step 2: Run tests to verify failure**

Run: `scripts/run_tests.sh tests/hermes_cli/test_kanban_story_integration.py tests/e2e/test_kanban_product_recovery_flow.py -k 'integration and (conflict or failure or attention)' -q`

Expected: FAIL until typed results are mapped.

- [ ] **Step 3: Implement exact state mappings**

Conflict/candidate failure atomically supersedes active approval intent, creates the first-class directive, moves task to Development, and keeps all attempt/approval history. Configuration/infrastructure moves intent to `attention_required`, leaves task engine-owned, records only safe failure code, and permits bounded retry.

- [ ] **Step 4: Run integration and recovery E2E suites**

Run: `scripts/run_tests.sh tests/hermes_cli/test_kanban_story_integration.py tests/e2e/test_kanban_product_recovery_flow.py -q`

Expected: PASS.

- [ ] **Step 5: Commit failure routing**

```bash
git add hermes_cli/kanban_story_integration.py hermes_cli/kanban_db.py tests/hermes_cli/test_kanban_story_integration.py tests/e2e/test_kanban_product_recovery_flow.py
git commit -m "fix: route story integration failures by cause"
```

### Task 6: Derive Epic readiness and persist release snapshots

**Files:**
- Modify: `hermes_cli/kanban_epic_release.py`
- Modify: `hermes_cli/kanban_db.py` (`epic_ready` replacement adapter)
- Test: `tests/hermes_cli/test_kanban_epic_release.py`
- Modify: `tests/hermes_cli/test_kanban_epics.py`

**Interfaces:**
- Consumes: current membership, terminal member exact SHAs, integration facts/intents/directives, current Epic tip, target SHA, repository contract/profile.
- Produces: `evaluate_epic_readiness(conn, epic_id) -> ReadinessResult` and `prepare_release_snapshot(conn, epic_id) -> EpicReleaseSnapshot`.

- [ ] **Step 1: Write failing readiness tests**

Cover no members, `done` without fact, old-source fact, active Review/directive/intent, non-terminal member, empty contribution without governed no-change disposition, and Epic tip missing a candidate. Only the complete exact set is ready.

- [ ] **Step 2: Run tests to verify failure**

Run: `scripts/run_tests.sh tests/hermes_cli/test_kanban_epic_release.py tests/hermes_cli/test_kanban_epics.py -k 'readiness or integration_fact' -q`

Expected: FAIL because current readiness primarily trusts story status.

- [ ] **Step 3: Implement fact-derived readiness**

Return a structured result with `ready`, safe blocker codes, exact member `IntegrationKey`s/candidate SHAs, Epic tip, target SHA, and contract digest. Do not mutate state during evaluation.

- [ ] **Step 4: Prepare aggregate candidate and verification**

Build an isolated final candidate from exact Epic tip onto exact target pre-SHA, run `epic_release` profile, and retain its candidate ref. Persist `repository_verification_runs`; only a passed run may create a snapshot.

- [ ] **Step 5: Persist the immutable snapshot atomically**

Re-check the readiness inputs, insert snapshot plus every member row, set Epic template `product_epic`, phase `awaiting_final_release`, and invalidate any older active snapshot. Snapshot uses exact integration composite keys and candidate SHAs.

- [ ] **Step 6: Add invalidation hooks**

Member add/remove/non-terminal transition, integration change, Epic/target movement, contract digest change, candidate/ref loss, or new aggregate verification invalidates the active snapshot and returns Epic to the appropriate pre-release phase.

- [ ] **Step 7: Run Epic tests**

Run: `scripts/run_tests.sh tests/hermes_cli/test_kanban_epic_release.py tests/hermes_cli/test_kanban_epics.py -q`

Expected: PASS.

- [ ] **Step 8: Commit snapshot preparation**

```bash
git add hermes_cli/kanban_epic_release.py hermes_cli/kanban_db.py tests/hermes_cli/test_kanban_epic_release.py tests/hermes_cli/test_kanban_epics.py
git commit -m "feat: prepare immutable epic release snapshots"
```

### Task 7: Add human release handoff and read-only CI observation

**Files:**
- Modify: `hermes_cli/kanban_epic_release.py`
- Create: `hermes_cli/kanban_ci.py`
- Modify: `hermes_cli/kanban_db.py`
- Test: `tests/hermes_cli/test_kanban_epic_release.py`
- Create: `tests/hermes_cli/test_kanban_ci.py`

**Interfaces:**
- Consumes: active release snapshot, local target ref, read-only remote target observation, and required workflow observations for exact SHA.
- Produces: `release_handoff(snapshot) -> ReleaseHandoff` and `observe_release(snapshot, ci_adapter) -> ReleaseObservation`.

- [ ] **Step 1: Write failing target-race and CI-state tests**

Immediately before handoff, move local target or return a different remote target SHA and assert snapshot invalidates with no actionable command. Cover remote at candidate → `ci_pending`, required workflows all pass → released/Done, fail/cancel/timeout → `ci_failed`, later same-SHA pass → Done, and different SHA → invalidated.

- [ ] **Step 2: Run tests to verify failure**

Run: `scripts/run_tests.sh tests/hermes_cli/test_kanban_epic_release.py tests/hermes_cli/test_kanban_ci.py -q`

Expected: FAIL because no snapshot handoff/observer exists.

- [ ] **Step 3: Define read-only adapter protocols**

```python
class RemoteTargetObserver(Protocol):
    def head_sha(self, remote: str, branch: str) -> str: ...


class CIAdapter(Protocol):
    def workflows_for_sha(
        self, *, repository: str, sha: str, names: tuple[str, ...]
    ) -> tuple[WorkflowObservation, ...]: ...
```

The Git remote implementation uses read-only `git ls-remote --refs`; the GitHub Actions implementation performs GET requests only. No adapter exposes rerun, dispatch, cancel, merge, update-ref, or push methods.

- [ ] **Step 4: Re-check both target heads immediately before handoff**

Compare local `refs/heads/<target>` and remote `refs/heads/<target>` to `target_pre_sha`. On any mismatch/unavailability, invalidate/refuse. On success, return IDs/SHAs, verification run, member keys, required workflows, and retained candidate ref; do not return an executable push primitive.

- [ ] **Step 5: Implement exact-SHA CI transitions**

Persist workflow name, provider run identity/URL, conclusion, observed SHA, and observation time. A later observation may close `ci_failed` only when every required workflow passes for `snapshot.pushed_sha == release_candidate_sha`. Do not create revert or forward-repair cards.

For a persistent failure, return an explicit `manual_recovery_required` safe
state alongside the retained snapshot. This is the accepted first-release
boundary, not an omitted automatic transition: Hermes observes but does not
change Git, reopen a member, or create work.

- [ ] **Step 6: Add capability-surface tests**

Instantiate real adapters with fake transports and assert recorded HTTP methods are all GET. Inject a Git runner and assert allowed subcommands are `rev-parse`, `show-ref`, `merge-base`, `cat-file`, `worktree`, `status`, `diff`, `ls-remote`, and local candidate operations; any `push` invocation raises before subprocess execution.

- [ ] **Step 7: Run release/CI tests**

Run: `scripts/run_tests.sh tests/hermes_cli/test_kanban_epic_release.py tests/hermes_cli/test_kanban_ci.py -q`

Expected: PASS.

- [ ] **Step 8: Commit human boundary and observer**

```bash
git add hermes_cli/kanban_epic_release.py hermes_cli/kanban_ci.py hermes_cli/kanban_db.py tests/hermes_cli/test_kanban_epic_release.py tests/hermes_cli/test_kanban_ci.py
git commit -m "feat: observe pinned epic releases without push authority"
```

### Task 8: Add CLI/API/dashboard lifecycle surfaces

**Files:**
- Modify: `hermes_cli/kanban.py`
- Modify: `plugins/kanban/dashboard/plugin_api.py`
- Modify: `plugins/kanban/dashboard/dist/index.js`
- Modify: `plugins/kanban/dashboard/dist/style.css`
- Modify: `tests/hermes_cli/test_kanban_release_cli.py`
- Modify: `tests/plugins/test_kanban_dashboard_plugin.py`
- Modify: `tests/plugins/kanban_dashboard_client_contract.js`

**Interfaces:**
- Consumes: integration intent safe status, Epic state, release handoff, and CI observations.
- Produces: operator/member views without member approval actions or engine push action.

- [ ] **Step 1: Write failing API/CLI/UI contract tests**

Assert members show Integrating/attempt/safe code and never show Release/Measure. Assert Epics show all typed states. Awaiting-final-release displays snapshot fields and a copyable candidate ref, but no API route/button invokes merge or push. CI failure displays workflow/run URL/exact SHA/rerun observation status.

- [ ] **Step 2: Run tests to verify failure**

Run: `scripts/run_tests.sh tests/hermes_cli/test_kanban_release_cli.py tests/plugins/test_kanban_dashboard_plugin.py -k 'integration or awaiting_final_release or ci_failed' -q`

Expected: FAIL because current UI models member Release/Measure and unassigned Epics as ready-like.

- [ ] **Step 3: Add bounded read models/endpoints**

Return only safe state, IDs, full SHAs, workflow URLs, candidate ref, contract digest, and verification IDs. Before returning `actionable: true`, call the immediate local/remote target re-check from Task 7.

- [ ] **Step 4: Render distinct member/Epic states**

Use server state keys, not generic status inference. Remove member Release/Measure controls. Render one final handoff panel on the Epic containing the immutable evidence and plain-language instruction that Ole performs merge/push externally.

- [ ] **Step 5: Run CLI/API/client tests**

Run: `scripts/run_tests.sh tests/hermes_cli/test_kanban_release_cli.py tests/plugins/test_kanban_dashboard_plugin.py -q`

Expected: PASS, including the existing dashboard client-contract harness.

- [ ] **Step 6: Commit lifecycle surfaces**

```bash
git add hermes_cli/kanban.py plugins/kanban/dashboard/plugin_api.py plugins/kanban/dashboard/dist/index.js plugins/kanban/dashboard/dist/style.css tests/hermes_cli/test_kanban_release_cli.py tests/plugins/test_kanban_dashboard_plugin.py tests/plugins/kanban_dashboard_client_contract.js
git commit -m "feat: show epic integration and final release state"
```

### Task 9: Build and prove the guarded migration on scratch copies

**Files:**
- Create: `hermes_cli/kanban_v2_migration.py`
- Modify: `hermes_cli/kanban.py`
- Create: `tests/hermes_cli/test_kanban_v2_migration.py`
- Create: `tests/fixtures/kanban/v2_migration/pre_integration.sql`
- Modify: `docs/hermes-kanban-v2.md`

**Interfaces:**
- Consumes: board/database path, repository contract, dry-run/apply mode, scratch repository paths.
- Produces: `plan_v2_migration(...) -> MigrationPlan` and `apply_v2_migration(...) -> MigrationReport`.

- [ ] **Step 1: Write failing scratch migration tests**

Build a fixture with standalone and Epic-member `release_measure` cards,
active worker, done member with/without fact, a done member whose exact
pre-existing fact rests on pre-envelope Review history, a nonterminal member
whose latest Test/Review run has no canonical outcome, existing Epic,
histories/comments, and old phase mappings. Assert active affected runs block
the whole migration and list exact IDs. Assert the nonterminal authority gap
is reported for fresh evidence, the valid existing fact is a grandfather
candidate, and a done member without a valid fact is a blocker.

- [ ] **Step 2: Run tests to verify failure**

Run: `scripts/run_tests.sh tests/hermes_cli/test_kanban_v2_migration.py -q`

Expected: FAIL because the migration module does not exist.

- [ ] **Step 3: Implement a read-only dry-run manifest**

Keep historical persisted-outcome classification local to the migration
module:

```python
VERDICT_BEARING_HISTORICAL_OUTCOMES = frozenset({
    "advanced", "rework_requested",
})
NON_VERDICT_RESOLVER_HISTORICAL_OUTCOMES = frozenset({
    "preflight_repaired", "preflight_resolved", "preflight_escalated",
})
```

These constants classify already-persisted rows for dry-run reporting only.
Neither `kanban_product_outcomes.py`, `complete_task`, nor any outcome
validation path may import or consult them.

The manifest lists contract validation, blockers, each affected task's
before/after state, refresh preflight result, history counts, Epic template
changes, expected schema version, the rolling-50 verdict-bearing outcome
compatibility measurement from the master plan, and `authority_gaps`.

Each authority gap contains exact `task_id`, current phase/status, latest Test
run ID/outcome/canonical-presence, latest Review run
ID/outcome/canonical-presence, integration composite key if present, and one
disposition: `fresh_test_review`, `legacy_fact_grandfather_candidate`, or
`blocking_invalid_fact`. Hash canonical manifest JSON. Dry-run opens DB
read-only and performs no ref/task mutation.

- [ ] **Step 4: Implement guarded apply**

Outside a DB write transaction, materialize every dispatcher refresh preflight
from the dry-run manifest and retain its exact old/new branch SHAs or refusal.
Then open one board transaction, re-check the manifest hash inputs and every
story/Epic ref, refuse active runs or any changed ref, and apply only DB state.
Standalone `release_measure` stays unchanged. Each successfully preflighted
non-terminal Epic member moves to Test for its current/refreshed exact SHA and
records `story_release_gate_migrated`; Review follows only after fresh Test.
Existing Epics become `product_epic/collecting_members`; none receives a
release snapshot.

For an already-done current member, migration may grandfather only an exact
pre-existing `epic_story_integrations` fact; it never grandfathers an approval
run or synthesizes canonical outcome from redundant metadata. The fact is
valid only when its `(epic_id, story_id)` matches current membership,
`source_sha` equals the latest Development handoff SHA, `candidate_sha` is a
full existing commit, and that candidate is an ancestor of the current Epic
tip. Record `legacy_integration_fact_grandfathered` with the composite key and
migration version. Missing/mismatched facts remain blockers and migration does
not reopen or manufacture history for them. Every nonterminal member with an
authority gap goes through fresh Test and Review.

- [ ] **Step 5: Preserve history and make re-run idempotent**

Before/after assertions require identical run/comment/integration rows and
identical pre-existing events; only the documented migration and grandfather
events plus schema objects may be added. Re-running returns zero state changes
with the same schema version. Add a negative test proving redundant-only
`approved` metadata cannot create a new fact or authority, and a positive test
proving a structurally/Git-valid pre-existing fact is retained without
re-deriving pre-envelope Review evidence.

- [ ] **Step 6: Add CLI safety rails and documentation**

Provide `hermes kanban v2-migrate --board <slug> --dry-run --json` and `--apply --manifest-sha <sha>`. Require explicit scratch DB/repository paths in automated tests. Document copying production-shaped DB/repo to a temporary directory, dry-run review, drain requirement, backup, authority-gap review, the 5% rolling compatibility stop, grandfathered-fact audit, and apply verification; do not instruct operators to test on a live Epic.

- [ ] **Step 7: Run migration tests**

Run: `scripts/run_tests.sh tests/hermes_cli/test_kanban_v2_migration.py tests/hermes_cli/test_kanban_db.py -q`

Expected: PASS.

- [ ] **Step 8: Commit guarded migration**

```bash
git add hermes_cli/kanban_v2_migration.py hermes_cli/kanban.py tests/hermes_cli/test_kanban_v2_migration.py tests/fixtures/kanban/v2_migration/pre_integration.sql docs/hermes-kanban-v2.md
git commit -m "feat: migrate epic release gates through fresh evidence"
```

### Task 10: Prove the complete Epic path end to end

**Files:**
- Create: `tests/e2e/test_kanban_epic_integration_release.py`
- Modify: `tests/e2e/test_kanban_product_recovery_flow.py`
- Modify: `docs/hermes-kanban-v2.md`

**Interfaces:**
- Consumes: all prior plan interfaces.
- Produces: real-Git/SQLite proof of automatic member flow and human-only final release boundary.

- [ ] **Step 1: Add the happy-path E2E scenario**

In temporary repositories/DB: Development commit → pinned Test pass → independent pinned Review approval → integration intent → verified Epic candidate → durable integration fact/story Done → aggregate candidate/snapshot → external test harness advances remote to candidate → exact-SHA CI observations pass → Epic Done. Assert no member approval action at any stage.

- [ ] **Step 2: Add recovery and invalidation scenarios**

Cover integration conflict to Development, crash at every two-phase boundary, later Review rejection, source/target CAS loss, member leaving terminal after snapshot, target movement before handoff display, CI failure plus same-SHA passing rerun, and different-SHA invalidation.

- [ ] **Step 3: Add no-push proof**

Use a fake `git` executable that logs argv and fails on `push`; execute dispatcher, integrator, snapshot, API, CLI, observer, and migration paths. Assert no logged command begins with `push` and the remote changes only through the test harness's external operation.

- [ ] **Step 4: Run all scoped suites**

Run:

```bash
scripts/run_tests.sh \
  tests/hermes_cli/test_kanban_product_outcomes.py \
  tests/hermes_cli/test_kanban_repository.py \
  tests/hermes_cli/test_kanban_story_integration.py \
  tests/hermes_cli/test_kanban_epic_release.py \
  tests/hermes_cli/test_kanban_ci.py \
  tests/hermes_cli/test_kanban_v2_migration.py \
  tests/hermes_cli/test_kanban_release_evidence.py \
  tests/hermes_cli/test_kanban_release_cli.py \
  tests/hermes_cli/test_kanban_epics.py \
  tests/plugins/test_kanban_dashboard_plugin.py \
  tests/e2e/test_kanban_product_recovery_flow.py \
  tests/e2e/test_kanban_epic_integration_release.py -q
```

Expected: PASS.

- [ ] **Step 5: Run full CI-parity suite**

Run: `scripts/run_tests.sh`

Expected: PASS.

- [ ] **Step 6: Commit E2E proof and operating guide**

```bash
git add tests/e2e/test_kanban_epic_integration_release.py tests/e2e/test_kanban_product_recovery_flow.py docs/hermes-kanban-v2.md
git commit -m "test: prove automatic epic integration and human release"
```
