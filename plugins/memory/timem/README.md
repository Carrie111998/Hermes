# TiMEM Memory Provider

[TiMEM](https://docs.timem.cloud) is a temporal-hierarchical memory engine.
Conversations are ingested server-side into a five-level Temporal Memory Tree:

| Layer | Content |
|-------|---------|
| L1 | Raw interactions |
| L2 | Session summaries |
| L3 | Cross-session topics |
| L4 | Long-term patterns |
| L5 | Persona / user profile |

Memory generation runs asynchronously on the TiMEM engine — Hermes submits
completed turns and moves on; extraction, layering, and fusion happen
server-side.

## What this plugin does

- **Auto recall** — before each turn, semantically searches layered memories
  and injects relevant results as background context (`<timem-context>`).
- **Auto capture** — after each turn, submits the user/assistant exchange for
  server-side L1–L5 memory generation (non-blocking, fire-and-forget).
- **Built-in mirror** — mirrors Hermes built-in memory-tool writes into TiMEM
  as tagged L1 facts.
- **Tools** — `timem_search` (semantic search), `timem_add` (store explicit
  fact), `timem_profile` (fetch the computed L5 user profile).

## Setup

```bash
hermes memory setup
# choose: timem
```

Or manually:

1. Get an API key from the [TiMEM console](https://console.timem.cloud/).
2. Set the environment variable (or add to `$HERMES_HOME/.env`):

   ```bash
   export TIMEM_API_KEY=your-key
   ```

3. Activate the provider in `config.yaml`:

   ```yaml
   memory:
     provider: timem
   ```

The `timem-ai` SDK is lazy-installed on first use (pin managed in
`tools/lazy_deps.py`, mirrored by the `timem` extra in `pyproject.toml`).

## Configuration

Optional settings live in `$HERMES_HOME/timem.json`:

| Key | Default | Description |
|-----|---------|-------------|
| `user_id` | `hermes-user` | Stable user identifier for memory scoping |
| `character_id` | `hermes` | Agent namespace (falls back to `agent_identity`) |
| `domain` | `hermes` | Business domain tag on generated memories |
| `base_url` | `https://api.timem.cloud` | Engine URL (self-hosted: e.g. `http://localhost:8001`) |
| `max_recall_results` | `8` | Max memories injected per turn (1–20) |
| `score_threshold` | `0.5` | Minimum semantic similarity for recall (0–1) |
| `auto_recall` | `true` | Inject recalled context each turn |
| `auto_capture` | `true` | Submit turns for memory generation |
| `api_timeout` | `60.0` | Request timeout in seconds (1–60) |

Environment overrides: `TIMEM_API_KEY` (required), `TIMEM_BASE_URL`.

## Behavior notes

- Only one external memory provider can be active at a time (Hermes rule).
- Non-primary agent contexts (cron, flush, subagents) are read-only — recall
  works, but no writes, so scheduled prompts can't pollute the memory tree.
- A circuit breaker opens after 5 consecutive API failures and retries after
  a 120s cooldown; the agent keeps working without memory in the meantime.
- Trivial turns (short acknowledgements) are not captured.
- **Reads and writes are serialized server-side.** While a turn is being
  ingested (`sync_turn` / `timem_add` / built-in mirror), the engine may
  briefly queue concurrent `timem_search` / prefetch calls — those can take
  longer or, in the worst case, time out and return no context for that one
  turn. The agent should treat prefetch as best-effort and fall back to the
  explicit `timem_search` tool when it genuinely needs a recall. Empirically,
  searches return in ~1–2s when no generation is in flight.
- The TiMEM sync SDK wraps a single asyncio event loop that is not
  thread-safe, so this plugin uses **two separate client instances** (one for
  reads, one for writes), each internally locked, so a slow background write
  never blocks a recall.
