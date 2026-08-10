# Task 5 Report

## Scope
Added read-only fleet reporting (`activity_telemetry/report.py`) and the operator contract (`docs/operations/fleet-activity-policy.md`). No runtime behavior changed; reporting cannot write the evidence it reads.

## RED evidence
- `python -m pytest tests/activity_telemetry/test_report.py -q` failed collection with `ModuleNotFoundError: No module named 'activity_telemetry.report'`.

## Implementation
- `summarize(db_path, since)` groups by activity ID, policy version, requested provider/model, served provider/model, and final outcome, returning exactly the 22 documented columns.
- `since` is validated with `datetime.fromisoformat`, must be timezone-aware, and is normalized to UTC. Naive timestamps are rejected rather than silently assumed UTC.
- The connection is opened as `db_path.resolve().as_uri() + "?mode=ro"` with `sqlite3.connect(uri=True)`. `as_uri()` percent-encodes spaces and `#`, which a hand-built `file:{path}` string would not.
- A missing database raises `FileNotFoundError` without creating the file or its parent directory.
- `runs` counts `DISTINCT run_id`, so a run with two served routes is not double-counted, while route usage still lands on each route row.
- Runs with no usage at all (deterministic and no-work fires) still appear, via a `LEFT JOIN`, with null served route and zero counters.
- Costs are summed as exact `Decimal` strings in Python rather than by SQLite, which would coerce them to floats. `NULL` (unknown) stays distinct from `0` (known free).
- Process-only completion is never relabelled: `semantic_successes` counts only runs whose derived `final_outcome` is `succeeded`.

## Verification evidence
- `python -m pytest tests/activity_telemetry/test_report.py -q`: **19 passed**.
- Full foundation gate `python -m pytest tests/activity_policy tests/activity_telemetry tests/cron/test_activity_telemetry.py tests/test_packaging_metadata.py -q`: **131 passed in 31.24s**.
- `python -m ruff check activity_policy activity_telemetry tests/activity_policy tests/activity_telemetry tests/cron/test_activity_telemetry.py`: **All checks passed**.
- `python -m build --wheel --sdist`: succeeded; both archives verified to contain `activity_policy/policies.yaml`, `activity_telemetry/report.py`, `recorder.py`, and `store.py`.

## Positive control
The read-only guarantee was proven armed, not merely asserted: temporarily changing the URI to `?mode=rw` made `test_read_only_connection_rejects_writes` fail, and restoring `?mode=ro` returned it to green. The test also opens a second connection on the captured URI and confirms SQLite raises `readonly` on an `UPDATE`, so the guarantee is enforced by the connection rather than by convention.

## Documentation verification
Both `python -c` snippets in the operator guide were extracted from the markdown and parsed with `ast.parse` to confirm they are valid, pastable commands. The report invocation was then run end to end against a seeded temporary database; it correctly showed a requested `deepseek/deepseek-v4-pro` route served by `openai-codex/gpt-5.6-sol` with `final_outcome: unknown` despite process success — exactly the fallback-plus-missing-evidence signal the subsystem exists to surface.

The guide covers all thirteen required sections, including the explicit non-goals, the policy revision procedure, the rollout gate (a shadow cohort or any enforcement mode needs separate runtime authorization), and rollback guidance that preserves the activity DB and SessionDB as evidence.

## Boundary preserved
No live `jobs.json` was read or written, no schedule/model/provider/profile/credential/delivery configuration changed, no service was restarted, and `enforcement` remains `observe`.
