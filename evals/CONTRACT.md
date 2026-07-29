# Eval Suite Contract

## Suite YAML Format
```yaml
name: suite_name
description: What this suite tests
# Optional. Tier-1 suites declare a hermetic production-path probe that runs
# before rubric fixtures in --deterministic-only mode (see "Runtime probes").
runtime_probe: suite_name
scenarios:
  - id: S1_unique_id
    description: Human-readable
    user_message: "The exact user prompt to send"
    system_message: "Optional system prompt override"
    config_overrides:
      delegation.max_concurrent_children: 8
      agent.max_iterations: 12
    enabled_toolsets: [terminal, file, delegation]
    skip_memory: true
    skip_context_files: true
    # Tier 2/live-only scenarios must skip deterministic mode explicitly.
    deterministic_skip: true
    deterministic_skip_reason: "Tier 2 live provider required"
    pass_conditions:
      - type: delegate_call_count
        min: 2
      - type: plan_score
        min: 0.8
      - type: no_tool_error
      - type: response_contains
        value: "expected substring"
      - type: no_cache_break  # cost_cache suite
      - type: verify_rate
        min: 0.9
      - type: recall_at_3
        min: 0.85
      - type: custom
        rubric: "module.function_name"
```

## Rubric Format
Each `evals/rubrics/<suite_name>.py` exports:
```python
def grade(scenario: dict, result: dict) -> dict:
    """Return {pass: bool, score: float 0-1, details: dict}"""
    ...
```

When no suite-specific rubric exists, every declared fallback condition must
be supported and pass. Unknown or missing conditions fail closed. A scenario
run with `--deterministic-only` must either provide `_mock_*` fixture data or
declare `deterministic_skip`; otherwise it is reported as an error rather than
being graded against an empty transcript.

## Runtime probes (production-path evidence)

`_mock_*` transcripts are **rubric unit coverage**, not evidence that Hermes
production paths work. A Tier-1 suite therefore declares a `runtime_probe`
that runs first, before any fixture grading, whenever the suite executes in
`--deterministic-only` mode.

A runtime probe:

- runs only in `--deterministic-only` mode — live mode measures the agent/provider
  path and does not add a second deterministic probe;
- imports and calls **real production modules** (e.g. `tools.memory_tool`,
  `tools.delegate_tool`, `agent.system_prompt`, `model_tools`,
  `hermes_cli.config`, `tools.file_tools`);
- creates an isolated `HERMES_HOME` and workspace via
  `set_hermes_home_override`, and restores prior process-global cache/tool state
  on every exit path;
- makes **zero** model/API calls (`api_calls` must be the integer `0`);
- returns a JSON-safe result with Boolean `pass`, integer `api_calls`, a list of
  production modules, and mapping `details`;
- **fails closed** — an invalid/unknown probe name, malformed result, assertion
  failure, or raised exception yields `{"pass": false}` with a captured
  `details.error`.

Probes are registered in `evals/runtime_probes.py::_PROBES` and invoked by the
runner. When a suite declares a probe that does not pass, the runner records a
`runtime_probe` block in the report and increments `errored`, so the CLI exits
non-zero even when every rubric fixture grades green. This closes the gap where
a production regression could not change a fixture-only result.

## Runner Output Format
```json
{
  "suite": "orchestration",
  "timestamp": "2026-06-30T02:30:00",
  "total": 5,
  "passed": 4,
  "failed": 1,
  "errored": 0,
  "skipped": 0,
  "pass_rate": 0.80,
  "runtime_probe": {
    "pass": true,
    "api_calls": 0,
    "production_modules": ["tools.delegate_tool", "hermes_cli.config"],
    "details": {"max_concurrent_children": 7}
  },
  "scenarios": [
    {
      "id": "S1",
      "pass": true,
      "score": 0.95,
      "details": {...},
      "api_calls": 3,
      "duration_s": 12.5
    }
  ]
}
```

`runtime_probe` is present only for suites that declare one. `errored` is
non-zero when the probe fails or when a deterministic scenario has neither
fixture data nor an explicit skip.

## AIAgent API (live mode only)

Live mode is the only runner mode that measures model behavior. Tier-1 runtime
probes measure deterministic production invariants (persistence, config
propagation, prompt/tool byte-stability, summary spill/read-back, and file
I/O); embedded transcripts measure rubric parsing only.

```python
from run_agent import AIAgent
agent = AIAgent(
    provider="openrouter",
    model="...",
    enabled_toolsets=["terminal", "file"],
    quiet_mode=True,
    save_trajectories=False,
    skip_context_files=True,
    skip_memory=True,
    platform="cli",
    max_iterations=12,
)
result = agent.run_conversation(
    user_message="...",
    system_message="...",
)
# result["final_response"], result["messages"]
```
