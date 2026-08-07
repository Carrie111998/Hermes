---
title: "Trading Automation — Create bounded DCA, limit, and stop-loss automations"
sidebar_label: "Trading Automation"
description: "Create bounded DCA, limit, and stop-loss automations"
---

{/* This page is auto-generated from the skill's SKILL.md by website/scripts/generate-skill-docs.py. Edit the source SKILL.md, not this page. */}

# Trading Automation

Create bounded DCA, limit, and stop-loss automations.

## Skill metadata

| | |
|---|---|
| Source | Optional — install with `hermes skills install official/moonpay/trading-automation` |
| Path | `optional-skills/moonpay/trading-automation` |
| Version | `0.2.0` |
| Author | Efren Plasencia (@tonyagents), Hermes Agent |
| License | MIT |
| Platforms | linux, macos |
| Tags | `MoonPay`, `Trading`, `Automation` |
| Related skills | `moonpay-swap-tokens`, `moonpay-check-wallet` |

## Reference: full SKILL.md

:::info
The following is the complete skill definition that Hermes loads when this skill is triggered. This is what the agent sees as instructions when the skill is active.
:::

# Trading Automation Skill

Create DCA, limit-buy, and stop-loss runners around the MoonPay CLI. Every
runner is constrained by a user-confirmed mandate; this skill does not permit
unbounded autonomous spending or silent changes to an approved strategy.

## When to Use

- The user requests a recurring fixed-size swap.
- The user requests a price-triggered buy or stop-loss.
- The user wants a local cron or LaunchAgent schedule.

Do not use this skill when the user has not confirmed exact spending bounds or
when the requested strategy requires leverage, derivatives, or discretionary
position sizing.

## Prerequisites

- MoonPay CLI (`mp`) installed and authenticated.
- A funded local wallet.
- Token addresses resolved on the selected chain.
- Python 3.10 or newer.
- The `terminal` tool for CLI and scheduler commands.

Helper script:
`~/.hermes/skills/moonpay/trading-automation/scripts/bounded_swap.py`

## How to Run

```bash
SCRIPT="${HERMES_HOME:-$HOME/.hermes}/skills/moonpay/trading-automation/scripts/bounded_swap.py"
python3 "$SCRIPT" --help
```

The default state path is:
`${HERMES_HOME:-~/.hermes}/moonpay/bounded-swap.json`.

## Quick Reference

```bash
# Resolve assets and inspect balances.
mp token search --query SOL --chain solana --limit 5
mp token balance list --wallet main --chain solana --json

# Preview without price lookup or swap execution.
python3 "$SCRIPT" run --dry-run

# Evaluate the trigger and execute when authorized.
python3 "$SCRIPT" run
```

Supported strategies:

| Strategy | Trigger |
|---|---|
| `dca` | Runs after the minimum interval |
| `limit-buy` | Current target-token price is at or below trigger |
| `stop-loss` | Current source-token price is at or below trigger |

## Procedure

### 1. Define the proposed mandate

Resolve token addresses and prepare:

- strategy;
- wallet and chain;
- source and destination token addresses;
- amount per run;
- total cap;
- maximum run count;
- minimum interval;
- expiration timestamp;
- trigger price, when applicable.

### 2. Obtain explicit authorization

Display every field above and ask the user to confirm the exact bounded
mandate. Do not treat a prior manual trade, a vague "set this up" message, or
permission to create a script as authorization to execute scheduled swaps.

The mandate authorizes future executions only inside its recorded bounds. A
change to any bound requires a new confirmation and a new mandate.

### 3. Create the mandate

DCA example:

```bash
python3 "$SCRIPT" create \
  --strategy dca \
  --wallet main \
  --chain solana \
  --from-token EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v \
  --to-token So11111111111111111111111111111111111111111 \
  --amount-per-run 5 \
  --max-total-amount 35 \
  --max-runs 7 \
  --min-interval-seconds 86400 \
  --expires-at 2026-08-15T17:00:00Z \
  --authorization-id user-confirmed-2026-08-01 \
  --confirm-bounded-spend "I AUTHORIZE THIS BOUNDED SCHEDULE"
```

Limit-buy example:

```bash
python3 "$SCRIPT" create \
  --strategy limit-buy \
  --wallet main \
  --chain solana \
  --from-token EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v \
  --to-token So11111111111111111111111111111111111111111 \
  --amount-per-run 50 \
  --max-total-amount 50 \
  --max-runs 1 \
  --min-interval-seconds 300 \
  --trigger-price 80 \
  --expires-at 2026-08-15T17:00:00Z \
  --authorization-id user-confirmed-2026-08-01 \
  --confirm-bounded-spend "I AUTHORIZE THIS BOUNDED SCHEDULE"
```

For stop-loss, set `--strategy stop-loss`, make the held asset the source
token, and specify a fixed maximum amount per run. "Sell all" is not accepted
as an unbounded amount.

### 4. Preview and schedule

```bash
python3 "$SCRIPT" run --dry-run
```

Read the preview back to the user before enabling the scheduler.

Linux cron example:

```bash
*/5 * * * * python3 "$HOME/.hermes/skills/moonpay/trading-automation/scripts/bounded_swap.py" run
```

On macOS, use a user LaunchAgent with absolute paths. The runner may execute
without a new prompt only when the trigger and all recorded authorization
bounds pass. Waiting on an unmet trigger does not consume a run.

### 5. Pause, revoke, or change

Disable the cron entry or LaunchAgent to pause execution. To revoke permanently,
disable the scheduler and archive the state file. To expand or change any
mandate field, obtain new explicit authorization and create new state.

## Pitfalls

- Token symbols can resolve to malicious assets; display addresses.
- Price-triggered orders depend on the MoonPay price response.
- A scheduler can retry frequently; the minimum interval prevents rapid fills.
- State files contain authorization metadata and should remain private.
- Never put private keys, seed phrases, or API tokens in scripts or logs.
- Failed swaps do not consume the authorized cap; investigate before retrying.

## Verification

- `run --dry-run` matches the user's confirmed mandate.
- The runner rejects expired and exhausted mandates.
- The runner enforces per-run, total, run-count, and interval limits.
- Price-triggered strategies wait when their condition is false.
- Successful swaps record a transaction hash and execution timestamp.
- Disabling the scheduler stops future execution.
