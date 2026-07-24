# Tests for configurable compression cooldown ladder

## Scope

These tests focus on the new `compression.cooldown.transient` config path and its effect on the compressor state.

## Files to touch

- `hermes-agent/tests/agent/test_context_compressor_cooldown.py` (new)

## Test cases

1. `test_default_ladder_used_when_no_config`
   - Setup: ensure `config.yaml` does not contain `compression.cooldown`.
   - Exercise: instantiate `ContextCompressor` for a session.
   - Verify: configured transient ladder is `(60, 300, 900)` or derives from default config.

2. `test_custom_ladder_overrides_defaults`
   - Setup: write a temporary `config.yaml` with `compression.cooldown.transient: [20, 60, 180]`.
   - Exercise: instantiate `ContextCompressor` and simulate timeout failure progression.
   - Verify:
     - After first timeout: transient cooldown == 20 seconds.
     - After second timeout: transient cooldown == 60 seconds.
     - After third timeout: transient cooldown == 180 seconds.

3. `test_failure_cooldown_configurable`
   - Setup: set `compression.cooldown.summary_failure_seconds: 300`.
   - Exercise: trigger a summary-failure path.
   - Verify: `_summary_failure_cooldown_until` grows by ~300 seconds after failure.

4. `test_manual_compress_bypasses_transient_cooldown`
   - Setup: create custom ladder and trigger transient cooldown.
   - Exercise: run manual compression.
   - Verify: transient cooldown does not block manual compression.

## Execution command

```
pytest tests/agent/test_context_compressor_cooldown.py -q
```

## Notes

- Keep tests hermetic: use a temp config fixture or monkeypatch config loading to avoid depending on `~/.hermes/config.yaml`.
- Maintain current assumptions about `_consecutive_timeout_failures` and `_summary_failure_cooldown_until` being listener-safe.
- If adding a new test file violates `tests/` naming conventions, place under `tests/agent/test_compression_cooldown_configurable.py`.
