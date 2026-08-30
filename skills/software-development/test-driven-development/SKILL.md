---
name: test-driven-development
description: Use RED-GREEN-REFACTOR for behavior-changing code.
version: 1.2.0
author: Hermes Agent (adapted from obra/superpowers)
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [testing, tdd, development, quality, red-green-refactor]
    related_skills: [systematic-debugging]
---

# Test-Driven Development

Use RED-GREEN-REFACTOR to prove behavior changes with the smallest useful
feedback loop. TDD is a verification discipline, not a reason to discard valid
work or invent tests that cannot observe the contract.

## When to Use

Use this skill for:

- new behavior with an executable test seam;
- bug fixes, where a regression test can reproduce the symptom;
- contract changes across APIs, persistence, state, or user-visible workflows;
- refactors that expose behavior not already protected by useful coverage.

Adapt the procedure instead of pretending strict test-first happened for:

- documentation, copy, comments, and formatting-only changes;
- generated artifacts whose source generator is the tested unit;
- configuration or migration changes that cannot safely execute the old state;
- throwaway spikes that will not be promoted as production code;
- legacy systems without a runnable harness or reproducible test seam.

For an adapted case, state why a meaningful RED is unavailable and name the
alternative evidence before editing: schema validation, dry-run, readback,
reproduction script, build, browser probe, or another deterministic check.

## Procedure

### 1. Define one observable behavior

Write the acceptance statement before the test:

```text
Given <state>, when <action>, then <observable result>.
```

Choose the lowest test layer that can observe the real contract without
reimplementing it. Prefer real boundaries over mocks when file, database,
network, browser, or process behavior is the point.

Completion criterion: the expected result and the command that will prove it
are explicit.

### 2. RED — prove the check can detect the missing behavior

Add one focused test or regression harness and run it immediately.

A valid RED:

- fails for the intended missing or broken behavior;
- reaches the relevant production seam;
- has a useful assertion failure, not a typo, import error, or bad fixture;
- is narrow and deterministic enough to run repeatedly.

If the test passes immediately, determine whether behavior already exists, the
assertion observes the wrong seam, or a broader implementation already covers
the case. Do not weaken production code merely to manufacture RED.

Completion criterion: the observed failure is recorded and explained.

### 3. GREEN — make the smallest coherent production change

Implement only what the current behavior requires. Keep the change coherent:
do not leave the repository uncompilable or knowingly break adjacent callers
just to minimize line count.

Run the focused command again. If it remains red, change one hypothesis or one
production variable at a time. Do not edit the expected behavior merely to
match an incorrect implementation.

Completion criterion: the focused check passes and no open intentional RED
remains.

### 4. REFACTOR — improve structure while preserving behavior

After GREEN:

- remove duplication introduced by the slice;
- improve names and boundaries;
- replace temporary scaffolding;
- keep public contracts stable unless the task changes them.

Re-run the focused check after production edits. Use small refactor steps when
the feedback loop is slow.

Completion criterion: the same behavior remains green after cleanup.

### 5. Expand verification proportionally

Run the repository's relevant accumulated suite and required lint, type, build,
or integration checks. A one-line pure helper does not always justify the same
gates as an authentication or migration change; use project instructions and
risk to choose scope.

Report exact commands and outcomes. Distinguish a focused pass from a full-suite
pass, and disclose unrelated pre-existing failures rather than calling the
whole repository green.

Completion criterion: every claimed gate was actually run after the final
production edit.

## Existing Code and Interrupted Work

Do not delete correct production code solely because it predates its test.
Instead:

1. inspect the current behavior and existing coverage;
2. build the smallest red-capable regression or characterization check;
3. make the requested delta;
4. verify the final behavior.

When recovering interrupted work, reconstruct the last observed RED/GREEN state
from tests, diffs, and logs. Continue from that frontier; do not invent a clean
history or restart valid work for ceremony.

For a refactor with strong existing behavior coverage, the existing green suite
is the baseline. Add a new failing test only for a new contract or a previously
unobservable regression risk.

## Delegated Implementation

When another agent implements a TDD slice, give it:

- the exact repository and writable scope;
- one behavior and its acceptance criteria;
- the focused test command;
- required RED and GREEN evidence;
- side-effect and commit boundaries.

Do not ask several writers to share the same RED/GREEN frontier. Child summaries
are evidence leads; verify the changed files and commands in the owning context.

## Pitfalls

- **Collection failure called RED:** a missing import does not prove behavior.
- **Mock-only confidence:** the mock passes while the real boundary is broken.
- **Horizontal batch:** many imagined tests are written before one path works.
- **Tests-after labeled TDD:** useful regression coverage is honest, but it is
  not observed RED.
- **Manufactured failure:** correct code is weakened so a new test can fail.
- **Stale GREEN:** production changes after the last test invalidate the result.
- **Suite inflation:** every tiny change triggers expensive unrelated gates.
- **False full-pass claim:** focused tests pass while broader required gates fail.

## Verification

Before reporting completion, confirm:

- [ ] The behavior and observation seam were explicit.
- [ ] RED failed for the expected reason, or an adapted-case reason and
      alternative check were recorded before the edit.
- [ ] GREEN ran against the final production behavior.
- [ ] Refactoring did not add unrequested behavior.
- [ ] Relevant accumulated gates ran after the last production edit.
- [ ] Test scope and any unrelated failures are reported honestly.
- [ ] No correct existing code was discarded merely to recreate ceremony.
