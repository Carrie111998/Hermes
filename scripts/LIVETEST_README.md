# Tool Search live test harness

Runs five scenarios against a real model (Claude Haiku 4.5 via OpenRouter) to
verify that the bridge tools work end-to-end. Records transcripts in
`scripts/out/`.

## Running

```bash
cd <repo root>
python3 scripts/tool_search_livetest.py        # runs all 5 scenarios x 2 modes
python3 scripts/analyze_livetest.py            # side-by-side report
```

Requires `OPENROUTER_API_KEY` set or present in `~/.hermes/.env`.

## What it verifies

| Scenario | Tests |
|----------|-------|
| A obvious_single | BM25 retrieval on an obvious tool name (github_create_issue) |
| B vague_paraphrased | Retrieval when the model has to paraphrase ("schedule meeting" → evt_create) |
| C multi_tool_chain | Multi-step task chaining two deferred tools (GitHub + Slack) |
| D core_plus_deferred | Mixed: core tool (read_file) called directly, deferred tool (Slack) via bridge |
| E no_tool_needed | Pure-knowledge prompt; verify no spurious tool_search invocations |

Each scenario runs with `tool_search.enabled = on` and again with `off` for an
A/B baseline. The harness records:

- bridge_calls (the tool_search / tool_describe / tool_call sequence the model emitted)
- underlying_tool_calls (what actually ran through the registry dispatcher)
- final_response, iteration count, elapsed time, any errors

## Output structure

```
scripts/out/
  <scenario>__enabled.json    # tool_search ON
  <scenario>__disabled.json   # tool_search OFF
  _summary.json               # one-line summary across all runs
```

The 2026-05 baseline run is checked in for reference. Re-running may produce
slightly different transcripts (the model is non-deterministic) but the
expected_underlying_tools assertions should remain satisfied.

## Atlas Opus Agent compatibility probe

`atlas_opus_compat_probe.py` reproduces the difference between a healthy
minimal Atlas stream and Ultra Studio's tool-enabled Agent request for Opus
4.6, 4.7, and 4.8. Paid requests require an explicit safety flag:

```bash
python3 scripts/atlas_opus_compat_probe.py --confirm-live
```

Add `--full` to exercise the real Run Orchestrator -> Hermes path. The script
cancels each full-path run after its first logged API failure or after 135
seconds, whichever comes first:

```bash
python3 scripts/atlas_opus_compat_probe.py \
  --confirm-live \
  --full \
  --output scripts/out/atlas-opus-compat.json
```

The API key is read from `ATLAS_API_KEY` or `~/.hermes/.env`. On macOS the
full-path signing key is read from the same Keychain item used by Panel BFF;
it is never printed. Every Atlas request is single-attempt, and the probe does
not implement model fallback.
