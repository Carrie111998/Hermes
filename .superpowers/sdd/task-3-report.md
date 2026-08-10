# Task 3 Report

## Scope
Added the optional best-effort `ActivityRecorder`, threaded it through both `AIAgent` constructor layers, and attributed sanitized per-response served-route usage from the conversation loop independently of plugin hooks. No routing, provider/model selection, schedule, delivery, credential, or service state was changed.

## RED evidence
- `python -m pytest tests/activity_telemetry/test_recorder.py tests/run_agent/test_activity_attribution.py -q` failed collection with `ModuleNotFoundError: No module named 'activity_telemetry.recorder'`.
- The reserved non-model route test failed with `ImportError: cannot import name 'NON_MODEL_ROUTE'` before the symbol existed.

## Implementation properties
- `activity_recorder` defaults to `None` in `run_agent.AIAgent.__init__` and `agent.agent_init.init_agent`; no recorder, SQLite handle, or policy lookup is ever created implicitly.
- `agent/conversation_loop.py` computes the sanitized usage summary once, calls `agent._record_activity_response(...)` outside the `has_hook("post_api_request")` gate, and reuses the same summary for the plugin hook.
- Attribution uses `response.model` when present, otherwise the agent's model; provider is the resolved provider. Distinct `(provider, model)` pairs create distinct route rows.
- Only canonical counters cross the boundary: `input_tokens`, `cache_read_tokens`, `cache_write_tokens`, `output_tokens`, `reasoning_tokens`, `request_count`. `raw_usage` is stripped; no raw response, headers, API key, base URL, or credential pool is forwarded.
- Every recorder and callback failure is swallowed and logged as operation plus exception class only, never exception text.
- Tool calls and retries are booked to the reserved `NON_MODEL_ROUTE` (`activity`/`non-model`) constant so Task 5 reports can exclude non-model traffic by symbol instead of by string literal.

## Verification evidence
- `python -m pytest tests/activity_telemetry/test_recorder.py tests/run_agent/test_activity_attribution.py tests/run_agent/test_iteration_budget_race.py tests/agent/test_usage_pricing.py tests/hermes_state/test_aux_usage_accounting.py tests/run_agent/test_provider_fallback.py tests/run_agent/test_switch_model_fallback_prune.py -q`: **90 passed in 78.09s**.
- `python -m pytest tests/activity_telemetry tests/run_agent/test_activity_attribution.py -q` after review fixes: **49 passed in 20.55s**.
- `python -m ruff check activity_telemetry tests/activity_telemetry tests/run_agent/test_activity_attribution.py`: **All checks passed**.
- `git diff --check`: clean.
- Full `tests/run_agent/test_run_agent.py` (436 tests), `test_provider_fallback.py`, and `test_switch_model_fallback_prune.py` pass; the no-listener fast path assertion `test_request_scoped_api_hooks_skip_payload_work_without_listeners` still passes because the gated raw-payload builders remain gated.

## Review
Independent review returned one Important finding: `test_response_model_switches_are_attributed_separately` called the helper directly and never drove the conversation loop, so a broken loop wiring would not have been caught. The test was rewritten to run a real two-response `run_conversation` turn through a tool call.

That fix was validated with a positive control: temporarily replacing the `_record_activity_response` call in `agent/conversation_loop.py` with a no-op made both end-to-end tests fail (`assert [] == ['first/model', 'second/model']`), and restoring it returned them to green. The seam is armed, not merely asserted.

The reviewer also noted the synthetic non-model route was dormant but could pollute future per-model reports; it is now a single exported constant with a documented exclusion contract.

## Known, accepted behavior change
`_usage_summary_for_api_request_hook()` is now called on every successful response instead of only when a `post_api_request` listener exists. This is required by the plan's Step 6 and is a small dataclass build plus dict operations. The expensive raw request/response payload builders remain gated behind `has_hook(...)` and are unchanged.
