# Certified Cumulative Merge Train Design

**Status:** Approved design; production implementation is explicitly out of scope for this commit.

**Base:** `NousResearch/hermes-agent` `origin/main` at `74cb4cb80c7`.

## Summary

The merge controller will certify a bounded ordered batch of pull requests against one frozen
base. It constructs a cumulative tree for every prefix, runs exact-tree CI in bounded parallel,
and publishes only the already-certified prefixes through sequential, SHA-fenced squash merges.
Canonical GitHub state remains authoritative. The ledger coordinates work and recovery but cannot
declare a pull request merged, a check passing, or a review resolved by itself.

The controller is deterministic and belongs at the plugin/control-plane edge. It does not add a
model tool or depend on an agent to decide safety. Model-driven feedback and repair workers remain
outside the train and may only produce a new candidate head for later deterministic admission.

## Problem

Serially repairing, auditing, and merging every pull request wastes time when many independent
changes are ready. Naive parallel merging is unsafe because every merge advances the base and can
invalidate CI for the remaining candidates. GitHub auto-merge alone also cannot prove that the
tree being merged is the cumulative tree that local CI certified.

The system therefore needs to gain parallelism in preparation and CI without gaining parallelism
at the irreversible publication boundary.

## Goals

- Freeze a precise base commit, policy digest, and ordered candidate set for each train epoch.
- Build cumulative prefix trees deterministically from immutable candidate heads.
- Run CI for independent prefix trees concurrently within explicit host and provider budgets.
- Squash-merge candidates one at a time, proving each published tree equals its certified prefix.
- Invalidate only the affected suffix when a candidate changes, while invalidating the whole epoch
  when the external base or governing policy changes.
- Recover idempotently from crashes before, during, or after GitHub mutation.
- Prevent duplicate scans, duplicate CI, duplicate comments, duplicate repair dispatch, and
  duplicate merge attempts.
- Keep failed, conflicted, ambiguous, or actively repaired pull requests out of the publication
  lane without blocking unrelated eligible work.
- Preserve strict scope: the train may certify and merge admitted changes, not repair, rewrite,
  approve, dismiss review, change repository settings, or weaken CI.

## Non-goals

- Replacing GitHub as the canonical source of pull request, review, check, or merge truth.
- Automatically resolving source conflicts or review comments.
- Force-pushing contributor branches or rewriting published history.
- Merging in parallel.
- Treating a narrative comment, Kanban result, agent claim, or local ledger row as CI authority.
- Bypassing branch protection, required reviews, exact-head checks, egress policy, or operator gates.
- Adding repository-specific automation to the Hermes core.

## Safety invariants

1. **One frozen external base per epoch.** An epoch starts at `B0`. Any non-train movement of the
   target branch invalidates the epoch before another merge is attempted.
2. **Immutable candidate identity.** A candidate is `(repository, pr_number, head_sha,
   patch_digest, admission_digest)`. A new head is a new candidate.
3. **Prefix certification.** Step `Si` certifies the cumulative tree after candidates `C1..Ci`.
   A receipt for `Si` cannot certify another step, order, base, manifest, or policy.
4. **No publication gaps.** `Ci` cannot publish until `C1..C(i-1)` are published and canonical
   GitHub truth matches their expected commits and trees.
5. **Single writer.** At most one valid publication lease may mutate a repository/base branch.
6. **Fence every write.** Every GitHub mutation is preceded by fresh canonical reads and a
   comparison to the lease generation, exact PR head, expected base commit, reviews, checks, and
   policy digest.
7. **Tree equality is the merge proof.** A successful API response is not sufficient. The squash
   commit must have the expected parent and certified tree.
8. **Failure closes the gate.** Unknown mergeability, incomplete process evidence, missing or stale
   CI, unavailable canonical state, or ambiguous reconciliation stops publication.
9. **Repair is not merge authority.** A repair worker can push a new head and report evidence; it
   cannot retain an old train slot or make itself merge-ready.
10. **No automatic destructive recovery.** A post-merge tree mismatch halts the train and requires
    operator review. The controller never auto-reverts or force-resets the protected branch.

## Terminology

- **Epoch:** One certification campaign rooted at frozen base `B0`.
- **Candidate:** One admitted pull request at an immutable head.
- **Patch:** The candidate's normalized change relative to its verified merge base.
- **Prefix tree:** The repository tree after applying candidates `C1..Ci` in order.
- **Step:** The typed record binding one candidate to its prefix parent and prefix tree.
- **Certification receipt:** Exact-tree CI evidence for one step.
- **Publication:** The sequential GitHub squash merge of a certified step.
- **Suffix invalidation:** Retiring step `Si` and every later step because a prefix input changed.

## Chosen architecture

### Why cumulative prefixes

Three approaches were considered:

1. **Independent CI on every PR head.** Fast, but it does not test interactions and every result
   becomes stale after the first merge.
2. **One combined batch branch.** Tests interactions, but a single failure blocks diagnosis and the
   batch cannot prove which sequential squash prefix is safe.
3. **Cumulative prefix trees with bounded parallel CI.** Each tree corresponds to an exact future
   publication state. Failures identify the first unsafe prefix, while already-certified earlier
   prefixes can still publish.

The third approach is selected. Tree construction is ordered and cheap; CI execution is the
expensive portion and can run in bounded parallel after the trees exist.

### Component boundaries

The implementation is expected to live in an installable merge-maintenance plugin, with no new
core model tool.

- **Admission reader:** Reads canonical PR state, reviews, feedback, labels, check state, and
  repository policy. Produces typed eligibility decisions and reason codes.
- **Epoch planner:** Freezes `B0`, orders candidates, calculates policy and manifest digests, and
  creates immutable candidate records.
- **Tree builder:** Uses disposable exact-object worktrees to apply normalized candidate patches in
  order and records parent/tree/diff identities. It never pushes contributor branches.
- **CI scheduler:** Claims exact step identities and runs repository-owned commands with bounded
  concurrency, resource budgets, durable lifecycle receipts, and real supervisor process identity.
- **Publication controller:** Holds the singleton repository/base lease, revalidates canonical
  state, performs one squash merge, and verifies the resulting commit and tree.
- **Reconciler:** Repairs ledger state from canonical GitHub truth after crashes or partial failures.
- **Observer:** Exposes typed status and reason codes without granting mutation authority.

Each component consumes and produces typed records. Free-form agent output is never an input to
admission, CI acceptance, or publication.

## Data model

All mutable rows carry `lease_version` and `updated_at`. All immutable evidence carries a schema
version and content digest. Timestamps are timezone-aware UTC values.

### `merge_train_epochs`

| Field | Meaning |
|---|---|
| `epoch_id` | Hash of repository, base branch, frozen base, policy digest, and nonce |
| `repository` / `base_branch` | Exact GitHub target |
| `frozen_base_sha` / `frozen_base_tree` | Canonical start identity |
| `policy_digest` | Admission, CI, review, and merge-policy contract |
| `manifest_digest` | Repository-owned required-lane manifest |
| `status` | `planning`, `certifying`, `publishing`, `completed`, `invalidated`, `halted` |
| `generation` | Monotonic epoch fencing token |
| `owner` / `lease_expires_at` | Current coordinator lease |
| `invalidation_code` | Typed reason when retired |

Only one non-terminal epoch may exist for a repository/base branch. A partial unique index or an
equivalent transactional claim enforces this.

### `merge_train_candidates`

| Field | Meaning |
|---|---|
| `epoch_id` / `ordinal` | Stable position in the epoch |
| `pr_number` / `head_sha` | Immutable PR identity |
| `merge_base_sha` | Verified merge base used to derive the patch |
| `patch_digest` | Digest of normalized binary-safe diff and metadata |
| `admission_digest` | Digest of canonical eligibility inputs |
| `status` | `admitted`, `excluded`, `invalidated`, `published` |
| `exclusion_code` | Stable reason such as `repair_in_flight` or `review_unresolved` |

The order is deterministic: configured priority, then oldest admissible `created_at`, then PR
number. Replanning may change the order only by creating a new generation and invalidating the
changed suffix.

### `merge_train_steps`

| Field | Meaning |
|---|---|
| `step_id` | Hash of epoch, ordinal, candidate identity, and prefix parent |
| `parent_commit` / `parent_tree` | `B0` for step 1; prior prefix for later steps |
| `candidate_head_sha` / `patch_digest` | Exact input |
| `synthetic_commit` / `cumulative_tree` | Locally constructed prefix identity |
| `tree_build_status` | `pending`, `building`, `built`, `conflicted`, `invalidated` |
| `ci_status` | `pending`, `running`, `passed`, `failed`, `expired`, `invalidated` |
| `publication_status` | `waiting`, `claimed`, `merged`, `reconciled`, `blocked` |
| `lease_version` | Fences builder, CI, and publication transitions |

Synthetic commits are local evidence objects. They are not pushed as contributor commits and do
not themselves authorize publication.

### `merge_train_ci_receipts`

A receipt binds `step_id`, cumulative tree, synthetic commit, candidate head, frozen base,
manifest digest, policy digest, command manifest, environment digest, start/end times, real
supervisor identity, per-command exit/timeout/output hashes, and terminal classification. Only a
fresh `passed` receipt for the exact current step is reusable.

### `merge_train_publications`

The publication row binds the step, expected current base, expected result tree, lease owner and
version, GitHub mutation idempotency key, API result, canonical post-read, merge commit, and final
classification. Status is `claimed`, `verification_required`, `completed`, or `failed`.

`verification_required` is mandatory when the GitHub response is unavailable or ambiguous. It is
not a failure and may not be retried until reconciliation rereads canonical state.

## Epoch creation and admission

1. Acquire the repository/base planning lease.
2. Read the target branch twice around candidate discovery. Both reads must return the same commit
   and tree; otherwise defer.
3. Read each candidate's canonical head, base, draft state, mergeability, reviews, required checks,
   unresolved actionable feedback, labels, and active repair/refresh claims.
4. Exclude candidates that are conflicted, ambiguous, draft, missing required review, awaiting a
   comment fix, undergoing repair/base refresh, or lacking an admissible patch.
5. Freeze the policy and CI manifest digests.
6. Order the remaining candidates deterministically and persist the epoch transactionally.
7. Reread the target branch. If it differs from `B0`, invalidate the new epoch without building.

An empty eligible set is a successful no-op, not an error. Excluded candidates retain reason codes
and may be reconsidered by a later epoch.

## Cumulative tree construction

For each candidate in order:

1. Resolve all Git objects by full SHA and verify they belong to the admitted repository.
2. Derive the candidate patch relative to the verified merge base. Renames, modes, symlinks,
   submodules, and binary changes use Git-native plumbing rather than text reconstruction.
3. Apply that patch to the prior prefix in a disposable worktree with no network and no hooks.
4. If application conflicts or changes outside the admitted patch, mark the candidate conflicted
   and invalidate it plus the suffix. Do not invoke a repair worker from the builder.
5. Record the resulting tree and a deterministic local synthetic commit whose parent is the prior
   prefix commit.
6. Verify the tree diff from parent to child equals the normalized candidate patch digest.

Tree building is sequential because step `i` depends on step `i-1`. It should be CPU- and I/O-light
relative to CI and must finish before CI fan-out begins.

## Bounded parallel CI

After all buildable prefix trees are materialized, the scheduler may certify steps concurrently.

- The default train-wide limit is four CI jobs, configurable up to the host's existing Kanban
  capacity but never above it.
- Per-repository and per-profile caps intersect with the train cap.
- A protected strict runtime lowers the effective cap through the existing runtime-priority guard.
- Every job uses a disposable worktree detached at the synthetic commit.
- Network is denied unless a repository-owned lane explicitly requires and governs it.
- Each job has command, wall-time, CPU, memory, process, and output-byte budgets.
- A durable lease is written before bootstrap. The recorded PID comes from the actual supervisor,
  never from model text or task evidence.
- A second worker for the same exact step is rejected while the supervisor is alive. A dead or
  expired supervisor may be replaced only with a higher fencing version.
- CI receipts are append-only. A retry creates a new attempt; it never edits prior evidence.

CI may complete out of order. Publication may not.

If `S4` passes before `S2`, it waits. If `S2` fails, `S1` may still publish when certified, while
`S2` and the suffix are removed from the current publication frontier. Because later trees include
the failed prefix, their receipts cannot be reused after removing or repairing `C2`.

## Sequential verified squash publication

Publication holds one exclusive lease for the repository/base branch. For step `Si`:

1. Require a fresh passing receipt for the exact cumulative tree and current policy/manifest.
2. Reread the PR, feedback, reviews, checks, and target branch from GitHub.
3. Require the PR head to equal the admitted head and the current target commit to equal the
   expected published prefix (`B0` for step 1, prior verified squash commit thereafter).
4. Reproduce the squash result locally using the exact current base and candidate head. Require its
   tree to equal `Si.cumulative_tree`; this prevents a stale branch from silently reverting newer
   prefix content.
5. Claim the publication row with a new fencing version and durable idempotency key.
6. Invoke GitHub squash merge once. Never retry from an API exception alone.
7. Reread canonical PR and branch state.
8. Require the PR to be `MERGED` and the reported merge commit to have exactly the expected parent
   and certified cumulative tree.
9. If the target still points to that commit, mark publication completed and advance the expected
   base. If the target advanced again, require the verified merge commit to be its ancestor, mark
   this publication completed, and invalidate the remaining epoch as external base movement. If
   the verified commit is not an ancestor, halt for operator review.

The squash commit SHA is not known in advance because GitHub creates it. Its parent and tree are
known and are the authoritative postcondition.

Before every later merge, the controller repeats all reads. A review dismissal, new actionable
comment, head update, check regression, policy change, or unexpected base movement closes the gate.

## Invalidation rules

| Change | Effect |
|---|---|
| External target-branch movement | Invalidate entire epoch |
| Candidate head, patch, or merge-base change | Invalidate candidate and suffix |
| Candidate removed or order changed | Invalidate from earliest changed ordinal |
| Review/check/actionable-feedback change | Exclude candidate; invalidate suffix |
| CI manifest or governing policy change | Invalidate entire epoch |
| CI receipt expiry | Re-certify exact step; do not rebuild when inputs are unchanged |
| Tree-build conflict | Exclude candidate and invalidate suffix |
| Expected squash-tree mismatch before merge | Block step and invalidate suffix |
| Canonical tree mismatch after merge | Halt repository train; operator review required |

Invalidation is monotonic for a generation. Rows are retained for audit and marked inactive; they
are not deleted or silently repurposed.

## Repair-lane exclusions

The merge train never repairs code or feedback. A pull request is excluded while any of these are
true:

- an actionable human or bot comment lacks a verified resolution;
- a typed CI failure has an active fixer receipt;
- a base-refresh, conflict-resolution, or branch-update task is active;
- a prior worker pushed a new head that has not completed canonical reread and CI admission;
- duplicate repair/comment tasks disagree about ownership or expected head;
- the latest worker outcome is prose-only, self-referential, or otherwise lacks typed evidence.

Repair workers may edit, push normally to the verified PR branch, and post one factual reply when
separately authorized. They may not merge, approve, dismiss reviews, acknowledge another head, or
write train receipts. A successful repair changes the head and therefore enters a future epoch as
a new candidate. It never resumes the old step.

The scanner suppresses acknowledgements, summaries of summaries, completion markers, and bot
replies already bound to the acted-on head. This prevents self-comment feedback loops. One PR/head
may have at most one active typed repair owner per failure class.

## Leases and fencing

There are separate leases for planning, tree building, CI, and publication. They share these rules:

- Lease identity contains repository, base branch, epoch generation, exact object identity, owner,
  claimed time, expiry, and monotonic version.
- Every state transition compares the full lease identity in the same transaction as the write.
- Renewal is allowed only by the current owner and version.
- Expiry alone does not authorize duplicate process execution when a verified local supervisor is
  still alive; the claim is extended or deferred.
- A replacement increments the version. Late output from an earlier version is retained as
  superseded evidence and cannot advance state.
- Publication uses a database uniqueness constraint plus a repository/base file lock where the
  controller is local. Neither mechanism substitutes for canonical GitHub rereads.
- No lock is held across a model call. The deterministic controller performs all protected steps.

## Crash reconciliation

Reconciliation runs before planning and before every publication attempt.

### Crash before GitHub mutation

If a publication row is claimed but the PR is open and the target branch is unchanged, mark the
attempt failed or reclaimable after verifying the old owner is dead. A higher fencing version may
retry.

### Ambiguous API result

If the squash request timed out or returned an indeterminate error, set `verification_required`.
Reread GitHub:

- PR merged and canonical parent/tree match: mark completed without another merge call.
- PR open and base unchanged: release for one fenced retry.
- PR merged but parent/tree mismatch: halt.
- State unavailable: remain `verification_required`; do not guess.

### Crash after merge before ledger finalization

Canonical `MERGED` truth plus the expected parent/tree reconciles the publication to completed.
If the target subsequently advanced and the verified merge commit is its ancestor, the completed
publication remains true but the epoch is invalidated before another candidate. This prevents a
successful merge from leaving a stale claim while still refusing to certify work across an
unexpected base change.

### External base movement

If the target branch moved to a commit that is not the train's last verified publication, invalidate
the epoch. A new epoch may reuse immutable object and CI evidence only when every binding remains
exact; cumulative receipts normally cannot survive a different base.

## Ordering, throughput, and fairness

- Epoch size is bounded, initially eight candidates.
- CI fan-out is bounded, initially four jobs.
- Only one epoch per repository/base may publish.
- Different repositories may certify concurrently under the global host cap.
- A failed candidate does not hold the whole queue indefinitely: the planner closes its suffix,
  publishes an already-certified safe prefix, then starts a new epoch without the excluded PR.
- Candidates keep their original age across exclusions so repaired work does not starve.
- A quiet-period debounce batches arrivals briefly, but an already-certified prefix is never held
  solely to wait for more work.

## Security and privacy

- The merge train is deterministic and requires no LLM call.
- GitHub tokens are read from the existing credential boundary and never stored in receipts.
- Logs and receipts contain repository/object identities, bounded command hashes, typed reason
  codes, and sanitized output excerpts only.
- Untrusted PR text, comments, branch names, and filenames are data. They are never evaluated as
  shell, prompt, policy, or command input.
- Commands are fixed repository-owned argv arrays executed without a shell.
- Exact repository allowlists and expected owner/repository identity are checked before every read
  and write.
- No train action changes repository settings, branch protection, required checks, or review state.

## Observability

Expose a read-only train status containing:

- epoch id, frozen base, generation, policy and manifest digests;
- ordered candidates and exclusions with stable reason codes;
- step parent/tree identities and CI status;
- active lease owner/version/age, without secrets;
- publication frontier and last canonical reconciliation result;
- invalidation lineage and replacement epoch;
- counters for CI reuse, suffix invalidation, duplicate suppression, repair exclusions, merge
  reconciliation, and halted mismatches.

Status must distinguish `waiting`, `running`, `passed`, `failed`, `invalidated`, `blocked`, and
`unknown`. It must never display “merged” from local intent or an API response without canonical
post-read verification.

## Configuration

All non-secret controls belong in the plugin's `config.yaml` section:

```yaml
merge_train:
  enabled: false
  mode: shadow
  max_candidates: 8
  max_parallel_ci: 4
  quiet_period_seconds: 60
  ci_receipt_ttl_seconds: 21600
  epoch_lease_seconds: 300
  ci_lease_seconds: 7200
  publication_lease_seconds: 300
  require_squash: true
  halt_on_post_merge_tree_mismatch: true
```

Defaults are off and fail closed. `max_parallel_ci` intersects with global Kanban, per-profile,
memory, and protected-runtime caps; it never raises them.

## Test strategy

Tests assert behavior through public interfaces and temporary repositories. They do not inspect
source text or freeze mutable catalog counts.

### Pure and property tests

- Candidate ordering is deterministic for every input permutation.
- `Si.parent_tree == S(i-1).cumulative_tree` for every built prefix.
- Changing candidate `i` invalidates exactly `i..n`.
- A receipt is reusable if and only if all identity, tree, manifest, policy, freshness, and command
  bindings match.
- No state-machine transition bypasses terminal or fenced states.

### Git integration tests

- Text, binary, rename, mode, symlink, deletion, and submodule patches produce expected trees.
- Two independently clean PRs that conflict only when combined stop at the first conflicting
  cumulative step.
- Stale-branch content cannot revert an earlier prefix.
- Local squash reproduction and a temporary bare-remote squash yield the same tree.
- Disposable worktrees remain clean and are removed only after receipts finalize.

### CI scheduler tests

- Eight built steps never exceed the configured concurrency or host cap.
- Strict-runtime activation lowers new admission without killing existing jobs.
- A live exact-step supervisor suppresses a duplicate.
- A dead supervisor permits one higher-version takeover.
- Late output from the prior version is stored as superseded and cannot pass the step.
- Timeout, output, memory, and process budgets classify truthfully and leave terminal receipts.

### Publication and fault-injection tests

- Only the earliest unpublished passing step may claim publication.
- Head, base, review, check, policy, or feedback drift between preflight and write prevents merge.
- Crash before mutation permits a fenced retry.
- Timeout after a successful merge reconciles from canonical GitHub truth without a second call.
- Canonical parent/tree mismatch halts the train.
- An external base commit invalidates the epoch.
- Two controllers racing produce one GitHub mutation.

### Repair and loop tests

- Failed CI excludes the PR and dispatches at most one typed repair owner outside the train.
- A repaired new head cannot reuse the old candidate or receipt.
- Bot completion markers and already-acted-on replies do not create feedback tasks.
- Duplicate scanner passes do not create duplicate train, CI, or repair claims.

### End-to-end acceptance

A temporary GitHub test repository or equivalent authenticated sandbox runs three PRs through:

1. frozen-base planning;
2. cumulative tree construction;
3. bounded out-of-order CI completion;
4. sequential squash publication;
5. crash injection after the second merge response;
6. canonical reconciliation and completion of the third merge.

Acceptance requires exact canonical commit parents and trees, one merge per PR, no duplicate
comments or tasks, and a complete append-only receipt lineage. Mock-only evidence is diagnostic,
not acceptance.

## Rollout

### Phase 0: Offline model and migration validation

Ship schema migrations, pure state-machine tests, Git integration tests, and read-only status.
`enabled` remains false. Verify existing single-PR merge behavior is unchanged.

### Phase 1: Shadow planning

On real repositories, read canonical state and build proposed epochs without creating worktrees,
CI jobs, comments, or mutations. Compare admission decisions with maintainers for at least one
week. Record false exclusions and unsafe admissions.

### Phase 2: Certify-only

Build cumulative trees and run bounded CI, but never call merge. Compare certified prefix trees
with manually merged outcomes. Any mismatch resets the phase clock.

### Phase 3: Single-candidate canary

Enable automatic squash for epochs of size one on an allowlisted repository. Require full
canonical reconciliation and operator-visible receipts. The kill switch remains immediately
available.

### Phase 4: Small cumulative canary

Raise epoch size to three and parallel CI to two. Exercise suffix invalidation, a repair exclusion,
and crash reconciliation before expanding.

### Phase 5: Bounded production

Raise to the default eight candidates and four CI jobs only after measured host capacity and
protected-runtime behavior remain healthy. Keep repository allowlists, strict squash, sequential
publication, and tree verification permanently enforced.

## Kill switch and recovery

Disabling the train stops new planning, CI claims, and publication claims. It does not terminate
running CI processes or modify GitHub. The reconciler remains read-only so operators can learn
whether an in-flight mutation completed.

Re-enabling begins with reconciliation. Halted post-merge mismatches require explicit operator
resolution and a new epoch; they are never cleared by time, retries, or model judgment.

## Success criteria

- Multiple cumulative CI jobs run concurrently without exceeding configured resource caps.
- GitHub squash merges remain strictly sequential.
- Every merged commit's parent and tree match the certified step.
- A changed head or external base can never consume stale CI.
- A crash after GitHub accepts a merge cannot cause a duplicate merge attempt or permanently block
  the train.
- Failed or repairing PRs do not stall unrelated admissible work.
- Repeated scans are idempotent and do not generate self-comment or duplicate-task loops.
- Operators can explain every wait, exclusion, invalidation, merge, and halt from typed receipts.

## Open implementation constraint

The upstream base used for this design does not contain the private GitHub feedback/merge
controller. Implementation must therefore either target the separately installed controller
plugin or first introduce a generic plugin-owned interface in a self-contained change. It must not
special-case a private repository workflow in Hermes core.
