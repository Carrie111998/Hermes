# AGY Model Label Fix Plan

## Objective

Preserve the canonical audit model identity while translating it into the exact AGY execution label and validating completion through a strict post-exit receipt.

## Required behavior

1. Keep the canonical audit `model_id` unchanged as `gemini-3.1-pro-high`.
2. At the AGY execution boundary, map `gemini-3.1-pro-high` explicitly to the exact display label `Gemini 3.1 Pro (High)`.
3. Build the AGY argument vector so the prompt is the positional argument immediately after `-p`.
4. Do not pass `--effort` for display-label variants because AGY rejects that flag for those variants.
5. Add a unique per-run `--log-file` path to every AGY invocation.
6. Wait for the child process to exit, then parse that run's log file.
7. Require an exact receipt matching the requested canonical model and mapped AGY display label.
8. Treat a missing or mismatched receipt as a closed failure:
   - return a failure result;
   - preserve diagnostic evidence without exposing secrets;
   - never report the audit run as completed.
9. Preserve the existing safe child-process environment construction.
10. Preserve keyring-backed subscription authentication; do not move credentials into arguments, logs, errors, or inherited unsafe environment values.
11. Add future Flash variants only through explicit canonical-ID-to-display-label mappings. Unknown variants must fail closed and must never silently fall back to another model or label.

## Implementation sequence

1. Introduce or update a single explicit AGY model-label mapping at the execution boundary.
2. Make unknown canonical model IDs raise or return a clear unsupported-model failure before process launch.
3. Construct argv according to this contract:
   - AGY executable and required command arguments;
   - `-p`;
   - prompt as the immediately following positional value;
   - explicit mapped model display label in the AGY-supported model option;
   - unique `--log-file` path;
   - no `--effort` for display-label variants.
4. Launch with the existing sanitized child environment and keyring subscription-auth flow.
5. After process exit, read only the unique log for that invocation and parse the completion receipt.
6. Accept success only when the receipt exactly identifies:
   - canonical `model_id`: `gemini-3.1-pro-high`;
   - AGY display label: `Gemini 3.1 Pro (High)`.
7. Propagate receipt mismatch, missing log, missing receipt, malformed receipt, or nonzero process exit as explicit failures that cannot produce a completion report.

## Verification

Add focused tests for:

1. Argv contract:
   - canonical ID maps to `Gemini 3.1 Pro (High)`;
   - prompt is immediately after `-p`;
   - `--effort` is absent;
   - `--log-file` is present and unique per run.
2. Exact receipt success:
   - matching canonical ID and exact display label succeeds only after process exit and log parsing.
3. Receipt failure:
   - mismatched canonical ID fails closed;
   - mismatched display label fails closed;
   - missing receipt fails closed;
   - missing or malformed run log fails closed;
   - none of these paths report completion.
4. Secret safety:
   - authentication remains keyring-backed;
   - secrets do not appear in argv, child environment leakage, logs, diagnostics, or returned errors.
5. Regression coverage:
   - run the full fleet test suite;
   - run selected AGY routing, model-selection, process-launch, receipt-parsing, authentication, and completion-reporting regressions.

## Completion evidence

Record the changed files, relevant hashes, exact test commands and results, and final git status. Completion is valid only when the focused tests, selected regressions, and full fleet suite pass and the strict receipt contract is demonstrated.

PLAN_READY
