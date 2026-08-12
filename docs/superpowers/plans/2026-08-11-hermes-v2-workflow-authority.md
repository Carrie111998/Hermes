# Hermes v2 Workflow Authority Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Test/Review lifecycle decisions fail closed on canonical outcomes and derive integration authority only from the latest immutable ended runs.

**Architecture:** Add a pure outcome/authority kernel and keep SQLite transitions in `kanban_db.py`. The kernel returns typed values and safe reason codes; callers validate before opening any workflow mutation transaction. Durable rework directives and the operator recovery verb are separate commits after the critical outcome/authority slice.

**Tech Stack:** Python dataclasses/enums, SQLite transactions and CAS updates, pytest through `scripts/run_tests.sh`.

## Global Constraints

- Canonical `metadata.workflow_outcome` is the only lifecycle authority.
- Serialized parameter markup is never parsed as authority.
- Runs 304 and 407 must reject as `missing` with qualifier `serialized_parameter`; run 410 must approve while recording `serialized_parameter_leak`.
- The kernel applies only to ordinary verdict-bearing Test/Review completion through `complete_task`; privileged Resolver finalization remains a separate non-verdict path with persisted outcomes `preflight_repaired`, `preflight_resolved`, and `preflight_escalated`.
- The latest ended Test/Review run wins; never scan backward for a convenient pass or approval.
- Authority uses full exact source SHA equality and dispatcher-pinned branch/SHA fields.
- For ordinary verdict-bearing completion, validation occurs before provenance mutation, run closure, handoff, routing, comments, or task mutation.
- `_release_run_evidence` remains an independent release-path check.
- Development keeps `_commit_worker_diff` and its existing no-commit refusal.
- Removing `_commit_worker_diff` from Test/Review is not part of this plan; it is Task 5 of the repository-correctness plan and must be its own commit.
- Use `scripts/run_tests.sh`; never invoke `pytest` directly.

---

### Task 1: Capture the five production completion envelopes

**Files:**
- Create: `tests/fixtures/kanban/product_outcomes/run_304.json`
- Create: `tests/fixtures/kanban/product_outcomes/run_354.json`
- Create: `tests/fixtures/kanban/product_outcomes/run_369.json`
- Create: `tests/fixtures/kanban/product_outcomes/run_407.json`
- Create: `tests/fixtures/kanban/product_outcomes/run_410.json`
- Create: `tests/hermes_cli/test_kanban_product_outcomes.py`

**Interfaces:**
- Consumes: JSON fields `id`, `task_id`, `epic_id`, `step_key`, `status`, `outcome`, `summary`, and `metadata` from production runs 304, 354, 369, 407, and 410.
- Produces: `_production_envelope(run_id: int) -> dict[str, object]` test helper and immutable regression fixtures.

- [ ] **Step 1: Extract and check in the exact production rows**

Read the live evidence database immutably and inspect the exact stored values:

```bash
sqlite3 -json \
  'file:/Users/cloudadvisor/.hermes/kanban/boards/the-trading-company/kanban.db?immutable=1' \
  "SELECT r.id, r.task_id, m.epic_id, r.step_key, r.status, r.outcome,
          r.summary, r.metadata
     FROM task_runs AS r
     LEFT JOIN epic_memberships AS m ON m.task_id = r.task_id
    WHERE r.id IN (304, 354, 369, 407, 410) ORDER BY r.id;"
```

Use `apply_patch` to add that output as the five fixture files, converting only
the `metadata` JSON string into a JSON object so the fixture loader does not
double-decode it. Preserve the complete `summary` and complete metadata from
each row byte-for-byte at the string/value level. Do not shorten review prose,
remove paths, normalize whitespace, synthesize findings, or drop redundant
fields: those irregularities are the regression evidence.

- [ ] **Step 2: Add fixture-loading tests that state the discriminator**

```python
def test_production_run_407_has_marker_without_canonical_outcome():
    row = _production_envelope(407)
    assert '<parameter name="workflow_outcome">' in row["summary"]
    assert "workflow_outcome" not in row["metadata"]


def test_production_run_304_is_an_independent_missing_canonical_occurrence():
    row = _production_envelope(304)
    assert row["task_id"] != _production_envelope(407)["task_id"]
    assert row["epic_id"] != _production_envelope(407)["epic_id"]
    assert row["outcome"] == "advanced"
    assert '<parameter name="workflow_outcome">' in row["summary"]
    assert "workflow_outcome" not in row["metadata"]


@pytest.mark.parametrize("run_id", [354, 369])
def test_production_preflight_repairs_are_non_verdict_terminal_runs(run_id):
    row = _production_envelope(run_id)
    assert row["step_key"] == "test"
    assert row["outcome"] == "preflight_repaired"
    assert "workflow_outcome" not in row["metadata"]


def test_production_run_410_has_marker_and_canonical_outcome():
    row = _production_envelope(410)
    assert '<parameter name="workflow_outcome">' in row["summary"]
    assert row["metadata"]["workflow_outcome"] == {"verdict": "approved"}
```

- [ ] **Step 3: Run the fixture tests**

Run: `scripts/run_tests.sh tests/hermes_cli/test_kanban_product_outcomes.py -q`

Expected: PASS; these tests contain no engine behavior yet.

- [ ] **Step 4: Commit the evidence fixtures**

```bash
git add tests/fixtures/kanban/product_outcomes tests/hermes_cli/test_kanban_product_outcomes.py
git commit -m "test: capture production workflow outcome envelopes"
```

### Task 2: Validate the canonical outcome before mutation

**Files:**
- Create: `hermes_cli/kanban_product_outcomes.py`
- Modify: `hermes_cli/kanban_db.py` (`complete_task`, `_route_product_rework_if_requested`, `_validate_product_workflow_outcome`)
- Modify: `tools/kanban_tools.py` (completion error response)
- Test: `tests/hermes_cli/test_kanban_product_outcomes.py`
- Test: `tests/hermes_cli/test_kanban_db.py`
- Test: `tests/tools/test_kanban_tools.py`

**Interfaces:**
- Consumes: `task_id`, `run_id`, active phase, `summary`, `result`, and raw metadata.
- Produces: `validate_terminal_outcome(...) -> TerminalOutcome`, `OutcomeValidationError.code`, `.qualifier`, and `.observations`.

- [ ] **Step 1: Write failing pure-kernel tests**

```python
def test_run_407_is_missing_not_changes_requested():
    row = _production_envelope(407)
    with pytest.raises(OutcomeValidationError) as raised:
        validate_terminal_outcome(
            task_id=row["task_id"], run_id=row["id"], phase="review",
            summary=row["summary"], result=None, metadata=row["metadata"],
        )
    assert raised.value.code == "missing"
    assert raised.value.qualifier == "serialized_parameter"


def test_run_410_approves_and_records_leak():
    row = _production_envelope(410)
    outcome = validate_terminal_outcome(
        task_id=row["task_id"], run_id=row["id"], phase="review",
        summary=row["summary"], result=None, metadata=row["metadata"],
    )
    assert outcome.verdict == "approved"
    assert outcome.observations == ("serialized_parameter_leak",)
```

Add table-driven cases for non-object outcome, extra fields, phase-mismatched positive verdict, empty findings, and contradictory root/reviewer/tester fields.

- [ ] **Step 2: Run the kernel tests to verify failure**

Run: `scripts/run_tests.sh tests/hermes_cli/test_kanban_product_outcomes.py -q`

Expected: FAIL because `hermes_cli.kanban_product_outcomes` does not exist.

- [ ] **Step 3: Implement the typed kernel**

```python
@dataclass(frozen=True)
class TerminalOutcome:
    verdict: Literal["passed", "approved", "changes_requested", "architecture_invalid"]
    target_step: str | None
    findings: tuple[str, ...]
    observations: tuple[Literal["serialized_parameter_leak"], ...]


class OutcomeValidationError(ValueError):
    def __init__(self, code: str, *, qualifier: str | None = None):
        self.code = code
        self.qualifier = qualifier
        super().__init__(code)


class ProductOutcomeError(ValueError):
    def __init__(
        self, task_id: str, run_id: int, phase: str,
        code: str, qualifier: str | None,
    ):
        self.task_id = task_id
        self.run_id = run_id
        self.phase = phase
        self.code = code
        self.qualifier = qualifier
        super().__init__(code)


def validate_terminal_outcome(
    *, task_id: str, run_id: int, phase: str, summary: str | None,
    result: str | None, metadata: Mapping[str, object] | None,
) -> TerminalOutcome:
    marker = _has_serialized_parameter_marker(summary, result)
    canonical = metadata.get("workflow_outcome") if isinstance(metadata, Mapping) else None
    if canonical is None:
        raise OutcomeValidationError(
            "missing", qualifier="serialized_parameter" if marker else None
        )
    outcome = _validate_exact_shape(phase, canonical)
    _validate_redundant_fields(phase, outcome, metadata)
    return replace(
        outcome,
        observations=("serialized_parameter_leak",) if marker else (),
    )
```

- [ ] **Step 4: Put the kernel only in `complete_task`'s verdict-bearing v2 Test/Review path**

Load the current task/run read-only. When the entrypoint is ordinary
`complete_task` and the active phase is Test or Review, call the kernel before
existing routing/provenance code. Do not call the kernel from `_end_run`,
`handoff`, a generic run finalizer, or `resolve_product_preflight`.

`resolve_product_preflight` is the privileged `kanban_resolve` route: its
worker is pinned to the exact `resolver_readonly` toolset and cannot call
`kanban_complete`. It must continue to close non-verdict terminal runs as
`preflight_repaired`, `preflight_resolved`, or `preflight_escalated` without canonical
`workflow_outcome`.

This exemption is structural, not value-based. `complete_task` selects the
kernel solely from its ordinary-completion entrypoint and the active Test or
Review phase. It must never inspect caller-supplied `metadata.outcome`,
`metadata.run_outcome`, `metadata.completion_outcome`, or any claimed
persisted run outcome to bypass validation. Only
`resolve_product_preflight` may author the three Resolver outcomes through
its independently-authorized path.

On ordinary-completion rejection, append one safe event in its own transaction
and raise a typed `ProductOutcomeError`; do not close the run:

```python
except OutcomeValidationError as exc:
    with write_txn(conn):
        _append_event(conn, task_id, "completion_rejected_outcome", {
            "run_id": expected_run_id,
            "phase": current_step,
            "code": exc.code,
            "qualifier": exc.qualifier,
        }, run_id=expected_run_id)
    raise ProductOutcomeError(task_id, expected_run_id, current_step, exc.code, exc.qualifier)
```

For accepted marker-bearing input, append `serialized_parameter_leak` with only task/run/phase and continue through the existing positive/rework route.

- [ ] **Step 5: Add mutation-order and tool-safety tests**

Assert runs 304 and 407 both reject without mutation, proving the behavior is
not card-specific. Assert the tool response includes `missing` and
`serialized_parameter` but excludes summary, payload, findings, digest, and
full metadata. Assert run 410 advances and records the leak observation.

Replay the resolver-repair shapes from runs 354 and 369 through
`resolve_product_preflight`; assert they close as `preflight_repaired`, retain
no canonical outcome, and route exactly as before without invoking the
kernel. Add the same behavior assertions for `preflight_resolved` and
`preflight_escalated` using the existing resolver-resume and escalation
fixtures.

Add a behavior-level impersonation test, parameterized over active Test and
Review phases and all three privileged strings. Call ordinary `complete_task`
with no canonical `workflow_outcome`, but place the claimed value in each of
`metadata.outcome`, `metadata.run_outcome`, and
`metadata.completion_outcome`. Assert `ProductOutcomeError.code == "missing"`,
the rejection event is safe, and the task/run snapshot is unchanged. The test
must exercise the public behavior; it must not inspect implementation source.

Then simulate an ordinary worker clean exit
after a rejected `kanban_complete` without successful retry and assert the
existing watcher closes the run as `crashed`, appends a run-scoped
`protocol_violation`, and leaves the product card in Review/blocked so the
missing terminal protocol is attributable.

- [ ] **Step 6: Run focused behavior tests**

Run: `scripts/run_tests.sh tests/hermes_cli/test_kanban_product_outcomes.py tests/hermes_cli/test_kanban_db.py tests/tools/test_kanban_tools.py -q`

Expected: PASS.

- [ ] **Step 7: Commit the fail-closed envelope**

```bash
git add hermes_cli/kanban_product_outcomes.py hermes_cli/kanban_db.py tools/kanban_tools.py tests/hermes_cli/test_kanban_product_outcomes.py tests/hermes_cli/test_kanban_db.py tests/tools/test_kanban_tools.py
git commit -m "fix: validate canonical product outcomes before mutation"
```

### Task 3: Derive latest immutable Test and Review authority

**Files:**
- Modify: `hermes_cli/kanban_product_outcomes.py`
- Modify: `hermes_cli/kanban_db.py` (`_latest_approved_review_candidate`, `_release_run_evidence`, candidate callers)
- Test: `tests/hermes_cli/test_kanban_product_outcomes.py`
- Test: `tests/hermes_cli/test_kanban_release_evidence.py`

**Interfaces:**
- Consumes: ordered ended-run records with dispatcher-pinned `test_branch`, `test_head_sha`, `review_branch`, `review_base_sha`, and `review_head_sha`.
- Produces: `latest_review_authority(runs) -> ApprovedCandidate | None` and `latest_test_authority(runs, source_sha) -> PassedTest | None`.

- [ ] **Step 1: Write failing latest-run tests**

```python
def test_later_review_rejection_invalidates_older_approval():
    runs = [approved_review(run_id=1, sha=SHA_A), rejected_review(run_id=2, sha=SHA_A)]
    assert latest_review_authority(runs) is None


def test_later_test_rejection_invalidates_older_pass():
    runs = [passed_test(run_id=1, sha=SHA_A), rejected_test(run_id=2, sha=SHA_A)]
    assert latest_test_authority(runs, SHA_A) is None


def test_authority_requires_exact_full_sha():
    runs = [approved_review(run_id=1, sha="a" * 12)]
    assert latest_review_authority(runs) is None
```

- [ ] **Step 2: Run the authority tests to verify failure**

Run: `scripts/run_tests.sh tests/hermes_cli/test_kanban_product_outcomes.py tests/hermes_cli/test_kanban_release_evidence.py -q`

Expected: FAIL on backward-scanning behavior or missing functions.

- [ ] **Step 3: Implement latest-ended-run selection**

```python
@dataclass(frozen=True)
class ApprovedCandidate:
    run_id: int
    branch: str
    base_sha: str
    source_sha: str
    reviewer_provider: str
    writer_provider: str


def latest_review_authority(runs: Sequence[TerminalRunRecord]) -> ApprovedCandidate | None:
    latest = next((run for run in reversed(runs) if run.phase == "review"), None)
    if latest is None or latest.outcome.verdict != "approved":
        return None
    return _approved_candidate_from_pinned_run(latest)
```

Implement Test with the same single-latest-run rule and exact `source_sha == test_head_sha`. Reject absent identity, same-provider writer/reviewer, non-full SHAs, worker-authored branch aliases, and mismatched phase.

- [ ] **Step 4: Replace backward scans in DB adapters**

Convert ended `Run` rows to `TerminalRunRecord`, call the kernel once, and make `_release_run_evidence` compare its requested branch/SHA to the returned latest authorities. Preserve its independent writer/tester/reviewer checks and existing `ReleaseEvidenceError.missing` vocabulary.

- [ ] **Step 5: Run the focused suites**

Run: `scripts/run_tests.sh tests/hermes_cli/test_kanban_product_outcomes.py tests/hermes_cli/test_kanban_release_evidence.py tests/hermes_cli/test_kanban_db.py -q`

Expected: PASS, including older release-evidence regression cases.

- [ ] **Step 6: Commit immutable authority**

```bash
git add hermes_cli/kanban_product_outcomes.py hermes_cli/kanban_db.py tests/hermes_cli/test_kanban_product_outcomes.py tests/hermes_cli/test_kanban_release_evidence.py tests/hermes_cli/test_kanban_db.py
git commit -m "fix: derive latest immutable workflow authority"
```

### Task 4: Reject empty new integration candidates

**Files:**
- Modify: `hermes_cli/kanban_product_outcomes.py`
- Modify: `hermes_cli/kanban_db.py` (`_build_verified_merge_candidate`, integration insert callers)
- Test: `tests/hermes_cli/test_kanban_db.py`

**Interfaces:**
- Consumes: pinned `review_base_sha`, `review_head_sha`, reviewed source SHA, latest Test authority, latest Review authority, and existing composite integration row.
- Produces: `candidate_eligibility(...) -> CandidateEligibility` or `CandidateEligibilityError(code="empty_contribution" | "stale_review")`.

- [ ] **Step 1: Write failing real-Git tests**

Create a temporary repository in which `review_base_sha == review_head_sha`, then assert no integration intent/fact is inserted. Add a replay case proving an already-existing `(epic_id, story_id, source_sha)` fact remains idempotently accepted.

- [ ] **Step 2: Run the candidate tests to verify failure**

Run: `scripts/run_tests.sh tests/hermes_cli/test_kanban_db.py -k 'empty_contribution or integration_replay' -q`

Expected: FAIL because the ancestor/no-op path currently permits a new fact.

- [ ] **Step 3: Add the eligibility guard before intent/fact creation**

```python
def candidate_eligibility(repo: Path, approved: ApprovedCandidate, passed: PassedTest) -> CandidateEligibility:
    if approved.source_sha != passed.source_sha:
        raise CandidateEligibilityError("stale_review")
    result = subprocess.run(
        ["git", "-C", str(repo), "diff", "--quiet", approved.base_sha, approved.source_sha],
        check=False,
    )
    if result.returncode == 0:
        raise CandidateEligibilityError("empty_contribution")
    if result.returncode != 1:
        raise CandidateEligibilityError("io_error")
    return CandidateEligibility(source_sha=approved.source_sha, non_empty=True)
```

Check exact existing composite fact/prepared intent before this guard only for crash replay; a bare ancestor relation cannot bypass it.

- [ ] **Step 4: Run the candidate and release suites**

Run: `scripts/run_tests.sh tests/hermes_cli/test_kanban_db.py tests/hermes_cli/test_kanban_release_evidence.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the no-op guard**

```bash
git add hermes_cli/kanban_product_outcomes.py hermes_cli/kanban_db.py tests/hermes_cli/test_kanban_db.py
git commit -m "fix: reject empty integration candidates"
```

This commit ends the first critical implementation slice.

### Task 5: Persist first-class rework directives

**Files:**
- Modify: `hermes_cli/kanban_db.py` (schema/migration, `_route_product_rework_if_requested`, `build_worker_context`)
- Test: `tests/hermes_cli/test_kanban_db.py`
- Test: `tests/e2e/test_kanban_product_recovery_flow.py`

**Interfaces:**
- Consumes: validated `TerminalOutcome`, origin phase/run/intent, rejected branch/SHA, Epic tip, and findings.
- Produces: `create_rework_directive(...)`, `active_rework_directive(...)`, `resolve_rework_directive(...)` and a `## Required rework directive` worker-context section.

- [ ] **Step 1: Write failing schema, routing, and context tests**

Assert one active directive per task, append-only supersession, atomic Test/Review rework routing, Development visibility before prior attempts, and resolution only when the Development handoff SHA differs from `rejected_sha`.

- [ ] **Step 2: Run tests to verify failure**

Run: `scripts/run_tests.sh tests/hermes_cli/test_kanban_db.py tests/e2e/test_kanban_product_recovery_flow.py -k 'rework_directive' -q`

Expected: FAIL because the table/context section does not exist.

- [ ] **Step 3: Add the table and partial unique index**

Use the final-design columns exactly and add:

```sql
CREATE UNIQUE INDEX IF NOT EXISTS idx_product_rework_directives_active
ON product_rework_directives(task_id)
WHERE status = 'active';
```

Create/supersede the directive in the same transaction that closes the rejection run and moves the task.

- [ ] **Step 4: Render and resolve directives**

Render origin, target phase, rejected branch/full SHA, Epic tip/full SHA, and every finding. On Development handoff, resolve by CAS only when the new full SHA differs; `architecture_invalid` stays active through Architecture.

- [ ] **Step 5: Run focused and E2E suites**

Run: `scripts/run_tests.sh tests/hermes_cli/test_kanban_db.py tests/e2e/test_kanban_product_recovery_flow.py -q`

Expected: PASS.

- [ ] **Step 6: Commit durable directives**

```bash
git add hermes_cli/kanban_db.py tests/hermes_cli/test_kanban_db.py tests/e2e/test_kanban_product_recovery_flow.py
git commit -m "feat: persist product rework directives"
```

### Task 6: Add the narrow operator recovery verb

**Files:**
- Modify: `hermes_cli/kanban_db.py`
- Modify: `hermes_cli/kanban.py`
- Test: `tests/hermes_cli/test_kanban_db.py`
- Test: `tests/hermes_cli/test_kanban_cli.py`

**Interfaces:**
- Consumes: exact task ID, expected status `done`, expected `completed_at`, expected current phase, expected latest event ID, actor, and reason.
- Produces: `clear_terminal_state(conn, request: ClearTerminalStateRequest) -> bool` and CLI `hermes kanban clear-terminal-state`.

- [ ] **Step 1: Write failing CAS and refusal tests**

Cover a successful clear, stale timestamp, stale event ID, non-`done` status, already-terminal phase, empty actor/reason, and attempts to change phase/assignee/evidence.

- [ ] **Step 2: Run tests to verify failure**

Run: `scripts/run_tests.sh tests/hermes_cli/test_kanban_db.py tests/hermes_cli/test_kanban_cli.py -k 'clear_terminal_state' -q`

Expected: FAIL because the verb does not exist.

- [ ] **Step 3: Implement the literal CAS**

```python
@dataclass(frozen=True)
class ClearTerminalStateRequest:
    task_id: str
    expected_completed_at: int
    expected_phase: str
    expected_latest_event_id: int
    actor: str
    reason: str
```

Inside one authorized write transaction, verify the snapshot, refuse `current_step_key == "done"`, derive generic status from the stored non-terminal phase, set only `status` and `completed_at`, then append `terminal_state_cleared` with the expected snapshot.

- [ ] **Step 4: Add an explicit CLI parser and structured output**

Require every expected value as an option; do not default actor, phase, timestamp, event ID, or reason. Return nonzero on CAS loss and print no payload/evidence bodies.

- [ ] **Step 5: Run DB and CLI tests**

Run: `scripts/run_tests.sh tests/hermes_cli/test_kanban_db.py tests/hermes_cli/test_kanban_cli.py -q`

Expected: PASS.

- [ ] **Step 6: Commit the recovery verb**

```bash
git add hermes_cli/kanban_db.py hermes_cli/kanban.py tests/hermes_cli/test_kanban_db.py tests/hermes_cli/test_kanban_cli.py
git commit -m "feat: add narrow terminal-state recovery"
```
