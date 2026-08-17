# usage-meter

Provider-agnostic, privacy-safe per-call usage ledger for Hermes.

## What it does

- Hooks `post_api_request` and records **one event per completed model API call**
  (primary turns, MoA fan-out, subagents, auxiliary calls).
- Stores an installation-wide SQLite ledger outside any profile `state.db`
  (`<hermes-root>/usage-meter/ledger.db`).
- Never persists prompts, completions, tool results, credentials, or auth headers.
- Estimates cost via Hermes' existing pricing resolver. Unknown routes are
  **`unpriced`**, never silent `$0.00`. Included subscription routes stay
  distinguishable.
- Exposes aggregates through `usage.meter.summary`, `usage.meter.details`, and
  `usage.meter.recent` for Desktop and TUI.

## Enable

```bash
hermes plugins enable usage-meter
```

Bundled standalone plugins are opt-in. Enabling is required for capture; the
RPC read path works whenever a ledger file exists.

## Design credit

Completion scope and product requirements come from
[@muhammadshess-10xe on #77221](https://github.com/NousResearch/hermes-agent/issues/77221#issuecomment-5256969393).
This plugin is the in-tree integration of that design into the desktop usage
surface work.
