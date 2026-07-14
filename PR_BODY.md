fix(gateway): resolve max_iterations from config.yaml directly instead of the env round-trip

## Problem

The gateway resolves the per-turn iteration cap by bridging config.yaml `agent.max_turns` into `HERMES_MAX_ITERATIONS` and then reading the env var back. That round-trip has three holes that let a stale `~/.hermes/.env` value (e.g. `HERMES_MAX_ITERATIONS=90` written by an older setup flow) silently win over the user's configured budget, reintroducing 90/90 iteration-exhaustion on long turns:

1. `agent.max_turns: null` in config.yaml makes `_bridge_max_turns_from_config` write the literal string `"None"` into the env (`"max_turns" in agent_cfg` is true), and `_current_max_iterations` then swallows the `int()` failure and returns the hardcoded default 90 — ignoring a perfectly valid `.env` fallback.
2. Legacy root-level `max_turns` (which `hermes_cli.config._normalize_max_turns_config` explicitly supports and migrates into `agent.max_turns`) is invisible to the gateway bridge, so those configs run at whatever stale value `.env` holds.
3. The startup "Agent budget" log line reads the raw env var, so it can report a value that disagrees with what per-turn resolution later computes.

## Change

`gateway/run.py`:

- New `_read_config_max_iterations(home)`: loads config.yaml (env-var expansion + managed-scope overlay, both fail-open, exactly as the bridge did), returns `int(agent.max_turns)` when present, falls back to legacy root-level `max_turns` (`agent.max_turns` wins when both are set), and returns `None` for missing/null/malformed values instead of propagating garbage into the environment.
- `_bridge_max_turns_from_config` now delegates to it and only writes the env var for a valid integer.
- New `_resolve_gateway_max_iterations(default=90, *, reload_runtime_env=False)`: optionally refreshes the runtime env (rotated credentials), then prefers the config value directly — syncing the env var for subprocess consumers — and only consults `HERMES_MAX_ITERATIONS` when config omits the key (with an int-parse guard).
- `_current_max_iterations` becomes a thin wrapper over `_resolve_gateway_max_iterations(reload_runtime_env=True)`, so all existing per-turn call sites (native gateway turns and the API-server adapter) pick up config-authoritative resolution without signature changes.
- The startup budget log now logs the resolved value instead of the raw env var.

`scripts/release.py`: contributor-attribution entry.

## Correctness notes

- Managed-scope overlay behavior is preserved: administrator-pinned `agent.max_turns` is applied inside `_read_config_max_iterations`, so direct config reads cannot bypass a managed pin.
- Multiplex mode is unchanged: `_reload_runtime_env_preserving_config_authority` still skips the global `.env` reload and only re-bridges config.
- Malformed `agent.max_turns` (non-integer) now falls back to the env var / default rather than crashing the turn or poisoning the env.
- Tests that monkeypatch `gateway.run._current_max_iterations` keep working since the symbol and signature are unchanged.

## Tests

- `tests/gateway/test_runtime_env_reload_config_authority.py`: new tests for (a) config winning over a stale `.env` after a runtime env reload, (b) legacy root-level `max_turns`, (c) `agent.max_turns: null` falling back to the env value; the existing `_current_max_iterations` reload test now pins a config-less hermes home so it keeps exercising the env-fallback path.
- Targeted suites all green: `tests/gateway/test_runtime_env_reload_config_authority.py`, `tests/gateway/test_cached_agent_max_iterations.py` (9 passed), `tests/gateway/test_api_server.py` + `tests/scripts` (201 passed).
- Full `tests/gateway` run: 9101 passed; the remaining failures (telegram/slack/feishu/path-completion modules, unrelated to this change) reproduce identically on a clean `main` checkout in the same environment.

## Measured impact

Reproduced the failure mode end-to-end: with `config.yaml agent.max_turns: 500` and a stale `.env HERMES_MAX_ITERATIONS=90`, per-turn resolution previously could run at 90; it now resolves 500 and re-syncs the env var to 500.
