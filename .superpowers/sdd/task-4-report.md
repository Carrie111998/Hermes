# Task 4 Report

## Scope
Attached observational activity telemetry to every cron lifecycle branch in `cron/scheduler.py`. Recording is strictly observational: no fire is gated, no routing/model/schedule/delivery/credential state changed, and the `(success, full_output_doc, final_response, error_message)` tuple is byte-for-byte unchanged on every path.

## RED evidence
- `python -m pytest tests/cron/test_activity_telemetry.py -q` initially reported **18 failed** (`AttributeError: module 'cron.scheduler' has no attribute '_load_activity_registry'` plus 17 collection errors) because the adapter did not exist.
- After wiring, four branch tests still failed with `activity telemetry finish failed: ValueError` — proving the store's evidence allowlist did not yet accept the `evidence:` kind.

## Implementation
Helpers added next to `run_job`:
- `_load_activity_registry()` / `_get_activity_registry()` — process-cached packaged registry. A load failure logs only the exception class and makes every job behave as unmapped.
- `_resolve_cron_activity_policy(job)` — explicit `activity_id` first, then the exact job-name alias. An unknown explicit ID fails closed in the registry, which here means "unmapped", because a policy lookup must never stop a scheduled job.
- `_open_cron_activity(...)` — best-effort open against `get_default_hermes_root()/telemetry/activity.db`. Requested provider/model come from the job's declared pre-agent configuration; the served route is never inferred from configuration.
- `_finish_cron_activity(...)` — best-effort single terminal enrichment.

Lifecycle wiring in `_run_job_impl`:
- The run opens before any branch, so deterministic and no-work fires are recorded too.
- `effective_hermes_home` is read via `_get_hermes_home()` while the job profile context is active, so it reflects the job's profile rather than the scheduler default. `get_default_hermes_root()` is used only for the cross-profile store path, never as attribution.
- `correlation_id` is the job's external `correlation_id` when present, otherwise the run ID. It is always distinct from the session ID.
- `link_session()` fires only at the inference boundary, after the wake and prompt gates, so deterministic and no-work branches stay session-less.
- `activity_recorder=` is passed to the existing `AIAgent(...)` constructor.

Terminal evidence per branch:

| Branch | Process | Final | Evidence |
|---|---|---|---|
| `no_agent` missing script | `failed` | `failed` | `evidence:script_missing` |
| `no_agent` script failure | `failed` | `failed` | `evidence:script_failed` |
| `no_agent` `wakeAgent:false` | `no_work` | `no_work` | `evidence:wake_gate_false` |
| `no_agent` empty stdout | `no_work` | `no_work` | `evidence:empty_stdout` |
| `no_agent` non-empty success | `succeeded` | `unknown` | `evidence:script_completed` |
| hybrid `wakeAgent:false` | `no_work` | `no_work` | `evidence:wake_gate_false` |
| prompt injection blocked | `blocked` | `blocked` | `evidence:prompt_injection_blocked` |
| empty prompt | `no_work` | `no_work` | `evidence:empty_prompt` |
| model loop failure | `failed` | `failed` | `session:<id>` |
| ordinary model completion | `succeeded` | `unknown` | `session:<id>` |

Delivery stays `unknown` on every row. Process-level success never becomes semantic success.

## Deliberate deviations from the plan text
1. The plan's `wakeAgent:false` evidence label contains a second colon, which the Task 2 store rejects as an unbounded reference. It is normalized to `evidence:wake_gate_false`; the label is documented in `_ACTIVITY_EVIDENCE`.
2. The `evidence` kind was added to the store's evidence allowlist (`activity_telemetry/store.py`) with parametrized accept/reject coverage. The allowlist remains an explicit allowlist — URLs, serialized payloads, extra colons, `=`, and oversized values are still rejected.
3. The plan's nine-row table omits the pre-existing `no_agent=True but no script` branch. Leaving it unfinished would strand an open run, so it records `failed` with `evidence:script_missing`.

## Verification evidence
- `python -m pytest tests/cron/test_activity_telemetry.py -q`: **20 passed**.
- Cron regression suite (`test_activity_telemetry`, `test_cron_script`, `test_cron_no_agent`, `test_script_claim_heartbeat`, `test_cron_profile`, `test_cron_profile_isolation`, `test_scheduler`): **428 passed, 1 skipped in 113.47s**.
- `python -m pytest tests/activity_telemetry tests/activity_policy tests/test_packaging_metadata.py tests/run_agent/test_activity_attribution.py -q`: **95 passed**.
- `python -m ruff check cron/scheduler.py activity_telemetry tests/cron/test_activity_telemetry.py tests/activity_telemetry`: **All checks passed**.
- `git diff --check`: clean.

## Positive control
Every test drives the real `run_job` seam and asserts against the real SQLite store rather than a mocked helper. To prove the suite is armed, `_resolve_cron_activity_policy` was temporarily short-circuited to return `None`: **15 of 20 tests failed**. The 5 that still passed are exactly the compatibility tests (unmapped script job, unmapped model job, unloadable registry, open-failure isolation, finish-helper guard), which assert "no telemetry and unchanged behavior" and therefore hold in a dormant adapter by design. The scheduler was then restored and re-verified green.

## Boundary preserved
No live `profiles/main/cron/jobs.json` was read or written, no schedule/model/provider/profile/credential/delivery configuration changed, no service was restarted, and telemetry remains `enforcement: observe`. The runtime path is not activated by this task.
