# Issue: Make compression transient cooldown ladder configurable

## Problem

Users see the warning:

```
⚠ Context is over the compression threshold (~456,379 tokens >= 131,072)
but compression is currently blocked (cooldown:60).
Run /new to start a fresh session or /compress to retry immediately.
```

even after tuning `compression.threshold` to `0.5` and setting `abort_on_summary_failure` to `false`.  
The transient backoff remains **hard‑coded** as `(60, 300, 900)` seconds, so users have no way to make compression recover faster or throttle more aggressively without editing source code.

## Why it matters

- It blocks automatic compression in long sessions, pushing context toward the hard provider limit.
- The only workaround is a manual `/compress`, but the UI message frames this as a hard stall rather than an action the user can take.
- The fixed 60 s timeout makes the assistant appear unresponsive for up to 60 seconds after a transient summary-LLM error.

## Log evidence

```
Preflight compression: ~613,477 tokens >= 131,072 threshold (model hermes-combo-free, ctx 262,144)
⚠ Context is over the compression threshold (~456,379 tokens >= 131,072)
but compression is currently blocked (cooldown:60).
```

The same pattern repeats with `cooldown:300` / `cooldown:900` once the ladder escalates.

## Code evidence

In `hermes-agent/agent/context_compressor.py`:

```python
_TIMEOUT_COOLDOWN_LADDER = (60, 300, 900)   # line ~3646
_transient_cooldown = _TIMEOUT_COOLDOWN_LADDER[...]  # line ~3647

```

This ladder is not read from `config.yaml`; the only currently configurable knob is `abort_on_summary_failure`, which controls *whether* compression aborts on a failure — not **how long** to wait afterward.

## Proposed solution

Add a new `compression.cooldown` block in `config.yaml` that exposes both the transient ladder and keeps the summary-failure cooldown configurable:

```yaml
compression:
  enabled: true
  threshold: 0.5
  target_ratio: 0.35
  protect_last_n: 2
  hygiene_hard_message_limit: 400
  protect_first_n: 3
  abort_on_summary_failure: false
  codex_gpt55_autoraise: false
  in_place: true

  # New: configurable compression cooldowns
  cooldown:
    transient:
      - 60        # first timeout
      - 300       # second timeout
      - 900       # third+ timeout
    summary_failure_seconds: 600

  auxiliary:
    model: openrouter/meta-llama/llama-3.1-8b-instruct
    provider: openrouter
```

In `agent/context_compressor.py`:

* Read `transient` and `summary_failure_seconds` from `config.yaml` at startup / session init.
* Replace the literal `_TIMEOUT_COOLDOWN_LADDER = (60, 300, 900)` with the configured list.
* Fallback to the current defaults if the config block is missing (preserves backward compatibility).
* Keep emitting `cooldown:<seconds>` in user-facing warnings; now `<seconds>` reflects the configured ladder entry.

## Scope

Files changed:

1. `hermes-agent/agent/context_compressor.py`
2. `docs/plans/compression-cooldown-configurable.md` (new or updated)
3. Optionally, the `hermes-context-compression` skill reference doc.

## Questions / design choices

- Should the `transient` ladder be capped at a minimum length (e.g., must have at least one value)?
- Should `summary_failure_seconds` stay in minutes for backwards compatibility, or rename it to a plain seconds field?
- Is there any other log/output path that hard-codes the 60/300/900 values elsewhere in the repo?

## Acceptance criteria

1. Adding `compression.cooldown.transient: [20, 60, 180]` changes the `cooldown:<seconds>` value emitted in the warning after the first timeout.
2. After the second timeout in the same session, the warning shows `cooldown:60` with that custom ladder (list index 1).
3. Manual `/compress` still bypasses the transient cooldown as before.
4. Removing the `compression.cooldown` block restores the previous default behavior (60 / 300 / 900).
5. Existing tests pass.

## First commit / task blocks

- [ ] Load `compression.cooldown` from `config.yaml` in `ContextCompressor` initialization.
- [ ] Replace literal `_TIMEOUT_COOLDOWN_LADDER` with the configured ladder.
- [ ] Replace the literal `_transient_cooldown = 60` assignment with `ladder[0]`.
- [ ] Update tests to cover a custom ladder.
- [ ] Document the new config block in `docs/plans/compression-cooldown-configurable.md` and `hermes-context-compression` skill references.

## References

- Existing compression config: `~/.hermes/config.yaml`
- Tests to extend: `hermes-agent/tests/agent/test_context_compressor.py`
- Related skill: `hermes-context-compression`
