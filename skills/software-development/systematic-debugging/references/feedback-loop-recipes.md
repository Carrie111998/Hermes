# Feedback Loop Recipes

How to build, tighten, and stabilize the red-capable command that Phase 1 requires.

## Ways to construct a loop — try in roughly this order

1. **Failing test** at the seam that reaches the bug: unit, integration, or end-to-end.
2. **HTTP script / curl** against a running dev server.
3. **CLI invocation** with fixture input, diffing stdout/stderr against expected output.
4. **Headless browser script** (Playwright/Puppeteer) asserting on DOM, console, or network.
5. **Replay a captured trace**: HAR, request payload, event log, queue message, or webhook body.
6. **Throwaway harness** that boots the smallest useful slice of the system and calls the failing path.
7. **Property / fuzz loop** when the bug is intermittent wrong output over a broad input space.
8. **Bisection harness** suitable for `git bisect run` when the bug appeared between two known states.
9. **Differential loop** comparing old vs new version, two configs, two providers, or two datasets.
10. **Human-in-the-loop script** only as a last resort: script the human steps and capture their result so the loop stays structured.

## Tighten the loop once it exists

- Make it faster: cache setup, narrow scope, skip unrelated initialization.
- Make the signal sharper: assert the exact symptom, not generic success.
- Make it more deterministic: pin time, seed randomness, isolate filesystem, freeze network.

## Non-deterministic bugs

For non-deterministic bugs, the immediate goal is a higher reproduction rate, not perfection.
Run the trigger 100x, parallelize, add stress, narrow timing windows, or inject sleeps.
A 50% flake is debuggable; a 1% flake usually is not.

## Running the loop

Use the `terminal` tool:

```bash
# Run a specific failing test
pytest tests/test_module.py::test_name -v

# Or run a scripted repro
python scripts/repro_bug.py

# Or run a high-repetition flaky repro
for i in {1..100}; do pytest tests/test_flake.py::test_name -q || break; done
```

## Minimizing the reproduction (Phase 2 step 0)

Once the loop is red, shrink the repro to the smallest scenario that still goes red.
Cut inputs, callers, config, data, and steps **one at a time**, re-running the loop after each cut.
Keep only what is load-bearing for the failure.

Done when removing any remaining element makes the loop go green. A minimal repro narrows the
hypothesis space and often becomes the cleanest regression test.
