---
name: requesting-code-review
description: Review code changes with risk-proportional verification.
version: 2.1.0
author: Hermes Agent (adapted from obra/superpowers + MorAlekss)
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [code-review, security, verification, quality, pre-commit]
    related_skills: [test-driven-development, github-code-review]
---

# Pre-Commit Code Review

Verify the actual candidate with deterministic evidence first, then add an
independent review when risk or user intent justifies its cost. Review does not
replace tests, and passing tests do not replace judgment about security,
contracts, or scope.

## When to Use

Use this skill when:

- the user explicitly asks for review, verification, commit, push, or ship;
- a change touches authentication, authorization, secrets, destructive writes,
  migrations, concurrency, deployment, or another high-impact boundary;
- a release or repository policy requires independent approval;
- a broad or unfamiliar diff would benefit from fresh context.

For a small low-risk change, focused tests plus an owning-context diff review may
be sufficient. Documentation-only, copy-only, and mechanical config changes do
not require an independent reviewer unless policy or the user requires one.

This skill reviews the current agent's candidate. Use `github-code-review` for
an existing remote pull request and its inline discussion.

## Procedure

### 1. Freeze the review question and candidate scope

State:

- intended behavior and acceptance criteria;
- comparison base and exact paths in scope;
- whether the candidate is working tree, staged index, commit, or PR;
- repository instructions and required gates;
- side effects that review must not perform.

Capture `git status`, `HEAD`, the comparison base, and the diff. Include untracked
files that belong to the candidate. Do not silently switch from the user's
working-tree scope to a staged or committed scope.

Completion criterion: every candidate file is accounted for and the comparison
scope is reproducible.

### 2. Run deterministic verification first

Use project-owned commands from `package.json`, `pyproject.toml`, `Makefile`, CI,
or repository instructions. Prefer focused checks for the changed subsystem,
then expand according to risk:

- regression tests;
- lint and type checks;
- build or package verification;
- schema or migration dry-run;
- browser, API, database, or process smoke on the real seam;
- diff whitespace and generated-file checks.

Do not stash or mutate unrelated user work merely to manufacture a baseline.
If the repository has pre-existing failures, compare against authoritative prior
evidence or a safe isolated baseline and report uncertainty honestly.

Completion criterion: exact commands, outcomes, and unrun required gates are
recorded after the final candidate edit.

### 3. Perform an owning-context diff review

Inspect the complete diff and relevant callers for:

- acceptance-criteria mismatches and scope drift;
- secrets, injection, path traversal, unsafe deserialization, and permission
  bypasses;
- error handling at I/O, network, database, and process boundaries;
- data loss, retries, races, idempotency, and cleanup;
- tests that cannot observe the guarded failure;
- public contract, migration, compatibility, and deployment impact;
- unrelated edits or generated churn.

Use security scanners when appropriate, but inspect findings rather than treating
string matches as verdicts.

Completion criterion: every material finding names an artifact location,
reproduction or evidence, impact, and minimum correction.

### 4. Decide whether independent review is warranted

Request independent review when required by policy/user, or when the candidate
is high-risk, broad, unfamiliar, security-sensitive, release-critical, or built
under substantial implementer context bias.

Skip independent review when the change is small and reversible, deterministic
checks directly prove the contract, and fresh-context value would not justify
the latency and token cost. Record that proportional decision instead of
pretending a mandatory gate ran.

When dispatching a reviewer:

- provide the repository path, comparison base, acceptance criteria, complete
  diff or exact candidate locator, and existing test evidence;
- make the review read-only unless the brief explicitly assigns fixes;
- require inspection of real files and execution of relevant checks when
  available;
- forbid commit, push, deploy, publication, messaging, destructive commands,
  credential changes, and data mutation unless separately authorized;
- treat the review summary as a lead and verify cited blockers in the owning
  context.

Completion criterion: either independent evidence is returned and reconciled,
or the recorded risk decision explains why it was not needed.

### 5. Reconcile findings and fix ownership

Classify each finding as:

- blocker: violates acceptance, safety, or a required gate;
- non-blocking improvement;
- incorrect or unsupported;
- out of scope.

The implementation owner applies accepted fixes unless a separate writer was
assigned a non-overlapping scope. Re-run affected focused checks after every
production fix. A changed candidate invalidates review evidence that depended on
its exact bytes; repeat exact-candidate review only where such identity is an
explicit release or security requirement.

Completion criterion: all blockers are closed with new evidence or remain
explicitly unresolved; advisory findings do not create an unbounded review loop.

### 6. Report the verdict without performing unrequested release actions

Report:

- reviewed scope and comparison base;
- deterministic commands and outcomes;
- independent reviewer identity/type when one was used;
- blockers fixed, remaining, or disputed;
- exact limitations of the evidence;
- commit, push, deploy, or publication state.

Do not commit, push, merge, deploy, or add special commit prefixes unless the
user or repository workflow explicitly requests that action.

Completion criterion: the verdict distinguishes review from release and does
not claim gates that did not run.

## High-Assurance Exact-Candidate Review

Use an immutable candidate identity only when release, compliance, or security
policy requires byte-level approval. Bind all reviewers and tests to the same
commit/tree or staged-diff digest, prevent overlay changes, and verify the final
commit matches the reviewed candidate.

Do not impose exact-hash ceremony on ordinary reversible changes. If a large
candidate exceeds a reviewer's context, partition it by coherent contract while
retaining an integration check over the whole candidate; never silently
truncate the diff.

## Pitfalls

- **Tests called review:** automation passes but acceptance or security is wrong.
- **Review called deploy:** approval is reported as a live release.
- **Mandatory fresh agent for every edit:** cost rises without useful independence.
- **String scan as verdict:** safe code is flagged or obfuscated risk is missed.
- **Reviewer edits by default:** ownership blurs and the reviewed candidate moves.
- **Unbounded re-review:** optional hardening prevents closure indefinitely.
- **Stale PASS:** production bytes changed after the evidence was collected.
- **Partial diff:** untracked, generated, or sibling files escape review.

## Verification

Before returning a verdict, confirm:

- [ ] Candidate scope, base, status, and relevant untracked files were captured.
- [ ] Required deterministic gates ran after the final production edit.
- [ ] Security and contract boundaries received direct inspection.
- [ ] Independent review was used or skipped by an explicit risk decision.
- [ ] Every blocker is tied to evidence and reconciled in the owning context.
- [ ] Exact-candidate identity was required only where policy justified it.
- [ ] Commit, push, merge, deploy, and publication state are reported separately.
