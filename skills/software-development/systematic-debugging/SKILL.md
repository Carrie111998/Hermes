---
name: systematic-debugging
description: Find root causes with reproducible evidence before fixing.
version: 1.2.0
author: Hermes Agent (adapted from obra/superpowers)
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [debugging, troubleshooting, problem-solving, root-cause, investigation]
    related_skills: [test-driven-development]
---

# Systematic Debugging

Find the root cause before changing production behavior. The required depth is
proportional to the problem, but every fix needs an observed symptom, evidence
that identifies the failing mechanism, and a post-fix check on the real path.

## When to Use

Use for test failures, regressions, crashes, incorrect output, performance
problems, flaky behavior, build failures, and integration incidents.

A simple issue may use a compressed pass when one command reproduces it and the
cause is directly evidenced. Use the full procedure when the system spans
components, the failure is intermittent, earlier fixes failed, or the cause is
uncertain.

Do not use this skill for planned feature design or speculative cleanup without
a reported failure.

## Procedure

### 1. Reproduce the exact symptom

Read the complete error, stack trace, request, screenshot, or user steps. Build
the tightest command that can go red on the reported behavior and green when it
is fixed:

- focused unit or integration test;
- HTTP or CLI reproduction with fixture input;
- browser assertion on DOM, console, or network;
- replay of a captured request, event, or trace;
- repeated or stress loop for a flaky failure.

If the issue cannot yet reproduce, gather more evidence instead of guessing.
For flakes, first raise the reproduction rate with repetition, controlled
scheduling, fixed seeds, or trace capture.

Completion criterion: the symptom is reproduced, or the missing evidence needed
to reproduce it is explicitly identified.

### 2. Locate the failing boundary

Inspect recent changes and trace data through each relevant component. At every
boundary ask:

- what entered;
- what left;
- which configuration and identity were active;
- where observed state first diverged from expected state.

Prefer existing logs, debugger inspection, database readback, and narrow probes.
Add temporary instrumentation only when needed, tag it for removal, and never
log credentials or sensitive payloads.

Completion criterion: the failure is isolated to a component, boundary, or
specific unresolved branch of the call graph.

### 3. Compare working and failing cases

Find a nearby working example or a known-good version. Minimize the reproduction
by removing inputs, callers, state, and steps one at a time while keeping it red.
List concrete differences; do not dismiss one because it appears small.

Completion criterion: the minimal failing case and its meaningful differences
from a working case are known.

### 4. Test ranked hypotheses

Form one or more falsifiable hypotheses. For each, state the observation that
would support or reject it. Test the cheapest high-likelihood hypothesis first
and change one variable at a time.

A failed probe is information, not permission to stack another speculative fix
on top. Update the hypothesis from the new evidence.

Completion criterion: one hypothesis explains the symptom and survives a
probe that distinguishes it from plausible alternatives.

### 5. Fix the root cause with a regression check

For behavior-changing code, follow `test-driven-development`:

1. preserve the smallest reproduction as an automated regression when feasible;
2. observe the focused failure;
3. make one coherent root-cause fix;
4. rerun the focused check and relevant accumulated gates.

When an automated regression is not feasible, record the alternative command or
readback that can fail before the fix and pass after it. Remove temporary debug
instrumentation before final verification.

Completion criterion: the original symptom is green on the real path and the
fix does not rely on unverified side effects.

### 6. Stop after repeated failed fixes

After three materially different fix attempts fail, stop editing and reassess
the architecture, reproduction, and ownership boundaries. Do not attempt a
fourth patch without explaining what the previous evidence invalidated and why
the next approach is structurally different.

Completion criterion: either the issue is resolved or the remaining blocker is
a specific architectural decision or missing external prerequisite.

## Multi-Component and Production Incidents

For API → service → database, queue → worker, CI → build → deploy, or similar
chains, identify the last correct boundary before editing. Check config and
identity propagation as carefully as data propagation.

For production incidents:

- preserve evidence before restarting or mutating state;
- prefer reversible mitigation when user impact is active;
- separate containment from the durable root-cause fix;
- verify process, artifact, data, and public behavior after recovery;
- do not claim the incident resolved from a worker's success message alone.

## Pitfalls

- **Fix first, investigate later:** destroys evidence and masks the cause.
- **Nearby failure substituted:** the check does not reproduce the user's symptom.
- **Several edits at once:** the successful variable cannot be identified.
- **Caller patch:** one site is fixed while sibling paths retain the mechanism.
- **Log flooding:** instrumentation changes timing or leaks sensitive data.
- **Unreproduced flake:** a green single run is treated as proof.
- **Architecture by accumulation:** each failed patch adds another special case.
- **Stale evidence:** tests ran before the last production edit.

## Verification

Before reporting resolution, confirm:

- [ ] The original symptom or an exact proxy was reproduced.
- [ ] The first failing boundary and root cause are explained with evidence.
- [ ] The fix addresses the mechanism, not only one symptom site.
- [ ] A regression check or explicit alternative proof went red and green.
- [ ] Relevant accumulated gates ran after the final edit.
- [ ] Temporary instrumentation and sensitive artifacts were removed or secured.
- [ ] Any containment-only action is distinguished from the durable fix.
