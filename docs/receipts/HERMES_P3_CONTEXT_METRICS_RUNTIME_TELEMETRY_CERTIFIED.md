# HERMES_P3_CONTEXT_METRICS_RUNTIME_TELEMETRY_CERTIFIED

## Mission

HERMES_P3_CONTEXT_METRICS_RUNTIME_TELEMETRY_V1

## Verdict

HERMES_P3_CONTEXT_METRICS_RUNTIME_TELEMETRY_CERTIFIED

## Design

Expose existing P3 ContextMetrics at the real runtime boundary via a stable read-only retrieval surface on AIAgent.

## Changes

### run_agent.py
- Added `AIAgent.get_context_metrics()` method that returns the current P3 `ContextMetrics` snapshot (or `None` if no turn context has been built yet).
- Uses `getattr(self, "_context_compiler_metrics", None)` for safe read-only access.
- No changes to P1/P2/P3 semantics. No P4. No provider/business mutations.

### tests/agent/test_context_compiler.py
- Added 3 focused TDD tests:
  - `test_get_context_metrics_returns_none_when_attribute_missing` — verifies None return before compilation
  - `test_get_context_metrics_returns_metrics_when_set` — verifies ContextMetrics return after turn
  - `test_get_context_metrics_is_read_only` — verifies no mutation of agent state

## Evidence

- 14/14 context_compiler tests pass
- 88/88 canonical regressions pass (context_compiler + durable_mission + action_commit)
- Simulated 2-turn Ling telemetry smoke test passed (method returns ContextMetrics after turn context compilation)
- Real 2-turn Ling smoke requires ZENMUX_API_KEY (not configured in this environment)

## Constraints Honored

- No change to P1/P2/P3 semantics
- No P4
- No provider/business mutations
- Read-only retrieval surface only
- Focused TDD first
- Canonical regressions verified

## SHA

b260bb5c9050e376a9680f147a7434e677d2465b