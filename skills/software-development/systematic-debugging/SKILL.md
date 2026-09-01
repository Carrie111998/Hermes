---
name: systematic-debugging
description: "Use when diagnosing test failures, unexpected behavior, build failures, integration issues, or performance regressions."
version: 1.1.0
author: Hermes Agent (adapted from obra/superpowers)
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [debugging, troubleshooting, problem-solving, root-cause, investigation]
    related_skills: [test-driven-development, subagent-driven-development]
---

# Systematic debugging

Find the root cause before changing code. A fix is not ready until one command can reproduce the bug, fail before the change, and pass after it.

Use this skill for technical failures and unexpected behavior. Do not skip it because the issue looks simple or urgent.

## 1. Build a feedback loop

Start with a fast, deterministic command that reproduces the user's exact symptom. It must be able to go red for this bug and green after the fix. A command that only proves the program does not crash is too broad.

Try these in order:

1. A focused unit, integration, or end-to-end test.
2. A CLI or HTTP command with fixture input and an exact assertion.
3. A browser script that checks the DOM, console, or network.
4. A replay of a request, event, trace, or webhook.
5. A small harness that boots only the failing path.
6. A property, fuzz, differential, or `git bisect run` loop.
7. Scripted human verification only when the result cannot be observed automatically.

Examples:

```bash
pytest tests/test_module.py::test_name -v
python scripts/repro_bug.py
for i in {1..100}; do pytest tests/test_flake.py::test_name -q || break; done
```

If the bug is flaky, first raise its reproduction rate. Repeat the trigger, add load, narrow timing windows, pin time, seed randomness, or isolate network and filesystem state.

If no red-capable loop exists, gather more evidence. Do not guess at a fix.

## 2. Find the root cause

### Read the failure

Read the complete error, stack trace, warnings, paths, and error codes. Then inspect the source that emits the error.

```python
read_file("src/problematic_file.py")
search_files("exact error text", path="src/")
```

### Check recent changes

```bash
git log --oneline -10
git diff
git log -p --follow -- src/problematic_file.py
```

Look for changed dependencies, configuration, environment, data shape, or call order. Use `git blame` or history when a suspicious line may encode an older constraint.

### Trace the bad value

Follow the value or state backward through callers until you find where it first becomes wrong. Fix the source, not the place where the symptom finally appears.

```python
search_files("function_name\\(", path="src/", file_glob="*.py")
search_files("variable_name\\s*=", path="src/", file_glob="*.py")
```

### Instrument component boundaries

For multi-component paths such as client to API to service to database, record what enters and leaves each boundary. Verify configuration and state propagation at every hop. One run should show where the data first diverges.

Keep temporary diagnostics searchable with a unique prefix such as `[DEBUG-a4f2]`, then remove them before committing.

### Minimize the reproduction

Once the loop is red, remove inputs, callers, configuration, data, and steps one at a time. Re-run after each removal. Stop when every remaining element is required to reproduce the failure.

## 3. Test hypotheses

Write three to five plausible, falsifiable hypotheses. Rank them by likelihood and cost to test.

For each hypothesis, state a prediction:

```text
If X causes the bug, observing or changing Y will produce Z.
```

Test the highest-ranked hypothesis with the smallest probe. Change one variable at a time. Prefer a debugger or REPL inspection over adding many logs.

When a probe fails, record what it ruled out and test the next hypothesis. Do not stack speculative changes.

## 4. Compare with working code

Find the nearest working example in the same codebase. Read the relevant implementation completely, then list every difference between the working and failing paths. Include configuration, environment, dependencies, timing, state, and error handling.

Do not dismiss a difference until a probe shows it is irrelevant.

## 5. Implement one fix

1. Turn the minimal reproduction into a regression test when possible.
2. Confirm the test fails for the expected reason.
3. Make one change that addresses the proven root cause.
4. Run the focused test.
5. Run the relevant broader suite, linter, or build.
6. Remove temporary diagnostics.
7. Inspect the final diff for unrelated changes.

```bash
pytest tests/test_module.py::test_regression -v
pytest tests/ -q
```

Do not bundle cleanup or refactoring with the fix unless the root cause requires it.

## Rule of three

After each failed fix attempt, return to the evidence and revise the hypothesis. After three failed fixes, stop changing code and question the design.

Signs of a design problem include:

- Each attempt exposes shared state or coupling in a different place.
- The proposed fix requires broad changes unrelated to the original symptom.
- Every fix moves the failure to another component.

Discuss the design with the user before attempting a fourth fix.

## Using delegation

For a large multi-component failure, delegate investigation by component. Give each investigator the error, relevant paths, and exact reproduction command. Ask for evidence and a root-cause hypothesis, not a patch.

```python
delegate_task(
    goal="Investigate why the specified test or behavior fails. Reproduce it, trace the data flow, and report evidence and the root cause. Do not edit files.",
    context="Error: [full error]\nPaths: [relevant paths]\nReproduction: [exact command]"
)
```

Keep dependent investigations serial. Run independent component checks in parallel.

## Completion checklist

Before editing:

- [ ] The exact failure and expected behavior are clear.
- [ ] A focused reproduction has run and can detect the bug.
- [ ] Recent changes and the full error path were inspected.
- [ ] The failure is isolated to a component or code path.
- [ ] A falsifiable hypothesis explains the evidence.

Before finishing:

- [ ] A regression test or equivalent check failed before the fix and passes after it.
- [ ] The fix addresses the root cause rather than masking the symptom.
- [ ] Relevant broader checks pass without new failures.
- [ ] Temporary diagnostics and unrelated edits are gone.
- [ ] The reported result comes from real tool output.

## Pitfalls

- Trying an obvious code change before reproducing the problem.
- Testing several fixes at once and losing causal evidence.
- Reading only the last line of a stack trace.
- Treating a nearby green test as proof that the user's symptom is fixed.
- Writing the regression test after the implementation and never seeing it fail.
- Continuing after three failed fixes without reconsidering the design.
- Claiming success from reasoning alone instead of rerunning the reproduction.
