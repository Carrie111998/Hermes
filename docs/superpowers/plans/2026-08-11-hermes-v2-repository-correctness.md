# Hermes v2 Repository Correctness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace ambient Git state and hard-coded verification with a validated board repository contract, dispatcher-owned refresh, and immutable Test/Review candidates.

**Architecture:** A focused repository service owns all ref resolution, worktree isolation, candidate preparation, verification, and Git error classification without changing task lifecycle state. `kanban_db.py` remains the coordinator: it persists pins and applies CAS transitions using typed repository results.

**Tech Stack:** Python dataclasses, Git CLI, subprocess execution, SQLite/WAL, pytest with real temporary repositories through `scripts/run_tests.sh`.

## Global Constraints

- Never issue `git push`, force-push, or any remote ref write.
- Governed Epic paths have no ambient-`HEAD` or `scripts/run_tests.sh` fallback.
- Every ref is resolved to a full 40-character commit SHA before use.
- Candidate verification runs only from the normalized board repository contract.
- Dirty user worktrees are preserved and rejected; no reset/clean/stash is permitted.
- Configuration, infrastructure, and candidate-test failures are distinct typed results.
- Test and Review do not author source commits; source/fixture changes route to Development.
- `boundary_evidence.generated_paths` is the only tracked-mutation allowlist for evidence phases.
- Development's commit-first behavior is unchanged.
- Use real temporary Git repositories and executable fixture commands.
- Use `scripts/run_tests.sh`; never invoke `pytest` directly.

---

### Task 1: Parse and normalize the repository contract

**Files:**
- Create: `hermes_cli/kanban_repository.py`
- Modify: `hermes_cli/kanban_db.py` (board metadata validation entry points)
- Create: `tests/hermes_cli/test_kanban_repository.py`
- Modify: `tests/hermes_cli/test_kanban_db.py`

**Interfaces:**
- Consumes: `board_metadata["repository"]`.
- Produces: `load_repository_contract(board_metadata: Mapping[str, object], *, repo_root: Path) -> RepositoryContract` and `RepositoryConfigurationError.code`.

- [ ] **Step 1: Write failing exact-schema tests**

Cover the accepted contract, absent/malformed `base_ref`, `target_branch`, profile commands, workdirs, timeouts, required CI workflows, and generated paths that are absolute, escape via `..`, or are untracked.

```python
def test_contract_normalizes_commands_and_generated_paths(repo, board_meta):
    contract = load_repository_contract(board_meta, repo_root=repo)
    assert contract.base_ref == "refs/remotes/origin/main"
    assert contract.target_branch == "main"
    assert contract.generated_paths == (
        PurePosixPath("dashboard/index.html"),
        PurePosixPath("dashboard/data.json"),
    )
    assert contract.verification["story_integration"].commands[0].argv == (
        "bash", "scripts/run_tests.sh",
    )
```

- [ ] **Step 2: Run tests to verify failure**

Run: `scripts/run_tests.sh tests/hermes_cli/test_kanban_repository.py -q`

Expected: FAIL because the module does not exist.

- [ ] **Step 3: Implement immutable contract types and validation**

```python
@dataclass(frozen=True)
class VerificationCommand:
    argv: tuple[str, ...]
    workdir: PurePosixPath
    timeout_seconds: int


@dataclass(frozen=True)
class RepositoryContract:
    repo_root: Path
    base_ref: str
    target_branch: str
    verification: Mapping[str, VerificationProfile]
    generated_paths: tuple[PurePosixPath, ...]
    ci_workflows: tuple[str, ...]
    digest: str
```

Reject unknown keys and normalize a canonical JSON representation before computing the SHA-256 contract digest. Resolve every generated path under `repo_root` and require `git ls-files --error-unmatch -- <path>` to succeed.

- [ ] **Step 4: Run contract and board-metadata tests**

Run: `scripts/run_tests.sh tests/hermes_cli/test_kanban_repository.py tests/hermes_cli/test_kanban_db.py -k 'repository_contract or board_metadata' -q`

Expected: PASS.

- [ ] **Step 5: Commit repository policy types**

```bash
git add hermes_cli/kanban_repository.py hermes_cli/kanban_db.py tests/hermes_cli/test_kanban_repository.py tests/hermes_cli/test_kanban_db.py
git commit -m "feat: validate v2 repository contracts"
```

### Task 2: Pin deterministic Epic base and remove governed fallbacks

**Files:**
- Modify: `hermes_cli/kanban_repository.py`
- Modify: `hermes_cli/kanban_db.py` (`materialize_epic`/Epic base-pin path, `_default_epic_verify` caller)
- Test: `tests/hermes_cli/test_kanban_repository.py`
- Test: `tests/hermes_cli/test_kanban_epics.py`

**Interfaces:**
- Consumes: `RepositoryContract.base_ref` and repository root.
- Produces: `resolve_commit(repo_root: Path, ref: str) -> str` and persisted full-SHA Epic base-pin evidence.

- [ ] **Step 1: Write failing branch-independence tests**

Create a repository whose checked-out branch is not the configured base. Assert first Epic materialization pins `git rev-parse --verify <base_ref>^{commit}`. Add missing/ambiguous ref cases expecting `RepositoryConfigurationError("missing_ref")` and no Epic branch/event mutation.

- [ ] **Step 2: Run tests to verify failure**

Run: `scripts/run_tests.sh tests/hermes_cli/test_kanban_repository.py tests/hermes_cli/test_kanban_epics.py -k 'base_ref or epic_base' -q`

Expected: FAIL on the ambient-HEAD behavior.

- [ ] **Step 3: Implement strict ref resolution and wire the governed path**

```python
def resolve_commit(repo_root: Path, ref: str) -> str:
    completed = _run_git(repo_root, "rev-parse", "--verify", f"{ref}^{{commit}}")
    sha = completed.stdout.strip()
    if not FULL_SHA.fullmatch(sha):
        raise RepositoryConfigurationError("missing_ref")
    return sha
```

Use the returned SHA in the existing Epic base-pin event. Retain ambient behavior only in the explicitly non-v2 branch.

- [ ] **Step 4: Run Epic and repository tests**

Run: `scripts/run_tests.sh tests/hermes_cli/test_kanban_repository.py tests/hermes_cli/test_kanban_epics.py -q`

Expected: PASS.

- [ ] **Step 5: Commit deterministic base selection**

```bash
git add hermes_cli/kanban_repository.py hermes_cli/kanban_db.py tests/hermes_cli/test_kanban_repository.py tests/hermes_cli/test_kanban_epics.py
git commit -m "fix: pin governed epic base refs"
```

### Task 3: Add dispatcher-owned story refresh

**Files:**
- Modify: `hermes_cli/kanban_repository.py`
- Modify: `hermes_cli/kanban_db.py` (Architecture/Development pre-dispatch path)
- Test: `tests/hermes_cli/test_kanban_repository.py`
- Test: `tests/e2e/test_kanban_product_recovery_flow.py`

**Interfaces:**
- Consumes: `RefreshRequest(repo_root, story_id, story_branch, story_worktree, story_sha, epic_branch, epic_tip_sha)`.
- Produces: `refresh_story_branch(request) -> RefreshResult` with kinds `unchanged`, `refreshed`, `dirty`, `conflict`, or `source_moved`.

- [ ] **Step 1: Write failing real-repository refresh tests**

Cover ancestor/no-op, clean merge/CAS, dirty original preservation, retained detached conflict worktree, and a source branch moved between preparation and CAS.

```python
def test_dirty_story_worktree_is_preserved(repo_with_epic_and_story):
    before = status_porcelain(repo_with_epic_and_story.story_worktree)
    result = refresh_story_branch(repo_with_epic_and_story.request)
    assert result.kind == "dirty"
    assert status_porcelain(repo_with_epic_and_story.story_worktree) == before
    assert rev_parse(repo_with_epic_and_story.repo, result.story_branch) == result.story_sha
```

- [ ] **Step 2: Run tests to verify failure**

Run: `scripts/run_tests.sh tests/hermes_cli/test_kanban_repository.py -k 'refresh' -q`

Expected: FAIL because refresh is worker-owned or absent.

- [ ] **Step 3: Implement isolated refresh and branch CAS**

Use `git worktree add --detach` at the pinned story SHA, merge the pinned Epic tip, and update with:

```bash
git update-ref refs/heads/<story-branch> <refreshed-sha> <story-sha>
```

Delete a clean temporary worktree after success. Retain the conflict worktree and list conflict paths. Never mutate the original dirty worktree.

- [ ] **Step 4: Wire pre-dispatch outcomes**

Before first Architecture and every Development dispatch:

- `unchanged` dispatches with pinned evidence;
- `refreshed` records old/new story SHA and invalidates old Test/Review authority;
- `dirty` emits `story_refresh_attention_required` without claim;
- `conflict` creates a Development directive with original SHA, Epic tip, conflict paths, and retained workspace;
- `source_moved` retries only on the next dispatcher tick.

- [ ] **Step 5: Run focused and recovery E2E suites**

Run: `scripts/run_tests.sh tests/hermes_cli/test_kanban_repository.py tests/e2e/test_kanban_product_recovery_flow.py -q`

Expected: PASS.

- [ ] **Step 6: Commit dispatcher refresh**

```bash
git add hermes_cli/kanban_repository.py hermes_cli/kanban_db.py tests/hermes_cli/test_kanban_repository.py tests/e2e/test_kanban_product_recovery_flow.py
git commit -m "feat: refresh epic stories before dispatch"
```

### Task 4: Run configured verification in isolated candidates

**Files:**
- Modify: `hermes_cli/kanban_repository.py`
- Modify: `hermes_cli/kanban_db.py` (`_build_verified_merge_candidate`, `_default_epic_verify` replacement path)
- Test: `tests/hermes_cli/test_kanban_repository.py`
- Test: `tests/hermes_cli/test_kanban_db.py`

**Interfaces:**
- Consumes: `VerificationProfile`, candidate path/SHA, contract digest, scope, and subject ID.
- Produces: `run_verification(...) -> VerificationResult` with status `passed`, `failed`, `configuration_error`, or `infrastructure_error` and bounded step results.

- [ ] **Step 1: Write failing executable-command tests**

Use fixture scripts to prove exact argv/workdir order, timeout classification, nonzero-as-`failed`, missing executable/profile-as-`configuration_error`, and OS/process failure-as-`infrastructure_error`. Assert output tails are capped and environment values are absent.

- [ ] **Step 2: Run tests to verify failure**

Run: `scripts/run_tests.sh tests/hermes_cli/test_kanban_repository.py -k 'verification' -q`

Expected: FAIL because `_default_epic_verify` hard-codes `scripts/run_tests.sh` and returns `bool`.

- [ ] **Step 3: Implement typed verification**

```python
@dataclass(frozen=True)
class VerificationResult:
    status: Literal["passed", "failed", "configuration_error", "infrastructure_error"]
    source_sha: str
    candidate_sha: str
    contract_digest: str
    profile: str
    steps: tuple[VerificationStepResult, ...]
```

Resolve each executable before running, use `shell=False`, pass only explicit argv, enforce per-step timeout, and cap redacted stdout/stderr tails. Stop at the first non-passing step.

- [ ] **Step 4: Replace governed verification callers**

Persist the typed result; only `failed` creates product rework. Park configuration/infrastructure results as attention-required without incrementing `rework_count`. Keep legacy verifier behavior solely outside v2.

- [ ] **Step 5: Run repository and candidate suites**

Run: `scripts/run_tests.sh tests/hermes_cli/test_kanban_repository.py tests/hermes_cli/test_kanban_db.py -k 'verification or merge_candidate' -q`

Expected: PASS.

- [ ] **Step 6: Commit configured verification**

```bash
git add hermes_cli/kanban_repository.py hermes_cli/kanban_db.py tests/hermes_cli/test_kanban_repository.py tests/hermes_cli/test_kanban_db.py
git commit -m "feat: verify candidates from repository policy"
```

### Task 5: Pin Test/Review and remove evidence-phase commits

**Files:**
- Modify: `hermes_cli/kanban_db.py` (`handoff`, `_commit_worker_diff` call site, `_prepare_review_target`, worker launch preparation)
- Modify: `hermes_cli/kanban_product_outcomes.py`
- Modify: `hermes_cli/kanban_repository.py`
- Modify: `hermes_cli/kanban_db.py` (`build_worker_context` evidence-phase instructions)
- Test: `tests/hermes_cli/test_kanban_db.py`
- Test: `tests/hermes_cli/test_kanban_product_outcomes.py`
- Test: `tests/e2e/test_kanban_product_recovery_flow.py`

**Interfaces:**
- Consumes: repository contract generated paths, Test/Review workspace, active run, and pinned exact branch/head SHA.
- Produces: `_prepare_test_target(...)`, extended `_prepare_review_target(...)`, `EvidenceWorkspaceResult`, and exact latest-Test/Review authority.

- [ ] **Step 1: Preserve the measured production baseline in behavior tests**

Document in test comments that ended handoffs measured 56 Test runs with six SHAs and 76 Review runs with zero SHAs. Add fixtures for source mutation, generated tracked mutation, artifact-only untracked output, and clean evidence phases; assert the intended new behavior rather than counts.

- [ ] **Step 2: Write failing pin/cleanliness tests**

Assert Test launch stamps `test_branch`/`test_head_sha`; Review launch stamps `review_branch` as well as base/head; branch movement rejects completion; undeclared tracked edits reject; declared generated tracked paths are recorded then restored to the pinned SHA; non-ignored untracked output rejects completion and is preserved for diagnosis.

- [ ] **Step 3: Run tests to verify failure**

Run: `scripts/run_tests.sh tests/hermes_cli/test_kanban_db.py tests/hermes_cli/test_kanban_product_outcomes.py -k 'test_target or review_target or evidence_workspace' -q`

Expected: FAIL because Test is not pinned and `handoff` commits evidence-phase diffs.

- [ ] **Step 4: Implement pinning and generated-path restoration**

At launch, require clean tracked index/worktree and store dispatcher-owned branch/full SHA. At completion:

```python
result = inspect_evidence_workspace(workspace, pinned_sha, contract.generated_paths)
if result.undeclared_tracked or result.branch_head != pinned_sha:
    raise EvidenceWorkspaceError("source_moved")
record_generated_mutations(conn, run_id, result.declared_generated)
restore_generated_paths(workspace, pinned_sha, result.declared_generated)
```

The restore helper accepts only contract-validated tracked paths and uses explicit pathspecs; it cannot reset the repository or arbitrary directories.

- [ ] **Step 5: Change the handoff commit policy in one isolated code commit**

Replace the unconditional call with:

```python
sha = _commit_worker_diff(conn, task_id) if step == "development" else pinned_evidence_sha
```

Test/Review source or fixture edits create Development rework through the canonical outcome path. Update worker guidance to say evidence workers never commit and must report required edits as findings.

- [ ] **Step 6: Run all handoff, evidence, and recovery suites**

Run: `scripts/run_tests.sh tests/hermes_cli/test_kanban_db.py tests/hermes_cli/test_kanban_product_outcomes.py tests/hermes_cli/test_kanban_release_evidence.py tests/e2e/test_kanban_product_recovery_flow.py -q`

Expected: PASS; Development no-commit remains refused.

- [ ] **Step 7: Commit this change by itself**

```bash
git add hermes_cli/kanban_db.py hermes_cli/kanban_product_outcomes.py hermes_cli/kanban_repository.py tests/hermes_cli/test_kanban_db.py tests/hermes_cli/test_kanban_product_outcomes.py tests/e2e/test_kanban_product_recovery_flow.py
git commit -m "fix: keep evidence phases source immutable"
```

Do not combine this commit with outcome parsing, repository-contract parsing, integration schema, or intake work.

### Task 6: Verify the repository boundary end to end

**Files:**
- Modify: `tests/hermes_cli/test_kanban_repository.py`
- Modify: `tests/e2e/test_kanban_product_recovery_flow.py`
- Create: `docs/hermes-kanban-v2.md`

**Interfaces:**
- Consumes: the complete repository service and DB adapters.
- Produces: migration/operator guidance and a real-Git E2E proof of refresh → Development → Test → Review candidate immutability.

- [ ] **Step 1: Add the complete real-Git scenario**

Build a temporary remote and local repository, configure a non-checked-out base ref, refresh a story over a newer Epic tip, run configured commands, pin Test and Review, and verify the final exact SHA authorities. Include dirty/conflict variants and assert no remote ref changes.

- [ ] **Step 2: Add operator documentation**

Document the required `repository` metadata fields, generated-path allowlist rules, configuration/infrastructure failure meaning, retained conflict worktrees, and the Test/Review no-commit behavior.

- [ ] **Step 3: Run the repository E2E suite**

Run: `scripts/run_tests.sh tests/hermes_cli/test_kanban_repository.py tests/e2e/test_kanban_product_recovery_flow.py -q`

Expected: PASS.

- [ ] **Step 4: Prove no remote-write path exists**

Run: `rg -n "git push|push --force|update-ref.*refs/remotes" hermes_cli/kanban_repository.py hermes_cli/kanban_db.py tests/hermes_cli/test_kanban_repository.py tests/e2e/test_kanban_product_recovery_flow.py`

Expected: matches only explicit refusal assertions/test data, or no matches.

- [ ] **Step 5: Commit E2E coverage and documentation**

```bash
git add tests/hermes_cli/test_kanban_repository.py tests/e2e/test_kanban_product_recovery_flow.py docs/hermes-kanban-v2.md
git commit -m "test: prove v2 repository boundaries end to end"
```
