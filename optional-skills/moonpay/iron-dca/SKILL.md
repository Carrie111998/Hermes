---
name: iron-dca
description: "Automate bounded fiat-to-token dollar-cost averaging."
version: 0.2.0
author: Efren Plasencia (@tonyagents), Hermes Agent
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [MoonPay, Fiat, Trading, Automation, Iron, DCA]
    related_skills: [moonpay-virtual-account, moonpay-check-wallet]
---

# Iron DCA Skill

Create a fiat-to-USDC Iron on-ramp, then execute a user-authorized series of
fixed-size token swaps. This skill never creates an open-ended trading mandate:
every schedule has a per-run cap, total cap, run limit, target, and expiry.

## When to Use

- The user wants to wire fiat and deploy it into one token over time.
- The user wants a fixed DCA schedule backed by an Iron virtual account.
- The user wants a resumable schedule with an auditable authorization record.

Do not use this skill for discretionary trading, changing targets mid-schedule,
or spending beyond an already confirmed mandate.

## Prerequisites

- MoonPay CLI (`mp`) installed and authenticated.
- Iron virtual account approved for the user's region.
- A registered Solana wallet and an active USDC on-ramp.
- Python 3.10 or newer.
- The `terminal` tool for CLI and scheduler commands.

Helper script:
`~/.hermes/skills/moonpay/iron-dca/scripts/iron_dca.py`

## How to Run

```bash
SCRIPT="${HERMES_HOME:-$HOME/.hermes}/skills/moonpay/iron-dca/scripts/iron_dca.py"
python3 "$SCRIPT" --help
```

The state file defaults to:
`${HERMES_HOME:-~/.hermes}/moonpay/iron-dca-state.json`.

## Quick Reference

```bash
# Inspect the Iron account and wallet.
mp virtual-account retrieve --json
mp virtual-account wallet list
mp virtual-account transaction list --json
mp token balance list --wallet main --chain solana --json

# Preview the next authorized run without executing it.
python3 "$SCRIPT" run --dry-run

# Execute the next authorized run.
python3 "$SCRIPT" run
```

## Procedure

### 1. Prepare the Iron on-ramp

Use the MoonPay CLI to retrieve or create the virtual account, accept required
agreements, register the wallet, and create the on-ramp:

```bash
mp virtual-account retrieve --json
mp virtual-account agreement list
mp virtual-account wallet register --wallet main --chain solana
mp virtual-account onramp create \
  --name "Iron DCA Onramp" \
  --fiat USD \
  --stablecoin USDC \
  --wallet <registered-wallet-address> \
  --chain solana
```

KYC and agreement acceptance are user-facing legal steps. Do not accept them
without the user present.

### 2. Wait for the deposit

Confirm that the Iron transaction is complete and the expected USDC balance is
available. Do not create a schedule from a pending or estimated deposit.

### 3. Obtain explicit bounded authorization

Before creating state, show the user all of these fields:

- wallet and chain;
- source asset and target token address;
- amount per run;
- maximum total amount;
- maximum number of runs;
- cadence;
- expiration timestamp.

Ask the user to explicitly confirm that exact mandate. A previous manual swap,
general request to "automate," or approval of the on-ramp is not authorization
for scheduled spending.

After confirmation, record a non-secret authorization identifier and create the
mandate:

```bash
python3 "$SCRIPT" create \
  --deposit-amount 500 \
  --days 7 \
  --target-token So11111111111111111111111111111111111111111 \
  --wallet main \
  --chain solana \
  --expires-at 2026-08-15T17:00:00Z \
  --authorization-id user-confirmed-2026-08-01 \
  --confirm-bounded-spend "I AUTHORIZE THIS BOUNDED SCHEDULE"
```

The acknowledgement flag may only be supplied after the user confirms the
displayed mandate.

### 4. Verify before scheduling

```bash
python3 "$SCRIPT" run --dry-run
```

Read the preview back to the user. Confirm that the target, amount, run count,
and authorization identifier match the approved mandate.

### 5. Schedule the runner

The runner may execute without another prompt only while every action remains
inside the recorded mandate. It rejects expired, exhausted, underfunded, or
over-cap runs.

Linux cron example:

```bash
0 9 * * * python3 "$HOME/.hermes/skills/moonpay/iron-dca/scripts/iron_dca.py" run
```

On macOS, use a user LaunchAgent with the absolute Python and script paths.
Never place wallet secrets or private keys in the scheduler definition.

### 6. Reauthorize changes

Pause the scheduler and obtain a new explicit confirmation before changing the
target, wallet, chain, per-run amount, total cap, run count, cadence, or expiry.
Create a new state file; do not hand-edit an existing mandate to expand it.

## Pitfalls

- Iron deposits can take business days to settle.
- Token symbols are ambiguous; resolve and display the token address.
- Scheduler environments have a minimal `PATH`; use absolute paths.
- The local state file is authorization evidence. Do not publish it.
- Deleting state does not reverse completed swaps.
- A failed run must not increment totals; inspect the CLI error before retrying.

## Verification

- `run --dry-run` shows the confirmed wallet, target, and amount.
- `executed_runs` never exceeds `max_runs`.
- `total_deployed` never exceeds `max_total_amount`.
- The runner refuses expired mandates.
- Each successful run records a transaction hash and timestamp.
- Pausing the scheduler prevents further execution.
